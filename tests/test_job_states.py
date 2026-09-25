"""`done` means verified, and it arrives with finished_at.

THE DEFECT, from a real deployment: a snapshot job was marked `done` the moment
snapshot_download returned, and verification ran AFTER that. A 24 GB file sat at
state=done, finished_at=null for about three and a half minutes while it was
still being hashed. A client that gates on `done` would have treated an
unverified file as good -- and a file that then FAILED verification would have
been announced as done first.

The job now says `verifying` while it hashes, reaches `done` only when the
check passes, and sets finished_at in the same step. These tests hold the
verification open on an event and look at the job while it is in flight, which
is the only way to see the window the defect lived in.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import jobs
from app.config import settings


@pytest.fixture
def env(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(settings, "cache_dir", str(cache))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"), raising=False)
    monkeypatch.setattr(settings, "hf_verify_ingest", True)
    monkeypatch.setattr(jobs.JobManager, "_record_manifest", lambda self, job: None, raising=False)
    return cache


async def _wait_for(pred, what: str) -> None:
    for _ in range(500):
        if pred():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


def _blocked(result):
    """A stand-in verifier that holds until released, then returns `result`."""
    entered, release = threading.Event(), threading.Event()

    def verifier(*args, **kwargs):
        entered.set()
        assert release.wait(10), "test never released verification"
        if isinstance(result, Exception):
            raise result
        return result

    return verifier, entered, release


@pytest.mark.parametrize("kind", ["snapshot", "file"])
def test_never_done_while_verification_is_running(env, monkeypatch, kind):
    target = Path(env) / "payload"
    target.write_bytes(b"x")
    if kind == "snapshot":
        monkeypatch.setattr(jobs.JobManager, "_download_snapshot", lambda self, job: env)
        verifier, entered, release = _blocked(jobs.TreeVerify(3, 0, [], 5))
        monkeypatch.setattr(jobs, "verify_tree", verifier)
    else:
        monkeypatch.setattr(jobs.JobManager, "_download_file", lambda self, job: target)
        verifier, entered, release = _blocked("VERIFIED")
        monkeypatch.setattr(jobs, "verify_ingested", verifier)

    seen: list[tuple[str, float | None]] = []

    async def scenario():
        m = jobs.JobManager()
        if kind == "snapshot":
            job = await m.ensure_snapshot("model", "org/r", "main")
        else:
            job = await m.ensure_file("model", "org/r", "main", "payload")
        await _wait_for(entered.is_set, "verification to start")
        # Sample the job repeatedly while the hash is "running".
        for _ in range(20):
            seen.append((job.state, job.finished_at))
            await asyncio.sleep(0.005)
        # The ledger carries the same state a poller would see.
        m.flush()
        restarted = jobs.JobManager()
        restarted.load_ledger()
        in_ledger = restarted.get(job.id).state

        release.set()
        await job.done.wait()
        return job, in_ledger

    job, in_ledger = asyncio.run(scenario())
    assert {s for s, _ in seen} == {"verifying"}, seen
    assert all(f is None for _, f in seen), "finished_at set before the check finished"
    # `verifying` persists, and a restart during it is an interruption.
    assert in_ledger == "interrupted"
    assert job.state == "done"
    assert job.finished_at is not None
    assert job.verify["new_verified"] >= 1


def test_a_failed_verification_ends_in_error_never_done(env, monkeypatch):
    monkeypatch.setattr(jobs.JobManager, "_download_snapshot", lambda self, job: env)
    states: list[str] = []
    real_persist = jobs.JobManager._persist

    def spy(self, transition, urgent=False):
        for j in list(self._active.values()) + self._history:
            states.append(j.state)
        return real_persist(self, transition, urgent)

    monkeypatch.setattr(jobs.JobManager, "_persist", spy)
    monkeypatch.setattr(
        jobs, "verify_tree", lambda *a, **k: jobs.TreeVerify(2, 0, ["bad.bin: expected x"], 0)
    )

    async def scenario():
        m = jobs.JobManager()
        job = await m.ensure_snapshot("model", "org/r", "main")
        await job.done.wait()
        return job

    job = asyncio.run(scenario())
    assert job.state == "error"
    assert "IngestDigestMismatch" in job.error
    assert job.finished_at is not None
    assert "done" not in states, f"the job passed through done on its way to error: {states}"
    assert job.verify["mismatched"] == 1


def test_done_always_carries_finished_at_without_verification(env, monkeypatch):
    monkeypatch.setattr(settings, "hf_verify_ingest", False)
    monkeypatch.setattr(jobs.JobManager, "_download_snapshot", lambda self, job: env)

    async def scenario():
        m = jobs.JobManager()
        job = await m.ensure_snapshot("model", "org/r", "main")
        await job.done.wait()
        return job

    job = asyncio.run(scenario())
    assert job.state == "done"
    assert job.finished_at is not None
    assert job.verify is None, "no check ran, so none may be reported"


def test_a_reprewarm_says_files_were_already_present_not_that_none_were_checked(
    env, monkeypatch, caplog
):
    """Re-prewarming a complete repo verifies nothing new. The log used to say
    "0 verified, 0 unverifiable, 0 mismatched", which reads as "nothing was
    checked". It now counts what was already there."""
    import hashlib
    import os
    import time

    commit = "a" * 40
    repo = Path(env) / "models--org--r"
    (repo / "blobs").mkdir(parents=True)
    snap = repo / "snapshots" / commit
    snap.mkdir(parents=True)
    old = time.time() - 3600
    for name in ("a.bin", "b.bin"):
        body = name.encode() * 10
        blob = repo / "blobs" / hashlib.sha256(body).hexdigest()
        blob.write_bytes(body)
        os.utime(blob, (old, old))
        (snap / name).symlink_to(blob)
    monkeypatch.setattr(jobs.JobManager, "_download_snapshot", lambda self, job: snap)

    async def scenario():
        m = jobs.JobManager()
        job = await m.ensure_snapshot("model", "org/r", "main")
        await job.done.wait()
        return job

    with caplog.at_level("INFO", logger="xhc.jobs"):
        job = asyncio.run(scenario())
    assert job.state == "done"
    assert job.verify == {
        "new_verified": 0,
        "new_unverifiable": 0,
        "mismatched": 0,
        "already_present_not_reverified": 2,
        "verified_at_tier_read": 0,
    }
    assert "2 already present, not re-verified" in caplog.text
    assert job.to_dict()["verify"]["already_present_not_reverified"] == 2
