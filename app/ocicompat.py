"""The OCI Distribution surface: `/v2/*`, pull only.

Addressed by path prefix, so no edge node needs configuring:

    docker pull muninn.host/ghcr.io/org/img:1.2.3
    docker pull muninn.host/nginx                  # -> docker.io/library/nginx

Everything that is not `/v2/*` stays Hugging Face and is untouched. This router
must be registered BEFORE hfcompat, whose catch-all would otherwise swallow it.

Pull only, deliberately: a cache that accepts pushes is a registry, with garbage
collection, quota and durability obligations that are a different product.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.responses import Response

from . import metrics, ocigc, ocistore, policy, registry, serving
from .config import settings

log = logging.getLogger("xhc.oci")

router = APIRouter()

# Single-flight per (upstream, digest). N nodes rolling the same deployment
# should cost exactly one upstream pull per blob -- the same property that makes
# the HF side worth having.
_inflight: dict = {}  # (upstream, digest) -> BlobJob, defined below
_inflight_lock = asyncio.Lock()
_tasks: set[asyncio.Task] = set()

# Single-flight per (upstream, repo, tag) for revalidation, so a herd arriving
# after a TTL expiry causes one upstream check rather than one each.
_tag_locks: dict[tuple[str, str, str], asyncio.Lock] = {}

_API_VERSION = {"Docker-Distribution-API-Version": "registry/2.0"}


def _err(status: int, code: str, message: str, headers: dict | None = None) -> JSONResponse:
    """OCI-shaped error. Clients parse this; a bare string confuses them."""
    h = dict(_API_VERSION)
    h.update(headers or {})
    return JSONResponse(
        {"errors": [{"code": code, "message": message}]}, status_code=status, headers=h
    )


def _unavailable(ref: registry.Ref, upstream_status: int | None,
                 reference: str, kind: str) -> JSONResponse:
    """Terminal answer when upstream would not serve and nothing is cached.

    THREE DISTINCT STATES used to render as one 404 MANIFEST_UNKNOWN, and they
    have three different fixes and potentially three different owners:

        upstream 401, no credentials configured  -> docker login on the host
        upstream 401, credentials rejected       -> wrong/expired/unscoped creds
        upstream 404                             -> genuinely not there

    The second only became reachable in v0.6.2. Before that nothing was ever
    sent, so every 401 was the first case.

    STATUS CODES, DECIDED BY MEASUREMENT RATHER THAN BY SEMANTICS:

      404 -> genuinely absent. Correct, and the client can act on it.
      502 -> the CACHE could not authenticate to upstream. Muninn is a gateway
             and did not obtain a valid response from it.
      401 -> RESERVED for Muninn's own client-facing auth (an internal issue). Never used
             for an upstream failure, because "authenticate to the cache" and
             "the cache cannot authenticate upstream" are different actors with
             different fixes.

    THE DOCKER CLI DISCARDS THE BODY AND HEADERS AND PRINTS ONLY THE STATUS, so
    a 404 carrying a perfect explanation is invisible to the person running the
    pull. Measured against docker 29.5.1:

      404 -> "not found"                              <- the status is ERASED
      401 -> "unexpected status ...: 401 Unauthorized"
      502 -> "unexpected status ...: 502 Bad Gateway"
      403 -> "unexpected status ...: 403 Forbidden"

    404 is the only status that hides itself. That is why an auth failure must
    not wear one -- a colleague hit exactly this, read "not found", and went looking
    for a missing image on a registry they could see the image in.

    Their stated objection to 502 -- that it might trigger client retry with
    backoff where 404 fails fast -- was measured and does not occur: 404, 401
    and 502 all fail in ~32ms.

    This never emits a WWW-Authenticate, and upstream's own is deliberately not
    forwarded: it points at a realm Muninn does not proxy, which is a retry loop
    with no exit, and after an internal issue it would be indistinguishable from Muninn's
    own challenge. An error is a routing instruction for whoever reads it next
    (a colleague, an internal issue).

    Only reached with nothing cached. The fail-open path is untouched: while a
    copy is held, an upstream 401 or 404 still serves it, because losing it the
    moment upstream stops answering is the irreversible loss orphan retention
    exists to prevent.
    """
    unknown = "MANIFEST_UNKNOWN" if kind == "manifest" else "BLOB_UNKNOWN"
    if upstream_status != 401:
        return _err(404, unknown, f"{kind} {reference} not available",
                    {"x-xhc-upstream-status": str(upstream_status or "none"),
                     "x-xhc-upstream-auth": "n/a"})

    configured = registry.has_credentials(ref.upstream)
    metrics.record_docker("UPSTREAM_AUTH", kind)
    if configured:
        detail = (f"{ref.upstream} rejected the credentials this cache holds for it. "
                  "They are wrong, expired, or lack scope for this repository. "
                  "Re-authenticate on the cache host.")
        state = "rejected"
    else:
        detail = (f"this cache is not authenticated to {ref.upstream} and holds no "
                  f"copy of {reference}. Run `docker login {ref.upstream}` on the "
                  "cache host and mount its config.json via XHC_REGISTRY_AUTH_FILE. "
                  "Muninn authenticates as itself; it does not forward your "
                  "credentials upstream.")
        state = "unconfigured"
    return _err(502, "UNAVAILABLE", detail,
                {"x-xhc-upstream-status": "401", "x-xhc-upstream-auth": state})


def _denied(reason: str, kind: str) -> JSONResponse:
    metrics.record_docker("DENIED", kind)
    # Deliberately NOT a registry auth error: answering 401 here would send
    # people to `docker login` to fix a policy refusal, which cannot work.
    return _err(403, "DENIED", f"muninn policy: {reason}", {"x-xhc-policy": "denied"})


# ---------------------------------------------------------------------------
# blob ingest
# ---------------------------------------------------------------------------


@dataclass
class BlobJob:
    """Duck-types the fields serving.tail_follow reads off an HF Job -- `state`,
    `error`, `id`, `incomplete_path` -- so the stream-while-caching path is
    shared rather than reimplemented."""

    id: str
    digest: str
    upstream: str
    final_path: Path
    incomplete_path: str | None
    size: int | None = None
    state: str = "pending"
    error: str | None = None
    # Set once the upstream response headers are in (or the attempt has failed),
    # so joiners can read `size` and `failure` without racing the opener.
    opened: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    failure: UpstreamError | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event, repr=False)


class UpstreamError(Exception):
    def __init__(self, status: int | None, message: str, headers: dict | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.headers = headers or {}


async def _write_blob(job: BlobJob, resp: httpx.Response) -> None:
    """Stream upstream bytes to disk, hashing as they land.

    The digest is checked before the file is committed. Unverified bytes are
    never renamed into place -- a corrupted layer must fail loudly rather than
    be served forever from cache.
    """
    tmp = Path(job.incomplete_path)
    h = hashlib.sha256()
    written = 0
    try:
        tmp.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "wb") as fh:
            async for chunk in resp.aiter_bytes(serving.CHUNK):
                fh.write(chunk)
                h.update(chunk)
                written += len(chunk)
                # Flush so a tail-following client's stat() sees the growth.
                fh.flush()
        got = "sha256:" + h.hexdigest()
        if got != job.digest:
            raise ocistore.DigestMismatch(f"expected {job.digest}, computed {got}")
        job.final_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp, job.final_path)
        job.size = written
        job.state = "done"
        metrics.record_docker_bytes(ingested=written)
    except ocistore.DigestMismatch as exc:
        job.state, job.error = "error", str(exc)
        log.error(
            "DIGEST MISMATCH on %s/%s -- discarding %d bytes, refusing to cache: %s",
            job.upstream,
            job.digest,
            written,
            exc,
        )
        tmp.unlink(missing_ok=True)
    except (httpx.HTTPError, OSError) as exc:
        job.state, job.error = "error", str(exc)
        log.warning("blob ingest %s failed: %s", job.digest, exc)
        tmp.unlink(missing_ok=True)
    finally:
        await resp.aclose()
        job.opened.set()
        job.done.set()
        async with _inflight_lock:
            if _inflight.get((job.upstream, job.digest)) is job:
                _inflight.pop((job.upstream, job.digest), None)


async def _ensure_blob(ref: registry.Ref, digest: str) -> BlobJob:
    """Start (or join) an ingest for this blob.

    The in-flight slot is claimed BEFORE the upstream request is opened. That
    ordering is the whole point: claiming it afterwards means N concurrent cold
    clients each open their own upstream request and then discard all but one,
    which is invisible in the bytes transferred but multiplies the REQUEST count
    by the size of the herd -- and Docker Hub rate-limits on requests. Measured
    against docker.io before the fix: 8 concurrent pulls, 8 upstream requests.

    The opener inspects `Content-Length` for the size policy before any body
    bytes move; opening and closing a response transfers headers only.
    """
    key = (ref.upstream, digest)
    async with _inflight_lock:
        existing = _inflight.get(key)
        if existing is not None:
            joiner = existing
        else:
            joiner = None
            final = ocistore.blob_path(ref.upstream, digest)
            job = BlobJob(
                id=uuid.uuid4().hex[:12],
                digest=digest,
                upstream=ref.upstream,
                final_path=final,
                incomplete_path=str(final) + ".incomplete",
            )
            _inflight[key] = job

    if joiner is not None:
        # Wait for the opener to learn the size, or to fail; either way the
        # answer we give this client must match the one the opener got.
        await joiner.opened.wait()
        if joiner.failure is not None:
            raise joiner.failure
        return joiner

    async def _abandon(failure: UpstreamError) -> None:
        job.failure = failure
        job.state = "error"
        job.error = failure.message
        async with _inflight_lock:
            _inflight.pop(key, None)
        job.opened.set()
        job.done.set()

    try:
        resp = await registry.open_stream(ref, f"blobs/{digest}")
    except httpx.HTTPError as exc:
        metrics.record_docker_upstream(ref.upstream, None)
        await _abandon(UpstreamError(None, f"upstream unreachable: {exc}"))
        raise job.failure from exc

    metrics.record_docker_upstream(ref.upstream, resp.status_code)
    if resp.status_code != 200:
        # Deliberately NOT forwarding upstream's www-authenticate. It invites the
        # client to authenticate against a realm Muninn does not proxy and cannot
        # satisfy -- a retry loop with no exit -- and once Muninn has client-facing
        # auth of its own (an internal issue) a forwarded challenge is indistinguishable from
        # Muninn's own. The diagnosis goes in headers a human reads instead.
        hdrs = {}
        await resp.aclose()
        await _abandon(
            UpstreamError(resp.status_code, f"upstream returned {resp.status_code}", hdrs)
        )
        raise job.failure

    size = int(resp.headers.get("content-length") or 0) or None
    verdict = policy.check_blob_size(size)
    if not verdict.allowed:
        await resp.aclose()
        await _abandon(UpstreamError(None, verdict.reason, {"x-xhc-policy": "denied"}))
        raise job.failure

    job.size = size
    job.state = "running"
    job.opened.set()
    task = asyncio.create_task(_write_blob(job, resp))
    # asyncio holds only a weak reference to tasks; without a strong ref an
    # in-flight ingest that clients are streaming from can be collected.
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return job


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------


@router.get("/v2/", include_in_schema=False)
@router.get("/v2", include_in_schema=False)
async def v2_root() -> Response:
    """Answered locally: it is not repo-scoped, so there is no upstream to
    forward it to and no way to pick one."""
    return JSONResponse({}, headers=dict(_API_VERSION))


def _resolve_or_error(name: str):
    try:
        return registry.resolve(name), None
    except registry.ResolveError as exc:
        return None, _err(400, "NAME_INVALID", str(exc))


async def _revalidate_tag(
    ref: registry.Ref,
    tag: str,
    accept: str | None,
    accept_fp: str,
    out: dict | None = None,
):
    """Fetch the current tag->digest mapping, storing the manifest if it moved.

    Fails OPEN: unreachable, rate-limited, 401 or 404 upstream all mean keep
    serving the digest we hold. That is required for orphan retention -- a tag
    deleted upstream 404s permanently and correctly, and losing the cached copy
    at that moment is exactly the irreversible loss the retention policy exists
    to prevent.
    """
    try:
        r = await registry.get(ref, f"manifests/{tag}", {"accept": accept or "*/*"})
    except httpx.HTTPError as exc:
        metrics.record_docker_upstream(ref.upstream, None)
        log.info("tag revalidation failed for %s:%s (%s) -- serving cached", ref.key, tag, exc)
        return None
    metrics.record_docker_upstream(ref.upstream, r.status_code)
    if out is not None:
        out["status"] = r.status_code
    if r.status_code != 200:
        log.info(
            "tag revalidation got %s for %s:%s -- serving cached if held",
            r.status_code,
            ref.key,
            tag,
        )
        return None
    body = r.content
    media = r.headers.get("content-type") or "application/vnd.oci.image.manifest.v1+json"
    digest = r.headers.get("docker-content-digest") or ocistore.compute_digest(body)
    try:
        ocistore.store_manifest(ref.upstream, digest, body, media)
    except ocistore.DigestMismatch as exc:
        log.error("manifest digest mismatch from %s for %s:%s: %s", ref.upstream, ref.key, tag, exc)
        return None
    ocistore.write_tag(ref.upstream, ref.repo, tag, accept_fp, digest, media)
    return ocistore.StoredManifest(body=body, media_type=media, digest=digest)


def _manifest_response(m: ocistore.StoredManifest, head: bool) -> Response:
    headers = dict(_API_VERSION)
    headers["docker-content-digest"] = m.digest
    headers["content-length"] = str(len(m.body))
    headers["etag"] = f'"{m.digest}"'
    # HEAD must return identical headers with no body, so content-length is set
    # explicitly rather than derived from what we send.
    return Response(
        content=b"" if head else m.body,
        media_type=m.media_type,
        headers=headers,
    )


@router.api_route("/v2/{name:path}/manifests/{reference}", methods=["GET", "HEAD"])
async def manifests(name: str, reference: str, request: Request) -> Response:
    if not settings.docker_enabled:
        return _err(404, "UNSUPPORTED", "docker/OCI caching is disabled")
    ref, err = _resolve_or_error(name)
    if err:
        return err
    head = request.method == "HEAD"
    accept = request.headers.get("accept")

    verdict = policy.check_docker(ref.upstream, ref.repo)
    enforce_on_hit = policy.docker_enforced_on_hits()
    if not verdict.allowed and enforce_on_hit:
        return _denied(verdict.reason, "manifest")

    # Carries the upstream status out of _revalidate_tag, which otherwise
    # returns a bare None for every non-200 and discards WHY. Bound here rather
    # than at either call site so the terminal answer at the end of the function
    # cannot read an unbound name on a path that never revalidated.
    up: dict = {}

    # A digest reference is immutable: never revalidated, at zero cost.
    if ocistore.DIGEST_RE.match(reference):
        held = ocistore.load_manifest(ref.upstream, reference)
        if held is not None:
            metrics.record_docker("HIT", "manifest")
            return _manifest_response(held, head)
        if not verdict.allowed:
            return _denied(verdict.reason, "manifest")
        fetched = await _revalidate_tag(ref, reference, accept, "immutable", up)
        if fetched is None:
            return _unavailable(ref, up.get("status"), reference, "manifest")
        metrics.record_docker("MISS", "manifest")
        return _manifest_response(fetched, head)

    accept_fp = ocistore.accept_fingerprint(accept)
    entry = ocistore.read_tag(ref.upstream, ref.repo, reference, accept_fp)
    if entry is None:
        # Exact fingerprint miss: fall back to any mapping whose media type this
        # client accepts, rather than going upstream for a manifest we hold.
        entry = ocistore.read_tag_compatible(ref.upstream, ref.repo, reference, accept)

    if entry and ocistore.tag_is_fresh(entry):
        held = ocistore.load_manifest(ref.upstream, entry["digest"])
        if held is not None:
            metrics.record_docker("HIT", "manifest")
            return _manifest_response(held, head)

    if not verdict.allowed and entry is None:
        return _denied(verdict.reason, "manifest")

    lock = _tag_locks.setdefault((ref.upstream, ref.repo, reference), asyncio.Lock())
    async with lock:
        # Another waiter may have refreshed it while we queued.
        entry2 = ocistore.read_tag(ref.upstream, ref.repo, reference, accept_fp)
        if entry2 and ocistore.tag_is_fresh(entry2):
            held = ocistore.load_manifest(ref.upstream, entry2["digest"])
            if held is not None:
                metrics.record_docker("HIT", "manifest")
                return _manifest_response(held, head)
        fresh = await _revalidate_tag(ref, reference, accept, accept_fp, up)

    if fresh is not None:
        metrics.record_docker("MISS", "manifest")
        return _manifest_response(fresh, head)

    # Fail open: upstream would not answer, so serve what we hold.
    if entry:
        held = ocistore.load_manifest(ref.upstream, entry["digest"])
        if held is not None:
            ocistore.touch_tag(ref.upstream, ref.repo, reference, accept_fp)
            # Upstream would not confirm this tag and we are serving a cached
            # copy: mark it so GC treats it as an orphan under `retain`. Deleted
            # tags are common in registry land, and the copy here may be the
            # only one left.
            ocigc.mark_orphan(ref.upstream, ref.repo, reference)
            metrics.record_docker("RETAINED", "manifest")
            return _manifest_response(held, head)
    return _unavailable(ref, up.get("status"), reference, "manifest")


@router.api_route("/v2/{name:path}/blobs/{digest}", methods=["GET", "HEAD"])
async def blobs(name: str, digest: str, request: Request) -> Response:
    if not settings.docker_enabled:
        return _err(404, "UNSUPPORTED", "docker/OCI caching is disabled")
    if not ocistore.DIGEST_RE.match(digest):
        return _err(400, "DIGEST_INVALID", f"not a sha256 digest: {digest}")
    ref, err = _resolve_or_error(name)
    if err:
        return err

    verdict = policy.check_docker(ref.upstream, ref.repo)
    path = ocistore.blob_path(ref.upstream, digest)
    head = request.method == "HEAD"

    if path.is_file():
        if not verdict.allowed and policy.docker_enforced_on_hits():
            return _denied(verdict.reason, "blob")
        size = path.stat().st_size
        metrics.record_docker("HIT", "blob")
        headers = dict(_API_VERSION)
        headers["docker-content-digest"] = digest
        if head:
            headers["content-length"] = str(size)
            headers["accept-ranges"] = "bytes"
            return Response(content=b"", headers=headers)
        metrics.record_docker_bytes(served=size)
        return serving.file_response(path, size, request.headers.get("range"), headers)

    if not verdict.allowed:
        return _denied(verdict.reason, "blob")

    try:
        job = await _ensure_blob(ref, digest)
    except UpstreamError as exc:
        if exc.headers.get("x-xhc-policy") == "denied":
            return _denied(exc.message, "blob")
        if exc.status == 404:
            return _err(404, "BLOB_UNKNOWN", f"blob {digest} not found upstream")
        if exc.status == 401:
            return _unavailable(ref, 401, digest, "blob")
        return _err(502, "UNAVAILABLE", exc.message)
    except httpx.HTTPError as exc:
        metrics.record_docker_upstream(ref.upstream, None)
        return _err(502, "UNAVAILABLE", f"upstream unreachable: {exc}")

    size = job.size
    headers = dict(_API_VERSION)
    headers["docker-content-digest"] = digest
    if head:
        if size:
            headers["content-length"] = str(size)
        return Response(content=b"", headers=headers)

    if size is None:
        # No Content-Length upstream: wait for the ingest rather than guess a
        # length we would then have to lie about.
        await job.done.wait()
        if job.state != "done":
            return _err(502, "UNAVAILABLE", job.error or "ingest failed")
        size = job.final_path.stat().st_size
        metrics.record_docker("MISS", "blob")
        metrics.record_docker_bytes(served=size)
        return serving.file_response(
            job.final_path, size, request.headers.get("range"), headers
        )

    ranges = serving.parse_ranges(request.headers.get("range"), size)
    start, end = (ranges[0] if ranges and len(ranges) == 1 else (0, size - 1))
    status = 206 if ranges and len(ranges) == 1 else 200
    headers["content-length"] = str(end - start + 1)
    headers["accept-ranges"] = "bytes"
    if status == 206:
        headers["content-range"] = f"bytes {start}-{end}/{size}"
    metrics.record_docker("MISS", "blob")
    metrics.record_docker_bytes(served=end - start + 1)
    return StreamingResponse(
        serving.tail_follow(job, job.final_path, size, start, end),
        status_code=status,
        headers=headers,
        media_type="application/octet-stream",
    )


@router.get("/v2/{name:path}/tags/list")
async def tags_list(name: str, request: Request) -> Response:
    if not settings.docker_enabled:
        return _err(404, "UNSUPPORTED", "docker/OCI caching is disabled")
    ref, err = _resolve_or_error(name)
    if err:
        return err
    verdict = policy.check_docker(ref.upstream, ref.repo)
    if not verdict.allowed:
        return _denied(verdict.reason, "tags")
    try:
        r = await registry.get(ref, "tags/list")
    except httpx.HTTPError as exc:
        metrics.record_docker_upstream(ref.upstream, None)
        return _err(502, "UNAVAILABLE", f"upstream unreachable: {exc}")
    metrics.record_docker_upstream(ref.upstream, r.status_code)
    metrics.record_docker("PROXIED", "tags")
    # Proxied, not cached: mutable, rarely on a hot path, and a stale tag list
    # is more confusing than a slow one.
    return Response(
        content=r.content,
        status_code=r.status_code,
        media_type=r.headers.get("content-type", "application/json"),
        headers=dict(_API_VERSION),
    )


@router.get("/v2/{name:path}/referrers/{digest}")
async def referrers(name: str, digest: str) -> Response:
    """Proxy the referrers API (attestations, signatures) upstream, uncached.

    Discovered while watching a real `docker pull`: Docker 29 queries this, and
    without a route for it the request fell through to the Hugging Face
    catch-all. It happened to answer 404, which is a legal response, but it was
    right by accident -- an HF handler should never see a `/v2/` request.

    Proxied rather than answered locally so cosign and friends keep working
    through the cache. Not cached: referrers are mutable by nature.
    """
    if not settings.docker_enabled:
        return _err(404, "UNSUPPORTED", "docker/OCI caching is disabled")
    ref, err = _resolve_or_error(name)
    if err:
        return err
    verdict = policy.check_docker(ref.upstream, ref.repo)
    if not verdict.allowed:
        return _denied(verdict.reason, "referrers")
    try:
        r = await registry.get(ref, f"referrers/{digest}")
    except httpx.HTTPError as exc:
        metrics.record_docker_upstream(ref.upstream, None)
        return _err(502, "UNAVAILABLE", f"upstream unreachable: {exc}")
    metrics.record_docker_upstream(ref.upstream, r.status_code)
    metrics.record_docker("PROXIED", "referrers")
    return Response(
        content=r.content,
        status_code=r.status_code,
        media_type=r.headers.get("content-type", "application/json"),
        headers=dict(_API_VERSION),
    )


@router.api_route("/v2/{rest:path}", methods=["GET", "HEAD"], include_in_schema=False)
async def unknown_v2(rest: str) -> Response:
    """Anything else under /v2/ is not a route we serve.

    This exists so NOTHING under /v2/ can reach the Hugging Face catch-all,
    which owns every other path. Registered after the specific routes, so it
    only catches what they did not.
    """
    return _err(404, "NOT_FOUND", f"/v2/{rest} is not a supported registry endpoint")


@router.api_route(
    "/v2/{rest:path}",
    methods=["POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    include_in_schema=False,
)
async def unsupported(rest: str) -> Response:
    return _err(
        405,
        "UNSUPPORTED",
        "muninn is a pull-through cache: push, delete and cross-repo mount are "
        "not supported. Push to the upstream registry directly.",
    )
