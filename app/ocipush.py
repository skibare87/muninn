"""Push-through: accept a docker push, land it, and forward it upstream (an internal issue).

    docker push muninn/<upstream>/<repo>:<tag>
      -> cached here, and pushed on to <upstream>/<repo>:<tag>

WHY THIS IS WORTH BUILDING, and it is not "Muninn becomes a registry": a
`docker push` does a MONOLITHIC PUT with no chunk-size knob. A registry behind a
body-size-limiting proxy rejects that outright, so pushing to one means knowing
a number the OCI protocol never advertises -- which is why regctl has to be
configured per host and plain docker does not work at all for large layers.
Muninn absorbs that. WHAT THE CLIENT SENDS AND WHAT GOES UPSTREAM ARE DECOUPLED.

BOTH MODES BUFFER TO DISK. That is not an implementation shortcut -- the digest
must be verified before anything is forwarded, and the client's framing bears no
relation to the upstream chunk size. The modes differ ONLY in when the client's
201 is sent:

    proxy          upstream confirmed FIRST, then 201. A 201 means it is really
                   there. Slower, and the honest default.
    store-forward  201 as soon as it is on disk, push after. Faster and
                   retryable, and it TELLS THE CLIENT THE PUSH SUCCEEDED BEFORE
                   IT HAS. CI that pushes and then triggers a pull elsewhere
                   will race it. The mode that can lie is the one you ask for.

PUSH IS OFF BY DEFAULT and, when on, is NOT gated by auth. the maintainer ruled that:
the network is the trust boundary for pull, and making one verb an exception
would be the odd thing. The consequence is real and documented rather than
prevented -- anyone who can reach Muninn can push to anything it holds
credentials for, under Muninn's identity, with no attribution, because a docker
push cannot identify itself.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import uuid as uuidlib
from dataclasses import dataclass, field
from pathlib import Path

from . import ocistore, pushlimits, registry
from .config import settings

log = logging.getLogger("xhc.ocipush")

_sessions: dict[str, Upload] = {}
_pending: dict[str, asyncio.Task] = {}


class PushError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


@dataclass
class Upload:
    """One in-flight blob upload from a client."""

    uuid: str
    ref: registry.Ref
    path: Path
    offset: int = 0
    digest: hashlib._Hash = field(default_factory=hashlib.sha256)

    @property
    def computed(self) -> str:
        return "sha256:" + self.digest.hexdigest()


def _staging(upstream: str) -> Path:
    d = Path(settings.docker_dir) / "_uploads" / upstream.replace("/", "_")
    d.mkdir(parents=True, exist_ok=True)
    return d


def begin(ref: registry.Ref) -> Upload:
    u = str(uuidlib.uuid4())
    up = Upload(uuid=u, ref=ref, path=_staging(ref.upstream) / u)
    up.path.touch()
    _sessions[u] = up
    return up


def get(uuid: str) -> Upload:
    up = _sessions.get(uuid)
    if up is None:
        raise PushError(404, "BLOB_UPLOAD_UNKNOWN", f"no upload session {uuid}")
    return up


def append(up: Upload, chunk: bytes) -> None:
    with up.path.open("ab") as fh:
        fh.write(chunk)
    up.digest.update(chunk)
    up.offset += len(chunk)


def discard(up: Upload) -> None:
    _sessions.pop(up.uuid, None)
    up.path.unlink(missing_ok=True)


async def _blob_exists(ref: registry.Ref, digest: str) -> bool:
    """Skip re-uploading a layer the registry already has.

    This is what makes a second push of a similar image fast, and it is the
    same check a docker client performs before offering a blob.
    """
    try:
        r = await registry.request(ref, "HEAD", f"blobs/{digest}")
    except Exception as exc:  # noqa: BLE001 - unreachable upstream is not "absent"
        log.debug("HEAD %s on %s failed (%s); will upload", digest, ref.upstream, exc)
        return False
    return r.status_code == 200


READ_SIZE = 1024 * 1024


def _chunk_reader(path: Path, start: int, length: int):
    """Body factory for one chunk: a FRESH ASYNC iterator per call.

    Two properties, both learned the hard way.

    ASYNC, because httpx's AsyncClient refuses a sync iterable as a streaming
    body -- "Attempted to send an sync request with an AsyncClient instance".
    The unit test for this factory passed while the real client raised, because
    the test consumed the iterator directly and never went through httpx. Only
    an end-to-end push against a real registry showed it.

    A FRESH one per call, because _authed_request may send twice for the
    bearer/basic dance. A consumed handle on the second attempt would upload
    NOTHING, and a registry accepts that -- leaving a blob that exists and is
    wrong.

    File reads go through a thread so a multi-gigabyte monolithic PUT does not
    block the event loop while every other request waits on it.
    """
    def factory():
        async def stream():
            fh = await asyncio.to_thread(path.open, "rb")
            try:
                await asyncio.to_thread(fh.seek, start)
                left = length
                while left > 0:
                    data = await asyncio.to_thread(fh.read, min(READ_SIZE, left))
                    if not data:
                        break
                    left -= len(data)
                    yield data
            finally:
                await asyncio.to_thread(fh.close)
        return stream()
    return factory


async def push_blob(ref: registry.Ref, path: Path, digest: str) -> None:
    """Upload one blob upstream, chunked according to that registry's policy."""
    if await _blob_exists(ref, digest):
        log.info("%s already has %s; skipping upload", ref.upstream, digest)
        return

    size = path.stat().st_size
    r = await registry.request(ref, "POST", "blobs/uploads/")
    if r.status_code not in (202, 201):
        raise PushError(502, "UNAVAILABLE",
                        f"{ref.upstream} refused an upload session: {r.status_code}")
    location = r.headers.get("location")
    if not location:
        raise PushError(502, "UNAVAILABLE",
                        f"{ref.upstream} returned no upload Location")

    limit = pushlimits.for_upstream(ref.upstream)
    while True:
        try:
            await _upload(ref, location, path, size, digest, limit)
            return
        except _TooLarge as exc:
            # The registry said 413. Halve and retry rather than failing --
            # unconfigured pushes should work, slowly, and tell the operator
            # what to configure. Returns None at the floor.
            nxt = pushlimits.learn(ref.upstream, exc.chunk)
            if nxt is None:
                raise PushError(
                    502, "UNAVAILABLE",
                    f"{ref.upstream} rejected the upload even at the minimum "
                    "chunk size; configure XHC_DOCKER_PUSH_LIMITS or check the "
                    "upstream",
                ) from exc
            limit = pushlimits.Limit(chunk=nxt, threshold=nxt)


class _TooLarge(Exception):
    def __init__(self, chunk: int):
        self.chunk = chunk


async def _upload(ref, location, path, size, digest, limit) -> None:
    if not limit.chunks or size <= limit.threshold:
        r = await registry.request_absolute(
            ref, "PUT", _with_digest(location, digest),
            {"content-type": "application/octet-stream",
             "content-length": str(size)},
            _chunk_reader(path, 0, size),
        )
        if r.status_code == 413:
            raise _TooLarge(0)
        if r.status_code not in (201, 202):
            raise PushError(502, "UNAVAILABLE",
                            f"{ref.upstream} rejected blob {digest}: {r.status_code}")
        return

    offset = 0
    url = location
    while offset < size:
        n = min(limit.chunk, size - offset)
        r = await registry.request_absolute(
            ref, "PATCH", url,
            {"content-type": "application/octet-stream",
             "content-length": str(n),
             "content-range": f"{offset}-{offset + n - 1}"},
            _chunk_reader(path, offset, n),
        )
        if r.status_code == 413:
            raise _TooLarge(limit.chunk)
        if r.status_code not in (202, 204):
            raise PushError(502, "UNAVAILABLE",
                            f"{ref.upstream} rejected a chunk at {offset}: "
                            f"{r.status_code}")
        # The session URL may move between chunks; the registry chooses it.
        url = r.headers.get("location") or url
        offset += n

    r = await registry.request_absolute(ref, "PUT", _with_digest(url, digest),
                                        {"content-length": "0"})
    if r.status_code not in (201, 202):
        raise PushError(502, "UNAVAILABLE",
                        f"{ref.upstream} refused to finalise {digest}: {r.status_code}")


def _with_digest(url: str, digest: str) -> str:
    return f"{url}{'&' if '?' in url else '?'}digest={digest}"


async def finalise_blob(up: Upload, claimed: str) -> None:
    """Verify, forward, and cache. Raises PushError; the caller answers."""
    if up.computed != claimed:
        discard(up)
        raise PushError(400, "DIGEST_INVALID",
                        f"content is {up.computed}, client claimed {claimed}")

    if settings.docker_push_mode == "proxy":
        # Upstream FIRST: the client's 201 must not outrun the registry.
        await push_blob(up.ref, up.path, claimed)
        _land(up, claimed)
        return

    # store-forward: land it, answer, push behind. The blob is the only copy
    # until that task completes, so it is pinned (see _forward_later).
    #
    # cache_on_push=0 composes with this rather than conflicting: the two
    # settings answer different questions. store-forward is WHEN THE CLIENT IS
    # ANSWERED; cache_on_push is WHETHER THE COPY IS KEPT AFTERWARDS. With it
    # off the store is EPHEMERAL -- the blob must survive long enough to be
    # forwarded, and is deleted once upstream confirms.
    _land(up, claimed, pin=True, force_keep=True)
    key = f"{up.ref.upstream}/{claimed}"
    _pending[key] = asyncio.create_task(_forward_later(up.ref, claimed, key))


def _land(up: Upload, digest: str, *, pin: bool = False,
           force_keep: bool = False) -> None:
    dest = ocistore.blob_path(up.ref.upstream, digest)
    if settings.docker_cache_on_push or force_keep:
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(up.path, dest)
        if pin:
            _pinned.add(f"{up.ref.upstream}/{digest}")
    else:
        up.path.unlink(missing_ok=True)
    _sessions.pop(up.uuid, None)


# Blobs that exist ONLY here until their upstream push completes. The evictor
# must not touch these: unlike a cached layer they cannot be re-fetched, so
# evicting one loses data. Only ever populated in store-forward mode, which
# keeps this coupling out of the default path entirely.
_pinned: set[str] = set()


def is_pinned(upstream: str, digest: str) -> bool:
    return f"{upstream}/{digest}" in _pinned


def pending() -> dict[str, str]:
    """What has been accepted but not yet confirmed upstream.

    store-forward owes an answer to "what is pending right now" -- an invisible
    queue is how a push gets silently lost.
    """
    return {k: ("running" if not t.done() else "failed" if t.exception() else "done")
            for k, t in _pending.items()}


async def _forward_later(ref: registry.Ref, digest: str, key: str) -> None:
    """Push behind the client's 201.

    THE PIN IS RELEASED ONLY ON SUCCESS, and that is deliberate. Releasing it
    in a `finally` would unpin a blob whose push FAILED or was CANCELLED at
    shutdown -- content the client was already told was pushed, existing
    nowhere else, and now collectable by the next GC. A failed forward must
    leave the evidence pinned and visible, not tidy it away.
    """
    try:
        await push_blob(ref, ocistore.blob_path(ref.upstream, digest), digest)
    except asyncio.CancelledError:
        log.warning("store-forward push of %s to %s was CANCELLED; it stays "
                    "pinned and pending, and must be retried", digest, ref.upstream)
        raise
    except Exception:
        log.exception("store-forward push of %s to %s FAILED; the client was "
                      "told 201, this blob exists only here, and it stays "
                      "pinned so nothing collects it", digest, ref.upstream)
        raise
    else:
        _pinned.discard(key)
        _pending.pop(key, None)
        if not settings.docker_cache_on_push:
            # Ephemeral store: it existed only to be forwarded, and upstream
            # now has it. Deleting AFTER confirmation is the whole point --
            # deleting before would have left nothing to push.
            ocistore.blob_path(ref.upstream, digest).unlink(missing_ok=True)
            log.debug("store-forward: %s forwarded and dropped "
                      "(XHC_DOCKER_CACHE_ON_PUSH=0)", digest)


async def push_manifest(ref: registry.Ref, body: bytes, media_type: str,
                        reference: str) -> str:
    """Forward a manifest and cache it. Returns its digest."""
    digest = ocistore.compute_digest(body)
    r = await registry.request(
        ref, "PUT", f"manifests/{reference}",
        {"content-type": media_type, "content-length": str(len(body))},
        lambda: body,
    )
    if r.status_code not in (201, 202):
        raise PushError(502, "UNAVAILABLE",
                        f"{ref.upstream} rejected the manifest: {r.status_code}")
    if settings.docker_cache_on_push:
        ocistore.store_manifest(ref.upstream, digest, body, media_type)
        ocistore.write_tag(ref.upstream, ref.repo, reference,
                           ocistore.accept_fingerprint(media_type), digest, media_type)
    return digest
