"""Per-registry blob chunking policy for push-through (an internal issue).

A `docker push` does a MONOLITHIC PUT and has no knob for this. Registries
behind a body-size-limiting proxy reject that, so anyone pushing to one has to
know a number the protocol never advertises -- which is why regctl has to be
hand-configured per host and docker simply does not work against them for large
layers.

Muninn absorbs that: the client pushes normally, and what goes UPSTREAM is
re-chunked according to a per-host policy. The client never learns the number.

THREE SOURCES, FIRST MATCH WINS:

    1. a mounted regctl-format file   XHC_DOCKER_PUSH_LIMITS
    2. a global fallback              XHC_DOCKER_BLOB_CHUNK
    3. adaptive discovery from a 413  (learn() below)

The file uses regctl's format rather than a bespoke one, for the same reason
XHC_REGISTRY_AUTH_FILE takes a `~/.docker/config.json`: anyone who has already
hit this problem has the values in the shape they already wrote them. It is
plain JSON with two integers, so regctl need not be installed.

    {"hosts": {"registry.example.com": {"blobChunk": 16777216,
                                        "blobMax": 16777216}}}

Semantics are regctl's, from `regctl registry set --help`:
    blobMax    blob size before switching to chunked push (-1 disables)
    blobChunk  the size of each chunk

DEFAULT IS NO CHUNKING. The problem is sparse -- of the three registries this
fleet uses, one needs nothing -- so the configuration should be sparse too.

CREDENTIALS IN THIS FILE ARE IGNORED. regctl's format also carries user/pass;
Muninn reads only the two size fields, because a second credential source is a
second thing to get wrong. Mount a limits-only file rather than a real regctl
config; there is no reason to put secrets in a container that does not need them.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from .config import settings

log = logging.getLogger("xhc.pushlimits")

# Floor for adaptive halving. Below this the chunk overhead dominates and a
# registry rejecting 1 MiB is not a size problem -- stop and say so rather than
# retrying forever in ever smaller pieces.
MIN_CHUNK = 1024 * 1024
# Where adaptive discovery starts when a monolithic PUT is refused and nothing
# is configured. Below the smallest ceiling this fleet has met (16 MiB) so the
# first retry is likely to succeed rather than starting another halving chain.
FIRST_GUESS = 16 * 1024 * 1024

_file_limits: dict | None = None
# Learned from a 413 at runtime. Deliberately NOT persisted: a value discovered
# on one deployment is not a fact about the registry, and writing it to disk
# would turn a guess into configuration nobody remembers making.
_learned: dict[str, int] = {}


@dataclass(frozen=True)
class Limit:
    """`chunk` is the piece size; `threshold` is the size above which to chunk."""

    chunk: int
    threshold: int

    @property
    def chunks(self) -> bool:
        return self.chunk > 0 and self.threshold >= 0


NO_CHUNKING = Limit(chunk=0, threshold=-1)


def _load_file() -> dict[str, Limit]:
    global _file_limits  # noqa: PLW0603 - module-level cache
    if _file_limits is not None:
        return _file_limits
    _file_limits = {}
    path = settings.docker_push_limits
    if not path:
        return _file_limits
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        # Deliberately NOT fatal, and deliberately loud. An unreadable limits
        # file means pushes fall back to no chunking, which fails visibly at the
        # registry with a 413 -- it does not silently corrupt anything or widen
        # any access. Refusing to start over a performance hint would be
        # imposing a requirement where a warning does the job.
        log.warning("XHC_DOCKER_PUSH_LIMITS %s unreadable (%s); "
                    "falling back to the global setting", path, exc)
        return _file_limits
    for host, entry in (raw.get("hosts") or {}).items():
        chunk = int(entry.get("blobChunk") or 0)
        # regctl treats a missing blobMax as "chunk anything"; mirror that
        # rather than inventing a different default for the same file format.
        threshold = int(entry.get("blobMax", chunk) or 0)
        if chunk > 0:
            _file_limits[host] = Limit(chunk=chunk, threshold=threshold)
    # No logging here: describe() reports this at boot with the resolved
    # values, and a bare list of hostnames alongside it was two lines saying
    # different amounts of the same thing.
    return _file_limits


def describe() -> None:
    """Log what was actually loaded, at boot, before anything pushes.

    The credential loader already logs "loaded registry credentials for [...]",
    and that line is what let a deployment prove the credentials had loaded and
    then narrow a bug to "loaded but never sent". This had no equivalent, so a
    typo in the path, malformed JSON, a wrong key name and a silently-ignored
    field were all indistinguishable from working.

    THE SECOND HALF MATTERS MORE THAN THE FIRST. Listing what WAS configured is
    easy and half an answer: an upstream someone believes is configured and is
    not looks exactly like one deliberately left unset. So this also states
    what happens to everything absent from the file.
    """
    path = settings.docker_push_limits
    limits = _load_file()

    if path and not limits:
        # _load_file has already warned if it was unreadable; this covers the
        # readable-but-useless cases -- valid JSON with no hosts, or every
        # entry missing blobChunk.
        log.warning("XHC_DOCKER_PUSH_LIMITS=%s loaded NO usable entries. Every "
                    "registry will use the fallback below.", path)
    elif limits:
        log.info("push chunking loaded from %s:", path)
        for host in sorted(limits):
            lim = limits[host]
            log.info("    %-45s chunk=%d bytes (%.0f MiB) threshold=%d",
                     host, lim.chunk, lim.chunk / 1048576, lim.threshold)
    elif not path:
        log.info("XHC_DOCKER_PUSH_LIMITS is unset; no per-registry chunking.")

    if settings.docker_blob_chunk > 0:
        log.info("    any registry NOT listed above uses XHC_DOCKER_BLOB_CHUNK="
                 "%d bytes (%.0f MiB)", settings.docker_blob_chunk,
                 settings.docker_blob_chunk / 1048576)
    else:
        log.info("    any registry NOT listed above: NO CHUNKING -- a monolithic "
                 "PUT. If such a registry sits behind a body-size limit, the "
                 "first large push will 413 and the size will be discovered "
                 "from that and logged here.")


def reset() -> None:
    global _file_limits  # noqa: PLW0603 - module-level cache
    _file_limits = None
    _learned.clear()


def for_upstream(upstream: str) -> Limit:
    """Resolve the policy for one host. First match wins."""
    from_file = _load_file().get(upstream)
    if from_file is not None:
        return from_file
    if upstream in _learned:
        size = _learned[upstream]
        return Limit(chunk=size, threshold=size)
    if settings.docker_blob_chunk > 0:
        return Limit(chunk=settings.docker_blob_chunk,
                     threshold=settings.docker_blob_chunk)
    return NO_CHUNKING


def learn(upstream: str, rejected_chunk: int) -> int | None:
    """A 413 came back. Halve, remember, and tell the operator the real value.

    Returns the new chunk size, or None at the floor.

    This is the difference between a push that FAILS unconfigured and one that
    WORKS unconfigured and tells you how to make it fast. The operator learns
    the number from their own logs rather than by bisecting a production
    registry with large uploads.

    NOT A COMPLETE MECHANISM, and the README says so: it depends on the proxy
    returning a clean 413. Some drop the connection instead, which is
    indistinguishable from a network failure. Those need configuring by hand.
    """
    current = rejected_chunk if rejected_chunk > 0 else settings.docker_blob_chunk
    # current <= 0 means a monolithic PUT was rejected and there is no size to
    # halve from, so start below every ceiling seen in practice rather than
    # guessing upward.
    nxt = FIRST_GUESS if current <= 0 else current // 2
    if nxt < MIN_CHUNK:
        log.error(
            "%s rejected an upload at %d bytes and halving has reached the %d "
            "byte floor. This is unlikely to be a size limit. Configure "
            "XHC_DOCKER_PUSH_LIMITS explicitly or investigate the upstream.",
            upstream, current, MIN_CHUNK,
        )
        return None
    _learned[upstream] = nxt
    log.warning(
        "%s rejected an upload; retrying with %d byte chunks. To avoid the "
        'retry, add to XHC_DOCKER_PUSH_LIMITS: {"hosts": {"%s": '
        '{"blobChunk": %d, "blobMax": %d}}}',
        upstream, nxt, upstream, nxt, nxt,
    )
    return nxt
