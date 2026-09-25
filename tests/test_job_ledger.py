"""Ingest jobs survive the process that ran them.

THE DEFECT, from a real deployment: a pod was OOM-killed during four concurrent
snapshot prewarms and restarted. Afterwards /_cache/jobs was empty, so anything
polling a job id got "no such job" and could not tell whether the work had
finished or died. Those are opposite answers and the API gave neither.

So the job table is written to a small ledger under the HF state dir, and a job
the ledger last saw queued or running comes back as `interrupted` -- not resumed
behind the operator's back, and not dropped.

These tests simulate a restart by building a SECOND JobManager over the same
state dir, which is what a new process does: nothing in memory survives, only
the file.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import jobs, statedir
from app.config import settings


@pytest.fixture
def state(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(settings, "cache_dir", str(cache))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"), raising=False)
    monkeypatch.setattr(settings, "hf_verify_ingest", False)
    return tmp_path / "state" / "hf"


def _ledger(state_hf: Path) -> Path:
    return state_hf / "jobs.json"


def test_a_running_job_survives_a_restart_as_interrupted_with_its_progress(state, monkeypatch):
    release = threading.Event()
    started = threading.Event()

    def blocked_snapshot(self, job):
        started.set()
        release.wait(10)
        raise RuntimeError("released")

    monkeypatch.setattr(jobs.JobManager, "_download_snapshot", blocked_snapshot)
    monkeypatch.setattr(jobs.JobManager, "_record_manifest", lambda self, job: None, raising=False)

    async def scenario():
        m1 = jobs.JobManager()
        job = await m1.ensure_snapshot("model", "org/big", "main", ["*.safetensors"])
        for _ in range(200):
            if started.is_set() and job.state == "running":
                break
            await asyncio.sleep(0.01)
        assert job.state == "running"
        # What the watcher would have measured by now.
        job.final_bytes = 32 * 1024 * 1024
        m1.flush()

        # --- the process dies here; a new one starts over the same state dir.
        m2 = jobs.JobManager()
        m2.load_ledger()
        seen = m2.get(job.id)

        release.set()
        await job.done.wait()
        return job.id, seen, m2

    job_id, seen, m2 = asyncio.run(scenario())
    assert seen is not None, "the job vanished across the restart: 'no such job'"
    d = seen.to_dict()
    assert d["state"] == "interrupted"
    assert d["interrupted_at"] is not None
    assert d["downloaded_bytes"] == 32 * 1024 * 1024
    assert d["allow_patterns"] == ["*.safetensors"]
    assert d["repo_id"] == "org/big"
    # An interrupted job is history, not work: it must not be resumed silently,
    # and a fresh prewarm of the same repo must start a new job rather than join
    # a dead one.
    assert all(j.id != job_id for j in m2._active.values())
    assert seen.done.is_set()


def test_a_finished_job_survives_a_restart_as_finished(state, monkeypatch):
    monkeypatch.setattr(
        jobs.JobManager, "_download_snapshot", lambda self, job: Path(settings.cache_dir)
    )
    monkeypatch.setattr(jobs.JobManager, "_record_manifest", lambda self, job: None, raising=False)

    async def scenario():
        m1 = jobs.JobManager()
        job = await m1.ensure_snapshot("model", "org/small", "main")
        await job.done.wait()
        # Let the finally-block bookkeeping run.
        await asyncio.sleep(0)
        return job

    job = asyncio.run(scenario())
    assert job.state == "done"

    m2 = jobs.JobManager()
    m2.load_ledger()
    seen = m2.get(job.id)
    assert seen is not None
    assert seen.state == "done"
    assert seen.finished_at == pytest.approx(job.finished_at)
    assert seen.to_dict()["interrupted_at"] is None


def test_a_corrupt_ledger_is_preserved_and_replaced_not_fatal(state):
    ledger = _ledger(state)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("{ this is not json")

    m = jobs.JobManager()
    m.load_ledger()  # must not raise

    assert m.list() == []
    kept = sorted(p.name for p in ledger.parent.glob("jobs.json.corrupt*"))
    assert kept, "the unreadable ledger was not preserved for inspection"
    assert (ledger.parent / kept[0]).read_text() == "{ this is not json"

    # And the next write produces a fresh, readable ledger.
    m.flush()
    assert json.loads(ledger.read_text())["jobs"] == []


def test_a_corrupt_ledger_does_not_stop_the_service_serving(state, monkeypatch):
    """The whole service, through its real startup, with a garbage ledger."""
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setattr(settings, "docker_enabled", False)
    ledger = _ledger(state)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_bytes(b"\x00\xff garbage")

    # A cached file, addressed by commit so no upstream revalidation happens.
    commit = "c" * 40
    repo = Path(settings.cache_dir) / "models--org--m"
    (repo / "blobs").mkdir(parents=True)
    (repo / "snapshots" / commit).mkdir(parents=True)
    blob = repo / "blobs" / ("e" * 64)
    blob.write_bytes(b"cached bytes")
    (repo / "snapshots" / commit / "config.json").symlink_to(blob)

    monkeypatch.setattr(settings, "manage_token", "ledger-test-token")
    with TestClient(app) as client:
        r = client.get(f"/org/m/resolve/{commit}/config.json")
        assert r.status_code == 200
        assert r.content == b"cached bytes"
        jobs_r = client.get("/_cache/jobs", headers={"authorization": "Bearer ledger-test-token"})
        assert jobs_r.status_code == 200
    assert list(ledger.parent.glob("jobs.json.corrupt*"))


def test_the_ledger_honours_its_bound(state, monkeypatch):
    monkeypatch.setattr(jobs, "_HISTORY_LIMIT", 5)
    monkeypatch.setattr(jobs, "_SNAPSHOT_HISTORY_LIMIT", 3)
    now = time.time()
    records = []
    for i in range(20):
        kind = "snapshot" if i % 2 else "file"
        records.append({
            "id": f"j{i:02d}", "kind": kind, "repo_type": "model",
            "repo_id": f"org/r{i}", "revision": "main",
            "filename": None if kind == "snapshot" else "a.bin",
            "state": "done", "created_at": now - 100 + i,
            "started_at": now - 100 + i, "finished_at": now - 90 + i,
        })
    # One record older than the retention window, however few there are.
    records.append({
        "id": "ancient", "kind": "snapshot", "repo_type": "model",
        "repo_id": "org/old", "revision": "main", "state": "done",
        "created_at": now - jobs._RETENTION_S - 100,
        "started_at": now - jobs._RETENTION_S - 100,
        "finished_at": now - jobs._RETENTION_S - 50,
    })
    ledger = _ledger(state)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(json.dumps({"version": 1, "jobs": records}))

    m = jobs.JobManager()
    m.load_ledger()
    kept = m.list()
    assert sum(1 for j in kept if j.kind == "file") == 5
    assert sum(1 for j in kept if j.kind == "snapshot") == 3
    assert m.get("ancient") is None
    # Newest are kept.
    assert m.get("j19") is not None and m.get("j18") is not None
    assert m.get("j00") is None

    m.flush()
    on_disk = json.loads(ledger.read_text())["jobs"]
    assert len(on_disk) == 8


def test_progress_writes_are_throttled_but_transitions_are_not(state, monkeypatch):
    """A large snapshot ticks progress every few seconds; that must not become
    a disk write every tick. A state change must still reach disk at once."""
    m = jobs.JobManager()
    job = jobs.Job(id="p1", kind="snapshot", repo_type="model", repo_id="org/p",
                   revision="main")
    m._by_id[job.id] = job
    m._active[job.key] = job

    writes = []
    real = jobs.JobManager._write_ledger
    monkeypatch.setattr(jobs.JobManager, "_write_ledger",
                        lambda self: (writes.append(time.time()), real(self)))

    m._persist(transition=True)
    assert len(writes) == 1
    for _ in range(50):
        m._persist(transition=False)
    assert len(writes) == 1, "progress ticks were not throttled"


def test_status_reports_process_start(state, monkeypatch):
    from app import manage

    monkeypatch.setattr(settings, "docker_enabled", False)
    body = asyncio.run(manage.status())
    assert isinstance(body["started_at"], float)
    assert body["started_at"] <= time.time()
    assert body["uptime_s"] >= 0


def test_the_ledger_lives_under_the_hf_state_dir(state):
    assert statedir.hf_file("jobs.json") == _ledger(state)


def test_a_burst_of_transitions_is_coalesced_and_still_reaches_disk(state, monkeypatch):
    """A client walking a 400-file repo produces a state change per miss. That
    must cost a few writes, not one per change -- and the last change must
    still land."""
    m = jobs.JobManager()
    writes = []
    real = jobs.JobManager._write_ledger
    monkeypatch.setattr(jobs.JobManager, "_write_ledger",
                        lambda self: (writes.append(1), real(self)))

    async def burst():
        for i in range(100):
            job = jobs.Job(id=f"b{i}", kind="file", repo_type="model",
                           repo_id="org/b", revision="main", filename=f"f{i}")
            m._by_id[job.id] = job
            m._active[job.key] = job
            m._persist(transition=True)
        await asyncio.sleep(jobs._TRANSITION_COALESCE_S + 0.2)

    asyncio.run(burst())
    assert len(writes) == 2, writes  # leading edge + one trailing write
    on_disk = json.loads(_ledger(state).read_text())["jobs"]
    assert len(on_disk) == 100
