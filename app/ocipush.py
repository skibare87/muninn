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
import base64
import errno
import hashlib
import json
import logging
import os
import shutil
import time
import uuid as uuidlib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin

import httpx

from . import ocistore, pushlimits, registry, statedir
from .config import settings
from .shutdown import reraise_if_cancelled

log = logging.getLogger("xhc.ocipush")

_sessions: dict[str, Upload] = {}
_pending: dict[str, asyncio.Task] = {}


# Transport-level failures: the connection broke, timed out, or was refused.
# These say nothing about whether the request was acceptable, so they are
# retryable in a way an HTTP status never is. httpx.TransportError covers
# ReadError, WriteError, ConnectError, PoolTimeout and the timeout family.
RETRYABLE = (httpx.TransportError,)
MAX_ATTEMPTS = 5
BACKOFF_S = (1, 4, 15, 45)


def describe_exc(exc: BaseException) -> str:
    """A reason that is never empty.

    str(httpx.ReadError()) is the EMPTY STRING -- that exception carries no
    message. The management API reported {"state":"failed","error":""}, which
    is worse than an ugly reason: an operator polling it sees a failure with no
    cause and has to go to the container logs for a word. Always lead with the
    type.
    """
    text = str(exc).strip()
    name = f"{type(exc).__module__}.{type(exc).__name__}".removeprefix("builtins.")
    return f"{name}: {text}" if text else name


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
    # THE KEY THAT OPENED THIS SESSION, and the reason it is recorded.
    #
    # A push is four requests: POST to open, PATCH to send, PUT to finish, and
    # the authorisation decision is made on the FIRST one. The later three carry
    # only the session uuid, and the repository they write to comes from this
    # object rather than from their own path -- so without this field, any
    # authenticated caller who learns a uuid can finish someone else's upload
    # into a repository they were never allowed to push to.
    #
    # None means the session was opened with per-key authz off, in which case
    # there is no key to bind to and nothing to compare.
    key_id: str | None = None
    # When a request last touched this session (monotonic), and how many
    # operations are inside it right now. A session is expired only when BOTH
    # say it is dead: idle past XHC_DOCKER_PARTIAL_MAX_AGE and nothing running.
    # `active` is what stops a proxy-mode finalise -- a long upstream push that
    # writes nothing here -- from being expired out from under itself.
    last_active: float = field(default_factory=time.monotonic)
    active: int = 0

    @property
    def computed(self) -> str:
        return "sha256:" + self.digest.hexdigest()


# --- the durable queue: what a pending push needs, and where it lives ---------
#
# A store-forward push that has been answered 201 needs exactly three things to
# be completed by a later process: the OBLIGATION (upstream, api, repo, digest,
# and for a manifest the reference it goes to), the BYTES (a blob's content, a
# manifest's body and media type), and credentials for the upstream. The
# credentials come from configuration (XHC_REGISTRY_AUTH_FILE), not from the
# obligation, so they survive anything configuration survives. Upload sessions
# are deliberately NOT durable: a session that was never finalised was never
# answered 201, and the client retries it.
#
# XHC_STATE_DIR UNSET (the default, unchanged): obligations live in
# `<docker dir>/_pending/` and the bytes live only in the cache tree. The docker
# dir is the durable storage; lose it and pending pushes go with it.
#
# XHC_STATE_DIR SET: the operator has declared the docker dir disposable, so
# everything above lives in `$XHC_STATE_DIR/oci/pending/` until the upstream
# confirms: `<key>.json` (a manifest's body embedded) and `<key>.blob` (a hard
# link to the landed blob when the two share a filesystem, otherwise a verified
# copy). Moving only the .json would not have been a fix -- an obligation to
# forward bytes that no longer exist is still a lost push.

# Kept free on the state volume when a hold has to COPY bytes there. The volume
# also carries pins, orphan marks and possibly the authz DB; a push that fills
# it to the last byte would break the writes that protect everything else.
PENDING_RESERVE_BYTES = 64 * 1024 * 1024

_HELD_SUFFIX = ".blob"
_TMP_SUFFIXES = (".partial", ".linking", ".writing")

# Bytes currently held in the pending directory (json + blobs). None until
# first needed, then maintained in memory: the check and the increment in
# _reserve() run with no await between them, so two concurrent pushes cannot
# both squeeze under the bound. None again means "re-count from disk".
_held: dict[str, int | None] = {"bytes": None}


def _durable() -> bool:
    """True when pending pushes must survive the docker dir (XHC_STATE_DIR set)."""
    return bool(getattr(settings, "state_dir", None))


def _legacy_obligations_dir() -> Path:
    return Path(settings.docker_dir) / "_pending"


def _obligations_dir() -> Path:
    d = statedir.oci_dir() / "pending" if _durable() else _legacy_obligations_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _stem(key: str) -> str:
    return key.replace("/", "_").replace(":", "_")


def _obligation_path(key: str) -> Path:
    return _obligations_dir() / (_stem(key) + ".json")


def _held_path(key: str) -> Path:
    return _obligations_dir() / (_stem(key) + _HELD_SUFFIX)


def _tmp_tag() -> str:
    """Unique per write: two concurrent pushes of the same blob or manifest
    must not share a temp file, or each could rename the other's half-copy."""
    return uuidlib.uuid4().hex[:12]


def _fsync_dir(d: Path) -> None:
    try:
        fd = os.open(d, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _write_durable(path: Path, data: bytes) -> None:
    """Temp, fsync, rename, fsync the directory. A crash leaves the old file or
    the new one, never a truncated obligation that then reads as unreadable."""
    tmp = path.with_name(f"{path.name}.{_tmp_tag()}.writing")
    try:
        with tmp.open("wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    _fsync_dir(path.parent)


def _copy_verified(src: Path, dest: Path, digest: str) -> None:
    """Copy across filesystems, hashing as it goes. The copy becomes `dest`
    only if it hashes to `digest` and has been fsynced; otherwise OSError, and
    nothing is left behind."""
    tmp = dest.with_name(f"{dest.name}.{_tmp_tag()}.partial")
    h = hashlib.sha256()
    try:
        with src.open("rb") as fin, tmp.open("wb") as fout:
            while True:
                data = fin.read(READ_SIZE)
                if not data:
                    break
                h.update(data)
                fout.write(data)
            fout.flush()
            os.fsync(fout.fileno())
        got = "sha256:" + h.hexdigest()
        if got != digest:
            raise OSError(errno.EIO, f"copy of {src} hashes to {got}, expected {digest}")
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    _fsync_dir(dest.parent)


def _link_or_copy(src: Path, dest: Path, digest: str, *, reserve: bool) -> None:
    """Make `dest` a durable copy of `src`: a hard link when the two share a
    filesystem (free, and the same already-verified inode), a verified copy
    when they do not. `reserve` enforces PENDING_RESERVE_BYTES on a copy."""
    tmp = dest.with_name(f"{dest.name}.{_tmp_tag()}.linking")
    try:
        os.link(src, tmp)
    except OSError:
        pass
    else:
        os.replace(tmp, dest)
        _fsync_dir(dest.parent)
        return
    if reserve:
        size = src.stat().st_size
        free = shutil.disk_usage(dest.parent).free
        if free - size < PENDING_RESERVE_BYTES:
            raise OSError(errno.ENOSPC,
                          f"holding {size} bytes would leave {free - size} free on "
                          f"{dest.parent}, under the {PENDING_RESERVE_BYTES}-byte "
                          "reserve kept for the rest of the durable state")
    _copy_verified(src, dest, digest)


def _held_total() -> int:
    if _held["bytes"] is None:
        total = 0
        for p in _obligations_dir().iterdir():
            try:
                total += p.stat().st_size
            except OSError:
                pass
        _held["bytes"] = total
    return _held["bytes"]


def _reserve(n: int) -> None:
    """Claim `n` bytes of the pending area, or refuse the push. Synchronous on
    purpose: see _held."""
    # The cap is a bound on the STATE volume; without one there is nothing
    # separate to protect, and the docker dir's own capacity applies.
    cap = getattr(settings, "docker_push_pending_max_bytes", None) if _durable() else None
    total = _held_total()
    if cap is not None and total + n > cap:
        raise PushError(
            507, "UNAVAILABLE",
            f"store-forward pending area is full: {total} bytes are held awaiting "
            f"their upstream, this push needs {n} more, and "
            f"XHC_DOCKER_PUSH_PENDING_MAX_SIZE is {cap}. Refused rather than "
            "accepted and dropped; retry once pending forwards are delivered "
            "(GET /_cache/docker/pending).")
    _held["bytes"] = total + n


def _release(n: int) -> None:
    if _held["bytes"] is not None:
        _held["bytes"] = max(0, _held["bytes"] - n)


def _refusal(exc: OSError, what: str) -> PushError:
    where = _obligations_dir_name()
    if exc.errno in (errno.ENOSPC, errno.EDQUOT):
        return PushError(
            507, "UNAVAILABLE",
            f"cannot write {what} to {where} ({exc}); the push was NOT accepted. "
            "Free space there"
            + (", or set XHC_DOCKER_PUSH_PENDING_MAX_SIZE so this is refused "
               "before the volume fills." if _durable() else "."))
    return PushError(503, "UNAVAILABLE",
                     f"cannot write {what} to {where} ({exc}); the push was NOT "
                     "accepted")


def _obligations_dir_name() -> str:
    """For messages only: never creates anything, so it cannot fail."""
    if _durable():
        return f"{settings.state_dir}/oci/pending (the state volume)"
    return str(_legacy_obligations_dir())


def _record_obligation(key: str, record: dict) -> None:
    """Write down that we owe an upstream something, BEFORE we owe it -- or
    refuse the push. Raises PushError; the caller answers.

    The queue used to live only in memory. A restart emptied it while the bytes
    stayed on disk, so the client had been told 201, the upstream did not have
    it, the blob was pinned, and THE ONLY RECORD OF THE OBLIGATION WAS GONE. An
    operator polling the queue saw empty -- which is exactly what they see when
    everything succeeded. Empty meant "all delivered" and "we forgot" at once.

    Worse, nothing could reconstruct it: after a restart a blob landed by a
    failed push is byte-identical on disk to one cached from a pull. Both are
    files under <upstream>/blobs/. Without this marker there is no way to tell
    an unmet obligation from an ordinary cache entry, so "just report it at
    startup" was not implementable either.

    This is a minimal durable queue, not a general one: one small file per
    outstanding forward, removed on success.

    STRICT IN BOTH MODES. A failed write used to be logged and the push answered
    201 anyway -- a push the client was told succeeded that would not survive
    the next restart, which is the same silent loss the record exists to
    prevent, one step earlier. Now it is refused: 507 if the volume is full,
    503 otherwise. The client can retry a refusal; it cannot retry a 201.
    """
    data = json.dumps(record).encode()
    grow = 0
    try:
        path = _obligation_path(key)
        grow = max(0, len(data) - (path.stat().st_size if path.exists() else 0))
        _reserve(grow)
        _write_durable(path, data)
    except OSError as exc:
        _release(grow)
        raise _refusal(exc, f"the record of {key}") from exc


def _clear_obligation(key: str) -> None:
    for p in (_obligation_path(key), _held_path(key)):
        try:
            size = p.stat().st_size
        except OSError:
            continue
        p.unlink(missing_ok=True)
        _release(size)


def _migrate_legacy() -> None:
    """XHC_STATE_DIR newly set with pushes already pending: bring them across.

    Obligations MOVE rather than copy. Pins are copied because a stale extra
    copy of a pin is harmless; a stale extra copy of an obligation re-sends a
    manifest to a tag that may have moved on since, if the variable is ever
    unset again. The bytes are held first and the marker written second, so a
    crash part-way leaves the old marker in place and the next boot repeats.

    A failure to hold bytes that ARE present stops the boot (StateDirError) and
    leaves the old marker where it was: continuing would mean running with an
    acknowledged push that the operator believes survives the disk and does
    not. Bytes already MISSING cannot be held; the marker still moves, so the
    loss stays visible as a failed forward instead of vanishing.
    """
    old_dir = _legacy_obligations_dir()
    if not _durable() or not old_dir.is_dir():
        return
    new_dir = _obligations_dir()
    for marker in sorted(old_dir.glob("*.json")):
        dest = new_dir / marker.name
        if not dest.exists():
            raw = marker.read_bytes()
            try:
                rec = json.loads(raw)
            except ValueError:
                rec = None
            try:
                if not isinstance(rec, dict):
                    pass    # unreadable moves byte for byte: unreadable is not absent
                elif rec.get("kind") == "manifest":
                    held = ocistore.load_manifest(rec["upstream"], rec["digest"])
                    if "body_b64" not in rec and held is not None:
                        rec["body_b64"] = base64.b64encode(held.body).decode()
                        rec["media_type"] = held.media_type
                        raw = json.dumps(rec).encode()
                else:
                    key = f"{rec['upstream']}/{rec['digest']}"
                    src = ocistore.blob_path(rec["upstream"], rec["digest"])
                    if src.is_file():
                        _link_or_copy(src, _held_path(key), rec["digest"], reserve=False)
                    else:
                        log.error("migrating the forward of %s: its bytes are already "
                                  "gone from the docker dir; moving the record so the "
                                  "loss stays visible", key)
                _write_durable(dest, raw)
            except OSError as exc:
                raise statedir.StateDirError(
                    f"XHC_STATE_DIR: could not move the pending push {marker} and its "
                    f"bytes to {new_dir}: {exc}. Refusing to start: that push was "
                    "acknowledged to a client and would not survive losing the docker "
                    "dir. The old record is left in place.") from exc
        marker.unlink()
        log.warning("migrated pending push %s -> %s (its bytes are held there until "
                    "the upstream confirms)", marker, dest)


def _sweep_strays() -> None:
    """Held bytes with no obligation are a push that was never acknowledged
    (the hold is written before the record) or one already delivered whose
    cleanup was interrupted. Either way nothing is owed; reclaim the space.

    BOTH LAYOUTS. This used to return at once unless XHC_STATE_DIR was set, so
    the `.writing` temps `_write_durable` leaves in `<docker dir>/_pending` when
    killed between write and rename -- the DEFAULT layout -- were never
    reclaimed by anything. The legacy directory is also swept when the state
    dir IS set: migration moves its markers but not a temp left beside them.

    Called only from recover(), at startup and before any request is served,
    so no write in this process can own a temp yet. A pending area is not
    shared between processes (each would forward every obligation in it), so
    no other writer is assumed either.
    """
    dirs = [_legacy_obligations_dir()]
    if _durable():
        dirs.append(_obligations_dir())
    for d in dirs:
        if not d.is_dir():
            continue
        for p in d.iterdir():
            stray = p.name.endswith(_TMP_SUFFIXES) or (
                p.name.endswith(_HELD_SUFFIX)
                and not (d / (p.name[: -len(_HELD_SUFFIX)] + ".json")).exists())
            if stray:
                log.info("removing %s from %s: nothing is owed for it", p.name, d)
                p.unlink(missing_ok=True)


def recover() -> list[dict]:
    """Re-enqueue forwards this process owes from a previous life.

    Called at startup. Re-pins and re-schedules rather than merely reporting,
    because store-forward's promise to the client was eventual delivery and a
    restart is not a reason to withdraw it.
    """
    _migrate_legacy()
    _sweep_strays()
    _held["bytes"] = None
    out = []
    for marker in sorted(_obligations_dir().glob("*.json")):
        try:
            rec = json.loads(marker.read_text())
        except (OSError, ValueError):
            log.warning("unreadable forward obligation %s; leaving it in place "
                        "rather than discarding an obligation we cannot read",
                        marker.name)
            continue
        out.append(rec)
    if not out:
        return out
    log.warning("recovered %d outstanding forward(s) from a previous run. The "
                "clients that pushed these were told 201 and the upstream does "
                "not have them yet.", len(out))
    for rec in out:
        log.warning("    %s %s -> %s", rec.get("kind"), rec.get("digest"),
                    rec.get("upstream"))
    return out


async def _fail(exc: Exception) -> None:
    raise exc


def _recovered_manifest(rec: dict) -> tuple[bytes, str] | None:
    """The body and media type a manifest obligation owes: embedded in the
    record since obligations became self-contained, from the store for a
    record written before that."""
    if "body_b64" in rec:
        try:
            body = base64.b64decode(rec["body_b64"])
        except ValueError:
            return None
        if ocistore.compute_digest(body) != rec["digest"]:
            return None
        return body, rec.get("media_type") or "application/vnd.oci.image.manifest.v1+json"
    held = ocistore.load_manifest(rec["upstream"], rec["digest"])
    return (held.body, held.media_type) if held is not None else None


async def resume() -> None:
    """Schedule everything recover() found. Separate so it can be tested."""
    for rec in recover():
        _schedule_from_record(rec)


def _schedule_from_record(rec: dict) -> None:
    """(Re)start the forward a durable record describes. Used at startup and by
    retry(), so a retried entry is rebuilt exactly as a restart would rebuild it."""
    ref = registry.Ref(upstream=rec["upstream"], api=rec["api"], repo=rec["repo"])
    key = f"{rec['upstream']}/{rec['digest']}"
    _pinned.add(key)
    if rec.get("kind") != "manifest":
        _schedule_blob(ref, rec["digest"], key)
        return
    got = _recovered_manifest(rec)
    if got is None:
        # Kept, pinned and visible as FAILED rather than cleared. This used to
        # delete the record: an acknowledged push erased with nothing but a log
        # line to say so. Giving up on it is the operator's call
        # (DELETE /_cache/docker/pending).
        log.error("manifest %s was owed to %s but its body is no longer "
                  "available; cannot resume it", rec["digest"], rec["upstream"])
        _meta[key] = {"kind": "manifest", "repo": ref.repo,
                      "reference": rec.get("reference")}
        _pending[key] = asyncio.create_task(_fail(PushError(
            502, "UNAVAILABLE",
            f"manifest {rec['digest']} was acknowledged but its body is "
            "gone; it cannot be delivered")))
        return
    body, media_type = got
    if ocistore.load_manifest(rec["upstream"], rec["digest"]) is None:
        # Back into the cache (the disk was replaced), so a pull resolves the
        # tag here while the forward is still owed.
        try:
            _store_manifest(ref, rec["digest"], body, media_type,
                            rec["reference"], force_keep=True)
        except (OSError, ocistore.DigestMismatch):
            log.exception("could not restore manifest %s to the cache; "
                          "forwarding it anyway", rec["digest"])
    _schedule_manifest(ref, body, media_type, rec["reference"], rec["digest"], key)


# --- upload sessions: in memory, and bounded -----------------------------------
#
# A session is opened by POST and lives until its PUT lands it or fails its
# digest. A client that opens one and never comes back -- a cancelled push, a
# CI runner killed mid-layer, a network that dropped -- used to leave the session
# in `_sessions` FOR THE LIFE OF THE PROCESS and its staging file under
# `_uploads/` forever: nothing expired the one, and the GC walk never looked at
# the other. After a restart the table is empty and the file was simply orphaned.
#
# Now both are reclaimed on the rule the docker GC already uses for a stale
# `.incomplete` download, and on the SAME age, XHC_DOCKER_PARTIAL_MAX_AGE:
#
#   * the session expires once it has been idle that long with nothing running
#     inside it (expire_sessions() from the GC loop, and lazily in get());
#     expiry discards its staging file;
#   * the GC's partial sweep removes a staging file only if no live session in
#     this process owns it (owns_upload), no process holds its lock (held while
#     a chunk is being written), and it has been idle that long.
#
# ONE AGE FOR BOTH IS THE POINT. If a session could outlive the sweep's age, the
# sweep would be entitled to delete the file of a session it cannot see (one in
# another process), and that session's next chunk would land in a file missing
# its beginning. append() also refuses that case structurally: the staging file
# must exist and be exactly `offset` bytes long, or the session is dropped.
#
# WHY SIX HOURS IS NOT TOO LONG: a docker client sends its next PATCH or PUT
# within seconds of the last; nothing in the protocol pauses for hours. The age
# only has to exceed any silence a LIVE session can have, which it does by a
# wide margin, and it bounds an abandoned upload to one GC interval past it.
# And not too short: a session held a little longer costs a hash state and a
# path in memory, while one expired early fails a push that was still going.


def _staging(upstream: str) -> Path:
    d = Path(settings.docker_dir) / "_uploads" / upstream.replace("/", "_")
    d.mkdir(parents=True, exist_ok=True)
    return d


def uploads_root() -> Path:
    return Path(settings.docker_dir) / "_uploads"


def begin(ref: registry.Ref, key_id: str | None = None) -> Upload:
    u = str(uuidlib.uuid4())
    up = Upload(uuid=u, ref=ref, path=_staging(ref.upstream) / u, key_id=key_id)
    up.path.touch()
    _sessions[u] = up
    return up


def owns_upload(path: Path) -> bool:
    """True while a session in THIS process has `path` as its staging file.

    Read from the GC's worker thread; a dict lookup needs no lock. A uuid is
    never reused, so once this answers False for a path it can never answer
    True again -- the sweep cannot race a session back into existence.
    """
    up = _sessions.get(path.name)
    return up is not None and up.path == path


def _expired(up: Upload, now: float) -> bool:
    return up.active == 0 and now - up.last_active >= settings.docker_partial_max_age_s


def _expire(up: Upload, idle: float) -> None:
    log.info("expiring upload session %s for %s/%s: idle %.0fs, past "
             "XHC_DOCKER_PARTIAL_MAX_AGE (%.0fs); %d bytes discarded", up.uuid,
             up.ref.upstream, up.ref.repo, idle, settings.docker_partial_max_age_s,
             up.offset)
    discard(up)


def expire_sessions() -> int:
    """Drop every session idle past XHC_DOCKER_PARTIAL_MAX_AGE, with its
    staging file. Runs in the event loop (the GC loop), where sessions are
    mutated, never from the GC's worker thread. Returns how many expired."""
    now = time.monotonic()
    gone = [up for up in list(_sessions.values()) if _expired(up, now)]
    for up in gone:
        _expire(up, now - up.last_active)
    return len(gone)


def get(uuid: str, key_id: str | None = None) -> Upload:
    """Fetch a session, refusing one opened by a different key.

    The mismatch is reported as BLOB_UPLOAD_UNKNOWN -- the same answer as a uuid
    that does not exist. Distinguishing them would confirm to a caller that
    someone else's session is live, which is the first half of hijacking it.

    A session opened WITHOUT a key (authz off at the time) is not bound, and a
    request carrying a key still gets it: that combination only arises if authz
    was switched on mid-upload, and failing a push in flight is the wrong answer
    to a configuration change.

    An EXPIRED session is BLOB_UPLOAD_UNKNOWN too, whether the GC loop already
    dropped it or this is the first request to notice: the spec's answer for a
    session the registry no longer has, which a docker client handles by
    starting the upload again.
    """
    up = _sessions.get(uuid)
    if up is None:
        raise PushError(404, "BLOB_UPLOAD_UNKNOWN", f"no upload session {uuid}")
    if up.key_id is not None and up.key_id != key_id:
        raise PushError(404, "BLOB_UPLOAD_UNKNOWN", f"no upload session {uuid}")
    now = time.monotonic()
    if _expired(up, now):
        _expire(up, now - up.last_active)
        raise PushError(404, "BLOB_UPLOAD_UNKNOWN",
                        f"upload session {uuid} expired after "
                        f"{settings.docker_partial_max_age_s:.0f}s idle; start it again")
    up.last_active = now
    return up


def _lost(up: Upload, why: str) -> PushError:
    """The staging file is not what this session wrote: drop the session rather
    than append to, or land, bytes that are not the ones it hashed."""
    log.error("upload session %s for %s/%s: %s; dropping the session", up.uuid,
              up.ref.upstream, up.ref.repo, why)
    discard(up)
    return PushError(404, "BLOB_UPLOAD_UNKNOWN",
                     f"upload session {up.uuid} lost its staged data ({why}); "
                     "start it again")


def _check_staging(up: Upload) -> None:
    try:
        size = up.path.stat().st_size
    except FileNotFoundError:
        raise _lost(up, "its staging file is gone") from None
    if size != up.offset:
        raise _lost(up, f"its staging file is {size} bytes, {up.offset} were written")


async def append(up: Upload, chunk: bytes) -> None:
    """Land one chunk from the client.

    The write goes through a thread. It was synchronous inside the event loop,
    which is fine on a fast disk and not fine on a slow one or a large layer:
    a blocked loop produces NO log lines, shows as idle CPU, and stalls every
    OTHER in-flight forward rather than only this one. Measured at 80 MiB in
    0.105s here, so it was invisible -- but the failure mode it creates is a
    stall that looks like it belongs somewhere else entirely.

    Reopening per chunk rather than holding a handle is deliberate: an upload
    session can be abandoned at any point, and a handle held across awaits is a
    file descriptor leaked per abandoned push. The writer's lock
    (ocistore.hold_partial) is therefore held per chunk, while bytes are being
    written; between chunks the in-process session table and the idle age are
    what keep the GC's sweep off the file.

    The file is opened WITHOUT create and must be exactly `offset` bytes long.
    Mode "ab" would silently recreate a file removed underneath the session,
    and the upload would then finish with a correct digest over bytes that were
    never all written -- landed as a blob under a name it does not hash to.
    Raises PushError (BLOB_UPLOAD_UNKNOWN) instead.
    """
    def _write() -> str | None:
        try:
            fd = os.open(up.path, os.O_WRONLY | os.O_APPEND)
        except FileNotFoundError:
            return "its staging file is gone"
        with os.fdopen(fd, "ab") as fh:
            ocistore.hold_partial(fh)
            size = os.fstat(fd).st_size
            if size != up.offset:
                return f"its staging file is {size} bytes, {up.offset} were written"
            fh.write(chunk)
        return None

    up.active += 1
    try:
        why = await asyncio.to_thread(_write)
    finally:
        up.active -= 1
        up.last_active = time.monotonic()
    if why is not None:
        raise _lost(up, why)
    up.digest.update(chunk)
    up.offset += len(chunk)


def discard(up: Upload) -> None:
    _sessions.pop(up.uuid, None)
    up.path.unlink(missing_ok=True)


async def _blob_exists(ref: registry.Ref, digest: str) -> bool:
    """Skip re-uploading a layer the registry already has.

    This is what makes a second push of a similar image fast, and it is the
    same check a docker client performs before offering a blob. It asks the
    TARGET REPOSITORY (`ref.repo`): registries scope blobs per repository.
    """
    try:
        r = await registry.request(ref, "HEAD", f"blobs/{digest}")
    except Exception as exc:  # noqa: BLE001 - unreachable upstream is not "absent"
        log.debug("HEAD %s on %s failed (%s); will upload", digest, ref.upstream, exc)
        return False
    return r.status_code == 200


async def _committed(ref: registry.Ref, digest: str, size: int) -> bool:
    """After a finalise that answered 5xx or broke at the transport: did the
    registry commit the blob anyway?

    Registries can store the blob and THEN fail the response -- a proxy that
    times out the request after the backend finished, or a backend that errors
    while writing its own bookkeeping. Declaring that a failure left a manifest
    waiting forever on a blob the registry already held. Present in the target
    repository with the right size is success; anything else, including a HEAD
    that itself fails, is not.
    """
    try:
        r = await registry.request(ref, "HEAD", f"blobs/{digest}")
    except Exception as exc:  # noqa: BLE001 - could not confirm, so not confirmed
        log.debug("HEAD %s after a failed finalise also failed: %s", digest, exc)
        return False
    if r.status_code != 200:
        return False
    got = r.headers.get("content-length")
    try:
        return got is not None and int(got) == size
    except ValueError:
        return False


READ_SIZE = 1024 * 1024


class _Verify:
    """Hashes bytes as they are SENT, so a local copy that no longer matches its
    digest is never committed upstream.

    The local blob was verified when it landed, but a dependency delivered on a
    manifest's behalf may be a cache entry from a pull months ago, read from a
    disk that has had time to rot. A registry checks the digest on finalise as
    well, but trusting the far end to catch our corruption is not a check.

    `base` is the hash of everything already ACCEPTED upstream. Each send starts
    from a copy of it, because the auth dance may send one body twice; a running
    hash updated by both attempts would hash the chunk twice and be wrong.
    """

    def __init__(self, digest: str):
        self.digest = digest
        self.base = hashlib.sha256()
        self.last: hashlib._Hash | None = None
        self.bad: str | None = None

    def accept(self) -> None:
        """The chunk last sent was accepted: it is now part of the base."""
        if self.last is not None:
            self.base = self.last
            self.last = None

    def matches(self) -> bool:
        return "sha256:" + self.base.hexdigest() == self.digest


def _read_hashed(fh, n: int, h) -> bytes:
    data = fh.read(n)
    if data:
        h.update(data)
    return data


def _chunk_reader(path: Path, start: int, length: int, verify: _Verify | None = None,
                  final: bool = False):
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

    With `verify`, every byte is hashed as it is read. With `final` too (the
    body that completes the blob, i.e. a monolithic PUT), the reader runs one
    read ahead and refuses to yield the LAST piece if the whole does not hash to
    the digest: the request then fails before the registry has the full body,
    so nothing wrong is committed. `verify.bad` records why.
    """
    def factory():
        h = verify.base.copy() if verify is not None else None
        if verify is not None:
            verify.last = h

        async def stream():
            fh = await asyncio.to_thread(path.open, "rb")
            try:
                await asyncio.to_thread(fh.seek, start)
                left = length
                held = b""
                while left > 0:
                    if h is not None:
                        data = await asyncio.to_thread(_read_hashed, fh,
                                                       min(READ_SIZE, left), h)
                    else:
                        data = await asyncio.to_thread(fh.read, min(READ_SIZE, left))
                    if not data:
                        break
                    left -= len(data)
                    if held:
                        yield held
                    held = data
                if final and verify is not None and h is not None:
                    got = "sha256:" + h.hexdigest()
                    if got != verify.digest:
                        verify.bad = (f"the local copy hashes to {got}, not {verify.digest}; "
                                      "refusing to send it")
                        raise PushError(500, "DIGEST_INVALID", verify.bad)
                if held:
                    yield held
            finally:
                await asyncio.to_thread(fh.close)
        return stream()
    return factory


def _stage(t0: float, what: str, key: str = "") -> float:
    """Timestamp a step of the forward, so a stall names itself.

    A forward stalled for 223 seconds with ZERO httpx lines, idle CPU, idle
    disk and a healthy network. Nothing in the logs covered the gap, so the
    only way to localise it was to reverse-engineer the internals -- which is
    exactly what a deployment should never have to do.

    These are DEBUG so they cost nothing normally, and XHC_LOG_LEVEL=DEBUG
    turns the forward path into a timeline. Anything over a second is also
    logged at WARNING regardless of level, because a step that slow is worth
    seeing without having to reproduce it.
    """
    now = time.monotonic()
    elapsed = now - t0
    log.debug("forward %s: %s took %.3fs", key, what, elapsed)
    if elapsed > 1.0:
        log.warning("forward %s: %s took %.1fs -- unexpectedly slow for this step",
                    key, what, elapsed)
    return now


async def _try_mounts(ref: registry.Ref, digest: str,
                      sources: list[str]) -> tuple[str, str | None]:
    """Ask the registry to link `digest` into `ref.repo` from a repository that
    already holds it, rather than sending the bytes again.

    Returns ("mounted", None), or ("session", location) when the registry
    declined the mount and opened an ordinary upload session instead -- the
    spec's answer to a mount it cannot honour, and not an error -- or
    ("none", None) when no mount was attempted or every attempt was refused
    outright. A wrong source therefore costs one request, never the push.
    """
    for src in sources:
        if src == ref.repo:
            continue
        r = await registry.request(ref, "POST",
                                   f"blobs/uploads/?mount={digest}&from={src}")
        if r.status_code == 201:
            log.info("mounted %s into %s/%s from %s", digest, ref.upstream, ref.repo, src)
            return "mounted", None
        if r.status_code == 202 and r.headers.get("location"):
            log.debug("mount of %s from %s declined; uploading through the session "
                      "the registry opened instead", digest, src)
            return "session", r.headers["location"]
        log.debug("mount of %s from %s refused: %s", digest, src, r.status_code)
    return "none", None


async def push_blob(ref: registry.Ref, path: Path, digest: str, *,
                    mount_from: list[str] | tuple[str, ...] = ()) -> str:
    """Put one blob into `ref.repo` upstream, chunked by that registry's policy.

    Returns how: "present" (the repository already had it), "mounted" (linked
    from one of `mount_from`), or "uploaded" (the local bytes, hashed as they
    were sent).
    """
    t = time.monotonic()
    key = f"{ref.upstream}/{digest[:19]}"
    if await _blob_exists(ref, digest):
        log.info("%s/%s already has %s; skipping upload", ref.upstream, ref.repo, digest)
        return "present"
    t = _stage(t, "HEAD blob (does upstream have it)", key)

    size = path.stat().st_size
    session_base = f"{ref.api}/v2/{ref.repo}/blobs/uploads/"
    how, location = await _try_mounts(ref, digest, list(mount_from))
    if how == "mounted":
        return "mounted"
    if location is None:
        r = await registry.request(ref, "POST", "blobs/uploads/")
        if r.status_code not in (202, 201):
            raise PushError(502, "UNAVAILABLE",
                            f"{ref.upstream} refused an upload session: {r.status_code}")
        t = _stage(t, "POST open upload session (incl. any auth retry)", key)
        location = r.headers.get("location")
    if not location:
        raise PushError(502, "UNAVAILABLE",
                        f"{ref.upstream} returned no upload Location")
    # THE OCI SPEC PERMITS A RELATIVE Location, and registries differ: zot
    # returns a relative path, registry:2 an absolute URL. Passing a relative
    # one to httpx raises ValueError out of urllib -- so push worked against
    # every registry that happened to answer absolutely, and failed on the
    # others with a crash rather than an error.
    #
    # This is the SECOND time an upstream's relative URL has broken this
    # module: the first was a Basic realm that was not a URL at all. Same
    # shape, different header. Resolve, never assume.
    location = urljoin(session_base, location)

    limit = pushlimits.for_upstream(ref.upstream)
    log.debug("forward %s: uploading %d bytes, chunking=%s", key, size,
              f"{limit.chunk} over {limit.threshold}" if limit.chunks else "none")
    while True:
        try:
            await _upload(ref, location, path, size, digest, limit)
            _stage(t, "upload body and finalise", key)
            return "uploaded"
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


async def _finalise(ref, digest, size, send, what: str, *, monolithic: bool) -> None:
    """Send the request that commits a blob, and decide whether it did.

    A 5xx, or a connection that broke, does not say the blob is absent: the
    registry may have committed it and failed afterwards. So HEAD it in the
    target repository before declaring failure (see _committed). A 4xx is an
    answer about the request and is reported as it stands.
    """
    try:
        r = await send()
    except RETRYABLE as exc:
        if await _committed(ref, digest, size):
            log.warning("%s of %s to %s/%s broke at the transport (%s), but the "
                        "registry holds it at the right size; treating it as "
                        "committed", what, digest, ref.upstream, ref.repo,
                        describe_exc(exc))
            return
        raise
    if r.status_code == 413 and monolithic:
        raise _TooLarge(0)
    if r.status_code in (201, 202):
        return
    if r.status_code >= 500 and await _committed(ref, digest, size):
        log.warning("%s of %s to %s/%s answered %d, but the registry holds it at "
                    "the right size; treating it as committed", what, digest,
                    ref.upstream, ref.repo, r.status_code)
        return
    raise PushError(502, "UNAVAILABLE",
                    f"{ref.upstream} refused to {what} {digest}: {r.status_code}")


async def _upload(ref, location, path, size, digest, limit) -> None:
    verify = _Verify(digest)
    if not limit.chunks or size <= limit.threshold:
        async def send_whole():
            return await registry.request_absolute(
                ref, "PUT", _with_digest(location, digest),
                {"content-type": "application/octet-stream",
                 "content-length": str(size)},
                _chunk_reader(path, 0, size, verify, final=True),
            )
        try:
            await _finalise(ref, digest, size, send_whole, "accept blob", monolithic=True)
        except Exception:
            if verify.bad is not None:
                raise PushError(500, "DIGEST_INVALID", verify.bad) from None
            raise
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
            _chunk_reader(path, offset, n, verify),
        )
        if r.status_code == 413:
            raise _TooLarge(limit.chunk)
        if r.status_code not in (202, 204):
            raise PushError(502, "UNAVAILABLE",
                            f"{ref.upstream} rejected a chunk at {offset}: "
                            f"{r.status_code}")
        verify.accept()
        # The session URL may move between chunks; the registry chooses it,
        # and may again choose a relative one. Resolve against the URL this
        # response actually came from, not against the original session base --
        # a registry that redirects the session mid-upload would otherwise have
        # each hop resolved against the wrong origin.
        nxt = r.headers.get("location")
        url = urljoin(url, nxt) if nxt else url
        offset += n

    if not verify.matches():
        # Every chunk was accepted but the whole is not the blob: do not ask
        # the registry to commit it under a digest it does not hash to.
        raise PushError(500, "DIGEST_INVALID",
                        f"the local copy of {digest} hashes to "
                        f"sha256:{verify.base.hexdigest()}; refusing to finalise it")

    async def send_finalise():
        return await registry.request_absolute(ref, "PUT", _with_digest(url, digest),
                                               {"content-length": "0"})
    await _finalise(ref, digest, size, send_finalise, "finalise", monolithic=False)


def _with_digest(url: str, digest: str) -> str:
    return f"{url}{'&' if '?' in url else '?'}digest={digest}"


async def finalise_blob(up: Upload, claimed: str) -> None:
    """Verify, forward, and cache. Raises PushError; the caller answers.

    Counted as activity for its whole length, so a proxy-mode upstream push
    that outlasts XHC_DOCKER_PARTIAL_MAX_AGE cannot expire its own session."""
    up.active += 1
    try:
        await _finalise_blob(up, claimed)
    finally:
        up.active -= 1
        up.last_active = time.monotonic()


async def _finalise_blob(up: Upload, claimed: str) -> None:
    _check_staging(up)
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
    key = f"{up.ref.upstream}/{claimed}"
    record = {"kind": "blob", "upstream": up.ref.upstream,
              "api": up.ref.api, "repo": up.ref.repo, "digest": claimed}
    if _durable():
        # Held on the state volume BEFORE the 201, or refused. Everything that
        # can fail happens before _land, so a refusal leaves nothing pinned,
        # recorded or landed.
        try:
            await _hold(up, claimed, key)
            _record_obligation(key, record)
        except PushError:
            discard(up)
            raise
    else:
        # Recorded before the 201 in this mode too, or refused.
        try:
            _record_obligation(key, record)
        except PushError:
            discard(up)
            raise
    _land(up, claimed, pin=True, force_keep=True)
    _schedule_blob(up.ref, claimed, key)


async def _hold(up: Upload, digest: str, key: str) -> None:
    """Put a copy of a verified upload in the pending area, within the bound.

    Content-addressed, so an existing hold for the same key is already these
    bytes and is neither re-copied nor re-counted."""
    held = _held_path(key)
    if held.is_file():
        return
    size = up.path.stat().st_size
    _reserve(size)
    try:
        await asyncio.to_thread(_link_or_copy, up.path, held, digest, reserve=True)
    except OSError as exc:
        _release(size)
        raise _refusal(exc, f"{size} bytes of {digest}") from exc


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


def ensure_forward(ref: registry.Ref, digest: str) -> bool:
    """Make sure an unconfirmed blob is actually scheduled for delivery.

    THE QUIET HALF OF THE PUSH BUG. `docker push` HEADs each blob and skips
    uploading any the registry reports it has. In store-forward the cache
    legitimately answers 200 for a blob it holds -- it has the bytes and will
    forward them -- so the client correctly skips the upload. But if that blob
    arrived from an EARLIER PUSH WHOSE FORWARD FAILED, nothing schedules a new
    attempt: the client uploads nothing, so no forward is enqueued, so the
    queue stays empty and NOTHING LOGS AN ERROR. From here nothing failed,
    because from here nothing was asked.

    That is why the second attempt failed faster and more quietly than the
    first: the first uploaded blobs and enqueued forwards that then failed
    loudly; the second skipped the upload and enqueued nothing at all.

    Returns True if a forward was (re)scheduled. Only ever acts on blobs this
    cache is holding UNCONFIRMED. A blob cached from a pull needs nothing HERE
    -- but note what that does and does not mean: it was fetched from SOME
    repository on that upstream, and registries scope blobs per repository, so
    it is not necessarily in the one now being pushed to. That gap is closed
    where it matters, before the manifest is forwarded: _resolve_dependencies
    HEADs every referenced blob in the target repository and delivers any that
    is missing.
    """
    key = f"{ref.upstream}/{digest}"
    if key not in _pinned:
        return False                       # confirmed, or not ours to deliver
    task = _pending.get(key)
    if task is not None and not task.done():
        return False                       # already in flight
    log.info("re-scheduling the forward of %s to %s: it is held here but not "
             "confirmed upstream, and no attempt is in flight",
             digest, ref.upstream)
    _schedule_blob(ref, digest, key)
    return True


# --- what each pending entry is, and what a manifest still needs ---------------
#
# _meta: kind ("blob" | "manifest"), repo and, for a manifest, the reference.
# _needs: for a manifest, every digest it references that has NOT yet been
#   confirmed present in the target repository upstream, with its state. This
#   is what makes the pending view honest: a manifest that references a blob the
#   client never uploaded through Muninn (it HEADed 200 here and skipped it)
#   used to show nothing outstanding, so an empty view could mean "delivered"
#   for an image the registry did not hold.
# _blocked: manifests parked because a dependency cannot be resolved right now,
#   with the reason. Parked is NOT failed: no delivery attempt was made, so none
#   is counted, and the manifest wakes when its dependencies change.
_meta: dict[str, dict] = {}
_needs: dict[str, dict[str, dict]] = {}
_blocked: dict[str, str] = {}

# Parked manifests re-check on this schedule (seconds) even if nothing wakes
# them, so a blob that appears upstream by other means -- pushed by someone
# else, or committed by a finalise that answered an error -- is noticed.
DEP_RECHECK_S = (30, 60, 120, 300, 600)

# Futures parked manifests wait on; _wake() resolves them all. Futures rather
# than an Event because an Event binds to one loop and this module outlives
# several in tests.
_waiters: set[asyncio.Future] = set()
# Bumped by every _wake(). A manifest compares it across its own check, so a
# wake that lands WHILE it is checking (and so before it parks) is not lost.
_generation = [0]


def _wake() -> None:
    """Something a parked manifest may depend on changed: re-check now."""
    _generation[0] += 1
    for fut in list(_waiters):
        if not fut.done():
            try:
                fut.set_result(None)
            except RuntimeError:        # its loop is gone
                pass
    _waiters.clear()


async def _park(delay: float) -> None:
    fut = asyncio.get_running_loop().create_future()
    _waiters.add(fut)
    try:
        await asyncio.wait({fut}, timeout=delay)
    finally:
        _waiters.discard(fut)


def _schedule_blob(ref: registry.Ref, digest: str, key: str) -> None:
    _pinned.add(key)
    _meta[key] = {"kind": "blob", "repo": ref.repo}
    _attempts.pop(key, None)
    _pending[key] = asyncio.create_task(_forward_later(ref, digest, key))
    _wake()


def _schedule_manifest(ref: registry.Ref, body: bytes, media_type: str,
                       reference: str, digest: str, key: str) -> None:
    _pinned.add(key)
    _meta[key] = {"kind": "manifest", "repo": ref.repo, "reference": reference}
    _needs[key] = {d: {"kind": kind, "state": "unchecked", "detail": None}
                   for d, kind, _ in _dependencies(body)}
    _blocked.pop(key, None)
    _attempts.pop(key, None)
    _pending[key] = asyncio.create_task(_forward_manifest_later(
        ref, body, media_type, reference, digest, key))
    _wake()


def _forget(key: str) -> None:
    _pending.pop(key, None)
    _meta.pop(key, None)
    _needs.pop(key, None)
    _blocked.pop(key, None)


def _task_state(task: asyncio.Task) -> tuple[str, str | None]:
    if not task.done():
        return "running", None
    if task.cancelled():
        return "cancelled", "the forward was cancelled, likely at shutdown"
    if task.exception() is not None:
        return "failed", describe_exc(task.exception())
    return "done", None


def pending() -> list[dict]:
    """What has been accepted but not yet confirmed upstream.

    store-forward owes an answer to "what is pending right now" -- an invisible
    queue is how a push gets silently lost. The failure text is included
    because "failed" without a reason sends the reader to the wrong place.

    A manifest row carries `needs`: every blob (or, for an index, child
    manifest) it references that has not yet been confirmed in the target
    repository upstream -- including ones the client never uploaded through
    Muninn. `blocked` means it is waiting on one of those and has made no
    delivery attempt; it is not a failure and needs no restart.
    """
    out = []
    for key, task in _pending.items():
        upstream, _, digest = key.partition("/")
        state, err = _task_state(task)
        if state == "running" and key in _blocked:
            state, err = "blocked", _blocked[key]
        attempts = _attempts.get(key, 0)
        if state == "running" and attempts > 1:
            state = "retrying"
        meta = _meta.get(key, {})
        row = {"upstream": upstream, "digest": digest, "state": state,
               "error": err, "pinned": key in _pinned,
               "attempts": attempts, "max_attempts": MAX_ATTEMPTS,
               "kind": meta.get("kind", "blob")}
        if meta.get("repo"):
            row["repo"] = meta["repo"]
        if meta.get("kind") == "manifest":
            row["reference"] = meta.get("reference")
            row["needs"] = [{"digest": d, **info}
                            for d, info in sorted(_needs.get(key, {}).items())]
        out.append(row)
    return sorted(out, key=lambda x: (x["state"], x["digest"]))


def abandon(upstream: str, digest: str) -> dict:
    """Give up on a forward that will not succeed, and let GC have the blob.

    A pinned blob is pinned because it may be the ONLY copy -- so releasing one
    is a deletion decision, not housekeeping, and it is deliberately explicit
    rather than something a sweep decides on its own.

    Refuses while the forward is still running: abandoning a push that is
    mid-flight would race the upload and could unpin content that is about to
    be needed. Stop it by other means first if that is really what you want.
    """
    key = f"{upstream}/{digest}"
    task = _pending.get(key)
    if task is None and key not in _pinned:
        raise PushError(404, "NOT_PENDING", f"{key} is not a pending push")
    if task is not None and not task.done():
        if key not in _blocked:
            raise PushError(409, "STILL_RUNNING",
                            f"{key} is still forwarding; refusing to unpin content "
                            "whose upload is in flight")
        # A parked manifest is waiting, not sending: nothing is in flight to race.
        task.cancel()
    _pinned.discard(key)
    _forget(key)
    # Drop the durable marker too, or an abandoned forward comes back from the
    # dead at the next restart -- the operator's decision must outlive the
    # process just as the obligation does.
    _clear_obligation(key)
    log.warning("ABANDONED the forward of %s to %s. It is no longer pinned and "
                "GC may now collect it. The client that pushed it was told 201.",
                digest, upstream)
    return {"abandoned": key, "note": "unpinned; the next sweep may collect it "
            "if no manifest references it"}


def _read_obligation(key: str) -> dict | None:
    try:
        rec = json.loads(_obligation_path(key).read_text())
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) else None


def _on_disk_keys() -> list[str]:
    out = []
    for marker in sorted(_obligations_dir().glob("*.json")):
        try:
            rec = json.loads(marker.read_text())
            out.append(f"{rec['upstream']}/{rec['digest']}")
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return out


def retry(upstream: str | None = None, digest: str | None = None) -> dict:
    """Retry one pending forward, or every failed one, without a restart.

    Before this the only ways to make a failed forward try again were a process
    restart (which re-sends everything) or ABANDON (which gives up). Neither is
    an answer to "the registry is back, send it now".

    Per entry:
      - failed or cancelled: rescheduled from its DURABLE RECORD -- the same
        record a restart resumes from, so this is exactly what a restart would
        do for that one entry, with a fresh attempt budget;
      - a manifest parked on a dependency: woken to re-check now (`rechecked`);
      - running: left alone (one entry asked for by name is a 409);
      - a record on disk with no task in this process: scheduled.

    Synchronous and bounded by the number of entries: it only creates tasks.
    The tasks are the ordinary forwards, bounded by MAX_ATTEMPTS, and a parked
    manifest's loop re-raises a cancel (shutdown.reraise_if_cancelled).
    """
    if (upstream is None) != (digest is None):
        raise PushError(400, "INVALID",
                        "give both upstream and digest to retry one entry, or "
                        "neither to retry every failed one")
    if upstream is not None:
        keys, single = [f"{upstream}/{digest}"], True
    else:
        keys, single = sorted(set(_pending) | set(_on_disk_keys())), False

    scheduled, rechecked, running, unreadable = [], [], [], []
    # Blobs before manifests, so a manifest scheduled here finds the forwards
    # it depends on already in flight.
    order = sorted(keys, key=lambda k: _meta.get(k, {}).get("kind") == "manifest")
    for key in order:
        task = _pending.get(key)
        if task is not None and not task.done():
            if key in _blocked:
                rechecked.append(key)
                continue
            if single:
                raise PushError(409, "STILL_RUNNING",
                                f"{key} is forwarding now; nothing to retry")
            running.append(key)
            continue
        if task is not None and _task_state(task)[0] == "done":
            continue
        rec = _read_obligation(key)
        if rec is None:
            if task is None:
                if single:
                    raise PushError(404, "NOT_PENDING", f"{key} is not a pending push")
                continue
            # Known in memory, but its record is gone or unreadable: there is
            # nothing to rebuild the forward from. Say so rather than skip it.
            unreadable.append(key)
            continue
        _schedule_from_record(rec)
        scheduled.append(key)
    if rechecked:
        _wake()
    if scheduled or rechecked:
        log.warning("pending retry: scheduled %d, re-checking %d parked manifest(s)",
                    len(scheduled), len(rechecked))
    out = {"scheduled": scheduled, "rechecked": rechecked}
    if not single:
        out["running"] = running
    if unreadable:
        out["unreadable"] = unreadable
    return out


async def _with_retries(make_coro, *, what: str, key: str | None):
    """Retry TRANSPORT failures; never retry an answer.

    A 401 already retried -- the auth dance re-sends after a challenge -- so a
    dropped connection failing permanently while an auth rejection recovered
    was exactly backwards: the transport error is the one that says nothing
    about whether the request was acceptable.

    An HTTP status is an ANSWER and is not retried here. A registry that says
    400 will say 400 again, and retrying it burns the upstream and delays the
    operator learning something true.
    """
    last: BaseException | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if key is not None:
            _attempts[key] = attempt
        try:
            return await make_coro()
        except RETRYABLE as exc:
            last = exc
            if attempt == MAX_ATTEMPTS:
                break
            delay = BACKOFF_S[min(attempt - 1, len(BACKOFF_S) - 1)]
            log.warning("forward of %s failed with %s (attempt %d/%d); "
                        "retrying in %ds", what, describe_exc(exc), attempt,
                        MAX_ATTEMPTS, delay)
            await asyncio.sleep(delay)
    log.error("forward of %s gave up after %d attempts: %s",
              what, MAX_ATTEMPTS, describe_exc(last))
    raise last


# Attempts made per pending key, so the pending view can distinguish "still
# trying" from "given up" rather than showing both as failed.
_attempts: dict[str, int] = {}


async def _forward_later(ref: registry.Ref, digest: str, key: str) -> None:
    """Push behind the client's 201.

    THE PIN IS RELEASED ONLY ON SUCCESS, and that is deliberate. Releasing it
    in a `finally` would unpin a blob whose push FAILED or was CANCELLED at
    shutdown -- content the client was already told was pushed, existing
    nowhere else, and now collectable by the next GC. A failed forward must
    leave the evidence pinned and visible, not tidy it away.
    """
    src = ocistore.blob_path(ref.upstream, digest)
    held = _held_path(key) if _durable() else None
    if held is not None and held.is_file():
        # The held copy is what survives the disk, so it is what gets sent.
        # If the cache copy went with a replaced disk, put it back too, so a
        # pull (and the HEAD that ensure_forward relies on) finds it here
        # while delivery is still owed. Best effort: forwarding does not need it.
        if not src.is_file():
            try:
                src.parent.mkdir(parents=True, exist_ok=True)
                await asyncio.to_thread(_link_or_copy, held, src, digest,
                                        reserve=False)
            except OSError:
                log.exception("could not restore %s to the cache from the pending "
                              "area; forwarding from the held copy anyway", digest)
        src = held
    try:
        await _with_retries(
            lambda: push_blob(ref, src, digest),
            what=f"blob {digest} to {ref.upstream}", key=key)
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
        _forget(key)
        _clear_obligation(key)
        note_source(ref, digest)
        # A manifest parked on this blob can go now.
        _wake()
        if not settings.docker_cache_on_push:
            # Ephemeral store: it existed only to be forwarded, and upstream
            # now has it. Deleting AFTER confirmation is the whole point --
            # deleting before would have left nothing to push.
            ocistore.blob_path(ref.upstream, digest).unlink(missing_ok=True)
            log.debug("store-forward: %s forwarded and dropped "
                      "(XHC_DOCKER_CACHE_ON_PUSH=0)", digest)


def referenced_digests(body: bytes) -> list[str]:
    """Every blob digest a manifest points at: config plus layers.

    A manifest is only valid once its blobs are upstream, so this is the
    ordering constraint for a deferred forward. An index or manifest list
    points at other manifests rather than blobs; those have their own
    dependencies and are not chased here.
    """
    return [d for d, kind, _ in _dependencies(body) if kind == "blob"]


def _dependencies(body: bytes) -> list[tuple[str, str, str | None]]:
    """(digest, "blob" | "manifest", media type) for everything a manifest
    needs in its repository before it may be PUT there: the config and layers
    of an image manifest, the child manifests of an index or manifest list."""
    try:
        doc = json.loads(body)
    except ValueError:
        return []
    if not isinstance(doc, dict):
        return []
    out: list[tuple[str, str, str | None]] = []
    seen: set[str] = set()

    def add(entry, kind):
        if not isinstance(entry, dict):
            return
        d = entry.get("digest")
        if isinstance(d, str) and ocistore.DIGEST_RE.match(d) and d not in seen:
            seen.add(d)
            out.append((d, kind, entry.get("mediaType")))

    add(doc.get("config"), "blob")
    for layer in doc.get("layers") or []:
        add(layer, "blob")
    for child in doc.get("manifests") or []:
        add(child, "manifest")
    return out


# --- where a blob can be MOUNTED from -----------------------------------------
#
# A cross-repository mount links a blob the registry already holds into another
# repository without sending the bytes. Muninn knows a source two ways:
#   - in memory, from what it has seen this process: a pull that fetched the
#     blob from repository R, a forward that delivered it to R, a HEAD that
#     found it in R. Bounded, newest kept;
#   - durably, from tag mappings the upstream was SEEN to hold
#     (ocistore.iter_observed_tags): if R's observed tag resolves, through the
#     manifests held here, to a manifest referencing the blob, R had it.
# Either can be stale -- a repository can be deleted -- and that is harmless: a
# mount the registry cannot honour is answered with an ordinary upload session,
# which is then used. A source is a hint; the HEAD before the manifest PUT is
# the check.
_SOURCES_MAX = 10_000
_SOURCES_PER_BLOB = 4
_sources: dict[str, list[str]] = {}


def note_source(ref: registry.Ref, digest: str) -> None:
    """`ref.repo` on `ref.upstream` holds `digest`."""
    key = f"{ref.upstream}/{digest}"
    repos = _sources.pop(key, [])
    if ref.repo in repos:
        repos.remove(ref.repo)
    repos.append(ref.repo)
    _sources[key] = repos[-_SOURCES_PER_BLOB:]
    while len(_sources) > _SOURCES_MAX:
        _sources.pop(next(iter(_sources)))


def _manifest_closure_has(upstream: str, digest: str, want: str, depth: int = 0) -> bool:
    held = ocistore.load_manifest(upstream, digest)
    if held is None or depth > 2:
        return False
    for d, kind, _ in _dependencies(held.body):
        if d == want:
            return True
        if kind == "manifest" and _manifest_closure_has(upstream, d, want, depth + 1):
            return True
    return False


def _mount_sources(upstream: str, digest: str, target_repo: str) -> list[str]:
    """Repositories on `upstream` believed to hold `digest`, newest first.
    Blocking (it may read tag files): call it in a thread."""
    out = [r for r in reversed(_sources.get(f"{upstream}/{digest}", []))
           if r != target_repo]
    if out:
        return out
    for entry in ocistore.iter_observed_tags():
        if entry.get("upstream") != upstream or entry.get("repo") == target_repo:
            continue
        if entry["repo"] in out:
            continue
        if _manifest_closure_has(upstream, entry["digest"], digest):
            out.append(entry["repo"])
            if len(out) >= _SOURCES_PER_BLOB:
                break
    return out


def _local_blob(upstream: str, digest: str) -> Path | None:
    """The bytes of `digest` held here, from the cache or the pending area."""
    p = ocistore.blob_path(upstream, digest)
    if p.is_file():
        return p
    held = _held_path(f"{upstream}/{digest}")
    return held if _durable() and held.is_file() else None


class _Unconfirmed(Exception):
    """The upstream did not say yes or no (an auth failure, a 5xx, a dropped
    connection). Not "absent": nothing is delivered on the strength of it."""


_MANIFEST_ACCEPT = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])


async def _present(ref: registry.Ref, digest: str, kind: str,
                   media_type: str | None) -> bool:
    """Is `digest` in `ref.repo` upstream? True, False, or _Unconfirmed."""
    if kind == "blob":
        path, headers = f"blobs/{digest}", None
    else:
        accept = f"{media_type}, {_MANIFEST_ACCEPT}" if media_type else _MANIFEST_ACCEPT
        path, headers = f"manifests/{digest}", {"accept": accept}
    try:
        r = await registry.request(ref, "HEAD", path, headers)
    except RETRYABLE as exc:
        raise _Unconfirmed(f"upstream unreachable: {describe_exc(exc)}") from exc
    if r.status_code == 200:
        return True
    if r.status_code == 404:
        return False
    raise _Unconfirmed(f"HEAD answered {r.status_code}")


async def push_manifest(ref: registry.Ref, body: bytes, media_type: str,
                        reference: str) -> str:
    """Deliver a manifest, synchronously or deferred depending on the mode.

    STORE-FORWARD IS EVENTUALLY CONSISTENT, NOT IMMEDIATELY CONSISTENT. This
    path did not honour that: it forwarded synchronously and handed the client
    the upstream's status, which is proxy behaviour. The tell is that a client
    saw the registry's own 400 -- a deferred forward cannot return an upstream
    status, so receiving one is proof no deferral happened.

    It also forwarded a manifest while the blobs it references were still in
    flight, which the registry correctly rejects: a manifest is only valid once
    its blobs are there. So the deferred forward must be ORDERED AFTER them.
    """
    digest = ocistore.compute_digest(body)

    if settings.docker_push_mode == "proxy":
        # The same guarantee as the deferred path, inline: nothing is PUT while
        # the target repository is missing something the manifest references.
        blocking = await _resolve_dependencies(ref, body, None, set())
        if blocking:
            raise PushError(
                502, "UNAVAILABLE",
                f"not forwarding the manifest: {ref.upstream}/{ref.repo} does not "
                "hold everything it references and Muninn could not deliver it: "
                + "; ".join(blocking))
        r = await registry.request(
            ref, "PUT", f"manifests/{reference}",
            {"content-type": media_type, "content-length": str(len(body))},
            lambda: body,
        )
        if r.status_code not in (201, 202):
            raise PushError(502, "UNAVAILABLE",
                            f"{ref.upstream} rejected the manifest: {r.status_code}")
        # The registry accepted it: a mapping the upstream holds, as observed as
        # one a pull reads back. Store-forward below has not been accepted yet.
        _store_manifest(ref, digest, body, media_type, reference, observed=True)
        return digest

    # store-forward: land it, answer, deliver behind -- after its blobs.
    #
    # The body travels IN the obligation, so the record is self-contained: a
    # manifest owed upstream no longer depends on the cache tree still holding
    # it, which is the half of a pending push that a replaced disk took away.
    key = f"{ref.upstream}/{digest}"
    record = {"kind": "manifest", "upstream": ref.upstream, "api": ref.api,
              "repo": ref.repo, "digest": digest, "reference": reference,
              "media_type": media_type,
              "body_b64": base64.b64encode(body).decode()}
    _record_obligation(key, record)     # refused before any 201, in both modes
    _store_manifest(ref, digest, body, media_type, reference, force_keep=True)
    _schedule_manifest(ref, body, media_type, reference, digest, key)
    return digest


def _store_manifest(ref, digest, body, media_type, reference, *,
                    force_keep: bool = False, observed: bool = False) -> None:
    if not (settings.docker_cache_on_push or force_keep):
        return
    ocistore.store_manifest(ref.upstream, digest, body, media_type)
    ocistore.write_tag(ref.upstream, ref.repo, reference,
                       ocistore.accept_fingerprint(media_type), digest, media_type,
                       observed=observed and not ocistore.DIGEST_RE.match(reference))


async def _deliver_blob(ref: registry.Ref, digest: str) -> tuple[bool, str | None]:
    """Put a blob Muninn holds into the target repository, for a manifest that
    needs it there. Mount first when a source repository is known; upload the
    local bytes otherwise (push_blob does both, and hashes what it sends).

    Raises PushError when the blob can never be delivered from here: not held,
    or held but no longer matching its digest. Returns (False, why) for a
    failure that a later attempt may not repeat.
    """
    src = _local_blob(ref.upstream, digest)
    if src is None:
        raise PushError(400, "MANIFEST_BLOB_UNKNOWN",
                        f"blob {digest} is not in {ref.upstream}/{ref.repo} and "
                        "Muninn does not hold it, so the manifest cannot be "
                        "forwarded: the registry would hold an image missing "
                        "that blob")
    sources = await asyncio.to_thread(_mount_sources, ref.upstream, digest, ref.repo)
    try:
        how = await _with_retries(
            lambda: push_blob(ref, src, digest, mount_from=sources),
            what=f"blob {digest} to {ref.upstream}/{ref.repo} (needed by a manifest)",
            key=None)
    except asyncio.CancelledError:
        raise
    except PushError as exc:
        if exc.code == "DIGEST_INVALID":
            raise
        return False, f"delivering it failed: {exc.message}"
    except Exception as exc:  # noqa: BLE001 - reported on the manifest, not raised
        return False, f"delivering it failed: {describe_exc(exc)}"
    log.warning("%s %s into %s/%s for a manifest that references it: the client "
                "skipped it because this cache held it, but that repository did "
                "not", how, digest, ref.upstream, ref.repo)
    try:
        if await _present(ref, digest, "blob", None):
            note_source(ref, digest)
            return True, None
    except _Unconfirmed as exc:
        return False, f"{how}, but not confirmed: {exc}"
    return False, f"{how}, but a HEAD in {ref.repo} still does not find it"


async def _deliver_manifest(ref: registry.Ref, digest: str, media_type: str | None,
                            key: str | None, confirmed: set[str],
                            depth: int) -> tuple[bool, str | None]:
    """Put a child manifest Muninn holds into the target repository, after its
    own dependencies. Same contract as _deliver_blob."""
    held = ocistore.load_manifest(ref.upstream, digest)
    if held is None:
        raise PushError(400, "MANIFEST_BLOB_UNKNOWN",
                        f"manifest {digest} is not in {ref.upstream}/{ref.repo} and "
                        "Muninn does not hold it, so the index cannot be forwarded")
    if depth >= 2:
        return False, "nested too deeply to deliver on the parent's behalf"
    sub = await _resolve_dependencies(ref, held.body, key, confirmed, depth + 1)
    if sub:
        return False, "its own dependencies are not all upstream yet"
    mt = held.media_type or media_type or "application/vnd.oci.image.manifest.v1+json"
    try:
        r = await _with_retries(
            lambda: registry.request(
                ref, "PUT", f"manifests/{digest}",
                {"content-type": mt, "content-length": str(len(held.body))},
                lambda: held.body),
            what=f"manifest {digest} to {ref.upstream}/{ref.repo} (needed by an index)",
            key=None)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - reported on the parent
        return False, f"delivering it failed: {describe_exc(exc)}"
    if r.status_code not in (201, 202):
        return False, f"{ref.upstream} answered {r.status_code} to its PUT"
    try:
        if await _present(ref, digest, "manifest", mt):
            return True, None
    except _Unconfirmed as exc:
        return False, f"PUT accepted, but not confirmed: {exc}"
    return False, f"PUT accepted, but a HEAD in {ref.repo} does not find it"


async def _resolve_dependencies(ref: registry.Ref, body: bytes, key: str | None,
                                confirmed: set[str], depth: int = 0) -> list[str]:
    """Make everything `body` references present in `ref.repo` upstream, or say
    why not.

    REGISTRIES SCOPE BLOBS PER REPOSITORY. A blob pulled through this cache from
    repository A is held here and answered 200 to a pusher's HEAD for
    repository B, so the pusher skips the upload -- correctly, as far as it can
    tell -- and nothing ever sends that blob to B. Waiting only on the forwards
    Muninn happens to be running cannot see that, and a registry that accepts a
    manifest without verifying its blobs then holds an image missing a layer.

    So every dependency is HEADed IN THE TARGET REPOSITORY, after any forward of
    it in flight here has finished. One that is missing is delivered from here
    if Muninn holds it (mount, else upload) and HEADed again.

    Returns the dependencies still blocking, as reasons (empty: all present,
    each confirmed by a HEAD in the target repository). Raises PushError when a
    dependency can never be supplied from here (not held, or held corrupt).
    A dependency whose OWN forward failed is blocking, not fatal: it may be
    retried, and the manifest must not be failed for it.
    """
    deps = [x for x in _dependencies(body) if x[0] not in confirmed]
    needs = _needs.setdefault(key, {}) if key is not None else {}

    def mark(d: str, kind: str, state: str, detail: str | None = None) -> None:
        if state == "present":
            needs.pop(d, None)
        else:
            needs[d] = {"kind": kind, "state": state, "detail": detail}

    inflight = []
    for d, kind, _ in deps:
        dkey = f"{ref.upstream}/{d}"
        t = _pending.get(dkey)
        if t is not None and not t.done() and dkey != key and dkey not in _blocked:
            mark(d, kind, "forwarding")
            inflight.append(t)
    if inflight:
        log.info("manifest for %s/%s waits on %d forward(s) in flight",
                 ref.upstream, ref.repo, len(inflight))
        # wait(), not gather(): cancelling this manifest must not cancel the
        # forwards it is waiting on, and one failing must not end the wait.
        await asyncio.wait(inflight)

    blocking: list[str] = []
    for d, kind, media in deps:
        reraise_if_cancelled()
        if d in confirmed:          # delivered on the way, as a child's dependency
            continue
        dkey = f"{ref.upstream}/{d}"
        try:
            if await _present(ref, d, kind, media):
                confirmed.add(d)
                mark(d, kind, "present")
                if kind == "blob":
                    note_source(ref, d)
                continue
        except _Unconfirmed as exc:
            mark(d, kind, "unconfirmed", str(exc))
            blocking.append(f"{d}: {exc}")
            continue
        task = _pending.get(dkey)
        if task is not None and dkey != key:
            state, err = _task_state(task)
            if state != "done":
                if dkey in _blocked:
                    why = f"its own forward is blocked ({_blocked[dkey]})"
                elif state == "running":
                    why = "its own forward is still running"
                else:
                    why = f"its own forward {state}: {err}"
                mark(d, kind, "blocked", why)
                blocking.append(f"{d}: {why}")
                continue
        mark(d, kind, "delivering")
        try:
            if kind == "blob":
                ok, why = await _deliver_blob(ref, d)
            else:
                ok, why = await _deliver_manifest(ref, d, media, key, confirmed, depth)
        except PushError as exc:
            mark(d, kind, "missing", exc.message)
            raise
        if ok:
            confirmed.add(d)
            mark(d, kind, "present")
        else:
            mark(d, kind, "blocked", why)
            blocking.append(f"{d}: {why}")
    return blocking


async def _forward_manifest_later(ref, body, media_type, reference, digest, key):
    """Make this manifest's dependencies present upstream, then deliver it.

    NEVER PUT WHILE ANYTHING IT REFERENCES IS MISSING FROM THE TARGET
    REPOSITORY (see _resolve_dependencies). A dependency that cannot be
    resolved right now PARKS the manifest rather than failing it: a dependency
    failure used to mark the manifest failed with no attempt counted and none
    scheduled, so it stayed failed until a restart even after its blob was
    delivered. Parked, it re-checks when a forward completes or is retried, and
    on a slow timer, and no attempt is counted until a PUT is actually tried.

    Released on success only, for the same reason as a blob: a manifest whose
    forward failed is the record that the client was told 201 and the registry
    never got it. Tidying it away turns a visible failure into a silent one.
    """
    try:
        confirmed: set[str] = set()
        rechecks = 0
        while True:
            reraise_if_cancelled()
            gen = _generation[0]
            blocking = await _resolve_dependencies(ref, body, key, confirmed)
            if not blocking:
                _blocked.pop(key, None)
                break
            _blocked[key] = "waiting on " + "; ".join(blocking)
            if _generation[0] != gen:
                continue        # something changed while checking: look again now
            delay = DEP_RECHECK_S[min(rechecks, len(DEP_RECHECK_S) - 1)]
            rechecks += 1
            log.warning("manifest %s to %s/%s is BLOCKED, not failed: %s. No "
                        "delivery attempted; re-checking when a dependency changes "
                        "or in %ds", reference, ref.upstream, ref.repo,
                        _blocked[key], delay)
            await _park(delay)

        async def deliver():
            r = await registry.request(
                ref, "PUT", f"manifests/{reference}",
                {"content-type": media_type, "content-length": str(len(body))},
                lambda: body,
            )
            if r.status_code not in (201, 202):
                raise PushError(
                    502, "UNAVAILABLE",
                    f"{ref.upstream} rejected the manifest: {r.status_code}")

        await _with_retries(deliver,
                            what=f"manifest {reference} to {ref.upstream}", key=key)
    except asyncio.CancelledError:
        log.warning("deferred manifest forward of %s to %s CANCELLED; it stays "
                    "pinned and pending", reference, ref.upstream)
        raise
    except Exception:
        log.exception("deferred manifest forward of %s to %s FAILED; the client "
                      "was told 201 and the registry does not have it",
                      reference, ref.upstream)
        raise
    else:
        _pinned.discard(key)
        _forget(key)
        _clear_obligation(key)
        _wake()                 # an index parked on this manifest can go now
    finally:
        _blocked.pop(key, None)


def reset() -> None:
    """Forget every piece of in-process push state, as a process restart would;
    nothing on disk is touched. One list, here beside the state it clears, so a
    new module-level table cannot be left out of it -- tests that cleared tables
    by hand missed the loop-bound `_waiters` and leaked futures across event loops.
    """
    for table in (_sessions, _pending, _pinned, _meta, _needs, _blocked,
                  _waiters, _attempts, _sources):
        table.clear()
    _held["bytes"] = None
