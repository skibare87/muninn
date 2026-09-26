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

from . import cachefs, contenthash, metrics, s3client, tier
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


def trigger_for_status(status: int | None, error_code: str | None = None) -> str | None:
    """Which Hub answers are a reason to restore. None means: pass it through.

    See the module docstring for the reasoning; 401 and 403 are never one.
    """
    if status is None:
        return TRIGGER_UNREACHABLE
    if status >= 500:
        return TRIGGER_5XX
    if status == 404 and error_code != "EntryNotFound":
        return TRIGGER_NOT_FOUND
    return None


def trigger_for_exception(exc: BaseException) -> str | None:
    """The same decision for an exception from huggingface_hub or httpx.

    A response is judged by its status and X-Error-Code, as the Hub sent them.
    No response is an outage ONLY when the exception is a transport failure
    (connection refused, DNS, TLS, timeout). Anything else with no response --
    a local OSError, a parsing bug -- says nothing about the Hub, and reading
    it as "unreachable" would start serving stale refs over a local fault.
    """
    import requests

    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if response is None or status is None:
        transport = (requests.exceptions.ConnectionError, requests.exceptions.Timeout,
                     httpx.TransportError)
        return TRIGGER_UNREACHABLE if isinstance(exc, transport) else None
    try:
        code = response.headers.get("X-Error-Code")
    except AttributeError:
        code = None
    return trigger_for_status(status, code)


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
    prefix = _refs_prefix(repo_type, repo_id, revision)
    names: list[tuple[str, str, str]] = []  # (observed, commit, key)
    async for obj in tier._s.client.list_prefix(prefix):
        name = obj.key[len(prefix):]
        obs, _, commit = name.partition("-")
        if len(obs) == 15 and obs.isdigit() and contenthash.GIT_SHA1_RE.match(commit):
            names.append((obs, commit, obj.key))
    if not names:
        raise Refused("missing", f"no observation of {repo_type}s/{repo_id}@{revision} "
                                 "in the tier index")
    names.sort(reverse=True)
    worst = None
    for obs, commit, key in names[:MAX_REF_CANDIDATES]:
        r = await tier._s.client.head(key)
        if r.status_code != 200:
            continue
        judged = "malformed"
        if _meta(r, "version") == tier.INDEX_VERSION:
            # `obs` and `commit` come from the object's NAME, which is what
            # orders observations. The observed-at metadata is a copy for
            # humans; an object copied verbatim under a newer name carries the
            # old one, and must fail here rather than be believed.
            ok = tier.index_sig_ok(_meta(r, "sig"), "hf-ref", host, repo, revision, "",
                                   commit, 0, obs)
            judged = _judge(_meta(r, "auth"), ok)
        metrics.record_tier_index_read(judged)
        if judged == "signed_ok":
            return Ref(commit, int(obs) / 1000, "signed")
        if judged == "unsigned_accepted":
            return Ref(commit, int(obs) / 1000, "unsigned")
        log.warning("tier index: ref observation %s refused (%s)%s", key, judged,
                    "; trying an older one" if judged != "unsigned_refused" else "")
        worst = _worse(worst, judged)
    raise Refused(_result_for(worst), f"no acceptable observation of "
                                      f"{repo_type}s/{repo_id}@{revision} ({worst})")


def _worse(a: str | None, b: str) -> str:
    order = ("malformed", "unsigned_refused", "bad_signature")
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


def headers(ref: Ref, trigger: str, auth: str) -> dict[str, str]:
    """What every restored answer says about itself."""
    h = {
        "x-xhc-cache": "TIER-RESTORE",
        "x-xhc-restore-reason": trigger,
        "x-xhc-index-auth": auth,
        "x-repo-commit": ref.commit,
    }
    if ref.observed_at is not None:
        # The ref was resolved from an observation, which may be stale: the Hub
        # may have moved it since. A request by commit carries neither header.
        h["x-xhc-ref-observed-at"] = observed_iso(ref.observed_at)
        h["x-xhc-ref-age"] = str(max(0, int(time.time() - ref.observed_at)))
    return h


def _auth_of(ref: Ref, entries) -> str:
    auths = {e.auth for e in entries} | ({ref.auth} - {"pinned"})
    return "unsigned" if "unsigned" in auths else "signed"


def _finish(what: str, trigger: str, result: str, detail: str = "") -> None:
    metrics.record_tier_restore(result)
    _last.update(at=time.time(), what=what, trigger=trigger, result=result, detail=detail)
    if result == "ok_unsigned":
        log.warning("UNSIGNED TIER RESTORE of %s (upstream %s): the index entries used "
                    "carry no signature, so this answer is only as trustworthy as every "
                    "credential that can write the bucket (XHC_TIER2_RESTORE_UNSIGNED=true)",
                    what, trigger)
    elif result == "ok":
        log.info("tier restore of %s (upstream %s)", what, trigger)
    elif result in ("missing", "content_missing"):
        log.info("tier restore of %s not possible (upstream %s): %s", what, trigger, detail)
    else:
        log.warning("tier restore of %s REFUSED (upstream %s): %s: %s", what, trigger,
                    result, detail)


def _gate(what: str, trigger: str) -> bool:
    if not tier.enabled() or not tier.cfg().read or not tier.cfg().restore:
        return False
    if not tier.readable():
        _finish(what, trigger, "error", "the tier is unhealthy, so the index cannot be read")
        return False
    if not tier.restore_enabled():
        _finish(what, trigger, "no_key",
                "signed entries are required and XHC_TIER2_INDEX_KEY is unset")
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
