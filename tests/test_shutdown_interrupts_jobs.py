"""A graceful stop records in-flight HF jobs as `interrupted`, and wakes their waiters.

The end-to-end proof is tests/test_graceful_stop_interrupted.py, under real
uvicorn. These pin the pieces it depends on, each of which can be broken on its
own without that test saying which:

- the lifespan marks and WRITES before anything is cancelled;
- the cancel handler leaves the mark alone, and any other cancel is still
  `error: cancelled`;
- a job the stop caught waiting for a slot, or whose download returned in the
  gap between the mark and the cancel, does not walk on to running or done;
- every in-process waiter wakes with an outcome that is not "done", and the
  streaming one ends instead of polling a file nobody is writing.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import hfcompat, jobs, serving, shutdown
from app.config import settings


@pytest.fixture
def env(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(settings, "cache_dir", str(cache))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"), raising=False)
    monkeypatch.setattr(settings, "hf_verify_ingest", False)
    monkeypatch.setattr(settings, "tier", None)
    monkeypatch.setattr(settings, "stream_poll_interval_s", 0.01)
    monkeypatch.setattr(jobs.JobManager, "_record_manifest", lambda self, job: None)
    monkeypatch.setattr(shutdown, "STEP_TIMEOUT_S", 2.0)
    return tmp_path


def _blocking_download(monkeypatch, env: Path, name: str = "_download_file"):
    """Replace a download with one that holds until released, then returns a
    real file. Returns (entered, release)."""
    entered, release = threading.Event(), threading.Event()
    target = env / "cache" / "payload"
    target.write_bytes(b"x" * 10)

    def download(self, job):
        entered.set()
        assert release.wait(10), "test never released the download"
        return target

    monkeypatch.setattr(jobs.JobManager, name, download)
    return entered, release


async def _until(pred, what: str, timeout: float = 5.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


def _ledger(env: Path) -> dict:
    return {j["id"]: j for j in json.loads((env / "state" / "hf" / "jobs.json").read_text())["jobs"]}


def test_stop_marks_and_writes_before_it_cancels(env, monkeypatch):
    entered, release = _blocking_download(monkeypatch, env)
    seen: list[tuple[str, bool]] = []

    async def main():
        m = jobs.JobManager()
        job = await m.ensure_file("model", "acme/w", "main", "f.bin")
        await _until(entered.is_set, "the download to start")
        runner = next(iter(m._runners))
        real_write = m._write_ledger

        def recording_write():
            seen.append((job.state, runner.done() or runner.cancelling() > 0))
            real_write()

        monkeypatch.setattr(m, "_write_ledger", recording_write)
        await m.stop()
        release.set()
        return m, job

    m, job = asyncio.run(main())
    # The first write of the stop already says interrupted, and the runner had
    # not been cancelled when it happened.
    assert seen[0] == ("interrupted", False), seen
    assert job.state == "interrupted"
    assert job.error is None
    assert job.finished_at is None
    assert job.interrupted_at is not None
    assert _ledger(env)[job.id]["state"] == "interrupted"


def test_the_lifespan_stops_hf_jobs_before_anything_else(env, monkeypatch):
    """Unit-level ordering: the lifespan's shutdown records jobs as interrupted
    first, with the job task still alive, and leaves them that way."""
    from app import main as app_main

    monkeypatch.setattr(settings, "docker_enabled", False)
    monkeypatch.setattr(settings, "orphan_check_interval_s", 0.0)
    monkeypatch.setattr(settings, "jwt_issuers", [])
    entered, release = _blocking_download(monkeypatch, env)
    m = jobs.JobManager()
    monkeypatch.setattr(app_main, "manager", m)

    order: list[str] = []
    real_interrupt = m.interrupt_active
    real_cancel_and_wait = shutdown.cancel_and_wait

    def recording_interrupt():
        runner = next(iter(m._runners))
        order.append(f"interrupt(runner alive={not runner.done() and not runner.cancelling()})")
        return real_interrupt()

    async def recording_cancel_and_wait(tasks, what, timeout=None):
        order.append(f"cancel:{what}")
        return await real_cancel_and_wait(tasks, what, timeout)

    monkeypatch.setattr(m, "interrupt_active", recording_interrupt)
    monkeypatch.setattr(shutdown, "cancel_and_wait", recording_cancel_and_wait)

    async def main():
        async with app_main.lifespan(app_main.app):
            job = await m.ensure_file("model", "acme/w", "main", "f.bin")
            await _until(entered.is_set, "the download to start")
        release.set()
        return job

    job = asyncio.run(main())
    assert order[:2] == ["interrupt(runner alive=True)", "cancel:HF ingest jobs"], order
    assert job.state == "interrupted", (job.state, job.error)
    assert _ledger(env)[job.id]["state"] == "interrupted"


def test_a_cancel_that_is_not_a_shutdown_is_still_an_error(env, monkeypatch):
    entered, release = _blocking_download(monkeypatch, env)

    async def main():
        m = jobs.JobManager()
        job = await m.ensure_file("model", "acme/w", "main", "f.bin")
        await _until(entered.is_set, "the download to start")
        runner = next(iter(m._runners))
        runner.cancel()
        await asyncio.wait({runner}, timeout=5)
        release.set()
        m.flush()
        return job

    job = asyncio.run(main())
    assert job.state == "error"
    assert job.error == "cancelled"
    assert job.finished_at is not None
    assert _ledger(env)[job.id]["state"] == "error"


def test_a_download_returning_after_the_mark_does_not_reach_done(env, monkeypatch):
    """The gap between the mark and the cancel: the loop still runs, and a
    download that finishes there must not turn interrupted into done."""
    entered, release = _blocking_download(monkeypatch, env)

    async def main():
        m = jobs.JobManager()
        job = await m.ensure_file("model", "acme/w", "main", "f.bin")
        await _until(entered.is_set, "the download to start")
        m.interrupt_active()
        release.set()  # the thread returns; nothing has cancelled the task
        runner = next(iter(m._runners))
        await asyncio.wait({runner}, timeout=5)
        return job

    job = asyncio.run(main())
    assert job.state == "interrupted"
    assert job.result_path is None
    assert job.finished_at is None


def test_a_job_waiting_for_a_slot_is_interrupted_and_never_starts(env, monkeypatch):
    monkeypatch.setattr(settings, "ingest_concurrency", 1)
    entered, release = _blocking_download(monkeypatch, env)

    async def main():
        m = jobs.JobManager()
        first = await m.ensure_file("model", "acme/w", "main", "a.bin")
        await _until(entered.is_set, "the first download to start")
        second = await m.ensure_file("model", "acme/w", "main", "b.bin")
        assert second.state == "pending"
        m.interrupt_active()
        release.set()
        await asyncio.wait(set(m._runners), timeout=5)
        return first, second

    first, second = asyncio.run(main())
    assert (first.state, second.state) == ("interrupted", "interrupted")
    assert second.started_at is None


def test_waiters_on_done_are_released_with_a_clear_answer(env, monkeypatch):
    """The wait paths in hfcompat block on job.done; an interrupted job must
    wake them, and what they then say must not be success or a 500."""
    entered, release = _blocking_download(monkeypatch, env)

    async def main():
        m = jobs.JobManager()
        job = await m.ensure_file("model", "acme/w", "main", "f.bin")
        await _until(entered.is_set, "the download to start")

        async def waiter():
            await job.tier_decided.wait()
            await job.done.wait()
            hfcompat._raise_unless_done(job)

        w = asyncio.create_task(waiter())
        await asyncio.sleep(0.05)
        assert not w.done()
        await m.stop()
        done, _ = await asyncio.wait({w}, timeout=2)
        release.set()
        assert done, "the waiter was not released by the stop"
        return w.exception()

    exc = asyncio.run(main())
    assert isinstance(exc, HTTPException)
    assert exc.status_code == 503
    assert "interrupted" in exc.detail


def test_a_streaming_response_ends_when_its_job_is_interrupted(env, monkeypatch):
    """tail_follow polls the partial file until the job is done or failed. An
    interrupted job is neither, so before this it would have polled forever."""
    entered, release = _blocking_download(monkeypatch, env)
    partial = env / "cache" / "partial.incomplete"
    partial.write_bytes(b"a" * 100)

    async def main():
        m = jobs.JobManager()
        job = await m.ensure_file("model", "acme/w", "main", "f.bin",
                                  expected_size=1000, incomplete_path=str(partial))
        await _until(entered.is_set, "the download to start")
        got = bytearray()

        async def consume():
            async for chunk in serving.tail_follow(job, env / "cache" / "final", 1000):
                got.extend(chunk)

        c = asyncio.create_task(consume())
        await _until(lambda: len(got) == 100, "the first bytes to stream")
        await m.stop()
        done, _ = await asyncio.wait({c}, timeout=2)
        release.set()
        if not done:
            c.cancel()
        return bool(done), bytes(got)

    ended, got = asyncio.run(main())
    assert ended, "the stream kept polling after its job was interrupted"
    assert got == b"a" * 100  # truncated: the client sees a short body
