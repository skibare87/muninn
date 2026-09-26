"""A durable, bounded job table: the machinery shared by every kind of job.

Two job tables need to outlive the process that ran them -- Hugging Face ingest
jobs (jobs.py) and OCI image prewarms (ocimanage.py) -- and both need exactly
the same guarantees. This module is the one copy of them; each owner supplies
only what differs (where the file lives, how a record becomes a job, and the
per-kind bounds).

THE GUARANTEES, and why each is the way it is:

- **A job survives a restart.** Records are written to a small JSON ledger in
  the protocol's state dir. On startup a job the ledger last saw `pending`,
  `running` or `verifying` belonged to a process that is gone: it comes back as
  `interrupted`, stamped with this process's start time, with its last recorded
  progress. It is NOT resumed (a prewarm that OOM-killed the pod must not
  restart itself on every boot) and NOT dropped (a poller holding its id needs
  an answer). Re-submitting is the resume.

- **BOUND.** Finished and interrupted jobs are kept up to a COUNT per kind and
  for at most a retention period, whichever drops them first. Kinds are bounded
  separately because they can arrive at wildly different rates. Active jobs are
  never dropped.

- **WRITE THROTTLE.** A state change is written at once unless another write
  happened in the last TRANSITION_COALESCE_S, in which case it is written at
  the end of that window -- so a burst costs at most a few writes a second, and
  a crash loses at most that window. An `urgent` change (a prewarm finishing or
  failing) is always written at once. Progress alone is written at most every
  PROGRESS_WRITE_S. Writes are temp file + os.replace, so a kill mid-write
  leaves the previous ledger, never half of one. There is no fsync: the failure
  this exists for is the PROCESS dying, which the page cache survives.

- **AN UNREADABLE LEDGER NEVER STOPS THE SERVICE.** The deliberate opposite of
  pins.json, which fails closed: an unreadable pins file means we cannot tell
  what is protected. Losing job history protects nothing and deletes nothing
  -- the worst outcome is the "no such job" the ledger exists to reduce. So:
  log loudly, move the bad file aside as `<name>.corrupt.<epoch>` for a human,
  and start fresh. If it cannot even be moved aside, the process keeps its jobs
  in memory only rather than overwrite the only copy.

A job type used with this module provides: `id`, `kind`, `state`, `created_at`,
`finished_at`, `updated_at`, `interrupted_at`, `restored`, a `key` (its
single-flight identity) and `to_record(now) -> dict`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

from . import build, statedir

LEDGER_VERSION = 1
RETENTION_S = 7 * 86400.0
TRANSITION_COALESCE_S = 0.5
PROGRESS_WRITE_S = 30.0
ACTIVE_STATES = ("pending", "running", "verifying")


def finished_key(job) -> float:
    """Sort key for history: when the job stopped, as best it is known."""
    return job.finished_at or job.interrupted_at or job.updated_at or job.created_at


class LedgeredJobs:
    """Base for a job manager whose table is durable and bounded.

    Subclasses implement `_ledger_path`, `_job_from_record` and `_kind_limits`,
    and may override `_retention_s`. Everything else -- load, quarantine,
    prune, atomic write, throttle, progress loop, lookup -- lives here.
    """

    LEDGER_FILE = "jobs.json"
    LEDGER_LABEL = "job ledger"
    log = logging.getLogger("xhc.ledger")

    def __init__(self) -> None:
        self._active: dict[str, object] = {}
        self._by_id: dict[str, object] = {}
        self._history: list = []
        # asyncio only holds a weak reference to running tasks, so a
        # fire-and-forget create_task() can be garbage-collected mid-flight.
        # Hold strong refs until each task completes.
        self._tasks: set[asyncio.Task] = set()
        # _ledger_ok goes False only when an unreadable ledger could not be
        # moved aside: writing would then overwrite the only copy of it.
        self._ledger_ok = True
        self._ledger_error: str | None = None
        self._last_write = 0.0
        self._flush_handle: asyncio.TimerHandle | None = None
        self._progress_task: asyncio.Task | None = None

    # -- what a subclass supplies -------------------------------------------

    def _ledger_path(self) -> Path:
        raise NotImplementedError

    def _job_from_record(self, rec: dict):
        raise NotImplementedError

    def _kind_limits(self) -> dict[str, int]:
        raise NotImplementedError

    def _retention_s(self) -> float:
        return RETENTION_S

    # -- load ------------------------------------------------------------------

    def load_ledger(self) -> None:
        """Startup: restore job history. Never raises for an unreadable ledger."""
        try:
            # A ledger is NOT in statedir's eager-migration lists, on purpose:
            # a failed eager migration stops the boot, which is right for pins
            # and wrong for history. The lazy migration inside hf_file() /
            # oci_file() can still raise, so it is caught here.
            p = self._ledger_path()
            exists = p.exists()
        except (OSError, statedir.StateDirError) as exc:
            self._ledger_ok = False
            self._ledger_error = f"ledger location unusable: {exc}"
            self.log.error("%s UNAVAILABLE (%s); job history is in memory only",
                           self.LEDGER_LABEL.upper(), exc)
            return
        if not exists:
            return
        try:
            data = json.loads(p.read_text())
            if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
                raise ValueError("ledger is not an object with a jobs list")
            restored = [self._job_from_record(r) for r in data["jobs"]]
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
        self._history = sorted(self._history + fresh, key=finished_key)
        self._prune(time.time())
        if interrupted:
            self.log.warning(
                "%s: %d job(s) were still active when the previous process "
                "stopped; marked interrupted. Re-submit to resume.",
                self.LEDGER_LABEL, interrupted,
            )
        self.log.info("%s: restored %d job(s) from %s",
                      self.LEDGER_LABEL, len(self._history), p)
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
            self.log.error(
                "%s %s IS UNREADABLE (%s) AND COULD NOT BE MOVED ASIDE (%s). "
                "Serving continues; job history for this process is in memory "
                "only, and the file is left untouched so nothing overwrites it.",
                self.LEDGER_LABEL.upper(), p, exc, move_exc,
            )
            return
        self._ledger_error = f"previous ledger unreadable, preserved as {kept.name}"
        self.log.error(
            "%s %s IS UNREADABLE (%s). Preserved as %s and starting a fresh "
            "ledger. Serving is unaffected; earlier job ids will answer "
            "'no such job'.",
            self.LEDGER_LABEL.upper(), p, exc, kept,
        )

    def ledger_status(self) -> dict:
        return {
            "file": self.LEDGER_FILE,
            "persisting": self._ledger_ok,
            "last_write": self._last_write or None,
            "error": self._ledger_error,
        }

    # -- bound -----------------------------------------------------------------

    def _prune(self, now: float) -> None:
        """Apply the bound to finished history. Active jobs are untouched."""
        cutoff = now - self._retention_s()
        limits = self._kind_limits()
        counts: dict[str, int] = {}
        keep: list = []
        for job in reversed(self._history):  # newest first
            n = counts.get(job.kind, 0)
            if finished_key(job) >= cutoff and n < limits.get(job.kind, 0):
                counts[job.kind] = n + 1
                keep.append(job)
            elif self._by_id.get(job.id) is job:
                del self._by_id[job.id]
        keep.reverse()
        self._history = keep

    # -- write -----------------------------------------------------------------

    def _write_ledger(self) -> None:
        if not self._ledger_ok:
            return
        now = time.time()
        self._last_write = now
        jobs = list(self._active.values()) + self._history
        body = {
            "version": LEDGER_VERSION,
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
            # A full or read-only state volume must not fail the work. Said
            # once per distinct error rather than once per job.
            msg = f"could not write {self.LEDGER_LABEL}: {exc}"
            if msg != self._ledger_error:
                self.log.error("%s (jobs continue; history may not survive a restart)", msg)
            self._ledger_error = msg

    def _persist(self, transition: bool, urgent: bool = False) -> None:
        """Write the ledger, throttled. See the module docstring.

        `urgent` skips the coalescing window: used for a PREWARM's outcome.
        Losing a "running" mark to a crash only turns pending into interrupted;
        losing a finished prewarm's outcome would report finished work as
        interrupted and send someone to redo it.
        """
        if urgent:
            self.flush()
            return
        if self._flush_handle is not None:
            return  # a write is already scheduled and will include this change
        window = TRANSITION_COALESCE_S if transition else PROGRESS_WRITE_S
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

    # -- tasks and progress ----------------------------------------------------

    def _track(self, task: asyncio.Task) -> asyncio.Task:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _ensure_progress_loop(self) -> None:
        if self._progress_task is None:
            self._progress_task = self._track(asyncio.create_task(self._progress_loop()))

    async def _progress_loop(self) -> None:
        """While anything is active, offer the ledger a progress write. The
        throttle in _persist decides whether one happens."""
        try:
            while self._active:
                await asyncio.sleep(PROGRESS_WRITE_S / 3)
                self._persist(transition=False)
        finally:
            self._progress_task = None

    # -- lookup ----------------------------------------------------------------

    def get(self, job_id: str):
        return self._by_id.get(job_id)

    def list(self) -> list:
        return list(self._active.values()) + list(reversed(self._history))
