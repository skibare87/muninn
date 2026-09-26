"""Shutdown ends, and names whatever made it slow.

The defect this pins: a tier upload worker was cancelled at shutdown, the
cancel was swallowed inside an HTTP request (anyio's connect_tcp drops a
Task.cancel() that lands as a new connection is established), and the worker
went back to waiting on its queue. The lifespan awaited it forever. It hung
about one full-suite run in eight, on whichever test's shutdown the upload
happened to be connecting during.

Every stuck task here SWALLOWS cancellation, which is the case that matters: a
task that honours cancel() never tested a bound. Each is released at the end so
asyncio.run's own teardown, which also awaits every task, can finish.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import httpclients, shutdown, tier
from app.config import settings


def _stubborn(release: asyncio.Event, entered: asyncio.Event | None = None):
    """A coroutine that ignores every cancel until `release` is set."""

    async def run():
        if entered is not None:
            entered.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass

    return run()


def test_cancel_and_wait_abandons_a_task_that_ignores_cancel(caplog):
    async def main():
        release = asyncio.Event()
        stuck = asyncio.create_task(_stubborn(release))
        fine = asyncio.create_task(asyncio.sleep(3600))
        await asyncio.sleep(0)
        t0 = time.monotonic()
        with caplog.at_level(logging.WARNING, logger="xhc.shutdown"):
            ok = await shutdown.cancel_and_wait([stuck, None, fine], "the test loops", timeout=0.2)
        elapsed = time.monotonic() - t0
        release.set()
        await stuck
        return ok, elapsed, fine.cancelled()

    ok, elapsed, fine_cancelled = asyncio.run(main())
    assert ok is False
    assert elapsed < 2, elapsed
    assert fine_cancelled, "a task that honours cancel is still cancelled and awaited"
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "the test loops" in msg
    assert "_stubborn" in msg, "the warning names the task that overran: " + msg


def test_cancel_and_wait_is_quiet_when_everything_stops(caplog):
    async def main():
        tasks = [asyncio.create_task(asyncio.sleep(3600)) for _ in range(3)]
        await asyncio.sleep(0)
        with caplog.at_level(logging.WARNING, logger="xhc.shutdown"):
            return await shutdown.cancel_and_wait(tasks, "loops", timeout=5)

    assert asyncio.run(main()) is True
    assert not caplog.records


def test_bounded_abandons_a_step_that_overruns(caplog):
    async def main():
        release = asyncio.Event()
        t0 = time.monotonic()
        with caplog.at_level(logging.WARNING, logger="xhc.shutdown"):
            ok = await shutdown.bounded(_stubborn(release), "the stuck step", timeout=0.2)
        elapsed = time.monotonic() - t0
        release.set()
        return ok, elapsed

    ok, elapsed = asyncio.run(main())
    assert ok is False
    assert elapsed < 2, elapsed
    assert "the stuck step did not finish" in " ".join(r.getMessage() for r in caplog.records)


def test_bounded_logs_a_failing_step_and_returns(caplog):
    async def boom():
        raise RuntimeError("close failed")

    with caplog.at_level(logging.ERROR, logger="xhc.shutdown"):
        assert asyncio.run(shutdown.bounded(boom(), "the failing step")) is False
    assert "the failing step failed" in caplog.text


def test_a_tier_worker_whose_cancel_was_swallowed_still_exits(monkeypatch):
    """The exact shape of the hang, without the network race that made it rare.

    process() here swallows the cancel the way the HTTP stack did and returns
    normally. Before the fix the worker then went back to q.get() and waited
    for an item that would never come.
    """
    tier.reset_for_tests()
    monkeypatch.setattr(settings, "tier", None)

    async def main():
        inside = asyncio.Event()
        swallow = [True]

        async def swallowing_process(item):
            inside.set()
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                if not swallow[0]:
                    raise
                return "ok"  # the request "completed"; the cancel is gone

        monkeypatch.setattr(tier, "process", swallowing_process)
        tier._ensure_queue().put_nowait(tier._content("k", Path("/nonexistent"), "0" * 64))
        worker = asyncio.create_task(tier._worker())
        await asyncio.wait_for(inside.wait(), 5)
        worker.cancel()
        done, _ = await asyncio.wait({worker}, timeout=5)
        if not done:
            # Unstick it so the test fails instead of hanging in teardown.
            swallow[0] = False
            tier._ensure_queue().put_nowait(tier._content("k", Path("/x"), "0" * 64))
            return "still running"
        return "cancelled" if worker.cancelled() else f"ended: {worker.exception()!r}"

    try:
        assert asyncio.run(main()) == "cancelled"
    finally:
        tier.reset_for_tests()


def test_the_lifespan_shutdown_finishes_past_a_stuck_step(tmp_path, monkeypatch, caplog):
    """A step that never finishes delays shutdown by the bound, is named, and
    does not stop the steps after it."""
    from app.main import app, lifespan

    (tmp_path / "cache").mkdir()
    monkeypatch.setattr(settings, "cache_dir", str(tmp_path / "cache"))
    monkeypatch.setattr(settings, "state_dir", None)
    monkeypatch.setattr(settings, "docker_enabled", False)
    monkeypatch.setattr(settings, "orphan_check_interval_s", 0.0)
    monkeypatch.setattr(settings, "jwt_issuers", [])
    monkeypatch.setattr(settings, "tier", None)
    monkeypatch.setattr(shutdown, "STEP_TIMEOUT_S", 0.3)

    state: dict = {}
    real_close_all = httpclients.close_all

    async def stuck_stop():
        await _stubborn(state["release"])

    async def recording_close_all():
        state["closed"] = True
        await real_close_all()

    monkeypatch.setattr(tier, "stop", stuck_stop)
    monkeypatch.setattr(httpclients, "close_all", recording_close_all)

    async def main():
        state["release"] = asyncio.Event()
        async with lifespan(app):
            pass
        state["release"].set()

    t0 = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="xhc.shutdown"):
        asyncio.run(asyncio.wait_for(main(), 20))
    elapsed = time.monotonic() - t0

    assert elapsed < 10, f"shutdown took {elapsed:.1f}s with a 0.3s step bound"
    assert "tier.stop() did not finish" in caplog.text
    assert state.get("closed"), "the step after the stuck one still ran"


def test_the_orphan_loop_exits_when_its_cancel_was_swallowed(monkeypatch):
    """The orphan sweep is Hub HTTP, so it has the same exposure as the tier."""
    from app import orphans

    monkeypatch.setattr(settings, "orphan_check_interval_s", 0.01)

    async def main():
        inside = asyncio.Event()
        swallow = [True]

        async def swallowing_check_all(**_kw):
            inside.set()
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                if not swallow[0]:
                    raise
                return {"checked": 0, "orphaned_total": 0, "inconclusive": 0}

        monkeypatch.setattr(orphans, "check_all", swallowing_check_all)
        loop_task = asyncio.create_task(orphans.orphan_loop())
        await asyncio.wait_for(inside.wait(), 5)
        loop_task.cancel()
        done, _ = await asyncio.wait({loop_task}, timeout=2)
        if not done:
            swallow[0] = False  # so asyncio.run's teardown can stop it
            return "still running"
        return "cancelled" if loop_task.cancelled() else repr(loop_task.exception())

    assert asyncio.run(main()) == "cancelled"
