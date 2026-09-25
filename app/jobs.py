"""Ingest jobs: the WAN leg.

Everything here runs `huggingface_hub` with Xet *enabled*, which is the whole
point of the design. The container fetches from HF with full parallel range-GET
fan-out, then serves the result to LAN clients over plain HTTP. Conflating those
two legs into a single reverse-proxy stream is what caps you at single-stream
throughput.

Requests are coalesced (single-flight) per (repo_type, repo_id, revision,
filename). When forty nodes ask for the same 140GB blob at once, exactly one
upstream fetch happens.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from huggingface_hub import HfApi, hf_hub_download, snapshot_download

from . import build, cachefs, manifests, metrics, statedir, tier
from .config import settings

log = logging.getLogger("xhc.jobs")

try:  # the threshold snapshot_download uses to distrust `siblings`
    from huggingface_hub._snapshot_download import (
        VERY_LARGE_REPO_THRESHOLD as _VERY_LARGE_REPO_THRESHOLD,
    )
except ImportError:  # pragma: no cover - private name, keep a sane default
    _VERY_LARGE_REPO_THRESHOLD = 50_000

# The Hub returns a sha256 as the ETag for LFS files and a git object id for
# everything else. huggingface_hub uses exactly this test to decide whether an
# ETag is a content hash (file_download.REGEX_SHA256) and does the same
# comparison itself -- but only in local_dir mode, which this cache does not
# use. So this is not a guarantee invented here; it is one the library already
# implements on a path we do not take.
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# A non-LFS file's ETag is its git blob id: sha1(b"blob <size>\0" + content).
# Measured against the Hub on real repos before relying on it.
_GIT_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")


class IngestDigestMismatch(Exception):
    """Ingested bytes do not hash to the ETag they were stored under.

    Named to sit beside ocistore.DigestMismatch: the two protocols now refuse
    the same way, which was the asymmetry this closes.
    """


def verify_ingested(path: Path) -> str:
    """Hash a just-ingested file against the ETag it was stored under.

    Returns VERIFIED, UNVERIFIABLE or raises IngestDigestMismatch.

    The blob's FILENAME is the upstream ETag -- that is huggingface_hub's
    on-disk layout, and it is why nothing here recomputed anything before. The
    snapshot entry is a symlink into blobs/<etag>, so resolving gives both the
    bytes and the claimed digest from one path.

    WHY A MISMATCH DELETES THE BLOB RATHER THAN JUST REPORTING IT. Leaving it on
    disk leaves a file whose NAME asserts a digest its bytes do not have, and
    every later check in this service keys on that name -- the cache would be
    self-consistently wrong and would serve those bytes forever. That is the
    failure the OCI path already refuses.

    WHAT THIS CANNOT DO, stated here because the guarantee gets read off this
    docstring: under the `stream` miss policy the bytes are served to the first
    caller AS THEY ARRIVE, so this can stop a bad blob being KEPT but cannot
    retract what was already sent. It also does not cover the xet download path,
    which reconstructs from content-addressed chunks and has NOT been measured
    from here.
    """
    blob = path.resolve()
    etag = blob.name
    if _SHA256_RE.match(etag):
        h = hashlib.sha256()
    elif _GIT_SHA1_RE.match(etag):
        # A small (non-LFS) file: the ETag is the git blob id, which covers the
        # content through a header naming its length. Same single pass.
        try:
            size = blob.stat().st_size
        except OSError as exc:
            metrics.record_ingest_verify("MISMATCH")
            raise IngestDigestMismatch(f"could not stat {blob} to verify: {exc}") from exc
        h = hashlib.sha1(usedforsecurity=False)
        h.update(b"blob %d\0" % size)
    else:
        # A copy-mode cache with no symlink to read, or an ETag of neither
        # shape. Not a failure -- but it must not be counted as a pass either.
        metrics.record_ingest_verify("UNVERIFIABLE")
        log.debug("ingest unverifiable (etag is neither sha256 nor a git blob id): %s", blob)
        return "UNVERIFIABLE"

    try:
        with blob.open("rb") as fh:
            for chunk in iter(lambda: fh.read(4 << 20), b""):
                h.update(chunk)
    except OSError as exc:
        # UNREADABLE IS NOT VERIFIED. Collapsing those two is the fail-open this
        # project keeps confessing, so this refuses rather than passing.
        metrics.record_ingest_verify("MISMATCH")
        raise IngestDigestMismatch(f"could not read {blob} to verify: {exc}") from exc

    got = h.hexdigest()
    if got != etag:
        metrics.record_ingest_verify("MISMATCH")
        try:
            path.unlink(missing_ok=True)  # the snapshot symlink
            blob.unlink(missing_ok=True)  # the bytes themselves
        except OSError as exc:
            log.warning("could not remove mismatched blob %s: %s", blob, exc)
        raise IngestDigestMismatch(f"expected {etag}, computed {got}")

    metrics.record_ingest_verify("VERIFIED")
    return "VERIFIED"


# THE STATE MACHINE. Every job moves forward only:
#
#   pending -> running -> [verifying] -> done
#                     \-------+-------> error
#   pending | running | verifying  --(process restart)-->  interrupted
#
# `verifying` is entered only when XHC_HF_VERIFY is on: the bytes have
# landed and are being hashed against their ETags. `done` is set ONLY after
# verification passes, and in the same step as finished_at, so a client gating
# on done never sees a file whose check is still running -- or a done job with
# no finished_at. That used to happen: a snapshot was marked done the moment
# snapshot_download returned and was verified afterwards, and a 24 GB file sat
# at state=done, finished_at=null for minutes while it was still being hashed.
# A verification failure ends in `error`, never `done`.
#
# `interrupted`: the ledger last saw this job pending, running or verifying, and
# then the process that owned it went away (OOM kill, crash, redeploy). Its
# outcome is unknown -- some files may have landed -- and it is not resumed
# automatically. See JobManager.load_ledger.
JobState = Literal["pending", "running", "verifying", "done", "error", "interrupted"]
ACTIVE_STATES = ("pending", "running", "verifying")
_SNAPSHOT_SAMPLE_S = 5.0

# ---------------------------------------------------------------------------
# The job ledger: jobs.json in the HF state dir.
#
# BOUND. Finished and interrupted jobs are kept up to a COUNT per kind and for
# at most _RETENTION_S, whichever drops them first. The kinds are bounded
# separately because they arrive at wildly different rates: every client cache
# miss is a file job, while a snapshot job is a deliberate prewarm somebody is
# probably polling. A single count let a client walking a 400-file repo push
# every prewarm out of history, which is "no such job" again by another route.
# Active jobs are never dropped. At the defaults the file stays in the low
# hundreds of KB.
#
# WRITE THROTTLE. A state change (submitted, started, finished, failed) is
# written at once unless another write happened in the last
# _TRANSITION_COALESCE_S, in which case it is written at the end of that window
# -- so a burst of cache misses costs at most a few writes a second, and a crash
# loses at most that window. The one exception is a PREWARM finishing or
# failing, which is always written immediately (see _persist). Progress alone (bytes growing on a running job) is
# written at most every _PROGRESS_WRITE_S. Writes are temp file + os.replace,
# so a kill mid-write leaves the previous ledger, never half of one. There is
# no fsync: the failure this exists for is the PROCESS dying, which the page
# cache survives; a power cut may lose the last few seconds of history.
# ---------------------------------------------------------------------------
LEDGER_FILE = "jobs.json"
_LEDGER_VERSION = 1
_HISTORY_LIMIT = 200  # finished FILE jobs kept
_SNAPSHOT_HISTORY_LIMIT = 50  # finished SNAPSHOT (prewarm) jobs kept
_RETENTION_S = 7 * 86400.0
_TRANSITION_COALESCE_S = 0.5
_PROGRESS_WRITE_S = 30.0


@dataclass
class Job:
    id: str
    kind: Literal["file", "snapshot"]
    repo_type: str
    repo_id: str
    revision: str
    filename: str | None = None
    allow_patterns: list[str] | None = None
    state: JobState = "pending"
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None
    result_path: str | None = None
    expected_size: int | None = None
    # Set once we know where the in-flight bytes are landing, so a `stream`
    # miss-policy request can tail-follow it.
    incomplete_path: str | None = None
    # Final measured size for a SNAPSHOT job, set once the tree walk completes.
    # A separate field on purpose: assigning to `downloaded_bytes` shadowed the
    # METHOD of the same name on the instance, so a completed snapshot job made
    # to_dict() raise "'int' object is not callable". See an internal issue.
    final_bytes: int | None = None
    # Set on a job loaded from the ledger by a later process. The live
    # measurement (stat of incomplete_path) belongs to whichever process owns
    # that file now, so a restored job reports what was RECORDED instead.
    restored: bool = False
    recorded_bytes: int | None = None
    # Outcome of ingest verification, once it has run. Distinguishes files
    # verified THIS run from files already present and not re-verified, so a
    # re-prewarm of a complete repo does not read as "nothing was checked".
    verify: dict | None = None
    # When the ledger last wrote this job: the last moment its state and
    # progress are known to have been true.
    updated_at: float | None = None
    interrupted_at: float | None = None
    # The upstream ETag this file job was submitted for (from serve_file's
    # HEAD). The tier is keyed on it; None for snapshot jobs.
    etag: str | None = None
    # True once the object-store tier has answered 200 for this job's blob, and
    # never cleared. tier_decided is set when that question has an answer
    # either way, so a request can choose how to respond without racing the
    # job: whatever the verification later finds, it chose the same way.
    tier_source: bool = False
    # "tier" when the bytes that landed came from the tier and were verified
    # there. Unset when the tier missed, failed, or was refused and the job
    # fell through to the upstream.
    served_from: str | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    tier_decided: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    @property
    def key(self) -> str:
        if self.kind == "file":
            return f"file:{self.repo_type}:{self.repo_id}:{self.revision}:{self.filename}"
        pats = ",".join(sorted(self.allow_patterns or []))
        return f"snap:{self.repo_type}:{self.repo_id}:{self.revision}:{pats}"

    def downloaded_bytes(self) -> int | None:
        """Best-effort progress. Never assign to this name -- it is a method, and
        an instance attribute of the same name shadows it (an internal issue)."""
        if self.restored:
            return self.recorded_bytes
        if self.final_bytes is not None:
            return self.final_bytes
        if self.state == "done" and self.expected_size is not None:
            return self.expected_size
        if not self.incomplete_path:
            return None
        try:
            return Path(self.incomplete_path).stat().st_size
        except OSError:
            return None

    def to_dict(self) -> dict:
        d = {
            "id": self.id,
            "kind": self.kind,
            "repo_type": self.repo_type,
            "repo_id": self.repo_id,
            "revision": self.revision,
            "filename": self.filename,
            "allow_patterns": self.allow_patterns,
            "state": self.state,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "result_path": self.result_path,
            "expected_size": self.expected_size,
            "downloaded_bytes": self.downloaded_bytes(),
            "updated_at": self.updated_at,
            "interrupted_at": self.interrupted_at,
            "verify": self.verify,
        }
        if self.state == "interrupted":
            d["note"] = (
                "the process running this job stopped before it finished; "
                "downloaded_bytes is the last recorded progress. Nothing resumes "
                "it automatically: submit the same prewarm (or request the same "
                "file) again, which skips files already cached."
            )
        if self.started_at:
            # An interrupted job's true end is unknown; its last record is the
            # latest moment it is known to have been running.
            end = self.finished_at or (
                self.updated_at if self.state == "interrupted" else None
            ) or time.time()
            d["elapsed_s"] = round(end - self.started_at, 2)
            got = d["downloaded_bytes"]
            if got and d["elapsed_s"] > 0:
                d["throughput_bytes_per_s"] = int(got / d["elapsed_s"])
        return d


    _RECORD_FIELDS = (
        "id", "kind", "repo_type", "repo_id", "revision", "filename",
        "allow_patterns", "state", "created_at", "started_at", "finished_at",
        "error", "result_path", "expected_size", "final_bytes",
        "interrupted_at", "verify",
    )

    def to_record(self, now: float) -> dict:
        """What the ledger stores. Progress is frozen into recorded_bytes."""
        rec = {k: getattr(self, k) for k in self._RECORD_FIELDS}
        rec["recorded_bytes"] = self.downloaded_bytes()
        rec["updated_at"] = self.updated_at if self.restored else now
        return rec

    @classmethod
    def from_record(cls, rec: dict) -> Job:
        if not isinstance(rec, dict):
            raise ValueError("job record is not an object")
        kwargs = {k: rec[k] for k in cls._RECORD_FIELDS if k in rec}
        for required in ("id", "kind", "repo_type", "repo_id", "revision", "state"):
            if not isinstance(kwargs.get(required), str):
                raise ValueError(f"job record missing {required}")
        if kwargs["kind"] not in ("file", "snapshot"):
            raise ValueError(f"job record has unknown kind {kwargs['kind']!r}")
        job = cls(**kwargs)
        job.restored = True
        job.recorded_bytes = rec.get("recorded_bytes")
        job.updated_at = rec.get("updated_at")
        job.done.set()
        job.tier_decided.set()
        return job


class JobManager:
    def __init__(self) -> None:
        self._active: dict[str, Job] = {}
        self._by_id: dict[str, Job] = {}
        self._history: list[Job] = []
        self._lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(settings.ingest_concurrency)
        # asyncio only holds a weak reference to running tasks, so a
        # fire-and-forget create_task() can be garbage-collected mid-flight --
        # which here would silently abort an in-progress ingest that clients are
        # streaming from. Hold strong refs until each task completes.
        self._tasks: set[asyncio.Task] = set()
        # Ledger bookkeeping. _ledger_ok goes False only when an unreadable
        # ledger could not be moved aside: writing would then overwrite the
        # only copy of it, so this process keeps its jobs in memory only.
        self._ledger_ok = True
        self._ledger_error: str | None = None
        self._last_write = 0.0
        self._flush_handle: asyncio.TimerHandle | None = None
        self._progress_task: asyncio.Task | None = None

    # -- ledger ------------------------------------------------------------

    def _ledger_path(self) -> Path:
        return statedir.hf_file(LEDGER_FILE)

    def load_ledger(self) -> None:
        """Startup: restore job history from the ledger.

        A job recorded as pending or running belonged to a process that is
        gone. It becomes `interrupted`, stamped with this process's start time,
        and keeps its last recorded progress. It is NOT resumed -- a prewarm
        that OOM-killed the pod must not restart itself on every boot -- and it
        is NOT dropped, because a poller holding its id needs an answer.
        Re-submitting the same prewarm is the resume.

        AN UNREADABLE LEDGER NEVER STOPS THE SERVICE. This is the deliberate
        opposite of pins.json, which fails closed: an unreadable pins file means
        we cannot tell what is protected, and guessing "nothing" would let
        eviction delete the only copy of something. Losing job history protects
        nothing and deletes nothing -- the worst outcome is the "no such job"
        this ledger exists to reduce. Refusing to boot, or refusing cache hits,
        over it would turn a bookkeeping loss into an outage. So: log loudly,
        move the bad file aside as jobs.json.corrupt.<epoch> for a human to
        read, and start a fresh ledger.
        """
        try:
            # NOT in statedir.HF_FILES, on purpose. hf_file() still copies an
            # in-tree ledger across the first time it is resolved, so history
            # survives an operator turning XHC_STATE_DIR on -- but a copy that
            # FAILS raises StateDirError, which for pins rightly stops the
            # boot. For a ledger it must not, so it is caught here and the
            # eager startup migration (which would be fatal) never sees it.
            p = self._ledger_path()
            exists = p.exists()
        except (OSError, statedir.StateDirError) as exc:
            self._ledger_ok = False
            self._ledger_error = f"ledger location unusable: {exc}"
            log.error("JOB LEDGER UNAVAILABLE (%s); job history is in memory only", exc)
            return
        if not exists:
            return
        try:
            data = json.loads(p.read_text())
            if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
                raise ValueError("ledger is not an object with a jobs list")
            restored = [Job.from_record(r) for r in data["jobs"]]
        except (OSError, ValueError, TypeError) as exc:
            # UnicodeDecodeError is a ValueError, so binary garbage lands here.
            self._quarantine(p, exc)
            return

        interrupted = 0
        fresh = []
        for job in restored:
            if job.id in self._by_id:
                continue  # already known to this process; memory wins
            if job.state in ACTIVE_STATES:
                job.state = "interrupted"
                job.interrupted_at = build.PROCESS_STARTED_AT
                interrupted += 1
            self._by_id[job.id] = job
            fresh.append(job)
        self._history = sorted(self._history + fresh, key=_finished_key)
        self._prune(time.time())
        if interrupted:
            log.warning(
                "job ledger: %d job(s) were still active when the previous "
                "process stopped; marked interrupted. Re-submit to resume.",
                interrupted,
            )
        log.info("job ledger: restored %d job(s) from %s", len(self._history), p)
        # Persist the interrupted marks now, so the ledger on disk agrees with
        # what the API reports from the first request on.
        self._write_ledger()

    def _quarantine(self, p: Path, exc: Exception) -> None:
        kept = p.with_name(f"{p.name}.corrupt.{int(time.time())}")
        try:
            os.replace(p, kept)
        except OSError as move_exc:
            self._ledger_ok = False
            self._ledger_error = f"unreadable and could not be moved aside: {move_exc}"
            log.error(
                "JOB LEDGER %s IS UNREADABLE (%s) AND COULD NOT BE MOVED ASIDE (%s). "
                "Serving continues; job history for this process is in memory "
                "only, and the file is left untouched so nothing overwrites it.",
                p, exc, move_exc,
            )
            return
        self._ledger_error = f"previous ledger unreadable, preserved as {kept.name}"
        log.error(
            "JOB LEDGER %s IS UNREADABLE (%s). Preserved as %s and starting a "
            "fresh ledger. Serving is unaffected; earlier job ids will answer "
            "'no such job'.",
            p, exc, kept,
        )

    def ledger_status(self) -> dict:
        return {
            "file": LEDGER_FILE,
            "persisting": self._ledger_ok,
            "last_write": self._last_write or None,
            "error": self._ledger_error,
        }

    def _prune(self, now: float) -> None:
        """Apply the bound to finished history. Active jobs are untouched."""
        cutoff = now - _RETENTION_S
        limits = {"file": _HISTORY_LIMIT, "snapshot": _SNAPSHOT_HISTORY_LIMIT}
        counts = {"file": 0, "snapshot": 0}
        keep: list[Job] = []
        for job in reversed(self._history):  # newest first
            if _finished_key(job) >= cutoff and counts[job.kind] < limits[job.kind]:
                counts[job.kind] += 1
                keep.append(job)
            elif self._by_id.get(job.id) is job:
                del self._by_id[job.id]
        keep.reverse()
        self._history = keep

    def _write_ledger(self) -> None:
        if not self._ledger_ok:
            return
        now = time.time()
        self._last_write = now
        jobs = list(self._active.values()) + self._history
        body = {
            "version": _LEDGER_VERSION,
            "written_at": now,
            "process_started_at": build.PROCESS_STARTED_AT,
            "jobs": [j.to_record(now) for j in jobs],
        }
        for j in jobs:
            if not j.restored:
                j.updated_at = now
        try:
            p = self._ledger_path()
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_text(json.dumps(body))
            os.replace(tmp, p)
        except (OSError, statedir.StateDirError) as exc:
            # A full or read-only state volume must not fail an ingest. Said
            # once per distinct error rather than once per job.
            msg = f"could not write job ledger: {exc}"
            if msg != self._ledger_error:
                log.error("%s (jobs continue; history may not survive a restart)", msg)
            self._ledger_error = msg

    def _persist(self, transition: bool, urgent: bool = False) -> None:
        """Write the ledger, throttled. See the constants at the top.

        `urgent` skips the coalescing window: used for a PREWARM's outcome.
        Losing a "running" mark to a crash only turns pending into interrupted;
        losing a finished prewarm's outcome would report finished work as
        interrupted and send someone to redo it. File jobs are not urgent --
        they arrive in bursts, and the client streaming one already has its
        answer.
        """
        if urgent:
            self.flush()
            return
        if self._flush_handle is not None:
            return  # a write is already scheduled and will include this change
        window = _TRANSITION_COALESCE_S if transition else _PROGRESS_WRITE_S
        wait = window - (time.time() - self._last_write)
        if wait <= 0:
            self._write_ledger()
            return
        if not transition:
            return  # progress only: a later tick past the window writes it
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._write_ledger()
            return
        self._flush_handle = loop.call_later(wait, self._scheduled_write)

    def _scheduled_write(self) -> None:
        self._flush_handle = None
        self._write_ledger()

    def flush(self) -> None:
        """Write now, cancelling any pending coalesced write. Used at shutdown."""
        if self._flush_handle is not None:
            self._flush_handle.cancel()
            self._flush_handle = None
        self._write_ledger()

    async def _progress_loop(self) -> None:
        """While anything is active, offer the ledger a progress write. The
        throttle in _persist decides whether one happens."""
        try:
            while self._active:
                await asyncio.sleep(_PROGRESS_WRITE_S / 3)
                self._persist(transition=False)
        finally:
            self._progress_task = None

    # -- lookup ------------------------------------------------------------

    def get(self, job_id: str) -> Job | None:
        return self._by_id.get(job_id)

    def list(self) -> list[Job]:
        return list(self._active.values()) + list(reversed(self._history))

    # -- submission --------------------------------------------------------

    async def ensure_file(
        self,
        repo_type: str,
        repo_id: str,
        revision: str,
        filename: str,
        expected_size: int | None = None,
        incomplete_path: str | None = None,
        etag: str | None = None,
    ) -> Job:
        job = Job(
            id=uuid.uuid4().hex[:12],
            kind="file",
            repo_type=repo_type,
            repo_id=repo_id,
            revision=revision,
            filename=filename,
            expected_size=expected_size,
            incomplete_path=incomplete_path,
            etag=etag,
        )
        return await self._submit(job)

    async def ensure_snapshot(
        self,
        repo_type: str,
        repo_id: str,
        revision: str,
        allow_patterns: list[str] | None = None,
    ) -> Job:
        job = Job(
            id=uuid.uuid4().hex[:12],
            kind="snapshot",
            repo_type=repo_type,
            repo_id=repo_id,
            revision=revision,
            allow_patterns=allow_patterns,
        )
        return await self._submit(job)

    async def _submit(self, job: Job) -> Job:
        async with self._lock:
            existing = self._active.get(job.key)
            if existing is not None:
                # Single-flight: join the in-flight fetch rather than starting
                # a second one. This is the whole ballgame for a fleet that
                # rotates models in lockstep.
                return existing
            self._active[job.key] = job
            self._by_id[job.id] = job
        task = asyncio.create_task(self._run(job))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        self._persist(transition=True)
        if self._progress_task is None:
            self._progress_task = asyncio.create_task(self._progress_loop())
            self._tasks.add(self._progress_task)
            self._progress_task.add_done_callback(self._tasks.discard)
        return job

    # -- execution ---------------------------------------------------------

    async def _run(self, job: Job) -> None:
        try:
            async with self._sem:
                job.state = "running"
                job.started_at = time.time()
                self._persist(transition=True)
                log.info("ingest start %s %s", job.id, job.key)
                if job.kind == "file":
                    # The tier, if configured, is tried first. On a verified
                    # hit the blob is already in place and hf_hub_download
                    # only links it; on anything else it fetches as before.
                    if tier.enabled():
                        await tier.fill_hf_blob(job)
                    job.tier_decided.set()
                    path = await asyncio.to_thread(self._download_file, job)
                else:
                    # Sample the tree while it downloads. Without this a healthy
                    # prewarm is indistinguishable from a stalled one -- the
                    # failure direction that makes people intervene in a job that
                    # is working. Measured once at 192 MB/s while every instrument
                    # read zero.
                    watcher = asyncio.create_task(self._watch_snapshot(job))
                    try:
                        path = await asyncio.to_thread(self._download_snapshot, job)
                    finally:
                        watcher.cancel()
                job.result_path = str(path)
                # NB: for a snapshot, `path` is a DIRECTORY. stat().st_size on it
                # returns the inode size (a few KB), not the tree -- so this used
                # to report ~4 KB for a 126 GB prewarm, which is worse than
                # reporting nothing because it looks like a real measurement.
                if job.kind == "file":
                    try:
                        # Tier bytes are counted on muninn_tier_bytes_read_total
                        # and never here: "ingested" is the upstream leg.
                        if job.served_from != "tier":
                            metrics.record_ingested(Path(path).stat().st_size)
                    except OSError:
                        pass
                else:
                    fetched = await asyncio.to_thread(
                        _tree_bytes, Path(path), job.started_at or 0
                    )
                    job.final_bytes = fetched
                    metrics.record_ingested(fetched)
                if settings.hf_verify_ingest:
                    job.state = "verifying"
                    self._persist(transition=True)
                    await self._verify(job, Path(path))
                # done and finished_at in one step: nothing can observe one
                # without the other, because there is no await between them.
                job.finished_at = time.time()
                job.state = "done"
                log.info("ingest done %s in %.1fs", job.id, time.time() - (job.started_at or 0))
        except asyncio.CancelledError:
            job.state = "error"
            job.error = "cancelled"
            raise
        except Exception as exc:
            job.state = "error"
            job.error = f"{type(exc).__name__}: {exc}"
            log.exception("ingest failed %s", job.id)
        finally:
            if job.finished_at is None:
                job.finished_at = time.time()
            job.tier_decided.set()
            job.done.set()
            cachefs.invalidate_view()
            async with self._lock:
                if self._active.get(job.key) is job:
                    del self._active[job.key]
            self._history.append(job)
            self._prune(time.time())
            self._persist(transition=True, urgent=job.kind == "snapshot")
            # Write-back fires on `done` and on nothing else: never from
            # `verifying`, never from `error`, never from an .incomplete file.
            if job.state == "done" and tier.writable():
                t = asyncio.create_task(tier.after_hf_job(job))
                self._tasks.add(t)
                t.add_done_callback(self._tasks.discard)

    async def _verify(self, job: Job, path: Path) -> None:
        """Hash what this job ingested. Raises on any mismatch, which the caller
        turns into `error`: a failed check must never end in `done`."""
        if job.kind == "file":
            # Outside the download thread on purpose, so the job can say
            # `verifying` while a large file is hashed instead of `running`.
            counts = {"new_verified": 0, "new_unverifiable": 0, "mismatched": 0,
                      "already_present_not_reverified": 0}
            if job.served_from == "tier" and path.resolve().name == job.etag:
                # Hashed as it arrived from the tier and renamed into place
                # only on a match. Hashing it again here would be a second
                # full read of the file -- minutes on a large shard -- to
                # re-establish a fact already established. The resolve()
                # check covers a branch that moved between the two HEADs, in
                # which case this is a different blob and is hashed below.
                job.verify = {**counts, "new_verified": 1, "verified_at": "tier-read"}
                return
            try:
                result = await asyncio.to_thread(verify_ingested, path)
            except IngestDigestMismatch:
                job.verify = {**counts, "mismatched": 1}
                raise
            job.verify = {
                **counts,
                "new_verified": int(result == "VERIFIED"),
                "new_unverifiable": int(result == "UNVERIFIABLE"),
            }
            return
        # A snapshot is many files and snapshot_download offers no per-file
        # hook, so this runs once the tree has landed. Bad blobs are already
        # deleted by then; failing the job is what stops the rest being
        # treated as a good prewarm.
        tv = await asyncio.to_thread(verify_tree, path, job.started_at or 0)
        job.verify = {
            "new_verified": tv.verified,
            "new_unverifiable": tv.unverifiable,
            "mismatched": len(tv.mismatches),
            "already_present_not_reverified": tv.already_present,
        }
        log.info(
            "snapshot verify %s: %d new file(s) verified, %d new unverifiable, "
            "%d mismatched; %d already present, not re-verified",
            job.id, tv.verified, tv.unverifiable, len(tv.mismatches), tv.already_present,
        )
        if tv.mismatches:
            raise IngestDigestMismatch(
                f"{len(tv.mismatches)} file(s) failed verification: "
                + "; ".join(tv.mismatches[:5])
            )

    def _download_file(self, job: Job) -> Path:
        path = Path(
            hf_hub_download(
                repo_id=job.repo_id,
                filename=job.filename,
                revision=job.revision,
                repo_type=job.repo_type,
                cache_dir=settings.cache_dir,
                token=settings.hf_token,
                endpoint=settings.upstream,
            )
        )
        # Verification is NOT done here: _run does it after this returns, as
        # its own `verifying` state.
        return path

    async def _watch_snapshot(self, job: Job) -> None:
        """Keep job.final_bytes roughly current while a snapshot runs.

        NEVER assign to `downloaded_bytes` here: it is a method, and an instance
        attribute of that name shadows it. This assignment used to make the type
        of job.downloaded_bytes depend on WHETHER THE WATCHER HAD TICKED YET --
        a bound method for the first ~5s of a snapshot job, an int afterwards,
        and a method forever for a file job, which has no watcher at all. That
        is why /metrics 500'd for the whole life of a file ingest but only
        briefly for a snapshot one. an internal issue.
        """
        root = Path(settings.cache_dir) / cachefs.repo_folder_name(job.repo_type, job.repo_id)
        started = job.started_at or time.time()
        try:
            while True:
                await asyncio.sleep(_SNAPSHOT_SAMPLE_S)
                job.final_bytes = await asyncio.to_thread(_tree_bytes, root, started)
        except asyncio.CancelledError:
            raise

    def _record_manifest(self, job: Job) -> None:
        """Record what this prewarm is about to fetch, BEFORE fetching it.

        Before, not after: the case this exists for is the prewarm that never
        finishes. A manifest written at the end would exist only for snapshots
        that were already complete, and "no manifest" would then mean "killed
        part-way" and "never prewarmed" alike.

        One extra upstream call per prewarm, never per listing. A failure is
        logged and tolerated: completeness then reports `null`, which is
        honest, and a prewarm must not fail over its own bookkeeping.

        Mirrors snapshot_download's own listing, including its fallback to the
        tree API when siblings is empty or too large to trust. If the branch
        moves between this call and snapshot_download's, the manifest is for a
        commit that is not the one downloaded, and that commit reports `null`.
        """
        try:
            api = HfApi(endpoint=settings.upstream, token=settings.hf_token)
            info = api.repo_info(
                repo_id=job.repo_id,
                repo_type=job.repo_type,
                revision=job.revision,
                files_metadata=True,
            )
            if not info.sha:
                raise ValueError("repo info carried no commit sha")
            siblings = info.siblings or []
            if not siblings or len(siblings) > _VERY_LARGE_REPO_THRESHOLD:
                from huggingface_hub.hf_api import RepoFile

                files = {
                    f.path: f.size
                    for f in api.list_repo_tree(
                        repo_id=job.repo_id, repo_type=job.repo_type,
                        revision=info.sha, recursive=True,
                    )
                    if isinstance(f, RepoFile)
                }
            else:
                files = {s.rfilename: s.size for s in siblings}
            manifests.record(job.repo_type, job.repo_id, info.sha, files, job.allow_patterns)
        except Exception as exc:  # noqa: BLE001 - bookkeeping must not fail the prewarm
            log.warning(
                "prewarm %s: could not record the expected file list (%s); "
                "/_cache/repos will report completeness as unknown",
                job.id, exc,
            )

    def _download_snapshot(self, job: Job) -> Path:
        self._record_manifest(job)
        return Path(
            snapshot_download(
                repo_id=job.repo_id,
                revision=job.revision,
                repo_type=job.repo_type,
                cache_dir=settings.cache_dir,
                token=settings.hf_token,
                endpoint=settings.upstream,
                allow_patterns=job.allow_patterns,
                max_workers=settings.snapshot_max_workers,
            )
        )


def _finished_key(job: Job) -> float:
    """When a job stopped being active, for ordering and retention."""
    return job.finished_at or job.interrupted_at or job.updated_at or job.created_at


@dataclass
class TreeVerify:
    verified: int
    unverifiable: int
    mismatches: list[str]
    # Files under the root that predate this run and were therefore NOT
    # re-hashed. Counted from the same walk, so it costs nothing extra. Without
    # it a re-prewarm of a complete repo logged "0 verified", which reads as
    # "nothing was checked" rather than "nothing new arrived".
    already_present: int = 0


def verify_tree(root: Path, since: float = 0.0) -> TreeVerify:
    """Verify every file freshly written under a snapshot root.

    Mismatched blobs are deleted by verify_ingested before this returns, so the
    caller decides what to do about a failed ingest with the bad bytes already
    gone.

    THE mtime FILTER IS THE SAME ONE _tree_bytes USES, and for the same reason:
    a snapshot that was already half-cached must not re-hash the half it did not
    fetch. That keeps a repeat prewarm cheap. It also means this is a check on
    INGEST and not a scrub -- on-disk rot in a blob nobody re-fetched is a
    different job, and calling this one a scrub would be the adjacent-measure
    mistake.

    Deduplicated by inode, because the HF layout points many snapshot entries at
    one blob and hashing it once per reference would be the same work repeated.
    """
    verified = unverifiable = already = 0
    mismatches: list[str] = []
    seen: set[tuple[int, int]] = set()
    if not root.exists():
        return TreeVerify(0, 0, [])
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            path = Path(dirpath) / fn
            try:
                st = os.stat(path)  # follows symlinks
            except OSError:
                continue
            key = (st.st_dev, st.st_ino)
            if key in seen:
                continue
            seen.add(key)
            if st.st_mtime < since - 1:
                already += 1
                continue
            try:
                if verify_ingested(path) == "VERIFIED":
                    verified += 1
                else:
                    unverifiable += 1
            except IngestDigestMismatch as exc:
                mismatches.append(f"{path.name}: {exc}")
    return TreeVerify(verified, unverifiable, mismatches, already)


def _tree_bytes(root: Path, since: float = 0.0) -> int:
    """Bytes under `root` that were written at or after `since`.

    Symlinks are resolved, because the HF layout points snapshot entries at
    blobs. The mtime filter is what makes this "ingested" rather than "present":
    a snapshot that was already half-cached should not report the cached half as
    freshly pulled. Deduplicated by inode, so two refs to one blob count once.
    """
    total = 0
    seen: set[tuple[int, int]] = set()
    if not root.exists():
        return 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            try:
                st = os.stat(os.path.join(dirpath, fn))  # follows symlinks
            except OSError:
                continue
            key = (st.st_dev, st.st_ino)
            if key in seen:
                continue
            seen.add(key)
            if st.st_mtime >= since - 1:
                total += st.st_size
    return total


manager = JobManager()
