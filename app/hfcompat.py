"""HF_ENDPOINT-compatible surface.

Design note: we do NOT reimplement the Hub API. Metadata requests (/api/...,
refs, repo info) are proxied straight upstream -- they are small, latency-bound,
and any divergence from the real API breaks clients in subtle ways. Only
`/…/resolve/…` file bytes are intercepted and served from cache. That keeps the
compatibility surface tiny while still capturing 100% of the bytes.
"""

from __future__ import annotations

import asyncio
import logging
import os
import posixpath
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from huggingface_hub import errors as hf_errors
from huggingface_hub import get_hf_file_metadata, hf_hub_url

from . import (
    cachefs,
    dockerauth,
    hfauthz,
    hfwrites,
    httpclients,
    managegate,
    metrics,
    policy,
    refs,
    serving,
    tier,
    viewer,
    webauth,
)
from .config import settings
from .jobs import manager

log = logging.getLogger("xhc.hfcompat")

router = APIRouter()

_REPO_TYPE_PREFIX = {"datasets": "dataset", "spaces": "space"}
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}
# httpx transparently decompresses .content, so any response we forward as
# decoded bytes must NOT keep the upstream's content-encoding -- the client
# would try to gunzip plain JSON. Only the streaming proxy, which forwards raw
# bytes via aiter_raw(), may pass content-encoding through.
_DECODED_DROP = _HOP_BY_HOP | {"content-encoding"}

_http = httpclients.LoopBound(
    "hfcompat",
    lambda: httpx.AsyncClient(
        timeout=httpx.Timeout(settings.request_timeout_s, read=None),
        follow_redirects=False,
    ),
)


def get_client() -> httpx.AsyncClient:
    return _http.get()


async def close_client() -> None:
    await _http.aclose()


def parse_resolve(full_path: str) -> tuple[str, str, str, str] | None:
    """'datasets/org/name/resolve/main/a/b.bin' -> (dataset, org/name, main, a/b.bin)."""
    if "/resolve/" not in full_path:
        return None
    head, rest = full_path.split("/resolve/", 1)
    repo_type = "model"
    for prefix, rtype in _REPO_TYPE_PREFIX.items():
        if head.startswith(prefix + "/"):
            repo_type = rtype
            head = head[len(prefix) + 1 :]
            break
    if "/" not in rest:
        return None
    revision, filename = rest.split("/", 1)
    revision = unquote(revision)
    if not head or not filename:
        return None
    return repo_type, head, revision, filename


def parse_repo_info_path(full_path: str) -> tuple[str, str, str | None] | None:
    """Match the repo-info endpoints snapshot_download uses, and only those.

    `api/models/org/name` and `api/models/org/name/revision/main` qualify.
    Sub-resources (`/tree/`, `/paths-info`, ...) deliberately do not: we can
    honestly synthesize a file listing, not arbitrary Hub API surface.
    """
    if not full_path.startswith("api/"):
        return None
    rest = full_path[len("api/") :]
    repo_type = tail = None
    for prefix, rtype in (("models/", "model"), ("datasets/", "dataset"), ("spaces/", "space")):
        if rest.startswith(prefix):
            repo_type, tail = rtype, rest[len(prefix) :]
            break
    if tail is None:
        return None

    revision = None
    if "/revision/" in tail:
        repo_id, _, revision = tail.partition("/revision/")
        revision = unquote(revision).strip("/")
    else:
        repo_id = tail
    repo_id = repo_id.strip("/")
    # "gpt2" (canonical) or "org/name". More segments means a sub-resource.
    if not repo_id or repo_id.count("/") > 1:
        return None
    return repo_type, repo_id, revision or None


def parse_tree_path(full_path: str) -> tuple[str, str, str, str] | None:
    """Match `api/{type}s/{repo}/tree/{rev}[/{path}]`, used by list_repo_files."""
    if not full_path.startswith("api/") or "/tree/" not in full_path:
        return None
    rest = full_path[len("api/") :]
    repo_type = tail = None
    for prefix, rtype in (("models/", "model"), ("datasets/", "dataset"), ("spaces/", "space")):
        if rest.startswith(prefix):
            repo_type, tail = rtype, rest[len(prefix) :]
            break
    if tail is None:
        return None
    repo_id, _, after = tail.partition("/tree/")
    repo_id = repo_id.strip("/")
    if not repo_id or repo_id.count("/") > 1 or not after:
        return None
    revision, _, path_in_repo = after.partition("/")
    return repo_type, repo_id, unquote(revision), unquote(path_in_repo).strip("/")


def synthesize_tree(
    repo_type: str,
    repo_id: str,
    revision: str,
    path_in_repo: str,
    recursive: bool,
    expand: bool,
) -> list[dict] | None:
    """Rebuild a tree listing from the cached snapshot.

    `oid` is the blob's git sha, which in the HF cache layout is the symlink
    target's filename -- the same value the resolve path serves as the ETag, so
    the two agree by construction rather than by coincidence.
    """
    commit = cachefs.resolve_commit(repo_type, repo_id, revision or "main")
    if commit is None:
        return None
    files = cachefs.snapshot_files(repo_type, repo_id, commit)
    if not files:
        return None

    root = (
        Path(settings.cache_dir)
        / cachefs.repo_folder_name(repo_id, repo_type)
        / "snapshots"
        / commit
    )
    prefix = f"{path_in_repo}/" if path_in_repo else ""
    scoped = [f for f in files if f.startswith(prefix)]
    if not scoped:
        return None

    entries: list[dict] = []
    seen_dirs: set[str] = set()
    for rel in scoped:
        remainder = rel[len(prefix) :]
        if not recursive and "/" in remainder:
            # Collapse to the immediate child directory.
            d = prefix + remainder.split("/", 1)[0]
            if d not in seen_dirs:
                seen_dirs.add(d)
                entries.append({"type": "directory", "oid": None, "size": 0, "path": d})
            continue
        target = root / rel
        try:
            size = target.stat().st_size
        except OSError:
            continue
        oid = None
        if target.is_symlink():
            try:
                oid = os.path.basename(os.readlink(target))
            except OSError:
                oid = None
        item = {"type": "file", "oid": oid, "size": size, "path": rel}
        if expand:
            # We cannot invent commit history or scan results; null is honest.
            item["lastCommit"] = None
            item["securityFileStatus"] = None
        entries.append(item)
    return sorted(entries, key=lambda e: e["path"])


def synthesize_repo_info(repo_type: str, repo_id: str, revision: str | None) -> dict | None:
    """Build a repo-info response from the cached snapshot.

    Only used when upstream 404s a repo we still hold. Without this, a repo
    deleted from the Hub is half-usable: hf_hub_download works per file, but
    snapshot_download fails because it cannot enumerate what to fetch.
    """
    commit = cachefs.resolve_commit(repo_type, repo_id, revision or "main")
    if commit is None:
        return None
    files = cachefs.snapshot_files(repo_type, repo_id, commit)
    if not files:
        return None

    body = {
        "_id": commit,
        "id": repo_id,
        "sha": commit,
        "siblings": [{"rfilename": f} for f in files],
        "private": False,
        "gated": False,
        "disabled": False,
        "tags": [],
        "downloads": 0,
        "likes": 0,
        "lastModified": None,
        "createdAt": None,
        # Tagged so a client -- or a human reading a reproducibility record --
        # can tell an archived answer from one the Hub confirmed. Verified that
        # huggingface_hub's ModelInfo/DatasetInfo accept unknown fields, so this
        # cannot break parsing.
        "xhcSynthesized": True,
        "xhcSynthesizedReason": "upstream returned 404; listing rebuilt from the cached snapshot",
    }
    if repo_type == "model":
        body["modelId"] = repo_id
    if repo_type == "dataset":
        body["author"] = repo_id.split("/")[0] if "/" in repo_id else None
    return body


def is_xet_token_path(full_path: str) -> bool:
    return "/xet-read-token/" in full_path or "/xet-write-token/" in full_path


def upstream_resolve_url(repo_type: str, repo_id: str, revision: str, filename: str) -> str:
    return hf_hub_url(
        repo_id=repo_id,
        filename=filename,
        repo_type=repo_type,
        revision=revision,
        endpoint=settings.upstream,
    )


# Upstream failures that are ANSWERS, not outages. Reporting "this file does not
# exist" as 502 is not a cosmetic wrong code: huggingface_hub treats 5xx as
# retryable and burns ~23s of backoff before giving up, while a 404 carrying
# X-Error-Code: EntryNotFound is understood immediately. Clients probe for
# optional files (processor_config.json, chat_template.jinja, ...) on every
# model load, so getting this wrong taxes every single load.
#
# Order matters: GatedRepoError and DisabledRepoError subclass
# RepositoryNotFoundError, so they must be tested first.
_UPSTREAM_ERRORS: tuple[tuple[type[Exception], str, int], ...] = (
    (hf_errors.GatedRepoError, "GatedRepo", 403),
    (hf_errors.DisabledRepoError, "DisabledRepo", 403),
    (hf_errors.EntryNotFoundError, "EntryNotFound", 404),
    (hf_errors.RevisionNotFoundError, "RevisionNotFound", 404),
    (hf_errors.RepositoryNotFoundError, "RepoNotFound", 404),
)


def upstream_failure(exc: Exception, repo_id: str, filename: str) -> HTTPException:
    """Translate an upstream exception into the response the Hub itself would send.

    Only genuine HTTP answers are passed through. Anything with no upstream
    response behind it (DNS, TLS, connection reset, timeout) really is a bad
    gateway and stays a 502.
    """
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    error_code = None
    if response is not None:
        try:
            error_code = response.headers.get("X-Error-Code")
        except AttributeError:
            error_code = None

    if error_code is None:
        for cls, code, default_status in _UPSTREAM_ERRORS:
            if isinstance(exc, cls):
                error_code = code
                status = status or default_status
                break

    if status is None:
        log.warning("upstream unreachable for %s/%s: %s", repo_id, filename, exc)
        return HTTPException(status_code=502, detail=f"upstream unreachable: {exc}")

    # Not a warning: a missing optional file is the single most common request
    # this service sees, and logging it at WARNING makes real problems invisible.
    log.debug("upstream %s for %s/%s (%s)", status, repo_id, filename, error_code)
    headers = {"X-Error-Code": error_code} if error_code else None
    return HTTPException(
        status_code=status,
        detail=f"upstream returned {status} for {repo_id}/{filename}",
        headers=headers,
    )


# --------------------------------------------------------------------------
# negative cache
#
# Fixing the status code stops the retry storm, but every probe for an absent
# optional file is still a WAN round-trip -- once per missing file, per model
# load, per node. A fleet rotating onto one model does that in lockstep. Hold
# 404s briefly so the first node pays and the rest do not.
#
# TTL is deliberately short: a file that does not exist today may be pushed
# tomorrow, and `main` moves. This trades a bounded window of staleness on
# absent files for removing a per-load WAN round-trip.
# --------------------------------------------------------------------------

_negative: dict[tuple[str, str, str, str], tuple[float, HTTPException]] = {}
_NEGATIVE_MAX = 20_000


def _negative_cache_get(
    repo_type: str, repo_id: str, revision: str, filename: str
) -> HTTPException | None:
    if settings.negative_ttl_s <= 0:
        return None
    key = (repo_type, repo_id, revision, filename)
    entry = _negative.get(key)
    if entry is None:
        return None
    expires, exc = entry
    if time.monotonic() >= expires:
        _negative.pop(key, None)
        return None
    return exc


def _negative_cache_put(
    repo_type: str, repo_id: str, revision: str, filename: str, exc: HTTPException
) -> None:
    if settings.negative_ttl_s <= 0:
        return
    if len(_negative) >= _NEGATIVE_MAX:
        # Cheap bound. Entries are tiny and short-lived; drop the whole map
        # rather than carry an LRU for what is only a latency optimisation.
        _negative.clear()
    _negative[(repo_type, repo_id, revision, filename)] = (
        time.monotonic() + settings.negative_ttl_s,
        exc,
    )


def negative_cache_size() -> int:
    return len(_negative)


def negative_cache_clear() -> int:
    n = len(_negative)
    _negative.clear()
    return n


def negative_cache_drop_repo(repo_type: str, repo_id: str) -> int:
    """Forget remembered 404s for one repo, after a write that may have created
    the file (or the repo) they describe."""
    target = repo_id.lower()
    doomed = [k for k in _negative if k[0] == repo_type and k[1].lower() == target]
    for k in doomed:
        _negative.pop(k, None)
    return len(doomed)


async def fetch_metadata(repo_type: str, repo_id: str, revision: str, filename: str):
    """HEAD upstream for etag/commit/size. Cheap, and always authoritative."""
    url = upstream_resolve_url(repo_type, repo_id, revision, filename)
    return await asyncio.to_thread(get_hf_file_metadata, url, token=settings.hf_token)


_TRUE = {"1", "true", "yes", "on"}


def _flag(request: Request, name: str) -> bool:
    """A request-scoped opt-in header. Absent or unrecognised means off.

    Deliberately not a query parameter: a query string changes the URL, and the
    URL is the cache key the client and every proxy between us agree on. A
    header asks for different HANDLING of the same resource, which is what these
    two do.
    """
    return (request.headers.get(name) or "").strip().lower() in _TRUE


def _cache_headers(commit: str, etag: str | None, extra: dict | None = None) -> dict[str, str]:
    hdrs: dict[str, str] = {"x-repo-commit": commit}
    if etag:
        hdrs["etag"] = etag if etag.startswith(('"', "W/")) else f'"{etag}"'
    if extra:
        hdrs.update(extra)
    return hdrs



# ---------------------------------------------------------------------------
# Muninn-reserved paths. THE ONE LIST.
#
# Every path Muninn itself owns is claimed here, whether or not the surface
# behind it is mounted. A surface switched off by configuration has no router,
# so without this its paths reach the catch-all and are proxied to the Hub: an
# operator who set XHC_DOCKER_ENABLED=0 got the Hub's 401 and HTML on /v2/,
# with the upstream's headers. A disabled surface must answer locally.
#
# A path here that reaches the catch-all is ALWAYS answered locally, never
# forwarded. That covers two cases with one rule: the surface is disabled, or
# it is enabled but the method or sub-path does not exist on it (POST /healthz,
# /_cache/typo). Both are Muninn's to answer, and neither is the Hub's.
#
# ADDING A SURFACE: add its path here in the same change that adds its router.
# `subtree=True` claims the path and everything under it; `False` claims the
# exact path (with or without a trailing slash) and leaves deeper paths to HF,
# for single endpoints whose name could plausibly also be a Hub namespace.
# More specific entries go first -- the first match names the setting.
#
# `served_here=True` marks the one surface whose handler lives INSIDE the
# catch-all (datasets-server). While enabled it passes the early check so its
# branch below can run; anything that branch does not take is refused by the
# second check just before the proxy, so it still never reaches the Hub.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Reserved:
    path: str
    subtree: bool
    enabled: Callable[[], bool]
    disabled_reason: str
    served_here: bool = False


_DOCS_OFF = "the API documentation is disabled (XHC_DOCS=0)"

_RESERVED: tuple[_Reserved, ...] = (
    _Reserved("v2", True, lambda: settings.docker_enabled,
              "the OCI registry surface is disabled (XHC_DOCKER_ENABLED=0)"),
    _Reserved("_cache/docker", True, lambda: settings.docker_enabled,
              "the docker management API is disabled (XHC_DOCKER_ENABLED=0)"),
    # Off when XHC_MANAGE_TOKEN is unset, so /_cache/typo on a deployment with
    # no token names the setting rather than calling it a typo. The routed
    # /_cache paths refuse in managegate.ManageRoute with the same text.
    _Reserved("_cache", True, managegate.enabled, managegate.DISABLED_REASON),
    _Reserved("_auth", True, webauth.enabled,
              "the browser login is disabled (XHC_OIDC_ISSUER is unset)"),
    _Reserved("_console", True, webauth.enabled,
              "the key-management API is disabled (XHC_OIDC_ISSUER is unset)"),
    _Reserved("datasets-server", True, lambda: bool(settings.datasets_server),
              "the datasets-server proxy is disabled (XHC_DATASETS_SERVER is empty)",
              served_here=True),
    _Reserved("docs", False, lambda: settings.docs_enabled, _DOCS_OFF),
    _Reserved("docs/oauth2-redirect", False, lambda: settings.docs_enabled, _DOCS_OFF),
    _Reserved("redoc", False, lambda: settings.docs_enabled, _DOCS_OFF),
    _Reserved("openapi.json", False, lambda: settings.docs_enabled, _DOCS_OFF),
    _Reserved("healthz", False, lambda: True, ""),
    _Reserved("metrics", False, lambda: True, ""),
)


def _reserved_path(full_path: str) -> _Reserved | None:
    """The reserved entry this request path falls under, or None for HF paths.

    Matched on the dot-segment-normalised path, because httpx normalises
    `api/../v2/` to `/v2/` when it builds the upstream URL -- matching the raw
    string would let that spelling through to the Hub.
    """
    key = posixpath.normpath("/" + full_path).lstrip("/")
    for entry in _RESERVED:
        if key == entry.path or (entry.subtree and key.startswith(entry.path + "/")):
            return entry
    return None


def _reserved_refusal(entry: _Reserved, full_path: str, request: Request) -> Response:
    if entry.enabled():
        reason = f"no such Muninn endpoint: {request.method} /{full_path}"
    else:
        reason = entry.disabled_reason
    return PlainTextResponse(reason + "\n", status_code=404)


# ---------------------------------------------------------------------------
# READ-ONLY TOWARD THE HUB.
#
# Everything this surface forwards goes out with the CACHE's Hub token
# (_apply_upstream_auth), so forwarding a write would let any client commit,
# create or delete repos, change settings or open discussions AS THE CACHE --
# with whatever write access that token has, and on an open cache, for anyone.
# Muninn has no push path for Hugging Face; nothing legitimate needs this.
#
# So only GET and HEAD go upstream, plus POSTs that are reads in disguise,
# named one by one. Taken from huggingface_hub 0.34.4, every POST it makes:
#
#   api/{type}s/{repo}/paths-info/{rev}   HfApi.get_paths_info, which
#                                         HfFileSystem uses to stat files. READ.
#
# Every other POST in that library writes: create_commit and preupload,
# create_repo, move_repo, create_branch, create_tag, super_squash_history,
# permanently_delete_lfs_files, the LFS batch/verify/complete calls,
# discussions, Space secrets/variables/hardware/storage/pause/restart/
# duplicate, collections, access requests, webhooks, jobs, inference
# endpoints -- and validate-yaml, which is only called on the way to a push.
# None of them is on a download or listing path.
#
# Applies in EVERY mode, with or without XHC_HF_AUTH and XHC_HF_RULES: this is
# not authorisation, it is what the cache's credential may be used for.
#
# THE ONE EXCEPTION IS OPT-IN: XHC_HF_WRITES=on (app/hfwrites.py). Then a named
# set of repository writes is forwarded, each only on a per-repo grant, after
# the credential gate. Anything that set does not name is still refused here.
# ---------------------------------------------------------------------------

_READ_ONLY_POSTS = (
    re.compile(r"api/(models|datasets|spaces)/[^/]+(/[^/]+)?/paths-info/.+"),
)


def _may_forward(method: str, full_path: str) -> bool:
    # A GET that is a WRITE: the xet write token is a CAS upload credential for
    # the cache's own account. Refused here in every mode and whatever
    # XHC_BLOCK_CLIENT_XET says -- that setting governs read tokens, and handing
    # this one to anyone with pull access is the exposure this section closes.
    # With XHC_HF_WRITES=on, hfwrites claims it first and requires push.
    if "/xet-write-token/" in f"/{full_path}":
        return False
    if method in ("GET", "HEAD"):
        return True
    if method == "POST":
        return any(p.fullmatch(full_path) for p in _READ_ONLY_POSTS)
    return False


def _read_only_refusal(request: Request, full_path: str) -> Response:
    log.warning("refused to forward %s /%s: read-only toward the Hub", request.method, full_path)
    if hfwrites.enabled():
        text = (f"Muninn does not forward this write to the Hugging Face Hub: with "
                f"XHC_HF_WRITES=on it forwards repository content writes only (commits, "
                f"uploads, branches, tags, and creating, moving or deleting repos). "
                f"{request.method} /{full_path} is not one of them.\n")
    else:
        text = (f"Muninn is read-only toward the Hugging Face Hub: {request.method} requests "
                "are not forwarded, except the read-only POST endpoints downloads use.\n")
    return PlainTextResponse(text, status_code=405, headers={"allow": "GET, HEAD"})


def _web_root_file(full_path: str) -> Path | None:
    """Resolve a request path inside XHC_WEB_ROOT, or None.

    Returns None when no web root is configured, when nothing exists at that
    path, or when the path escapes the root. The caller falls through to Hugging
    Face on None, which is what lets one hostname serve a homepage AND a cache.

    CONTAINMENT IS ENFORCED BY RESOLUTION, NOT BY STRING COMPARISON. `..` and
    symlinks both escape a prefix check on the raw path -- that is the classic
    bypass -- so the candidate is fully resolved and then tested for containment
    against the resolved root. A symlink out of the root fails the same test as
    `../../etc/passwd`, without needing to be special-cased.

    An empty path is index.html, and so is a directory, because that is what a
    browser asking for "/" means.
    """
    root_cfg = settings.web_root
    if not root_cfg:
        return None
    try:
        root = Path(root_cfg).resolve(strict=True)
    except OSError:
        # A configured-but-missing web root is a misconfiguration, not a reason
        # to start serving HF for the homepage. Say so once per request rather
        # than failing the request: the cache still works.
        log.warning("XHC_WEB_ROOT=%s does not exist; serving nothing from it", root_cfg)
        return None

    rel = full_path.strip("/")
    candidate = root / rel if rel else root
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        return None

    if resolved != root and root not in resolved.parents:
        log.warning("web root traversal refused: %r resolved outside %s", full_path, root)
        return None

    if resolved.is_dir():
        index = resolved / "index.html"
        if not index.is_file():
            return None
        try:
            index = index.resolve(strict=True)
        except OSError:
            return None
        if root not in index.parents:
            return None
        return index
    return resolved if resolved.is_file() else None


@router.api_route(
    "/{full_path:path}",
    methods=["GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
)
async def catch_all(full_path: str, request: Request) -> Response:
    # 0. A static web root, if one is configured, so this hostname can be a
    #    homepage as well as a cache. Checked FIRST among the catch-all's
    #    branches because it is the only one keyed on a file existing rather
    #    than on a path shape -- and it falls through when the file is absent,
    #    so HF traffic is untouched. /v2, /healthz, /metrics and /_cache do not
    #    reach here while their routers are mounted -- those are mounted before
    #    this one. When a surface is switched off they DO; see 0r.
    if settings.web_root and request.method in ("GET", "HEAD"):
        served = _web_root_file(full_path)
        if served is not None:
            return FileResponse(served)

    # 0r. MUNINN-RESERVED PATHS NEVER REACH THE HUB. After the web root, so an
    #     operator may still serve their own page at a reserved path whose
    #     surface they switched off (a static /docs, say): a local answer they
    #     chose, not an upstream one. BEFORE the credential gate, because the
    #     gate protects the Hub proxy and this branch never reaches it. A
    #     docker client probing /v2/ on a cache with docker off should learn
    #     that, not receive the HF surface's Basic challenge and try to log in
    #     to a registry that is not there. Nothing is disclosed that the
    #     surface's absence does not already disclose.
    reserved = _reserved_path(full_path)
    if reserved is not None and not (reserved.served_here and reserved.enabled()):
        return _reserved_refusal(reserved, full_path, request)

    # 0d. NO DOT OR EMPTY SEGMENTS, in every mode. The upstream client
    #     normalises them, so `org/allowed/../secret` would be checked -- by the
    #     ingest policy, by XHC_ALLOW_REPOS, by a key's rules -- as one repo and
    #     fetched as another. Reserved paths are matched on the normalised path
    #     above, so this cannot shadow them.
    if reserved is None:
        try:
            hfauthz.segments(full_path)
        except hfauthz.BadPath as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    # 0w. NO WRITES REACH THE HUB, whatever the credentials or rules say: what
    #     goes upstream carries the cache's own token. Before the credential gate
    #     because the answer is the same for everyone and discloses nothing.
    #     A reserved path is Muninn's and is answered locally below, never
    #     forwarded, so it keeps its own 404.
    #     With XHC_HF_WRITES=on, a write hfwrites names is let past HERE and
    #     nowhere else; it is authorised after the credential gate, at 0w2.
    write = (hfwrites.match(request.method, full_path)
             if reserved is None and hfwrites.enabled() else None)
    if reserved is None and write is None and not _may_forward(request.method, full_path):
        return _read_only_refusal(request, full_path)

    # 0a. THE CREDENTIAL GATE FOR THIS ENTIRE SURFACE, and its position is the
    #     design. It sits AFTER the web root so a homepage and its assets stay
    #     public -- the login button has to render before anyone has a
    #     credential -- and BEFORE every other branch, so there is no path
    #     shape that reaches the Hub without passing it.
    #
    #     Placed here rather than as a router dependency for one reason: the web
    #     root is served from inside this function, so a dependency would have
    #     to gate the homepage too, and a cache whose front page 401s is not a
    #     front page.
    #
    #     Off by default. When on, it covers api paths, resolve paths, the
    #     datasets-server proxy and everything unrecognised, because it runs
    #     before any of them are parsed.
    refused = await dockerauth.authenticate_hf_request(request)
    if refused is not None:
        return refused

    # 0w2. A WRITE (XHC_HF_WRITES=on only). Authorised per repository with push
    #      (and delete, for a destructive one), its body inspected, forwarded
    #      and audited, all in hfwrites. Before 0b because 0b asks `pull`, and
    #      before the xet block because a write token is not a read bypass.
    if write is not None:
        return await hfwrites.handle(request, full_path, write)

    # 0b. PER-KEY RULES (XHC_HF_RULES), decided from the path alone and before
    #     any branch below, so a cached hit is refused exactly as a miss is.
    #     Every path gets a decision -- see hfauthz. The handlers re-check the
    #     repo they actually serve against what was authorised here.
    denied = hfauthz.authorize(request, full_path)
    if denied is not None:
        return denied

    # 1. Stop clients from negotiating Xet through us. If they got a real
    #    casUrl they would pull bytes straight from HF and the cache would
    #    never see them -- a silent, and very expensive, bypass.
    if settings.block_client_xet and is_xet_token_path(full_path):
        log.debug("blocking client xet negotiation: %s", full_path)
        return JSONResponse(
            {"error": "Xet is disabled on this cache endpoint; use the LFS resolve path."},
            status_code=404,
        )

    # Opt-in datasets-server proxy. Checked first: it owns its own prefix and
    # must never be parsed as a repo path.
    if settings.datasets_server and request.method in ("GET", "HEAD"):
        ds = viewer.parse_datasets_server_path(full_path)
        if ds is not None:
            return await serve_datasets_server(*ds, request)

    parsed = parse_resolve(full_path)
    if parsed is not None and request.method in ("GET", "HEAD"):
        repo_type, repo_id, revision, filename = parsed
        response = await serve_file(repo_type, repo_id, revision, filename, request)
        # The one place every file request's outcome is known. The client label
        # is self-reported (X-Muninn-Client): useful for attribution, but not an
        # audit trail -- see docs/SPEC-0.3.0.md item 7.
        metrics.record_request(
            response.headers.get("x-xhc-cache", str(response.status_code)),
            request.headers.get("x-muninn-client"),
        )
        # HEAD carries the full content-length and transfers NO BODY. Counting
        # it inflated bytes_served_total by one whole file size per metadata
        # probe -- and huggingface_hub HEADs every file in a repo before
        # downloading any of it, so a 419-file snapshot booked the entire repo
        # as "served" before a byte moved. Measured: 5 HEADs of a 10,985 B file
        # added exactly 54,925. Every historical served figure from this cache
        # is inflated by the metadata traffic that preceded the transfers.
        #
        # 206 is NOT excluded: there content-length is the range length, which
        # is exactly what was sent.
        if request.method != "HEAD":
            try:
                metrics.record_served(int(response.headers.get("content-length", 0)))
            except (TypeError, ValueError):
                pass
        return response

    # Repo info is the one metadata endpoint we can honestly answer ourselves,
    # and it is the one that decides whether an orphaned repo is usable at all:
    # snapshot_download enumerates through it before fetching anything.
    if request.method == "GET" and settings.synthesize_repo_info:
        info = parse_repo_info_path(full_path)
        if info is not None:
            return await serve_repo_info(*info, full_path, request)
        tree = parse_tree_path(full_path)
        if tree is not None:
            return await serve_tree(*tree, full_path, request)

        view = viewer.parse_path(full_path)
        if view is not None:
            return await serve_viewer(*view, full_path, request)

    # The second half of 0r: a served_here surface whose branch above did not
    # take the request (wrong method, bare prefix) is still Muninn's, not HF's.
    if reserved is not None:
        return _reserved_refusal(reserved, full_path, request)

    return await proxy_upstream(full_path, request)


def _passthrough(upstream: httpx.Response) -> Response:
    """Forward an upstream response we have already decoded.

    content-encoding is stripped: httpx decompresses .content, so keeping the
    header would make clients try to gunzip plain bytes.
    """
    out = {k: v for k, v in upstream.headers.items() if k.lower() not in _DECODED_DROP}
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=out,
        media_type=upstream.headers.get("content-type"),
    )


def _apply_upstream_auth(headers: dict) -> dict:
    """Replace whatever the CLIENT sent with the cache's own Hub credential.

    THE CACHE AUTHENTICATES TO THE HUB AS ITSELF. A client reaches us with a
    ravencache key, sent through Hugging Face's own tooling -- `HF_TOKEN` becomes
    `Authorization: Bearer <our key>` -- so that header is a credential for THIS
    service and is meaningless upstream. It is stripped unconditionally.

    This replaced an `if "authorization" not in headers` guard that applied the
    cache's token only when the client had sent none. Two things were wrong with
    it, and the first is the one that bites immediately:

      * A client authenticating to us would have OUR OWN KEY FORWARDED TO
        HUGGING FACE -- a third party -- and the pull would then fail there,
        because a ravencache key is not a Hub token.
      * Which credential the cache used upstream depended on what the client
        happened to send. A user with their own HF_TOKEN set silently caused
        ingest to be authorised as them rather than as the cache, so what landed
        in a SHARED cache was decided by whoever asked first.

    The /v2 surface already worked this way -- the cache presents its own
    registry credentials and never forwards a client's. This makes the two
    surfaces agree.

    A consequence worth stating rather than discovering: a user cannot reach a
    gated repo through this cache by supplying their own entitlement. The cache
    can fetch what the cache can fetch. That is the correct behaviour for shared
    storage, because anything fetched is then served to everyone whose rules
    cover the path, regardless of their own Hub access.
    """
    headers = {k: v for k, v in headers.items() if k.lower() != "authorization"}
    if settings.hf_token:
        headers["authorization"] = f"Bearer {settings.hf_token}"
    return headers


async def _proxy_get(full_path: str, request: Request) -> httpx.Response:
    """GET upstream, non-streamed.

    Unreachable is not the same as deleted, so a transport error surfaces as 502
    rather than falling back to cached data -- a Hub outage must never start
    serving stale listings.
    """
    url = f"{settings.upstream}/{quote(full_path)}"
    if request.url.query:
        url = f"{url}?{request.url.query}"
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}
    headers = _apply_upstream_auth(headers)
    headers.setdefault("accept-encoding", "identity")
    try:
        resp = await get_client().get(url, headers=headers)
    except httpx.HTTPError as exc:
        metrics.record_upstream(None)
        raise HTTPException(status_code=502, detail=f"upstream error: {exc}") from exc
    metrics.record_upstream(resp.status_code)
    return resp


async def serve_repo_info(
    repo_type: str, repo_id: str, revision: str | None, full_path: str, request: Request
) -> Response:
    """Proxy repo info, falling back to the cached snapshot on an upstream 404."""
    if (denied := hfauthz.require(request, repo_type, repo_id)) is not None:
        return denied
    upstream = await _proxy_get(full_path, request)
    if upstream.status_code != 404:
        return _passthrough(upstream)

    body = synthesize_repo_info(repo_type, repo_id, revision)
    if body is None:
        return _passthrough(upstream)

    log.info(
        "repo info synthesized for %s/%s (upstream 404, %d files from cache)",
        repo_type,
        repo_id,
        len(body["siblings"]),
    )
    return JSONResponse(
        body,
        headers={
            "x-xhc-synthesized": "true",
            "x-xhc-cache": "SYNTHESIZED",
            "x-repo-commit": body["sha"],
        },
    )


async def serve_tree(
    repo_type: str,
    repo_id: str,
    revision: str,
    path_in_repo: str,
    full_path: str,
    request: Request,
) -> Response:
    """Proxy a tree listing, falling back to the cached snapshot on a 404."""
    if (denied := hfauthz.require(request, repo_type, repo_id)) is not None:
        return denied
    upstream = await _proxy_get(full_path, request)
    if upstream.status_code != 404:
        return _passthrough(upstream)

    params = request.query_params
    entries = synthesize_tree(
        repo_type,
        repo_id,
        revision,
        path_in_repo,
        recursive=params.get("recursive", "").lower() in ("1", "true"),
        expand=params.get("expand", "").lower() in ("1", "true"),
    )
    if entries is None:
        return _passthrough(upstream)
    log.info(
        "tree synthesized for %s/%s@%s (upstream 404, %d entries)",
        repo_type,
        repo_id,
        revision,
        len(entries),
    )
    # One page, no Link header: huggingface_hub's paginate() stops when Link is
    # absent, so this terminates correctly rather than by accident.
    return JSONResponse(
        entries, headers={"x-xhc-synthesized": "true", "x-xhc-cache": "SYNTHESIZED"}
    )


def _policy_refusal(repo_type: str, repo_id: str, decision: policy.Decision) -> Response:
    """403 for a local policy decision.

    Deliberately does not borrow an HF X-Error-Code: this is our rule, not the
    Hub's answer, and labelling it GatedRepo would send people hunting for a
    token that would not help.
    """
    log.warning("policy refused %ss/%s: %s", repo_type, repo_id, decision.reason)
    return JSONResponse(
        {"error": f"blocked by cache policy: {decision.reason}", "repo": f"{repo_type}s/{repo_id}"},
        status_code=403,
        headers={"x-xhc-policy": "denied"},
    )


async def serve_datasets_server(endpoint: str, upstream_path: str, request: Request) -> Response:
    """Proxy datasets-server.huggingface.co, caching the small stable endpoints.

    Nothing routes here unless a caller deliberately uses the /datasets-server/
    prefix, so this cannot affect a node that does not know about it.
    """
    if (denied := hfauthz.require_path(request, f"{viewer.DS_SERVER_PREFIX}{upstream_path}")):
        return denied
    url = f"{settings.datasets_server}/{quote(upstream_path)}"
    if request.url.query:
        url = f"{url}?{request.url.query}"

    cacheable = viewer.ds_cacheable(endpoint)
    key = viewer.ds_cache_key(upstream_path, request.url.query) if cacheable else ""

    if cacheable:
        entry = viewer.ds_load(key)
        if entry is not None and viewer.is_fresh(entry):
            metrics.record_request("DSSERVER-HIT", request.headers.get("x-muninn-client"))
            return Response(
                content=entry["body"].encode(),
                status_code=200,
                headers={"x-xhc-cache": "DSSERVER-HIT"},
                media_type=entry.get("content_type", "application/json"),
            )
    else:
        entry = None

    headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}
    headers = _apply_upstream_auth(headers)
    headers.setdefault("accept-encoding", "identity")
    try:
        upstream = await get_client().get(url, headers=headers)
    except httpx.HTTPError as exc:
        metrics.record_upstream(None)
        raise HTTPException(status_code=502, detail=f"datasets-server error: {exc}") from exc
    metrics.record_upstream(upstream.status_code)

    if cacheable and upstream.status_code == 200:
        viewer.ds_store(key, upstream.content, upstream.headers.get("content-type"))
        resp = _passthrough(upstream)
        resp.headers["x-xhc-cache"] = "DSSERVER-MISS"
        metrics.record_request("DSSERVER-MISS", request.headers.get("x-muninn-client"))
        return resp

    if upstream.status_code == 404 and entry is not None:
        # Dataset gone upstream; the copy we hold is the only one left.
        metrics.record_request("DSSERVER-SYNTHESIZED", request.headers.get("x-muninn-client"))
        return Response(
            content=entry["body"].encode(),
            status_code=200,
            headers={"x-xhc-cache": "DSSERVER-SYNTHESIZED", "x-xhc-synthesized": "true"},
            media_type=entry.get("content_type", "application/json"),
        )

    metrics.record_request(
        f"DSSERVER-{upstream.status_code}", request.headers.get("x-muninn-client")
    )
    return _passthrough(upstream)


async def serve_viewer(
    repo_id: str, endpoint: str, key_suffix: str, full_path: str, request: Request
) -> Response:
    """Serve small dataset metadata from cache, and keep serving it if the
    dataset is deleted upstream."""
    if (denied := hfauthz.require(request, "dataset", repo_id)) is not None:
        return denied
    entry = viewer.load(repo_id, key_suffix)
    if entry is not None and viewer.is_fresh(entry):
        metrics.record_request("VIEWER-HIT", request.headers.get("x-muninn-client"))
        return Response(
            content=entry["body"].encode(),
            status_code=200,
            headers={"x-xhc-cache": "VIEWER-HIT"},
            media_type=entry.get("content_type", "application/json"),
        )

    upstream = await _proxy_get(full_path, request)

    if upstream.status_code == 200:
        viewer.store(repo_id, key_suffix, upstream.content, upstream.headers.get("content-type"))
        resp = _passthrough(upstream)
        resp.headers["x-xhc-cache"] = "VIEWER-MISS"
        return resp

    if upstream.status_code == 404 and entry is not None:
        # The dataset is gone; the copy we hold is the only one left. Same
        # reasoning as repo-info synthesis, and tagged the same way.
        log.info("viewer %s for %s served from cache (upstream 404)", endpoint, repo_id)
        metrics.record_request("VIEWER-SYNTHESIZED", request.headers.get("x-muninn-client"))
        return Response(
            content=entry["body"].encode(),
            status_code=200,
            headers={"x-xhc-cache": "VIEWER-SYNTHESIZED", "x-xhc-synthesized": "true"},
            media_type=entry.get("content_type", "application/json"),
        )

    return _passthrough(upstream)


async def _hit_response(
    local: cachefs.ResolvedFile,
    repo_type: str,
    repo_id: str,
    revision: str,
    filename: str,
    request: Request,
    range_header: str | None,
) -> Response:
    etag = local.etag
    if etag is None and _flag(request, "x-muninn-local-only"):
        # local-only promises NO upstream call, and that promise has to hold on
        # the hit path too. Without it this branch would reach the Hub for an
        # etag on a cached file -- the one case a caller using this header is
        # least expecting it, because the answer was on disk the whole time.
        log.debug("local-only: serving %s/%s without an etag", repo_id, filename)
    elif etag is None:
        # No symlink to read the etag from (copy-mode cache, or an odd
        # filesystem). huggingface_hub refuses to download without an ETag, so
        # pay for one upstream HEAD rather than serving an unusable response.
        try:
            meta = await fetch_metadata(repo_type, repo_id, revision, filename)
            etag = (meta.etag or "").strip('"') or None
        except Exception as exc:  # noqa: BLE001 - degrade to no-etag, do not fail the hit
            log.warning("hit for %s/%s but no etag available: %s", repo_id, filename, exc)

    extra = {"x-xhc-cache": "HIT"}
    if _flag(request, "x-muninn-local-only"):
        # Self-describing: this answer was not revalidated against upstream.
        extra["x-xhc-local-only"] = "1"
        extra["x-xhc-cache"] = "HIT-LOCAL"
    headers = _cache_headers(local.commit, etag, extra)

    # Conditional request: if the client already holds this exact blob, say so
    # instead of resending it. Deliberately no If-Modified-Since -- blob mtimes
    # come from our ingest, not from the Hub, so any answer would be a guess.
    inm = request.headers.get("if-none-match")
    if inm and etag:
        quoted = etag if etag.startswith(('"', "W/")) else f'"{etag}"'
        candidates = {t.strip() for t in inm.split(",")}
        if "*" in candidates or quoted in candidates or etag in candidates:
            headers["x-xhc-cache"] = "HIT-304"
            return Response(status_code=304, headers=headers)

    # A prewarm on something already cached is a no-op, and saying "204, already
    # here" is more useful than streaming the file to a caller who asked us NOT
    # to send it.
    if _flag(request, "x-muninn-prewarm"):
        headers["content-length"] = "0"
        return Response(status_code=204, headers=headers)

    if request.method == "HEAD":
        headers["content-length"] = str(local.size)
        headers["accept-ranges"] = "bytes"
        return Response(status_code=200, headers=headers)
    return serving.file_response(local.path, local.size, range_header, headers)


def _raise_unless_done(job) -> None:
    """After `await job.done.wait()`: answer for any outcome other than done.

    `done` is set for three outcomes, not one. `error` is a failed ingest.
    `interrupted` is a job the cache recorded as cut short because it is
    shutting down (JobManager.interrupt_active): nothing is wrong with the
    file, this process will just not finish it. Checking only for `error` let
    that case fall through to "ingest reported success but file missing", a
    500 describing something that did not happen.
    """
    if job.state == "error":
        raise HTTPException(status_code=502, detail=f"ingest failed: {job.error}")
    if job.state != "done":
        raise HTTPException(
            status_code=503,
            detail=f"ingest {job.state}: this cache is shutting down; retry",
            headers={"retry-after": "5"},
        )


async def serve_file(
    repo_type: str, repo_id: str, revision: str, filename: str, request: Request
) -> Response:
    if (denied := hfauthz.require(request, repo_type, repo_id)) is not None:
        return denied
    range_header = request.headers.get("range")

    # Policy gates ingest by default, so a repo already cached keeps serving
    # even after a policy change -- tightening policy must not break a fleet
    # mid-rollout. scope=all opts into enforcing on hits too.
    pol = policy.load()
    decision = policy.check(repo_type, repo_id, pol)
    if not decision.allowed and policy.enforced_on_hits(pol):
        return _policy_refusal(repo_type, repo_id, decision)

    # --- fast path: already cached -----------------------------------------
    local = cachefs.resolve_local(repo_type, repo_id, revision, filename)
    if local is not None:
        # A mutable ref may have moved upstream. This is a ref-level check with
        # a TTL, not a per-file HEAD -- inside the TTL, and always for
        # sha-pinned requests, it costs nothing and the hit stays a disk read.
        # refs.is_stale asks the Hub whether a mutable ref has moved. local-only
        # must not, so it serves what is on disk WITHOUT revalidating and says so
        # in the response. That is the trade the header buys: an answer that is
        # certainly local, rather than one that is certainly current. A caller
        # who needs currency should not be using this header.
        if _flag(request, "x-muninn-local-only"):
            pass
        elif await refs.is_stale(repo_type, repo_id, revision, local.commit):
            local = None  # fall through and ingest the new commit
        else:
            return await _hit_response(
                local, repo_type, repo_id, revision, filename, request, range_header
            )

    # --- local-only: answer from disk, or not at all -------------------------
    # Placed HERE, before the upstream metadata fetch, because that fetch is the
    # thing the header exists to avoid. A local-only check that still asks the
    # Hub is not local-only -- it is a slower miss with a promise attached, and
    # it fails when the Hub is unreachable, which is exactly when a caller most
    # wants to know what is on disk.
    if _flag(request, "x-muninn-local-only"):
        if _flag(request, "x-muninn-prewarm"):
            raise HTTPException(
                status_code=400,
                detail="X-Muninn-Local-Only and X-Muninn-Prewarm are mutually exclusive: "
                "a prewarm requires upstream metadata, which local-only forbids",
            )
        return JSONResponse(
            {"detail": f"not cached locally: {repo_id}/{filename}", "revision": revision},
            status_code=404,
            headers={"x-xhc-cache": "MISS-LOCAL", "cache-control": "no-store"},
        )

    # --- miss: ask upstream what this actually is --------------------------
    cached_miss = _negative_cache_get(repo_type, repo_id, revision, filename)
    if cached_miss is not None:
        raise cached_miss

    try:
        meta = await fetch_metadata(repo_type, repo_id, revision, filename)
    except Exception as exc:
        # A MISSING FILE IS NOT AN UPSTREAM FAILURE. huggingface_hub probes for
        # OPTIONAL files on every single model load -- processor_config.json,
        # chat_template.jinja, preprocessor variants -- and most repos do not
        # have most of them. Returning 502 told the client the mirror was
        # broken, so it retried 5 times with exponential backoff (~23 s) before
        # falling back, on EVERY absent optional file. Observed loading
        # MIT/ast-finetuned-audioset: six 502s and five retries for one file
        # that simply does not exist.
        failure = upstream_failure(exc, repo_id, filename)
        if failure.status_code == 404:
            _negative_cache_put(repo_type, repo_id, revision, filename, failure)
        raise failure from exc

    commit = meta.commit_hash or revision
    etag = (meta.etag or "").strip('"')
    size = meta.size or 0

    # The requested revision may have been a branch; re-check by commit sha in
    # case we already hold these exact bytes under a different ref.
    if meta.commit_hash:
        local = cachefs.resolve_local(repo_type, repo_id, meta.commit_hash, filename)
        if local is not None:
            headers = _cache_headers(local.commit, local.etag or etag, {"x-xhc-cache": "HIT"})
            if request.method == "HEAD":
                headers["content-length"] = str(local.size)
                headers["accept-ranges"] = "bytes"
                return Response(status_code=200, headers=headers)
            return serving.file_response(local.path, local.size, range_header, headers)

    if request.method == "HEAD":
        # Answer metadata without triggering an ingest. Clients HEAD constantly
        # (every hf_hub_download starts with one); ingesting here would prefetch
        # things nobody asked to download.
        headers = _cache_headers(
            commit,
            etag,
            {
                "content-length": str(size),
                "accept-ranges": "bytes",
                "x-xhc-cache": "MISS",
            },
        )
        return Response(status_code=200, headers=headers)

    # --- policy: refuse before any bytes move -------------------------------
    if not decision.allowed:
        return _policy_refusal(repo_type, repo_id, decision)
    size_decision = policy.check_size(size, pol)
    if not size_decision.allowed:
        return _policy_refusal(repo_type, repo_id, size_decision)

    # --- kick off (or join) the single-flight ingest ------------------------
    incomplete = str(cachefs.blob_incomplete_path(repo_type, repo_id, etag)) if etag else None
    job = await manager.ensure_file(
        repo_type,
        repo_id,
        revision,
        filename,
        expected_size=size or None,
        incomplete_path=incomplete,
        etag=etag or None,
    )

    headers = _cache_headers(
        commit,
        etag,
        {
            "x-xhc-cache": "MISS",
            "x-xhc-job": job.id,
            "x-xhc-miss-policy": settings.miss_policy,
        },
    )

    # Prewarm: the ingest is running; the caller explicitly does not want the
    # bytes. Return the job so they can poll, and send no body. This is the only
    # branch that ignores miss_policy, because miss_policy answers "how do we
    # serve this request" and prewarm has already said "do not serve it to me".
    if _flag(request, "x-muninn-prewarm"):
        headers["x-xhc-cache"] = "MISS-PREWARM"
        headers["content-length"] = "0"
        return Response(status_code=202, headers=headers)

    if settings.miss_policy == "redirect":
        # Client pulls from HF with its own hf_xet at full fan-out speed -- no
        # slower than going direct -- while we ingest in the background for
        # everyone who asks next.
        url = upstream_resolve_url(repo_type, repo_id, revision, filename)
        return RedirectResponse(url, status_code=302, headers=headers)

    # --- the object-store tier -----------------------------------------------
    # The job tries the tier before the Hub. Once it knows whether the tier is
    # supplying the bytes, this request can choose how to answer.
    #
    # verify-first (the default): tier bytes land where tail_follow does not
    # look and are renamed into place only after their hash matches, so this
    # waits for the job exactly as `wait` does. A Hugging Face client in cache
    # mode does not hash what it receives, so streaming unverified tier bytes
    # to it would let a wrong object of the right length through in full.
    tier_stream = False
    if tier.enabled():
        await job.tier_decided.wait()
        if job.tier_source and (
            settings.tier.read_mode == "verify-first" or settings.miss_policy == "wait"
        ):
            await job.done.wait()
            _raise_unless_done(job)
            local = cachefs.resolve_local(repo_type, repo_id, revision, filename)
            if local is None:
                raise HTTPException(
                    status_code=500, detail="ingest reported success but file missing"
                )
            # TIER-HIT only when the tier's bytes are what landed. A tier
            # object that failed verification fell through to the Hub inside
            # the same job, and says so.
            headers["x-xhc-cache"] = "TIER-HIT" if job.served_from == "tier" else "MISS-WAIT"
            return serving.file_response(local.path, local.size, range_header, headers)
        tier_stream = job.tier_source

    if settings.miss_policy == "wait":
        await job.done.wait()
        _raise_unless_done(job)
        local = cachefs.resolve_local(repo_type, repo_id, revision, filename)
        if local is None:
            raise HTTPException(status_code=500, detail="ingest reported success but file missing")
        headers["x-xhc-cache"] = "MISS-WAIT"
        return serving.file_response(local.path, local.size, range_header, headers)

    # stream: tail-follow the partial file as the ingest writes it.
    final_path = (
        Path(settings.cache_dir)
        / cachefs.repo_folder_name(repo_id, repo_type)
        / "snapshots"
        / commit
        / filename
    )
    # TIER-STREAM: served from the tier as it arrives, BEFORE verification.
    # Only under XHC_TIER2_READ_MODE=stream, which is documented as such.
    headers["x-xhc-cache"] = "TIER-STREAM" if tier_stream else "MISS-STREAM"

    # Honour Range on a miss too. Without this, a client resuming an
    # interrupted transfer that lands on a cold cache is served the whole file
    # from byte 0 -- legal, but on a 140GB shard it is minutes of NIC time for
    # bytes the client already has.
    ranges = serving.parse_ranges(range_header, size) if size else None

    if ranges and len(ranges) == 1:
        start, end = ranges[0]
        headers["content-length"] = str(end - start + 1)
        headers["content-range"] = f"bytes {start}-{end}/{size}"
        headers["accept-ranges"] = "bytes"
        return StreamingResponse(
            serving.tail_follow(job, final_path, size, start=start, end=end),
            status_code=206,
            headers=headers,
            media_type="application/octet-stream",
        )

    if ranges and len(ranges) > 1:
        # Multipart off a partially-written file would mean seeking backwards
        # into bytes that may not have landed yet. Rare enough on a cold miss
        # that waiting for the ingest and serving from the finished file is the
        # right trade: correct, and still one upstream fetch.
        await job.done.wait()
        _raise_unless_done(job)
        local = cachefs.resolve_local(repo_type, repo_id, revision, filename)
        if local is None:
            raise HTTPException(status_code=500, detail="ingest reported success but file missing")
        headers["x-xhc-cache"] = "MISS-WAIT-MULTIRANGE"
        return serving.file_response(local.path, local.size, range_header, headers)

    if size:
        headers["content-length"] = str(size)
    return StreamingResponse(
        serving.tail_follow(job, final_path, size),
        status_code=200,
        headers=headers,
        media_type="application/octet-stream",
    )


async def proxy_upstream(full_path: str, request: Request) -> Response:
    """Transparent pass-through for everything that is not file bytes."""
    # Checked again here, at the one function that forwards arbitrary methods,
    # so a new caller cannot skip it.
    if not _may_forward(request.method, full_path):
        return _read_only_refusal(request, full_path)
    if (denied := hfauthz.require_path(request, full_path)) is not None:
        return denied
    url = f"{settings.upstream}/{quote(full_path)}"
    if request.url.query:
        url = f"{url}?{request.url.query}"

    headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}
    # Use the cache's identity unless the client brought its own. This is how
    # you keep Hub tokens off the edge nodes entirely.
    headers = _apply_upstream_auth(headers)
    # httpx injects its own accept-encoding, which would make us request a
    # gzipped body the client never asked for and then forward it verbatim.
    # Pin it to whatever the client actually sent.
    headers.setdefault("accept-encoding", "identity")

    body = await request.body()
    client = get_client()
    req = client.build_request(request.method, url, headers=headers, content=body or None)
    try:
        resp = await client.send(req, stream=True)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"upstream error: {exc}") from exc

    out_headers = {k: v for k, v in resp.headers.items() if k.lower() not in _HOP_BY_HOP}

    async def body_iter():
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
        finally:
            await resp.aclose()

    return StreamingResponse(
        body_iter(),
        status_code=resp.status_code,
        headers=out_headers,
        media_type=resp.headers.get("content-type"),
    )
