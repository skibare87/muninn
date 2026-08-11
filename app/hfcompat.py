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
import time
from pathlib import Path
from urllib.parse import quote, unquote

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from huggingface_hub import errors as hf_errors
from huggingface_hub import get_hf_file_metadata, hf_hub_url

from . import cachefs, policy, refs, serving
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

_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    global _client  # noqa: PLW0603 - module-level singleton client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.request_timeout_s, read=None),
            follow_redirects=False,
        )
    return _client


async def close_client() -> None:
    global _client  # noqa: PLW0603 - module-level singleton client
    if _client is not None:
        await _client.aclose()
        _client = None


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


async def fetch_metadata(repo_type: str, repo_id: str, revision: str, filename: str):
    """HEAD upstream for etag/commit/size. Cheap, and always authoritative."""
    url = upstream_resolve_url(repo_type, repo_id, revision, filename)
    return await asyncio.to_thread(get_hf_file_metadata, url, token=settings.hf_token)


def _cache_headers(commit: str, etag: str | None, extra: dict | None = None) -> dict[str, str]:
    hdrs: dict[str, str] = {"x-repo-commit": commit}
    if etag:
        hdrs["etag"] = etag if etag.startswith(('"', "W/")) else f'"{etag}"'
    if extra:
        hdrs.update(extra)
    return hdrs


@router.api_route(
    "/{full_path:path}",
    methods=["GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
)
async def catch_all(full_path: str, request: Request) -> Response:
    # 1. Stop clients from negotiating Xet through us. If they got a real
    #    casUrl they would pull bytes straight from HF and the cache would
    #    never see them -- a silent, and very expensive, bypass.
    if settings.block_client_xet and is_xet_token_path(full_path):
        log.debug("blocking client xet negotiation: %s", full_path)
        return JSONResponse(
            {"error": "Xet is disabled on this cache endpoint; use the LFS resolve path."},
            status_code=404,
        )

    parsed = parse_resolve(full_path)
    if parsed is not None and request.method in ("GET", "HEAD"):
        repo_type, repo_id, revision, filename = parsed
        return await serve_file(repo_type, repo_id, revision, filename, request)

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
    if "authorization" not in {k.lower() for k in headers} and settings.hf_token:
        headers["authorization"] = f"Bearer {settings.hf_token}"
    headers.setdefault("accept-encoding", "identity")
    try:
        return await get_client().get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"upstream error: {exc}") from exc


async def serve_repo_info(
    repo_type: str, repo_id: str, revision: str | None, full_path: str, request: Request
) -> Response:
    """Proxy repo info, falling back to the cached snapshot on an upstream 404."""
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
    if etag is None:
        # No symlink to read the etag from (copy-mode cache, or an odd
        # filesystem). huggingface_hub refuses to download without an ETag, so
        # pay for one upstream HEAD rather than serving an unusable response.
        try:
            meta = await fetch_metadata(repo_type, repo_id, revision, filename)
            etag = (meta.etag or "").strip('"') or None
        except Exception as exc:  # noqa: BLE001 - degrade to no-etag, do not fail the hit
            log.warning("hit for %s/%s but no etag available: %s", repo_id, filename, exc)

    headers = _cache_headers(local.commit, etag, {"x-xhc-cache": "HIT"})

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

    if request.method == "HEAD":
        headers["content-length"] = str(local.size)
        headers["accept-ranges"] = "bytes"
        return Response(status_code=200, headers=headers)
    return serving.file_response(local.path, local.size, range_header, headers)


async def serve_file(
    repo_type: str, repo_id: str, revision: str, filename: str, request: Request
) -> Response:
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
        if await refs.is_stale(repo_type, repo_id, revision, local.commit):
            local = None  # fall through and ingest the new commit
        else:
            return await _hit_response(
                local, repo_type, repo_id, revision, filename, request, range_header
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

    if settings.miss_policy == "redirect":
        # Client pulls from HF with its own hf_xet at full fan-out speed -- no
        # slower than going direct -- while we ingest in the background for
        # everyone who asks next.
        url = upstream_resolve_url(repo_type, repo_id, revision, filename)
        return RedirectResponse(url, status_code=302, headers=headers)

    if settings.miss_policy == "wait":
        await job.done.wait()
        if job.state == "error":
            raise HTTPException(status_code=502, detail=f"ingest failed: {job.error}")
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
    headers["x-xhc-cache"] = "MISS-STREAM"

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
        if job.state == "error":
            raise HTTPException(status_code=502, detail=f"ingest failed: {job.error}")
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
    url = f"{settings.upstream}/{quote(full_path)}"
    if request.url.query:
        url = f"{url}?{request.url.query}"

    headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}
    # Use the cache's identity unless the client brought its own. This is how
    # you keep Hub tokens off the edge nodes entirely.
    if "authorization" not in {k.lower() for k in headers} and settings.hf_token:
        headers["authorization"] = f"Bearer {settings.hf_token}"
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
