"""The object-store second tier (XHC_TIER2), phase 1.

An optional S3-compatible bucket between the local disk and the upstream. On a
local miss, content is read from the bucket before the upstream; after a
verified ingest it is copied to the bucket in the background. Off unless
XHC_TIER2 is set, and every entry point here returns early when it is not.

THE SPLIT THIS IS BUILT ON.

- CONTENT is named by its own hash: an HF blob by its sha256 ETag, an OCI blob
  or manifest by its digest. The value it is checked against comes from the
  REQUEST -- the Hub's HEAD, or the digest in the URL -- and never from the
  bucket. A bucket can withhold content; it cannot forge it.
- MAPPINGS (revision -> commit -> file -> etag, tag -> digest) are only as
  trustworthy as whoever can write the bucket. Phase 1 WRITES an index of them
  -- HMAC-signed when XHC_TIER2_INDEX_KEY is set, marked unsigned when it is
  not -- and never READS it. Restoring from it, and whether an unsigned entry
  may be restored at all, is phase 2.

ONE HASH PASS, IN BOTH DIRECTIONS. A tier read hashes the bytes as they land in
a temporary file and renames it into place only on a match -- it never fetches
and then re-reads to hash. A write-back reads each byte of the local file once,
hashes it in the same pass, and sends it. On a deployment where hashing one
49.9 GB file took 432 s, a second pass is minutes per file.

MUNINN NEVER DELETES FROM THE TIER. There is no delete call in this module. The
only DELETE the client can issue is AbortMultipartUpload, which discards parts
of an upload this process started and never completed; it removes no object.
Retention is the operator's cost decision.

THE TIER FAILS OPEN TO THE UPSTREAM. It is an accelerator and an archive, never
the authority: an outage, a 401, a 5xx or a mismatch all fall through to the
Hub or the registry.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlparse

import filelock
import httpx

from . import cachefs, metrics, ocistore, s3client
from .config import settings

log = logging.getLogger("xhc.tier")

# Phase 1 reads back sha256-named content only. Files keyed by a git blob id
# (40 hex) are the next phase; when they are added, verify them with the rule
# jobs.verify_ingested already applies -- sha1(b"blob <size>\0" + content), in
# the same single pass -- rather than with a second copy of it.
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE = re.compile(r"[^A-Za-z0-9._-]")
CHUNK = 4 * 1024 * 1024
# Transport failures are retried; an HTTP status is an answer and is not.
RETRYABLE = (httpx.TransportError,)
BACKOFF_S: tuple[float, ...] = (1, 4, 15, 45)
# How long an unhealthy tier waits before probing again.
REPROBE_S = 60.0
MANIFEST_MAX = 32 * 1024 * 1024
INDEX_VERSION = "muninn-tier-index-v1"
# How an index object says whether it is signed, in the object itself: a field
# of a commit object's JSON body, and user metadata on a ref or tag object.
AUTH_SIGNED = "hmac-sha256-v1"
AUTH_UNSIGNED = "unsigned"

# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------


@dataclass
class _State:
    client: s3client.S3Client | None = None
    http: httpx.AsyncClient | None = None
    healthy: bool = False
    probe: dict = field(default_factory=dict)
    last_error: str | None = None
    # Keys whose bytes failed verification this process. Never read again, so
    # the retry goes to the upstream; never deleted, because nothing here
    # deletes from the tier. Reported on /_cache/status instead.
    bad: set[str] = field(default_factory=set)
    queue: asyncio.Queue | None = None
    tasks: list[asyncio.Task] = field(default_factory=list)
    reconcile: dict = field(default_factory=dict)
    refs_written: set[tuple] = field(default_factory=set)
    healthy_event: asyncio.Event | None = None


_s = _State()


def cfg():
    return settings.tier


def enabled() -> bool:
    return settings.tier is not None


def readable() -> bool:
    return enabled() and cfg().read and _s.healthy


def writable() -> bool:
    return enabled() and cfg().write


def instance_id() -> str:
    return _SAFE.sub("_", socket.gethostname()) or "muninn"


def reset_for_tests() -> None:
    global _s  # noqa: PLW0603 - test helper
    _s = _State()


def use_client(client: s3client.S3Client, healthy: bool = True) -> None:
    """Install a client directly. Tests use this; start() builds its own."""
    _s.client = client
    _s.healthy = healthy
    _ensure_queue()


def _ensure_queue() -> asyncio.Queue:
    if _s.queue is None:
        _s.queue = asyncio.Queue(maxsize=cfg().queue_max if enabled() else 0)
    return _s.queue


def build_client(http: httpx.AsyncClient | None = None) -> s3client.S3Client:
    t = cfg()
    http = http or httpx.AsyncClient(timeout=httpx.Timeout(60.0), follow_redirects=False)
    _s.http = http
    if t.credentials == "gcp-metadata":
        creds = s3client.GcpMetadataToken(http)
    else:
        creds = s3client.StaticKeys(t.access_key_id, t.secret_access_key)
    return s3client.S3Client(
        endpoint=t.endpoint, bucket=t.bucket, region=t.region, path_style=t.path_style,
        creds=creds, client=http, gcs=t.scheme == "gs",
    )


def _mark_unhealthy(reason: str) -> None:
    if _s.healthy:
        log.error("TIER DISABLED until the next probe: %s. Serving falls through "
                  "to the upstream.", reason)
    _s.healthy = False
    _s.last_error = reason
    if _s.healthy_event is not None:
        _s.healthy_event.clear()


def _mark_healthy() -> None:
    _s.healthy = True
    if _s.healthy_event is not None:
        _s.healthy_event.set()


# ---------------------------------------------------------------------------
# keys
# ---------------------------------------------------------------------------


def _base() -> str:
    p = cfg().prefix
    return f"{p}/v1" if p else "v1"


def hf_host() -> str:
    return _SAFE.sub("_", urlparse(settings.upstream).netloc or "hub")


def _hf_repo(repo_type: str, repo_id: str) -> str:
    return f"{hf_host()}/{repo_type}s/{repo_id}"


def hf_content_prefix(repo_type: str, repo_id: str) -> str:
    return f"{_base()}/content/hf/{_hf_repo(repo_type, repo_id)}/sha256/"


def hf_content_key(repo_type: str, repo_id: str, etag: str) -> str:
    return hf_content_prefix(repo_type, repo_id) + etag


def _oci_sharded(upstream: str, kind: str, digest: str) -> str:
    hexpart = digest.split(":", 1)[1]
    return f"{_base()}/content/oci/{_SAFE.sub('_', upstream)}/{kind}/sha256/{hexpart[:2]}/{hexpart}"


def oci_blob_key(upstream: str, digest: str) -> str:
    return _oci_sharded(upstream, "blobs", digest)


def oci_manifest_key(upstream: str, digest: str) -> str:
    return _oci_sharded(upstream, "manifests", digest)


def hf_commit_index_key(repo_type: str, repo_id: str, commit: str, path: str) -> str:
    return (f"{_base()}/index/hf/{_hf_repo(repo_type, repo_id)}/commits/{commit}/"
            f"{quote(path, safe='')}.json")


def _observed(t: float) -> str:
    # Fixed width, so "the greatest observed_at" is also the lexically greatest
    # key and a reader can take the last entry of a sorted LIST.
    return f"{int(t * 1000):015d}"


def hf_ref_index_key(repo_type: str, repo_id: str, ref: str, observed_at: float,
                     commit: str) -> str:
    return (f"{_base()}/index/hf/{_hf_repo(repo_type, repo_id)}/refs/{quote(ref, safe='')}/"
            f"{_observed(observed_at)}-{commit}")


def oci_tag_index_key(upstream: str, repo: str, tag: str, observed_at: float,
                      digest: str) -> str:
    return (f"{_base()}/index/oci/{_SAFE.sub('_', upstream)}/tags/{repo}/{tag}/"
            f"{_observed(observed_at)}-{digest}")


def probe_key(suffix: str = "") -> str:
    return f"{_base()}/_probe/{instance_id()}{suffix}"


def index_sig(kind: str, host: str, repo: str, ref_or_commit: str, path: str,
              value: str, size: int, observed_at: str = "") -> str:
    """HMAC over a canonical, unambiguous encoding of one mapping.

    The key comes from configuration and is never stored in the bucket, so the
    expected value comes from an authority a bucket writer does not hold. A hash
    stored beside the object by the same writer would prove nothing.

    observed_at is signed as well, for refs and tags. Without it, anyone who can
    write the bucket could copy an old signed observation under a newer name and
    roll a ref back with a valid signature.
    """
    canon = json.dumps([INDEX_VERSION, kind, host, repo, ref_or_commit, path, value, size,
                        observed_at], separators=(",", ":"))
    return hmac.new(cfg().index_key, canon.encode(), hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------


class TierReadFailed(Exception):
    """A tier read failed after bytes had already been exposed to a follower
    (stream mode only). The job must end in error rather than fall through,
    because a follower has been sent a prefix it cannot un-receive."""


def is_bad(key: str) -> bool:
    return key in _s.bad


def mark_bad(key: str, why: str) -> None:
    metrics.record_tier_verify("mismatch")
    _s.bad.add(key)
    log.error("TIER CONTENT MISMATCH at %s: %s. Bytes discarded, nothing linked; "
              "this key will not be read again by this process and the request "
              "falls through to the upstream. The object is NOT deleted from the "
              "tier -- Muninn never deletes there. Inspect it.", key, why)


async def open_read(key: str, proto: str, kind: str,
                    expected_size: int | None = None) -> httpx.Response | None:
    """GET a key, streamed. Returns a 200 response or None, never raises.

    None covers every way the tier cannot help: not configured for reads,
    unhealthy, a known-bad key, absent, refused, or failing. The caller falls
    through to the upstream in all of them.
    """
    if not readable() or key in _s.bad:
        return None
    try:
        resp = await _s.client.get(key)
    except s3client.TierAuthError as exc:
        metrics.record_tier_request(proto, kind, "error")
        _mark_unhealthy(str(exc))
        return None
    except httpx.HTTPError as exc:
        metrics.record_tier_request(proto, kind, "error")
        _s.last_error = f"GET {key}: {type(exc).__name__}: {exc}"
        log.warning("tier read failed, falling through to upstream: %s", _s.last_error)
        return None
    if resp.status_code == 200:
        if expected_size is not None:
            got = resp.headers.get("content-length")
            if got is not None and int(got) != expected_size:
                await resp.aclose()
                metrics.record_tier_request(proto, kind, "hit")
                mark_bad(key, f"size {got} != expected {expected_size}")
                return None
        metrics.record_tier_request(proto, kind, "hit")
        return resp
    await resp.aclose()
    if resp.status_code == 404:
        metrics.record_tier_request(proto, kind, "miss")
    elif resp.status_code == 403:
        metrics.record_tier_request(proto, kind, "refused")
        _s.last_error = f"GET {key}: 403"
    else:
        metrics.record_tier_request(proto, kind, "error")
        _s.last_error = f"GET {key}: {resp.status_code}"
    return None


async def hash_into(resp: httpx.Response, tmp: Path, flush: bool) -> tuple[str, int]:
    """Stream a response into `tmp`, hashing each chunk as it is written.

    THE ONE HASH PASS: the digest returned here is the only one ever computed
    over these bytes. The caller compares it and renames; nothing re-reads.
    `flush` makes growth visible to a tail-follower (stream mode only).
    """
    h = hashlib.sha256()
    written = 0
    tmp.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(tmp, "wb") as fh:
            async for chunk in resp.aiter_bytes(CHUNK):
                fh.write(chunk)
                h.update(chunk)
                written += len(chunk)
                if flush:
                    fh.flush()
    finally:
        metrics.record_tier_bytes(read=written)
    return h.hexdigest(), written


def hf_lock(repo_type: str, repo_id: str, etag: str) -> filelock.FileLock:
    """huggingface_hub's own per-blob lock, so a tier fill and an hf_hub_download
    of the same blob in another job never write the same .incomplete file.

    thread_local=False: it is acquired and released from worker threads that
    need not be the same one. flock is per open file, so this conflicts with
    huggingface_hub's own FileLock on the same path even inside one process.
    """
    p = (Path(settings.cache_dir) / ".locks" / cachefs.repo_folder_name(repo_id, repo_type)
         / f"{etag}.lock")
    p.parent.mkdir(parents=True, exist_ok=True)
    return filelock.FileLock(str(p), thread_local=False)


async def _fill_blob(repo_type: str, repo_id: str, etag: str, size: int | None, *,
                     stream: bool = False, on_answer=None) -> bool:
    """Put blobs/<etag> in place from the tier, verified, under huggingface_hub's
    own per-blob lock. Returns True only when the tier's bytes landed.

    The one routine for both a file miss and a prewarm, so both keep the same
    guarantees: one hash pass, rename only on a match, a mismatch marked bad
    and never deleted from the tier.

    `on_answer` is called once the tier has answered 200, before any body byte
    is read. `stream` writes where a tail-follower looks (file misses under
    XHC_TIER2_READ_MODE=stream only); a failure after that raises
    TierReadFailed instead of falling through, because a follower has already
    been sent a prefix it cannot un-receive.
    """
    t = cfg()
    if not (readable() and _SHA256_RE.match(etag or "")):
        return False
    if (size or 0) < t.min_size:
        return False
    key = hf_content_key(repo_type, repo_id, etag)
    if key in _s.bad:
        return False
    blobs = Path(settings.cache_dir) / cachefs.repo_folder_name(repo_id, repo_type) / "blobs"
    blob = blobs / etag
    if blob.exists():
        return False
    lock = hf_lock(repo_type, repo_id, etag)
    await asyncio.to_thread(lock.acquire)
    try:
        if blob.exists():
            return False
        resp = await open_read(key, "hf", "blob", size)
        if resp is None:
            return False
        if on_answer is not None:
            on_answer()
        # verify-first writes where tail_follow does not look, so no client
        # sees a byte before the hash has matched. stream writes to the path
        # tail_follow follows, and is documented as serving unverified bytes.
        tmp = blobs / (f"{etag}.incomplete" if stream else f"{etag}.tier.incomplete")
        try:
            got, _n = await hash_into(resp, tmp, flush=stream)
        except (httpx.HTTPError, OSError) as exc:
            tmp.unlink(missing_ok=True)
            metrics.record_tier_request("hf", "blob", "error")
            _s.last_error = f"GET {key} failed mid-body: {exc}"
            if stream:
                raise TierReadFailed(_s.last_error) from exc
            return False
        finally:
            await resp.aclose()
        if got != etag:
            tmp.unlink(missing_ok=True)
            mark_bad(key, f"expected sha256 {etag}, computed {got}")
            if stream:
                raise TierReadFailed(f"tier bytes for {etag} failed verification")
            return False
        os.replace(tmp, blob)
        metrics.record_tier_verify("verified")
        return True
    finally:
        await asyncio.to_thread(lock.release)


async def fill_hf_blob(job) -> bool:
    """A file miss: try the tier before hf_hub_download.

    Returns True when the blob is now present and came from the tier. The
    download that follows then finds it and only creates the snapshot link.

    Sets job.tier_source as soon as the tier has answered 200, which is the
    moment serve_file needs in order to choose how to answer the client. It is
    never cleared: a request that sees it waits for the job, whether the tier's
    bytes verified or the job fell through to the Hub. job.served_from says
    which of the two happened.
    """

    def _answered() -> None:
        job.tier_source = True
        job.tier_decided.set()

    ok = await _fill_blob(job.repo_type, job.repo_id, job.etag or "", job.expected_size,
                          stream=cfg().read_mode == "stream", on_answer=_answered)
    if ok:
        job.served_from = "tier"
    return ok


async def fill_snapshot(repo_type: str, repo_id: str,
                        expected: dict[str, tuple[int | None, str | None]]) -> set[str]:
    """A prewarm: fill every expected sha256 blob from the tier first.

    `expected` is the prewarm's own listing, path -> (size, sha256 or None),
    already filtered by its allow_patterns. snapshot_download then runs as
    before: it finds these blobs present and only links them, and fetches the
    rest -- git-blob files, tier misses, and anything that failed verification
    -- from the Hub.

    Always verify-first: nothing tail-follows a prewarm. At most
    XHC_SNAPSHOT_MAX_WORKERS fills run at once, the same bound the Hub fetch
    uses, because each holds a connection and a file and the node matters.

    Returns the ETags that landed from the tier. The job's verification skips
    exactly these (they were hashed as they arrived) and the write-back does
    not re-upload them.
    """
    if not readable():
        return set()
    wanted: dict[str, int | None] = {}
    for size, sha in expected.values():
        if sha and _SHA256_RE.match(sha):
            wanted[sha] = size  # one fill per blob, however many paths name it
    if not wanted:
        return set()
    sem = asyncio.Semaphore(max(1, settings.snapshot_max_workers))

    async def one(etag: str, size: int | None) -> str | None:
        async with sem:
            return etag if await _fill_blob(repo_type, repo_id, etag, size) else None

    got = await asyncio.gather(*(one(e, s) for e, s in wanted.items()))
    filled = {e for e in got if e}
    log.info("prewarm %s/%s: %d of %d sha256 blob(s) filled from the tier",
             repo_type, repo_id, len(filled), len(wanted))
    return filled


async def read_oci_manifest(upstream: str, digest: str) -> tuple[bytes, str] | None:
    """A manifest by digest from the tier, verified in the same pass it is read.

    Returns (verbatim bytes, media type) or None. The media type travels as the
    object's Content-Type, which every S3-compatible store returns as stored.
    """
    key = oci_manifest_key(upstream, digest)
    resp = await open_read(key, "oci", "manifest")
    if resp is None:
        return None
    h = hashlib.sha256()
    parts: list[bytes] = []
    total = 0
    try:
        async for chunk in resp.aiter_bytes():
            total += len(chunk)
            if total > MANIFEST_MAX:
                mark_bad(key, f"manifest larger than {MANIFEST_MAX} bytes")
                return None
            h.update(chunk)
            parts.append(chunk)
        media = resp.headers.get("content-type") or "application/vnd.oci.image.manifest.v1+json"
    except httpx.HTTPError as exc:
        metrics.record_tier_request("oci", "manifest", "error")
        _s.last_error = f"GET {key} failed mid-body: {exc}"
        return None
    finally:
        metrics.record_tier_bytes(read=total)
        await resp.aclose()
    got = "sha256:" + h.hexdigest()
    if got != digest:
        mark_bad(key, f"expected {digest}, computed {got}")
        return None
    metrics.record_tier_verify("verified")
    return b"".join(parts), media


# ---------------------------------------------------------------------------
# write-back
# ---------------------------------------------------------------------------


@dataclass
class Upload:
    kind: str  # "content" | "index"
    key: str
    path: Path | None = None
    sha256: str | None = None  # hex; for content, the object's NAME
    content_type: str | None = None
    body: bytes = b""
    metadata: dict[str, str] = field(default_factory=dict)
    # Index objects that must follow this content, never precede it.
    then: list[Upload] = field(default_factory=list)
    # Index objects only: whether it carries a signature.
    signed: bool = False


def enqueue(item: Upload) -> bool:
    """Non-blocking. Overflow is dropped and counted; the reconciler finds it."""
    if not writable():
        return False
    q = _ensure_queue()
    try:
        q.put_nowait(item)
        return True
    except asyncio.QueueFull:
        metrics.record_tier_upload("dropped_queue_full")
        return False


def queue_depth() -> int:
    return _s.queue.qsize() if _s.queue is not None else 0


def _content(key: str, path: Path, sha_hex: str, content_type: str | None = None) -> Upload:
    return Upload(kind="content", key=key, path=path, sha256=sha_hex, content_type=content_type)


def _auth(*sig_args) -> dict[str, str]:
    """The authentication fields for one index object.

    Signed when XHC_TIER2_INDEX_KEY is set. Otherwise written anyway and MARKED
    unsigned in the object, so a later reader never has to infer it from an
    absent field. Nothing reads the index in phase 1, so an unsigned entry
    costs no trust today; whether phase 2 will restore from one is a policy
    decision that has not been made. Writing it keeps that option open, and
    setting the key from the start keeps the stronger one open too: an entry
    written unsigned is never re-signed later (index objects are immutable and
    skipped when they already exist).
    """
    if cfg().index_key:
        return {"auth": AUTH_SIGNED, "sig": index_sig(*sig_args)}
    return {"auth": AUTH_UNSIGNED}


def _hf_commit_index(repo_type: str, repo_id: str, commit: str, path: str, etag: str,
                     size: int) -> Upload:
    host, repo = hf_host(), f"{repo_type}s/{repo_id}"
    auth = _auth("hf-commit", host, repo, commit, path, etag, size)
    body = s3client.to_json({"etag": etag, "size": size, "version": INDEX_VERSION, **auth})
    return Upload(kind="index", key=hf_commit_index_key(repo_type, repo_id, commit, path),
                  body=body, content_type="application/json", signed="sig" in auth)


def _hf_ref_index(repo_type: str, repo_id: str, ref: str, commit: str,
                  observed_at: float) -> Upload | None:
    if ref == commit:
        return None  # a commit-pinned request observes no ref
    host, repo = hf_host(), f"{repo_type}s/{repo_id}"
    obs = _observed(observed_at)
    auth = _auth("hf-ref", host, repo, ref, "", commit, 0, obs)
    return Upload(
        kind="index", key=hf_ref_index_key(repo_type, repo_id, ref, observed_at, commit),
        metadata={**auth, "observed-at": obs, "version": INDEX_VERSION}, signed="sig" in auth,
    )


def _oci_tag_index(upstream: str, repo: str, tag: str, digest: str,
                   observed_at: float, media: str) -> Upload:
    obs = _observed(observed_at)
    auth = _auth("oci-tag", upstream, repo, tag, "", digest, 0, obs)
    return Upload(
        kind="index", key=oci_tag_index_key(upstream, repo, tag, observed_at, digest),
        metadata={**auth, "observed-at": obs, "media-type": media, "version": INDEX_VERSION},
        signed="sig" in auth,
    )


def enqueue_oci_blob(upstream: str, digest: str, path: Path, size: int | None = None) -> None:
    """After a verified OCI ingest (the os.replace in _write_blob)."""
    if not writable() or (size is not None and size < cfg().min_size):
        return
    enqueue(_content(oci_blob_key(upstream, digest), path, digest.split(":", 1)[1]))


def enqueue_oci_manifest(upstream: str, digest: str, media: str, repo: str | None = None,
                         tag: str | None = None) -> None:
    """After a manifest is stored. Manifests ignore XHC_TIER2_MIN_SIZE: they are
    small by nature and a digest-pinned pull on a fresh disk needs them."""
    if not writable():
        return
    item = _content(oci_manifest_key(upstream, digest), ocistore.manifest_path(upstream, digest),
                    digest.split(":", 1)[1], content_type=media)
    if repo and tag:
        item.then.append(_oci_tag_index(upstream, repo, tag, digest, time.time(), media))
    enqueue(item)


def _hf_job_items(job) -> list[Upload]:
    """What a finished HF job should upload. Runs in a thread (it walks a
    snapshot directory)."""
    t = cfg()
    if not job.result_path:
        return []
    repo_root = Path(settings.cache_dir) / cachefs.repo_folder_name(job.repo_id, job.repo_type)
    snaps = repo_root / "snapshots"
    result = Path(job.result_path)
    try:
        rel = result.relative_to(snaps)
    except ValueError:
        return []
    commit = rel.parts[0]
    if job.kind == "file":
        files = [result]
        since = None
    else:
        files = [Path(d) / f for d, _dirs, fs in os.walk(snaps / commit) for f in fs]
        since = (job.started_at or 0) - 1
    items: list[Upload] = []
    loose: list[Upload] = []
    for f in files:
        try:
            blob = f.resolve()
            st = blob.stat()
        except OSError:
            continue
        path_in_repo = f.relative_to(snaps / commit).as_posix()
        etag = blob.name
        idx = _hf_commit_index(job.repo_type, job.repo_id, commit, path_in_repo, etag, st.st_size)
        fresh = since is None or st.st_mtime >= since
        tier_sourced = (
            (job.kind == "file" and getattr(job, "served_from", None) == "tier")
            or etag in getattr(job, "tier_etags", ())
        )
        if (_SHA256_RE.match(etag) and fresh and not tier_sourced
                and st.st_size >= t.min_size):
            item = _content(hf_content_key(job.repo_type, job.repo_id, etag), blob, etag)
            item.then.append(idx)
            items.append(item)
        else:
            # Git-object files (phase 2 content), content already in the tier,
            # or content below MIN_SIZE: the mapping is still true, so record it.
            loose.append(idx)
    ref = _hf_ref_index(job.repo_type, job.repo_id, job.revision, commit, time.time())
    rkey = (job.repo_type, job.repo_id, job.revision, commit)
    if ref is not None and rkey not in _s.refs_written:
        _s.refs_written.add(rkey)
        loose.append(ref)
    return items + loose


async def after_hf_job(job) -> None:
    """Write-back trigger for HF. Called only for a job that reached `done`."""
    if not writable() or job.state != "done":
        return
    try:
        for item in await asyncio.to_thread(_hf_job_items, job):
            enqueue(item)
    except Exception:
        log.exception("tier write-back could not be scheduled for job %s", job.id)


async def _retry(make, what: str):
    last: BaseException | None = None
    for attempt in range(len(BACKOFF_S) + 1):
        try:
            return await make()
        except RETRYABLE as exc:
            last = exc
            if attempt == len(BACKOFF_S):
                break
            log.warning("tier %s failed (%s); retrying in %ss", what, exc, BACKOFF_S[attempt])
            await asyncio.sleep(BACKOFF_S[attempt])
    raise last


def _read_exact(fh, n: int) -> bytes:
    return fh.read(n)


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def _exists(key: str) -> bool | None:
    r = await _retry(lambda: _s.client.head(key), f"HEAD {key}")
    if r.status_code == 200:
        return True
    if r.status_code == 404:
        return False
    log.warning("tier HEAD %s returned %s", key, r.status_code)
    return None


async def _upload_content(item: Upload) -> str:
    """One read of every byte, hashed in the same pass, then sent.

    Single PUT (size <= part size): the file is read once into memory and
    hashed once. A mismatch against the NAME refuses the upload. The name is
    also sent as x-amz-content-sha256, so a SigV4 store checks the body too.

    Multipart: each part is read once, folded into the whole-object hash, and
    sent from memory (a retry re-sends the buffer, never re-reads the file).
    CompleteMultipartUpload is only called once the whole-object hash has
    matched; on a mismatch the upload is aborted and no object appears.
    """
    t = cfg()
    path = item.path
    if path is None or not path.exists():
        return "skipped_evicted"
    exists = await _exists(item.key)
    if exists:
        return "skipped_exists"
    if exists is None:
        return "failed"
    try:
        size = path.stat().st_size
        fh = open(path, "rb")  # noqa: SIM115 - closed in the finally below
    except OSError:
        return "skipped_evicted"
    try:
        if size <= t.part_size:
            data = await asyncio.to_thread(_read_exact, fh, size + 1)
            got = await asyncio.to_thread(_sha256_hex, data)
            if got != item.sha256:
                log.error("TIER UPLOAD REFUSED: %s hashes to %s, not its name %s. "
                          "Local bytes changed after ingest.", path, got, item.sha256)
                return "verify_mismatch"
            r = await _retry(
                lambda: _s.client.put(item.key, data, sha256_hex=item.sha256,
                                      checksum_header=t.checksum_header,
                                      content_type=item.content_type),
                f"PUT {item.key}",
            )
            if r.status_code != 200:
                log.warning("tier PUT %s refused: %s", item.key, s3client.describe(r))
                return "failed"
            metrics.record_tier_bytes(written=len(data))
            return "ok"

        upload_id = await _retry(
            lambda: _s.client.create_multipart(item.key, item.content_type),
            f"CreateMultipartUpload {item.key}",
        )
        completed = False
        try:
            h = hashlib.sha256()
            parts: list[str] = []
            number = 1
            while True:
                buf = await asyncio.to_thread(_read_exact, fh, t.part_size)
                if not buf:
                    break
                await asyncio.to_thread(h.update, buf)
                n = number
                etag = await _retry(
                    lambda b=buf, n=n: _s.client.upload_part(item.key, upload_id, n, b),
                    f"UploadPart {n} {item.key}",
                )
                metrics.record_tier_bytes(written=len(buf))
                parts.append(etag)
                number += 1
            got = h.hexdigest()
            if got != item.sha256:
                log.error("TIER UPLOAD REFUSED: %s hashes to %s, not its name %s. "
                          "Multipart upload aborted; no object was created.",
                          path, got, item.sha256)
                return "verify_mismatch"
            await _retry(lambda: _s.client.complete_multipart(item.key, upload_id, parts),
                         f"CompleteMultipartUpload {item.key}")
            completed = True
            return "ok"
        finally:
            if not completed:
                try:
                    await _s.client.abort_multipart(item.key, upload_id)
                except Exception as exc:  # noqa: BLE001 - best effort; parts are billed
                    log.warning("could not abort multipart upload %s for %s: %s. Its "
                                "parts are billed until an abort-incomplete-multipart "
                                "lifecycle rule removes them.", upload_id, item.key, exc)
    finally:
        fh.close()


async def _put_index(item: Upload) -> None:
    try:
        exists = await _exists(item.key)
        if exists:
            metrics.record_tier_index_write("skipped_exists")
            return
        r = await _retry(
            lambda: _s.client.put(item.key, item.body, content_type=item.content_type,
                                  metadata=item.metadata),
            f"PUT {item.key}",
        )
        if r.status_code == 200:
            metrics.record_tier_index_write("signed" if item.signed else "unsigned")
            metrics.record_tier_bytes(written=len(item.body))
        else:
            metrics.record_tier_index_write("failed")
            log.warning("tier index PUT %s refused: %s", item.key, s3client.describe(r))
    except s3client.TierAuthError as exc:
        metrics.record_tier_index_write("failed")
        _mark_unhealthy(str(exc))
    except (httpx.HTTPError, s3client.TierHTTPError) as exc:
        metrics.record_tier_index_write("failed")
        log.warning("tier index PUT %s failed: %s", item.key, exc)


async def process(item: Upload) -> str | None:
    """Upload one queue item. Returns the content result, or None for an index."""
    if item.kind == "index":
        await _put_index(item)
        return None
    try:
        result = await _upload_content(item)
    except s3client.TierAuthError as exc:
        _mark_unhealthy(str(exc))
        result = "failed"
    except (httpx.HTTPError, s3client.TierHTTPError, OSError) as exc:
        log.warning("tier upload of %s failed: %s", item.key, exc)
        result = "failed"
    metrics.record_tier_upload(result)
    if result == "ok":
        # Keep the status totals live between listings: they are set from a
        # bucket listing at each reconcile (startup, then every interval), and
        # without this a first backfill of an empty bucket read 0 objects and
        # 0 bytes throughout. Listed + uploaded since, not a fresh listing.
        rec = _s.reconcile
        if isinstance(rec, dict) and "tier_objects" in rec:
            try:
                size = item.path.stat().st_size if item.path is not None else 0
            except OSError:
                size = 0
            rec["tier_objects"] += 1
            rec["tier_bytes"] += size
            rec["uploaded_since_listing"] = rec.get("uploaded_since_listing", 0) + 1
    if result in ("ok", "skipped_exists"):
        for idx in item.then:
            await _put_index(idx)
    return result


async def drain() -> None:
    """Process everything queued, inline. Used by tests and by shutdown-free
    callers; the workers do the same thing in the background."""
    q = _ensure_queue()
    while not q.empty():
        item = q.get_nowait()
        try:
            await process(item)
        finally:
            q.task_done()


async def _worker() -> None:
    q = _ensure_queue()
    while True:
        item = await q.get()
        try:
            if _s.healthy_event is not None:
                await _s.healthy_event.wait()
            await process(item)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("tier upload worker failed on %s", item.key)
        finally:
            q.task_done()


# ---------------------------------------------------------------------------
# probe
# ---------------------------------------------------------------------------


async def probe() -> dict:
    """PUT then GET v1/_probe/<instance>, and GET a key that must be absent.

    The absent-key GET is the part that catches a missing list permission:
    without it, S3 answers 403 rather than 404 for a missing key, and every tier
    miss would read as an auth failure. The probe deletes nothing -- that would
    need delete permission, which Muninn never asks for.
    """
    t = cfg()
    result: dict = {"at": time.time(), "ok": False}
    payload = secrets.token_bytes(32)
    try:
        r = await _s.client.put(probe_key(), payload, checksum_header=t.checksum_header)
        if r.status_code != 200:
            result["error"] = f"probe PUT returned {s3client.describe(r)}"
            if r.status_code == 400 and t.checksum_header:
                result["error"] += (" -- if the store rejects x-amz-checksum-sha256, set "
                                    "XHC_TIER2_CHECKSUM_HEADER=false")
            return _finish_probe(result)
        r = await _s.client.get_bytes(probe_key())
        if r.status_code != 200 or r.content != payload:
            result["error"] = f"probe GET did not return what was PUT ({r.status_code})"
            return _finish_probe(result)
        absent = probe_key(f".absent-{secrets.token_hex(8)}")
        r = await _s.client.get_bytes(absent)
        if r.status_code == 403:
            result["error"] = ("a key known to be absent returned 403, not 404: the credential "
                               "lacks list permission on the bucket, so every miss would read "
                               "as an auth failure. Grant ListBucket (s3:ListBucket) on the "
                               "prefix.")
            return _finish_probe(result)
        if r.status_code != 404:
            result["error"] = f"a key known to be absent returned {r.status_code}, not 404"
            return _finish_probe(result)
        result["ok"] = True
    except s3client.TierAuthError as exc:
        result["error"] = f"401: the credential is dead ({exc})"
    except httpx.HTTPError as exc:
        result["error"] = f"store unreachable: {type(exc).__name__}: {exc}"
    return _finish_probe(result)


def _finish_probe(result: dict) -> dict:
    _s.probe = result
    if result["ok"]:
        _mark_healthy()
        log.info("tier probe OK: %s (read=%s write=%s mode=%s)", cfg().url, cfg().read,
                 cfg().write, cfg().read_mode)
    else:
        _mark_unhealthy(f"probe failed: {result['error']}")
    return result


# ---------------------------------------------------------------------------
# reconciler
# ---------------------------------------------------------------------------


async def _list_keys(prefix: str) -> dict[str, int]:
    out: dict[str, int] = {}
    async for obj in _s.client.list_prefix(prefix):
        out[obj.key] = obj.size
    return out


def _local_hf(view_repos) -> list[tuple[str, str, Path]]:
    out = []
    for r in view_repos:
        blobs = (Path(settings.cache_dir) / cachefs.repo_folder_name(r.repo_id, r.repo_type)
                 / "blobs")
        try:
            with os.scandir(blobs) as it:
                for e in it:
                    if _SHA256_RE.match(e.name) and e.is_file():
                        out.append((r.repo_type, r.repo_id, Path(e.path)))
        except OSError:
            continue
    return out


def _local_hf_index(view_repos) -> list[tuple]:
    """(repo_type, repo_id, commit, path, etag, size) for every snapshot entry,
    and (repo_type, repo_id, ref, commit, mtime) for every local ref."""
    files, refs = [], []
    for r in view_repos:
        root = Path(settings.cache_dir) / cachefs.repo_folder_name(r.repo_id, r.repo_type)
        snaps = root / "snapshots"
        try:
            commits = [e.name for e in os.scandir(snaps) if e.is_dir()]
        except OSError:
            commits = []
        for commit in commits:
            for d, _dirs, fs in os.walk(snaps / commit):
                for f in fs:
                    p = Path(d) / f
                    try:
                        blob = p.resolve()
                        size = blob.stat().st_size
                    except OSError:
                        continue
                    files.append((r.repo_type, r.repo_id, commit,
                                  p.relative_to(snaps / commit).as_posix(), blob.name, size))
        refs_dir = root / "refs"
        for d, _dirs, fs in os.walk(refs_dir):
            for f in fs:
                p = Path(d) / f
                try:
                    commit = p.read_text().strip()
                    mtime = p.stat().st_mtime
                except OSError:
                    continue
                refs.append((r.repo_type, r.repo_id, p.relative_to(refs_dir).as_posix(),
                             commit, mtime))
    return [files, refs]


def _local_oci() -> list[tuple[str, str, str, Path, str | None]]:
    """(kind, upstream_dirname, digest, path, media) for every local blob and
    manifest. Two shard levels per upstream: bounded by this cache's own tree."""
    out = []
    root = ocistore.root()
    try:
        ups = [e for e in os.scandir(root) if e.is_dir() and not e.name.startswith(".")]
    except OSError:
        return out
    for up in ups:
        for kind in ("blobs", "manifests"):
            base = Path(up.path) / kind / "sha256"
            try:
                shards = [e for e in os.scandir(base) if e.is_dir()]
            except OSError:
                continue
            for sh in shards:
                with os.scandir(sh.path) as it:
                    for e in it:
                        if not _SHA256_RE.match(e.name):
                            continue
                        media = None
                        if kind == "manifests":
                            try:
                                media = json.loads(Path(e.path + ".meta").read_text()).get(
                                    "media_type")
                            except (OSError, ValueError):
                                media = None
                        out.append((kind, up.name, "sha256:" + e.name, Path(e.path), media))
    return out


async def reconcile() -> dict:
    """Enqueue whatever is held locally and absent from the tier.

    The bucket is the durable record of what was uploaded; there is no state
    file, so a restart loses nothing but time and nothing new can be
    unreadable. Also backfills the index for local snapshots and refs.
    """
    started = time.time()
    summary: dict = {"started_at": started}
    try:
        content = await _list_keys(f"{_base()}/content/")
        summary["tier_objects"] = len(content)
        summary["tier_bytes"] = sum(content.values())
        enq = 0
        view = await cachefs.get_view(force=True)
        for repo_type, repo_id, path in await asyncio.to_thread(_local_hf, view.repos):
            key = hf_content_key(repo_type, repo_id, path.name)
            if key in content:
                continue
            try:
                if path.stat().st_size < cfg().min_size:
                    continue
            except OSError:
                continue
            enq += enqueue(_content(key, path, path.name))
        if settings.docker_enabled:
            for kind, up, digest, path, media in await asyncio.to_thread(_local_oci):
                key = _oci_sharded(up, kind, digest)
                if key in content:
                    continue
                if kind == "blobs":
                    try:
                        if path.stat().st_size < cfg().min_size:
                            continue
                    except OSError:
                        continue
                enq += enqueue(_content(key, path, digest.split(":", 1)[1],
                                        content_type=media if kind == "manifests" else None))
        summary["content_enqueued"] = enq
        # The index, with or without a key: unsigned entries are marked so.
        index = await _list_keys(f"{_base()}/index/hf/")
        files, refs = await asyncio.to_thread(_local_hf_index, view.repos)
        ienq = 0
        for repo_type, repo_id, commit, path, etag, size in files:
            if hf_commit_index_key(repo_type, repo_id, commit, path) in index:
                continue
            item = _hf_commit_index(repo_type, repo_id, commit, path, etag, size)
            ienq += enqueue(item)
        # A ref observation is "<ref dir>/<observed_at>-<commit>"; any
        # observation of this ref at this commit is enough.
        seen_refs = {
            (k.rsplit("/", 1)[0], k.rsplit("/", 1)[1].split("-", 1)[-1])
            for k in index if "/refs/" in k
        }
        for repo_type, repo_id, ref, commit, mtime in refs:
            ref_dir = hf_ref_index_key(repo_type, repo_id, ref, 0, commit).rsplit("/", 1)[0]
            if (ref_dir, commit) in seen_refs:
                continue
            item = _hf_ref_index(repo_type, repo_id, ref, commit, mtime)
            if item is not None:
                ienq += enqueue(item)
        summary["index_enqueued"] = ienq
        summary["ok"] = True
    except s3client.TierAuthError as exc:
        _mark_unhealthy(str(exc))
        summary.update(ok=False, error=str(exc))
    except (httpx.HTTPError, s3client.TierHTTPError, OSError) as exc:
        summary.update(ok=False, error=f"{type(exc).__name__}: {exc}")
        log.warning("tier reconcile failed: %s", exc)
    summary["finished_at"] = time.time()
    _s.reconcile = summary
    return summary


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------


async def _loop() -> None:
    t = cfg()
    next_reconcile = 0.0
    while True:
        if not _s.healthy:
            await probe()
            if not _s.healthy:
                await asyncio.sleep(REPROBE_S)
                continue
        if t.write and t.reconcile_interval_s > 0 and time.time() >= next_reconcile:
            await reconcile()
            next_reconcile = time.time() + t.reconcile_interval_s
        await asyncio.sleep(min(REPROBE_S, t.reconcile_interval_s or REPROBE_S))


async def start() -> None:
    if not enabled():
        return
    t = cfg()
    if _s.client is None:
        _s.client = build_client()
    _s.healthy_event = asyncio.Event()
    if _s.healthy:
        _s.healthy_event.set()
    _ensure_queue()
    if t.write:
        for _ in range(t.upload_concurrency):
            _s.tasks.append(asyncio.create_task(_worker()))
    _s.tasks.append(asyncio.create_task(_loop()))
    log.warning(
        "object-store tier ENABLED at %s (read=%s write=%s read_mode=%s index=%s). "
        "Muninn never deletes from it: it grows without bound until the operator "
        "sets a retention rule, and retention is the operator's cost decision.",
        t.url, t.read, t.write, t.read_mode,
        "signed" if t.index_key else "UNSIGNED (XHC_TIER2_INDEX_KEY unset)",
    )
    if t.read_mode == "stream":
        log.warning("XHC_TIER2_READ_MODE=stream serves tier bytes to Hugging Face "
                    "clients BEFORE they are verified; a mismatch is detected only after "
                    "the last byte, when the client has already accepted it.")


async def stop() -> None:
    for task in _s.tasks:
        task.cancel()
    for task in _s.tasks:
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    _s.tasks.clear()
    if _s.http is not None:
        await _s.http.aclose()
        _s.http = None


def status() -> dict:
    if not enabled():
        return {"enabled": False}
    t = cfg()
    return {
        "enabled": True,
        "url": t.url,
        "endpoint": t.endpoint,
        "credentials": t.credentials,
        "read": t.read,
        "write": t.write,
        "read_mode": t.read_mode,
        "index": "signed" if t.index_key else (
            "unsigned: XHC_TIER2_INDEX_KEY is unset. Entries are written and marked "
            "unsigned; whether a later restore will trust them is not decided"),
        "healthy": _s.healthy,
        "probe": _s.probe,
        "last_error": _s.last_error,
        "queue_depth": queue_depth(),
        "bad_keys": sorted(_s.bad)[:50],
        "reconcile": _s.reconcile,
        "deletes": "never: Muninn does not delete from the tier; retention is the operator's",
    }


def gauges() -> dict[str, float]:
    if not enabled():
        return {}
    g: dict[str, float] = {
        "muninn_tier_healthy": 1 if _s.healthy else 0,
        "muninn_tier_upload_queue_depth": queue_depth(),
    }
    rec = _s.reconcile
    if rec.get("finished_at"):
        g["muninn_tier_last_reconcile_timestamp"] = rec["finished_at"]
    if "tier_objects" in rec:
        g["muninn_tier_objects"] = rec["tier_objects"]
        g["muninn_tier_bytes"] = rec["tier_bytes"]
    return g
