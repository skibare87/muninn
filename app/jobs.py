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
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from huggingface_hub import hf_hub_download, snapshot_download

from . import cachefs
from .config import settings

log = logging.getLogger("xhc.jobs")

JobState = Literal["pending", "running", "done", "error"]
_HISTORY_LIMIT = 200


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
    done: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    @property
    def key(self) -> str:
        if self.kind == "file":
            return f"file:{self.repo_type}:{self.repo_id}:{self.revision}:{self.filename}"
        pats = ",".join(sorted(self.allow_patterns or []))
        return f"snap:{self.repo_type}:{self.repo_id}:{self.revision}:{pats}"

    def downloaded_bytes(self) -> int | None:
        """Best-effort progress for file jobs, from the .incomplete file."""
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
        }
        if self.started_at:
            end = self.finished_at or time.time()
            d["elapsed_s"] = round(end - self.started_at, 2)
            got = d["downloaded_bytes"]
            if got and d["elapsed_s"] > 0:
                d["throughput_bytes_per_s"] = int(got / d["elapsed_s"])
        return d


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
        return job

    # -- execution ---------------------------------------------------------

    async def _run(self, job: Job) -> None:
        try:
            async with self._sem:
                job.state = "running"
                job.started_at = time.time()
                log.info("ingest start %s %s", job.id, job.key)
                if job.kind == "file":
                    path = await asyncio.to_thread(self._download_file, job)
                else:
                    path = await asyncio.to_thread(self._download_snapshot, job)
                job.result_path = str(path)
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
            job.finished_at = time.time()
            job.done.set()
            cachefs.invalidate_view()
            async with self._lock:
                if self._active.get(job.key) is job:
                    del self._active[job.key]
            self._history.append(job)
            if len(self._history) > _HISTORY_LIMIT:
                dropped = self._history.pop(0)
                self._by_id.pop(dropped.id, None)

    def _download_file(self, job: Job) -> Path:
        return Path(
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

    def _download_snapshot(self, job: Job) -> Path:
        return Path(
            snapshot_download(
                repo_id=job.repo_id,
                revision=job.revision,
                repo_type=job.repo_type,
                cache_dir=settings.cache_dir,
                token=settings.hf_token,
                endpoint=settings.upstream,
                allow_patterns=job.allow_patterns,
                max_workers=8,
            )
        )


manager = JobManager()
