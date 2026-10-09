"""Restore from the tier's index when the Hub cannot answer (phase 2).

Phase 1 writes, beside the content, an index of what the Hub said: which commit
a ref pointed at (one immutable object per observation) and which file in a
commit has which ETag and size. This module READS it, so a model survives the
Hub being unreachable, or the repo being deleted upstream.

WHEN IT RUNS. Only when the Hub has failed to answer in one of these ways, and
never otherwise (trigger_for_status):

- no response at all (DNS, connection refused, TLS, timeout)      "unreachable"
- a 5xx                                                            "upstream_5xx"
- a 404 for the repo or the revision (RepoNotFound,
  RevisionNotFound, or a 404 carrying no code)                     "not_found"

and NOT on:

- 401 or 403, whatever the error code. The upstream said NO. That is how a
  revoked gated grant, a repo made private, or a dead token looks, and serving
  from the index anyway would turn every revocation into a no-op for anything
  this cache ever held. Measured on the public Hub (2026-09-26): an anonymous
  request for a repo that does not exist answers 401, not 404 -- so a cache
  with no valid Hub token cannot tell "deleted" from "not allowed", and does
  not restore. With a valid token the Hub is expected to answer 404 for a
  deleted repo; that is [UNVERIFIED] from here.
- a 404 whose code is EntryNotFound: the Hub has answered about this revision,
  and the file is not in it. Restoring would serve a file the current `main`
  has dropped.
- 429: the upstream is alive and says "later".

TRUST. A mapping is only as trustworthy as whoever can write the bucket, so by
default an entry is used only if its HMAC verifies with XHC_TIER2_INDEX_KEY --
the key from configuration, never anything in the bucket (tier.index_sig_ok).
An unsigned entry is used only when XHC_TIER2_RESTORE_UNSIGNED is on, and
every such restore is logged. A signature that does not verify is treated as
an absent entry: refused, logged, counted.

CONTENT is still verified against the ETag the (verified) entry names, with the
same routine and the same rule as every other tier read (tier._fill_blob,
contenthash). The index says WHICH bytes; the hash says whether these are them.

FRESHNESS. A ref restores to its most recent observation that verifies. That
may be stale: `main` may have moved on the Hub since. Every restored answer
says when the ref was observed (x-xhc-ref-observed-at). A request by commit has
no staleness and carries no such header.

WHAT IS RESTORABLE is exactly what was cached, written back and indexed. A
file this cache never fetched is not in the index, and a commit's listing here
is the files that were indexed for it, which for a repo only ever fetched file
by file is a subset of the repo.

OCI TAGS (restore_oci_tag) use the same machinery: one observation walk, one
signature check, one trigger rule. A tag observation is
index/oci/<upstream>/tags/<repo>/<tag>/<observed_at>-<digest>, signed over
(version, "oci-tag", upstream, repo, tag, "", digest, 0, observed_at) by
tier._oci_tag_index. The manifest it names is then read by digest -- local disk,
else the tier, verified against the digest as it is read -- and served. Blobs,
and the platform manifests an index names, follow BY DIGEST through the
ordinary read-through (ocicompat), which reads the tier before the registry and
so needs no restore. The registry's triggers, mapped from the Hub's
(trigger_for_registry):

- no response from the registry, or from its token endpoint     "unreachable"
- a 5xx from either                                               "upstream_5xx"
- a 404 whose OCI error code is MANIFEST_UNKNOWN or NAME_UNKNOWN  "not_found"

and NOT on a 401 or 403 (UNAUTHORIZED, DENIED, or the token endpoint refusing
the scope), a 429 from either (TOOMANYREQUESTS), a token endpoint that answered
with something unusable, or a 404 carrying any other code or none. The last is
stricter than the Hub's rule on purpose: a registry's 404 for a deleted tag
names itself, and a bare 404 is as likely a proxy in front of the registry
answering for a path it does not route.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote

import httpx

from . import cachefs, contenthash, metrics, ocistore, s3client, tier
from .config import settings

log = logging.getLogger("xhc.tier.restore")

# How many of a ref's newest observations are examined before giving up. Each
# is one HEAD. Bounded, so a bucket stuffed with junk observations costs a
# fixed amount per restore rather than a LIST-sized one.
MAX_REF_CANDIDATES = 16
# Entries fetched at once when a commit's listing is read.
LISTING_CONCURRENCY = 16
# Verified listings kept in memory. Commits are immutable, so a verified
# listing never goes stale; the bound is only about memory.
_LISTING_CACHE_MAX = 64

TRIGGER_UNREACHABLE = "unreachable"
TRIGGER_5XX = "upstream_5xx"
TRIGGER_NOT_FOUND = "not_found"


class Refused(Exception):
    """A restore that did not happen, and why. `result` is the metric label."""

    def __init__(self, result: str, detail: str):
        super().__init__(detail)
        self.result = result


@dataclass(frozen=True)
class Ref:
    commit: str
    # When the Hub was seen to say ref -> commit. None for a request by commit,
    # which needs no observation and has no staleness.
    observed_at: float | None
    auth: str  # "signed" | "unsigned" | "pinned"


@dataclass(frozen=True)
class Entry:
    etag: str
    size: int
    auth: str  # "signed" | "unsigned"


_listings: dict[tuple[str, str, str], dict[str, Entry]] = {}
_last: dict = {}


def reset_for_tests() -> None:
    _listings.clear()
    _last.clear()


def status() -> dict:
    return {"last": dict(_last)} if _last else {}


# ---------------------------------------------------------------------------
# when to restore
# ---------------------------------------------------------------------------


# The OCI error codes that say "this tag or repository is not here" (OCI
# distribution spec, error codes table). TAG_UNKNOWN is not in the spec.
OCI_NOT_FOUND_CODES = frozenset({"MANIFEST_UNKNOWN", "NAME_UNKNOWN"})


def trigger_for_status(status: int | None, error_code: str | None = None,
                       proto: str = "hf") -> str | None:
    """Which upstream answers are a reason to restore. None means: pass it through.

    The one rule for both protocols; only what a 404 must say differs. The
    Hub's X-Error-Code names what is MISSING when it is the file
    (EntryNotFound) and a 404 restores otherwise; a registry's OCI error code
    must name the manifest or the repository. See the module docstring for
    the reasoning; 401, 403 and 429 are never one.
    """
    if status is None:
        return TRIGGER_UNREACHABLE
    if status >= 500:
        return TRIGGER_5XX
    if status == 404:
        if proto == "oci":
            return TRIGGER_NOT_FOUND if error_code in OCI_NOT_FOUND_CODES else None
        if error_code != "EntryNotFound":
            return TRIGGER_NOT_FOUND
    return None


def is_transport_failure(exc: BaseException) -> bool:
    """Connection refused, DNS, TLS, timeout: no answer from the far end.

    The ONLY kind of exception that counts as the upstream being unreachable.
    A local OSError, a parse error or a bug says nothing about the upstream.
    """
    import requests

    return isinstance(exc, requests.exceptions.ConnectionError
                      | requests.exceptions.Timeout | httpx.TransportError)


def trigger_for_exception(exc: BaseException) -> str | None:
    """The same decision for an exception from huggingface_hub or httpx.

    A response is judged by its status and X-Error-Code, as the Hub sent them.
    No response is an outage ONLY when the exception is a transport failure
    (connection refused, DNS, TLS, timeout). Anything else with no response --
    a local OSError, a parsing bug -- says nothing about the Hub, and reading
    it as "unreachable" would start serving stale refs over a local fault.
    """
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if response is None or status is None:
        return TRIGGER_UNREACHABLE if is_transport_failure(exc) else None
    try:
        code = response.headers.get("X-Error-Code")
    except AttributeError:
        code = None
    return trigger_for_status(status, code)


def trigger_for_registry(up: dict) -> str | None:
    """The same decision for a registry, from what registry.get recorded.

    `up` is the `out` dict a manifest fetch filled (ocicompat._revalidate_tag):

        status           the registry's status; absent when no response came
        transport        True when the missing response was a transport failure
        error_code       the first OCI error code in the body, if any
        token            set only when the bearer dance failed:
                           declined     401/403 from the token endpoint
                           unreachable  no response, or a non-200 that is not a refusal
                           unusable     no realm, a non-URL realm, a 200 with no token
        token_status     the token endpoint's status (None: no response)
        token_transport  True when the token fetch failed in transport

    A failed bearer dance hands back the registry's 401, so the status alone
    would call every token-endpoint outage a refusal. The token layer is judged
    first: an outage there (no response, or a 5xx) is the registry being
    unreachable; a refusal there is a refusal; a 429 there is a live endpoint
    asking for patience; anything unusable is not evidence of an outage.
    """
    status = up.get("status")
    if status is None:
        return TRIGGER_UNREACHABLE if up.get("transport") else None
    token = up.get("token")
    if status == 401 and token is not None:
        if token != "unreachable":
            return None
        ts = up.get("token_status")
        if ts is None:
            return TRIGGER_UNREACHABLE if up.get("token_transport") else None
        return TRIGGER_5XX if ts >= 500 else None
    return trigger_for_status(status, up.get("error_code"), proto="oci")


# ---------------------------------------------------------------------------
# reading the index
# ---------------------------------------------------------------------------


def _meta(resp: httpx.Response, name: str) -> str | None:
    # x-amz-meta- for S3 and HMAC interop, x-goog-meta- for GCS under OAuth.
    return resp.headers.get(f"x-amz-meta-{name}") or resp.headers.get(f"x-goog-meta-{name}")


def _judge(auth: object, sig_ok: bool) -> str:
    """signed_ok | unsigned_accepted | unsigned_refused | bad_signature.

    An entry that claims a signature and fails is bad whatever the opt-in says.
    A signature this process cannot check (no key configured) is treated as
    what it then is: unauthenticated.
    """
    t = tier.cfg()
    if auth == tier.AUTH_SIGNED and t.index_key:
        return "signed_ok" if sig_ok else "bad_signature"
    if auth in (tier.AUTH_SIGNED, tier.AUTH_UNSIGNED):
        return "unsigned_accepted" if t.restore_unsigned else "unsigned_refused"
    return "malformed"


def _host_repo(repo_type: str, repo_id: str) -> tuple[str, str]:
    return tier.hf_host(), f"{repo_type}s/{repo_id}"


def _refs_prefix(repo_type: str, repo_id: str, ref: str) -> str:
    return tier.hf_ref_index_key(repo_type, repo_id, ref, 0, "0" * 40).rsplit("/", 1)[0] + "/"


def _commit_prefix(repo_type: str, repo_id: str, commit: str) -> str:
    return tier.hf_commit_index_key(repo_type, repo_id, commit, "x").rsplit("/", 1)[0] + "/"


async def resolve_revision(repo_type: str, repo_id: str, revision: str) -> Ref:
    """revision -> commit, from the newest observation that verifies.

    A 40-hex revision IS a commit and needs no observation. Otherwise the ref's
    observations are listed, newest first (the names sort by time), and each is
    HEADed until one is acceptable. The observation time and the commit are
    read from the object's NAME and are both covered by its signature, so an
    old observation copied under a newer name does not verify.
    """
    if contenthash.GIT_SHA1_RE.match(revision):
        return Ref(revision, None, "pinned")
    host, repo = _host_repo(repo_type, repo_id)

    def sig_ok(sig: object, obs: str, commit: str) -> bool:
        return tier.index_sig_ok(sig, "hf-ref", host, repo, revision, "", commit, 0, obs)

    obs, commit, auth, _ = await _newest_observation(
        _refs_prefix(repo_type, repo_id, revision), contenthash.GIT_SHA1_RE.match, sig_ok,
        f"{repo_type}s/{repo_id}@{revision}", "hf")
    return Ref(commit, int(obs) / 1000, auth)


async def _newest_observation(prefix: str, value_ok, sig_ok, what: str, proto: str,
                              usable=None) -> tuple[str, str, str, object]:
    """The newest observation under `prefix` that verifies (and is usable).

    THE ONE OBSERVATION WALK, for HF refs and OCI tags alike. An observation is
    an object named `<observed_at>-<value>`, newest last when sorted. Each
    candidate, newest first, is HEADed and judged by its signature, which
    `sig_ok(sig, observed, value)` recomputes from the configured key and from
    the object's NAME -- the observation time and the value are taken from the
    name, which is what orders observations. The observed-at metadata is a
    copy for humans; an object copied verbatim under a newer name carries the
    old one, and must fail here rather than be believed.

    A refused candidate (bad signature, unsigned without the opt-in, malformed)
    counts as absent and the next older one is tried, up to MAX_REF_CANDIDATES.

    `usable(value)`, when given, is asked about a candidate that verified. It
    returns something (passed back as the fourth element) to accept it, None to
    pass over it for an older one, or raises Refused to stop the walk -- a
    content failure is not a reason to quietly serve something older.

    Returns (observed, value, "signed" | "unsigned", whatever usable returned).
    """
    names: list[tuple[str, str, str]] = []  # (observed, value, key)
    async for obj in tier._s.client.list_prefix(prefix):
        name = obj.key[len(prefix):]
        obs, _, value = name.partition("-")
        if len(obs) == 15 and obs.isdigit() and value_ok(value):
            names.append((obs, value, obj.key))
    if not names:
        raise Refused("missing", f"no observation of {what} in the tier index")
    names.sort(reverse=True)
    worst = None
    for obs, value, key in names[:MAX_REF_CANDIDATES]:
        r = await tier._s.client.head(key)
        if r.status_code != 200:
            continue
        judged = "malformed"
        if _meta(r, "version") == tier.INDEX_VERSION:
            judged = _judge(_meta(r, "auth"), sig_ok(_meta(r, "sig"), obs, value))
        metrics.record_tier_index_read(judged, proto)
        if judged in ("signed_ok", "unsigned_accepted"):
            auth = "signed" if judged == "signed_ok" else "unsigned"
            if usable is None:
                return obs, value, auth, None
            got = await usable(value)
            if got is not None:
                return obs, value, auth, got
            worst = _worse(worst, "unusable")
            continue
        log.warning("tier index: observation %s refused (%s)%s", key, judged,
                    "; trying an older one" if judged != "unsigned_refused" else "")
        worst = _worse(worst, judged)
    raise Refused(_result_for(worst), f"no acceptable observation of {what} ({worst})")


def _worse(a: str | None, b: str) -> str:
    order = ("unusable", "malformed", "unsigned_refused", "bad_signature")
    return b if a is None or order.index(b) > order.index(a) else a


def _result_for(judged: str | None) -> str:
    if judged in ("bad_signature", "malformed"):
        return "bad_signature"
    if judged == "unsigned_refused":
        return "unsigned_refused"
    return "missing"


def _parse_entry(body: bytes, repo_type: str, repo_id: str, commit: str,
                 path: str) -> tuple[str, Entry | None]:
    host, repo = _host_repo(repo_type, repo_id)
    try:
        d = json.loads(body)
        etag, size = d["etag"], d["size"]
        if (not isinstance(size, int) or isinstance(size, bool) or size < 0
                or contenthash.etag_kind(etag) is None
                or d.get("version") != tier.INDEX_VERSION):
            raise ValueError("fields")
    except (ValueError, KeyError, TypeError):
        return "malformed", None
    ok = tier.index_sig_ok(d.get("sig"), "hf-commit", host, repo, commit, path, etag, size)
    judged = _judge(d.get("auth"), ok)
    if judged == "signed_ok":
        return judged, Entry(etag, size, "signed")
    if judged == "unsigned_accepted":
        return judged, Entry(etag, size, "unsigned")
    return judged, None


async def file_entry(repo_type: str, repo_id: str, commit: str, path: str) -> Entry:
    cached = _listings.get((repo_type, repo_id, commit))
    if cached is not None and path in cached:
        return cached[path]
    key = tier.hf_commit_index_key(repo_type, repo_id, commit, path)
    r = await tier._s.client.get_bytes(key)
    if r.status_code == 404:
        raise Refused("missing", f"{repo_type}s/{repo_id}@{commit[:12]}/{path} is not in "
                                 "the tier index")
    if r.status_code != 200:
        raise Refused("error", f"GET {key}: {r.status_code}")
    judged, entry = _parse_entry(r.content, repo_type, repo_id, commit, path)
    metrics.record_tier_index_read(judged)
    if entry is None:
        log.warning("tier index: entry %s refused (%s)", key, judged)
        raise Refused(_result_for(judged), f"index entry {key} refused ({judged})")
    return entry


def _safe_path(path: str) -> bool:
    """A repo-relative path that stays inside snapshots/<commit>/ when linked.

    A signed entry's path is covered by its signature, but an unsigned one is
    whatever a bucket writer chose, and it becomes a symlink on this disk.
    """
    if not path or path.startswith("/") or "\\" in path or "\0" in path:
        return False
    return all(seg not in ("", ".", "..") for seg in path.split("/"))


async def commit_listing(repo_type: str, repo_id: str, commit: str) -> dict[str, Entry]:
    """Every indexed file of a commit, each verified. All or nothing.

    One entry that fails verification refuses the whole listing: a listing
    missing a file would let a snapshot "succeed" without it, which is the
    silent-incompleteness failure this project keeps refusing. What the index
    never held is a different matter and cannot be detected here: the listing
    is what was indexed, not necessarily the whole repo.
    """
    ck = (repo_type, repo_id, commit)
    if ck in _listings:
        return _listings[ck]
    prefix = _commit_prefix(repo_type, repo_id, commit)
    paths: list[str] = []
    async for obj in tier._s.client.list_prefix(prefix):
        name = obj.key[len(prefix):]
        if not name.endswith(".json") or "/" in name:
            continue
        path = unquote(name[: -len(".json")])
        if quote(path, safe="") + ".json" != name or not _safe_path(path):
            metrics.record_tier_index_read("malformed")
            raise Refused("bad_signature", f"index key {obj.key} does not name a safe path")
        paths.append(path)
    if not paths:
        raise Refused("missing", f"{repo_type}s/{repo_id}@{commit[:12]} is not in the "
                                 "tier index")
    sem = asyncio.Semaphore(LISTING_CONCURRENCY)

    async def one(p: str) -> tuple[str, Entry]:
        async with sem:
            return p, await file_entry(repo_type, repo_id, commit, p)

    out = dict(await asyncio.gather(*(one(p) for p in paths)))
    if len(_listings) >= _LISTING_CACHE_MAX:
        _listings.pop(next(iter(_listings)))
    _listings[ck] = out
    return out


# ---------------------------------------------------------------------------
# putting it on disk
# ---------------------------------------------------------------------------


def _link(repo_type: str, repo_id: str, commit: str, path: str, etag: str) -> Path:
    """snapshots/<commit>/<path> -> ../../blobs/<etag>, as hf_hub_download makes
    it, so resolve_local and everything after it work unchanged. Atomic, and
    idempotent: an existing link is replaced by an identical one."""
    root = Path(settings.cache_dir) / cachefs.repo_folder_name(repo_id, repo_type)
    link = root / "snapshots" / commit / path
    blob = root / "blobs" / etag
    link.parent.mkdir(parents=True, exist_ok=True)
    rel = Path(*([".."] * (len(Path(path).parts) + 1))) / "blobs" / etag
    tmp = link.with_name(f".{link.name}.restore-{time.monotonic_ns()}")
    tmp.symlink_to(rel)
    tmp.replace(link)
    if link.resolve() != blob.resolve():  # a bug in the arithmetic above, never data
        link.unlink(missing_ok=True)
        raise OSError(f"restored link {link} does not resolve to {blob}")
    return link


async def materialize(repo_type: str, repo_id: str, commit: str, path: str,
                      entry: Entry) -> Path:
    """The file on local disk, verified, linked where the HF layout puts it.

    A blob already on local disk is trusted as it is everywhere else in the
    cache (local disk is written only by this process). Otherwise the tier's
    object is fetched and verified against the entry's ETag by the same routine
    as every other tier read. Raises Refused when the content is not there or
    does not verify; nothing is linked in either case.
    """
    if not _safe_path(path):
        raise Refused("bad_signature", f"refusing to link unsafe path {path!r}")
    blob = (Path(settings.cache_dir) / cachefs.repo_folder_name(repo_id, repo_type)
            / "blobs" / entry.etag)
    if not blob.exists():
        await tier._fill_blob(repo_type, repo_id, entry.etag, entry.size, restore=True)
    if not blob.exists():
        key = tier.hf_blob_key(repo_type, repo_id, entry.etag)
        if tier.is_bad(key):
            raise Refused("content_mismatch", f"tier content {key} failed verification")
        raise Refused("content_missing", f"tier content {key} is absent or unreadable")
    return await asyncio.to_thread(_link, repo_type, repo_id, commit, path, entry.etag)


async def content_present(repo_type: str, repo_id: str, entry: Entry) -> bool:
    """For a HEAD: is the content there to be fetched? Local disk, or the tier."""
    blob = (Path(settings.cache_dir) / cachefs.repo_folder_name(repo_id, repo_type)
            / "blobs" / entry.etag)
    if blob.exists():
        return True
    key = tier.hf_blob_key(repo_type, repo_id, entry.etag)
    if tier.is_bad(key):
        return False
    r = await tier._s.client.head(key)
    return r.status_code == 200


# ---------------------------------------------------------------------------
# the public surface: one restore attempt, counted once
# ---------------------------------------------------------------------------


def observed_iso(t: float) -> str:
    return (_dt.datetime.fromtimestamp(t, tz=_dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
            + "Z")


def restore_headers(trigger: str, auth: str, observed_at: float | None) -> dict[str, str]:
    """What every restored answer says about itself, for either protocol."""
    h = {
        "x-xhc-cache": "TIER-RESTORE",
        "x-xhc-restore-reason": trigger,
        "x-xhc-index-auth": auth,
    }
    if observed_at is not None:
        # Resolved from an observation (a ref, a tag), which may be stale: the
        # upstream may have moved it since. A request by commit or digest has
        # no observation and carries neither header.
        h["x-xhc-ref-observed-at"] = observed_iso(observed_at)
        h["x-xhc-ref-age"] = str(_age(observed_at))
    return h


def _age(observed_at: float) -> int:
    return max(0, int(time.time() - observed_at))


def headers(ref: Ref, trigger: str, auth: str) -> dict[str, str]:
    """What every restored HF answer says about itself."""
    h = restore_headers(trigger, auth, ref.observed_at)
    h["x-repo-commit"] = ref.commit
    return h


def _auth_of(ref: Ref, entries) -> str:
    auths = {e.auth for e in entries} | ({ref.auth} - {"pinned"})
    return "unsigned" if "unsigned" in auths else "signed"


def _finish(what: str, trigger: str, result: str, detail: str = "",
            proto: str = "hf") -> None:
    metrics.record_tier_restore(result, proto)
    _last.update(at=time.time(), proto=proto, what=what, trigger=trigger, result=result,
                 detail=detail)
    said = f": {detail}" if detail else ""
    if result == "ok_unsigned":
        log.warning("UNSIGNED TIER RESTORE of %s (upstream %s)%s. The index entries used "
                    "carry no signature, so this answer is only as trustworthy as every "
                    "credential that can write the bucket (XHC_TIER2_RESTORE_UNSIGNED=true)",
                    what, trigger, said)
    elif result == "ok":
        log.info("tier restore of %s (upstream %s)%s", what, trigger, said)
    elif result in ("missing", "content_missing"):
        log.info("tier restore of %s not possible (upstream %s): %s", what, trigger, detail)
    else:
        log.warning("tier restore of %s REFUSED (upstream %s): %s: %s", what, trigger,
                    result, detail)


def _gate(what: str, trigger: str, proto: str = "hf") -> bool:
    if not tier.enabled() or not tier.cfg().read or not tier.cfg().restore:
        return False
    if not tier.readable():
        _finish(what, trigger, "error", "the tier is unhealthy, so the index cannot be read",
                proto)
        return False
    if not tier.restore_enabled():
        _finish(what, trigger, "no_key",
                "signed entries are required and XHC_TIER2_INDEX_KEY is unset", proto)
        return False
    return True


@dataclass(frozen=True)
class RestoredFile:
    ref: Ref
    entry: Entry
    path: Path | None  # the linked snapshot file; None for a HEAD or a refusal
    headers: dict[str, str]
    # What `admit` said when it refused this file's size, else None.
    refusal: object = None


async def restore_file(repo_type: str, repo_id: str, revision: str, filename: str,
                       trigger: str, *, fetch: bool, admit=None) -> RestoredFile | None:
    """One file, for a resolve request the Hub could not answer. None when the
    index cannot (or may not) supply it; the caller then answers as before.

    `fetch=False` is a HEAD: nothing is downloaded, but the content's presence
    is checked so the HEAD does not promise a GET that would fail.

    `admit(size)` is the ingest policy's size check, applied before any byte
    moves: a restore puts a file on local disk exactly as an ingest does. It
    returns a refusal, or None to admit.
    """
    what = f"{repo_type}s/{repo_id}@{revision}/{filename}"
    if not _gate(what, trigger):
        return None
    try:
        ref = await resolve_revision(repo_type, repo_id, revision)
        entry = await file_entry(repo_type, repo_id, ref.commit, filename)
        refusal = admit(entry.size) if admit is not None else None
        if refusal is not None:
            _finish(what, trigger, "policy_refused", f"size {entry.size} refused by policy")
            return RestoredFile(ref, entry, None, {}, refusal)
        if fetch:
            path = await materialize(repo_type, repo_id, ref.commit, filename, entry)
        else:
            path = None
            if not await content_present(repo_type, repo_id, entry):
                raise Refused("content_missing", "the content object is not in the tier")
    except Refused as exc:
        _finish(what, trigger, exc.result, str(exc))
        return None
    except (httpx.HTTPError, s3client.TierHTTPError, s3client.TierAuthError, OSError) as exc:
        _finish(what, trigger, "error", f"{type(exc).__name__}: {exc}")
        return None
    auth = _auth_of(ref, [entry])
    _finish(what, trigger, "ok" if auth == "signed" else "ok_unsigned")
    return RestoredFile(ref, entry, path, headers(ref, trigger, auth))


async def restore_listing(repo_type: str, repo_id: str, revision: str,
                          trigger: str) -> tuple[Ref, dict[str, Entry], dict[str, str]] | None:
    """A revision's listing, for repo info and tree requests. Nothing is fetched."""
    what = f"{repo_type}s/{repo_id}@{revision} (listing)"
    if not _gate(what, trigger):
        return None
    try:
        ref = await resolve_revision(repo_type, repo_id, revision)
        listing = await commit_listing(repo_type, repo_id, ref.commit)
    except Refused as exc:
        _finish(what, trigger, exc.result, str(exc))
        return None
    except (httpx.HTTPError, s3client.TierHTTPError, s3client.TierAuthError) as exc:
        _finish(what, trigger, "error", f"{type(exc).__name__}: {exc}")
        return None
    auth = _auth_of(ref, listing.values())
    _finish(what, trigger, "ok" if auth == "signed" else "ok_unsigned")
    return ref, listing, headers(ref, trigger, auth)


def repo_info_body(repo_type: str, repo_id: str, ref: Ref, listing: dict[str, Entry],
                   trigger: str) -> dict:
    """A repo-info response from the index. Shaped like synthesize_repo_info's,
    with files_metadata fields, so a client asking for sizes and hashes gets
    them. pointerSize is not recorded anywhere, so it is null, not invented."""
    siblings = []
    for path in sorted(listing):
        e = listing[path]
        s: dict = {"rfilename": path, "size": e.size}
        if contenthash.etag_kind(e.etag) == "sha256":
            s["blobId"] = None
            s["lfs"] = {"sha256": e.etag, "size": e.size, "pointerSize": None}
        else:
            s["blobId"] = e.etag
        siblings.append(s)
    reason = f"upstream {trigger}; listing restored from the object-store tier's index"
    if ref.observed_at is not None:
        reason += f", ref observed at {observed_iso(ref.observed_at)} and possibly stale"
    reason += ". It lists the files that were indexed, which may not be the whole repo."
    body = {
        "_id": ref.commit, "id": repo_id, "sha": ref.commit, "siblings": siblings,
        "private": False, "gated": False, "disabled": False, "tags": [],
        "downloads": 0, "likes": 0, "lastModified": None, "createdAt": None,
        "xhcSynthesized": True, "xhcSynthesizedReason": reason,
    }
    if repo_type == "model":
        body["modelId"] = repo_id
    if repo_type == "dataset":
        body["author"] = repo_id.split("/")[0] if "/" in repo_id else None
    return body


def tree_entries(listing: dict[str, Entry], path_in_repo: str, recursive: bool,
                 expand: bool) -> list[dict] | None:
    """A tree listing from the index, the same shape as hfcompat.synthesize_tree."""
    prefix = f"{path_in_repo}/" if path_in_repo else ""
    scoped = [p for p in listing if p.startswith(prefix)]
    if not scoped:
        return None
    out: list[dict] = []
    seen: set[str] = set()
    for rel in sorted(scoped):
        rest = rel[len(prefix):]
        if not recursive and "/" in rest:
            d = prefix + rest.split("/", 1)[0]
            if d not in seen:
                seen.add(d)
                out.append({"type": "directory", "oid": None, "size": 0, "path": d})
            continue
        e = listing[rel]
        item: dict = {"type": "file", "oid": e.etag, "size": e.size, "path": rel}
        if contenthash.etag_kind(e.etag) == "sha256":
            item["lfs"] = {"oid": e.etag, "size": e.size, "pointerSize": None}
        if expand:
            item["lastCommit"] = None
            item["securityFileStatus"] = None
        out.append(item)
    return sorted(out, key=lambda e: e["path"])


# ---------------------------------------------------------------------------
# prewarm
# ---------------------------------------------------------------------------


async def prewarm(job, trigger: str) -> Path:
    """A whole prewarm from the tier, when the Hub could not list the revision.

    Resolves the revision and reads the commit's listing from the index,
    filters it by the prewarm's allow_patterns as snapshot_download would, and
    materializes every file, verified. All or nothing: a file whose content is
    absent or fails verification fails the job, naming it, rather than
    returning a snapshot with a hole in it.

    Sets job.index_restore (so write-back records no new ref observation) and
    job.tier_etags (so verification does not hash a second time what was
    hashed as it arrived). Raises Refused when the index cannot supply it.
    """
    from huggingface_hub.utils import filter_repo_objects

    from . import manifests

    what = f"{job.repo_type}s/{job.repo_id}@{job.revision} (prewarm)"
    if not _gate(what, trigger):
        raise Refused("no_key" if tier.enabled() and tier.readable()
                      and not tier.restore_enabled() else "error",
                      "tier restore is not available: " + tier.restore_mode())
    try:
        ref = await resolve_revision(job.repo_type, job.repo_id, job.revision)
        listing = await commit_listing(job.repo_type, job.repo_id, ref.commit)
        manifests.record(job.repo_type, job.repo_id, ref.commit,
                         {p: e.size for p, e in listing.items()}, job.allow_patterns)
        kept = sorted(filter_repo_objects(listing, allow_patterns=job.allow_patterns))
        sem = asyncio.Semaphore(max(1, settings.snapshot_max_workers))
        failures: list[str] = []
        before = {p for p in kept
                  if (Path(settings.cache_dir)
                      / cachefs.repo_folder_name(job.repo_id, job.repo_type)
                      / "blobs" / listing[p].etag).exists()}

        async def one(p: str) -> None:
            async with sem:
                try:
                    await materialize(job.repo_type, job.repo_id, ref.commit, p, listing[p])
                except Refused as exc:
                    failures.append(f"{p}: {exc.result}")

        await asyncio.gather(*(one(p) for p in kept))
        if failures:
            result = ("content_mismatch" if any("content_mismatch" in f for f in failures)
                      else "content_missing")
            raise Refused(result, f"{len(failures)} of {len(kept)} file(s) could not be "
                                  "restored: " + "; ".join(sorted(failures)[:5]))
    except Refused as exc:
        _finish(what, trigger, exc.result, str(exc))
        raise
    except (httpx.HTTPError, s3client.TierHTTPError, s3client.TierAuthError) as exc:
        _finish(what, trigger, "error", f"{type(exc).__name__}: {exc}")
        raise Refused("error", f"{type(exc).__name__}: {exc}") from exc
    auth = _auth_of(ref, [listing[p] for p in kept])
    _finish(what, trigger, "ok" if auth == "signed" else "ok_unsigned")
    job.tier_etags = {listing[p].etag for p in kept if p not in before}
    job.index_restore = {
        "commit": ref.commit, "trigger": trigger, "auth": auth, "files": len(kept),
        "ref_observed_at": observed_iso(ref.observed_at) if ref.observed_at else None,
    }
    return (Path(settings.cache_dir) / cachefs.repo_folder_name(job.repo_id, job.repo_type)
            / "snapshots" / ref.commit)


# ---------------------------------------------------------------------------
# OCI: a tag from the index, when the registry cannot answer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RestoredManifest:
    body: bytes
    media_type: str
    digest: str
    observed_at: float
    auth: str  # "signed" | "unsigned"
    headers: dict[str, str]


def _oci_tag_prefix(upstream: str, repo: str, tag: str) -> str:
    return tier.oci_tag_index_key(upstream, repo, tag, 0, "sha256:" + "0" * 64).rsplit(
        "/", 1)[0] + "/"


def _manifest_media(body: bytes, stored: str) -> str:
    """The media type to serve a manifest as.

    The manifest's own `mediaType` field when it has one: it is inside the
    bytes the digest covers. The type stored beside it (the tier object's
    Content-Type, the observation's metadata) is covered by nothing, so it is
    only the fallback for a manifest that does not name itself.
    """
    try:
        doc = json.loads(body)
    except ValueError:
        return stored
    media = doc.get("mediaType") if isinstance(doc, dict) else None
    return media if isinstance(media, str) and media else stored


def _referenced_blobs(body: bytes) -> list[str]:
    """An image manifest's config and layers. An index references manifests,
    not blobs, and returns nothing here."""
    try:
        doc = json.loads(body)
    except ValueError:
        return []
    if not isinstance(doc, dict):
        return []
    out = []
    cfg = doc.get("config")
    if isinstance(cfg, dict) and isinstance(cfg.get("digest"), str):
        out.append(cfg["digest"])
    for layer in doc.get("layers") or []:
        if isinstance(layer, dict) and isinstance(layer.get("digest"), str):
            out.append(layer["digest"])
    return out


async def _oci_manifest(upstream: str, digest: str) -> tuple[bytes, str]:
    """A manifest by digest: local disk, else the tier, verified as it is read.

    Local disk is trusted as it is everywhere else in the cache (only this
    process writes it, and store_manifest verified it on the way in). The
    tier's copy is hashed against the digest the verified observation named,
    in tier.read_oci_manifest -- the expected value comes from the signed
    observation, never from the object.
    """
    held = ocistore.load_manifest(upstream, digest)
    if held is not None:
        return held.body, held.media_type
    got = await tier.read_oci_manifest(upstream, digest)
    if got is None:
        key = tier.oci_manifest_key(upstream, digest)
        if tier.is_bad(key):
            raise Refused("content_mismatch", f"tier manifest {key} failed verification")
        raise Refused("content_missing", f"tier manifest {key} is absent or unreadable")
    return got


async def _oci_blob_present(upstream: str, digest: str) -> bool:
    if not ocistore.DIGEST_RE.match(digest):
        return False
    if ocistore.blob_path(upstream, digest).is_file():
        return True
    key = tier.oci_blob_key(upstream, digest)
    if tier.is_bad(key):
        return False
    r = await tier._s.client.head(key)
    return r.status_code == 200


async def restore_oci_tag(upstream: str, repo: str, tag: str, accept: str | None,
                          trigger: str) -> RestoredManifest | None:
    """A tag's manifest from the index, for a pull the registry could not answer.

    None when the index cannot (or may not) supply it; the caller then answers
    as before. The same for GET and HEAD: a manifest is small, and a HEAD that
    said yes to a manifest the GET could not produce would be a lie.

    The newest observation that verifies AND whose manifest this client accepts
    is used: a tag is observed once per media type a client asked for (an index
    for docker, a single manifest for an old client), and an older observation
    of another type is not stale, it is another answer. A manifest that is
    absent or fails its digest stops the walk rather than falling back.

    An IMAGE manifest is served only if every blob it names is on local disk
    or in the tier, so a restore never promises a pull that will fail on its
    first layer (an OCI blob below XHC_TIER2_MIN_SIZE is never written back).
    An INDEX is served whole: its platform manifests follow by digest, and one
    never pulled is not in the bucket and fails then, by digest.
    """
    what = f"{upstream}/{repo}:{tag}"
    if not _gate(what, trigger, "oci"):
        return None

    def sig_ok(sig: object, obs: str, digest: str) -> bool:
        return tier.index_sig_ok(sig, "oci-tag", upstream, repo, tag, "", digest, 0, obs)

    async def usable(digest: str):
        body, stored = await _oci_manifest(upstream, digest)
        media = _manifest_media(body, stored)
        if not ocistore.accepts(accept, media):
            log.info("tier index: %s observed at %s is %s, which this client does not "
                     "accept; trying an older observation", what, digest, media)
            return None
        missing = [d for d in _referenced_blobs(body)
                   if not await _oci_blob_present(upstream, d)]
        if missing:
            raise Refused("content_missing", f"{len(missing)} blob(s) of {digest} are not in "
                                             f"the tier, e.g. {missing[0]}")
        return body, media

    try:
        obs, digest, auth, got = await _newest_observation(
            _oci_tag_prefix(upstream, repo, tag), ocistore.DIGEST_RE.match, sig_ok, what,
            "oci", usable)
        body, media = got
        # Kept on local disk so the platform manifests' and the next restore's
        # reads are local. No tag file is written: the next pull by tag asks the
        # registry again, and is restored again (and labelled) if it still
        # cannot answer. A restore never records an observation.
        ocistore.store_manifest(upstream, digest, body, media)
    except Refused as exc:
        _finish(what, trigger, exc.result, str(exc), "oci")
        return None
    except (httpx.HTTPError, s3client.TierHTTPError, s3client.TierAuthError, OSError,
            ocistore.DigestMismatch) as exc:
        _finish(what, trigger, "error", f"{type(exc).__name__}: {exc}", "oci")
        return None
    observed_at = int(obs) / 1000
    # The docker CLI prints none of the headers below, so the log line is the
    # operator's view of a restore: repo, tag, digest and how old the answer is.
    _finish(what, trigger, "ok" if auth == "signed" else "ok_unsigned",
            f"digest {digest}, tag observed at {observed_iso(observed_at)} "
            f"(age {_age(observed_at)}s)", "oci")
    return RestoredManifest(body, media, digest, observed_at, auth,
                            restore_headers(trigger, auth, observed_at))
