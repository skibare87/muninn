"""OCI prewarm jobs survive the process that ran them.

THE DEFECT: POST /_cache/docker/prewarm kept its jobs in a dict in memory. It
was unbounded, and a restart emptied it, so anyone polling a prewarm across a
redeploy got "no such job" and could not tell a finished pull from a dead one
-- the exact failure the HF job ledger was built to end, left open on the other
protocol. The table now uses the same machinery (app/ledger.py), in the OCI
state dir.

A restart is simulated the way test_job_ledger.py does it: a SECOND manager
over the same state dir. Nothing in memory survives, only the file.

The test that fails against the in-memory table is
test_the_status_endpoint_answers_for_a_job_after_a_restart: before the ledger,
GET /_cache/docker/prewarm/{id} for a job from a previous process was a 404.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import ocicompat, ocimanage, ocistore, registry
from app.config import settings

UP = "ghcr.io"
IMAGE = f"{UP}/org/app:v1"
MEDIA = "application/vnd.oci.image.manifest.v1+json"


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "docker_dir", str(tmp_path / "docker"))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"), raising=False)
    ocistore.reset_stats_cache()
    return tmp_path / "state" / "oci"


def _ledger(state_oci: Path) -> Path:
    return state_oci / "prewarm.json"


class FakeUpstream:
    """A registry with one image: a manifest naming a config and two layers.

    Blob fetches go through a fake _ensure_blob that writes the blob to its
    content address, which is what the real one does after a digest match.
    `gate` holds a blob back until the test releases it.
    """

    def __init__(self, layers=(b"layer-one", b"layer-two")):
        self.cfg = b"config-bytes"
        self.layers = list(layers)
        self.blobs = {ocistore.compute_digest(b): b for b in [self.cfg, *self.layers]}
        self.manifest = json.dumps({
            "schemaVersion": 2,
            "config": {"digest": ocistore.compute_digest(self.cfg)},
            "layers": [{"digest": ocistore.compute_digest(b)} for b in self.layers],
        }).encode()
        self.manifest_digest = ocistore.compute_digest(self.manifest)
        self.fetched: list[str] = []
        self.gate: dict[str, asyncio.Event] = {}
        self.reached: dict[str, asyncio.Event] = {}
        self.swallow_cancel = False

    def layer_digest(self, i: int) -> str:
        return ocistore.compute_digest(self.layers[i])

    async def get(self, ref, path, headers=None, **_kw):
        return SimpleNamespace(
            status_code=200, content=self.manifest,
            headers={"content-type": MEDIA, "docker-content-digest": self.manifest_digest},
        )

    async def ensure_blob(self, ref, digest):
        self.reached.setdefault(digest, asyncio.Event()).set()
        gate = self.gate.get(digest)
        if gate is not None:
            if self.swallow_cancel:
                # What anyio's connect_tcp does to a cancel that lands at the
                # wrong instant: the call completes, the cancel is gone.
                try:
                    await gate.wait()
                except asyncio.CancelledError:
                    pass
            else:
                await gate.wait()
        body = self.blobs[digest]
        p = ocistore.blob_path(ref.upstream, digest)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body)
        self.fetched.append(digest)
        ev = asyncio.Event()
        ev.set()
        return SimpleNamespace(done=ev, state="done", size=len(body), error=None)


@pytest.fixture
def upstream(monkeypatch):
    up = FakeUpstream()
    monkeypatch.setattr(registry, "get", up.get)
    monkeypatch.setattr(ocicompat, "_ensure_blob", up.ensure_blob)
    return up


def _submit(m: ocimanage.PrewarmManager, image: str = IMAGE, pin: bool = False):
    name, reference = ocimanage._split(image)
    return m.submit(image, registry.resolve(name), reference, pin)


async def _wait_for(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


# -- restart -------------------------------------------------------------------


def test_a_running_prewarm_survives_a_restart_as_interrupted_with_its_progress(
        state, upstream):
    second = upstream.layer_digest(1)
    upstream.gate[second] = asyncio.Event()

    async def scenario():
        m1 = ocimanage.PrewarmManager()
        job = _submit(m1)
        await _wait_for(lambda: second in upstream.reached)
        assert job.state == "running"
        m1.flush()
        # --- the process dies here; a new one starts over the same state dir.
        m2 = ocimanage.PrewarmManager()
        m2.load_ledger()
        seen = m2.get(job.id)
        upstream.gate[second].set()
        await job.done.wait()
        return job, seen, m2

    job, seen, m2 = asyncio.run(scenario())
    assert seen is not None, "the prewarm vanished across the restart: 'no such job'"
    d = seen.as_dict()
    assert d["state"] == "interrupted"
    assert d["interrupted_at"] is not None
    assert d["image"] == IMAGE
    # config + first layer landed before the kill; that is the recorded progress.
    assert d["blobs_total"] == 3
    assert d["blobs_done"] == 2
    assert d["bytes_done"] == len(upstream.cfg) + len(upstream.layers[0])
    assert "note" in d
    # History, not work: not resumed behind the operator's back.
    assert not m2._active


def test_a_finished_prewarm_survives_a_restart_as_finished(state, upstream):
    async def scenario():
        m1 = ocimanage.PrewarmManager()
        job = _submit(m1)
        await job.done.wait()
        return job

    job = asyncio.run(scenario())
    assert job.state == "done", job.error

    m2 = ocimanage.PrewarmManager()
    m2.load_ledger()
    seen = m2.get(job.id)
    assert seen is not None
    assert seen.state == "done"
    assert seen.finished_at == pytest.approx(job.finished_at)
    assert seen.blobs_done == 3 and seen.blobs_total == 3
    assert seen.interrupted_at is None


def test_the_ledger_lives_under_the_oci_state_dir(state):
    from app import statedir

    assert statedir.oci_file("prewarm.json") == _ledger(state)


# -- corrupt ledger ----------------------------------------------------------------


def test_a_corrupt_prewarm_ledger_is_preserved_and_replaced_not_fatal(state):
    ledger = _ledger(state)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("{ not json")

    m = ocimanage.PrewarmManager()
    m.load_ledger()  # must not raise

    assert m.list() == []
    kept = list(ledger.parent.glob("prewarm.json.corrupt*"))
    assert kept, "the unreadable ledger was not preserved for inspection"
    assert kept[0].read_text() == "{ not json"
    assert "preserved" in m.ledger_status()["error"]
    m.flush()
    assert json.loads(ledger.read_text())["jobs"] == []


def _app_client(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setattr(settings, "docker_enabled", True)
    monkeypatch.setattr(settings, "manage_token", "oci-ledger-test-token")
    # A fresh table per test: the module-level manager is what the routes use.
    # Guarded so that, run against the old in-memory table, the endpoint test
    # fails on its assertion (a 404) rather than on a missing name.
    if hasattr(ocimanage, "PrewarmManager"):
        monkeypatch.setattr(ocimanage, "manager", ocimanage.PrewarmManager())
    return TestClient(app, headers={"authorization": "Bearer oci-ledger-test-token"})


def test_a_corrupt_prewarm_ledger_does_not_stop_the_service(state, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "cache_dir", str(tmp_path / "cache"))
    ledger = _ledger(state)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_bytes(b"\x00\xff garbage")
    # A cached blob, so "serving is unaffected" is a pull and not just a boot.
    body = b"cached layer"
    d = ocistore.compute_digest(body)
    p = ocistore.blob_path(UP, d)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(body)

    with _app_client(monkeypatch) as client:
        r = client.get(f"/v2/{UP}/org/img/blobs/{d}")
        assert r.status_code == 200, r.text
        assert r.content == body
        listing = client.get("/_cache/docker/prewarm")
        assert listing.status_code == 200
        assert listing.json()["jobs"] == []
        assert listing.json()["ledger"]["persisting"] is True
    assert list(ledger.parent.glob("prewarm.json.corrupt*"))


# -- the endpoint across a restart ---------------------------------------------------


def test_the_status_endpoint_answers_for_a_job_after_a_restart(state, monkeypatch, tmp_path):
    """FAILS against the in-memory table: this was a 404."""
    monkeypatch.setattr(settings, "cache_dir", str(tmp_path / "cache"))
    now = time.time()
    ledger = _ledger(state)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(json.dumps({"version": 1, "jobs": [
        {"id": "was-running", "image": IMAGE, "pin": False, "kind": "prewarm",
         "state": "running", "created_at": now - 60, "started_at": now - 59,
         "blobs_total": 10, "blobs_done": 4, "bytes_done": 4096,
         "updated_at": now - 5},
        {"id": "was-done", "image": IMAGE, "pin": False, "kind": "prewarm",
         "state": "done", "created_at": now - 600, "started_at": now - 599,
         "finished_at": now - 500, "blobs_total": 3, "blobs_done": 3},
    ]}))

    with _app_client(monkeypatch) as client:
        r = client.get("/_cache/docker/prewarm/was-running")
        assert r.status_code == 200, r.text
        job = r.json()["job"]
        assert job["state"] == "interrupted"
        assert job["blobs_done"] == 4 and job["blobs_total"] == 10
        assert job["bytes_done"] == 4096
        r = client.get("/_cache/docker/prewarm/was-done")
        assert r.status_code == 200
        assert r.json()["job"]["state"] == "done"
        assert client.get("/_cache/docker/prewarm/never-existed").status_code == 404

    # The interrupted mark was persisted at load, not only reported.
    on_disk = {j["id"]: j for j in json.loads(ledger.read_text())["jobs"]}
    assert on_disk["was-running"]["state"] == "interrupted"


# -- bound ---------------------------------------------------------------------------


def test_the_prewarm_ledger_honours_its_bound(state, monkeypatch):
    monkeypatch.setattr(ocimanage, "_HISTORY_LIMIT", 5)
    now = time.time()
    records = [
        {"id": f"p{i:02d}", "image": f"{UP}/org/r{i}:v1", "kind": "prewarm",
         "state": "done", "created_at": now - 100 + i, "finished_at": now - 90 + i}
        for i in range(20)
    ]
    records.append({
        "id": "ancient", "image": f"{UP}/org/old:v1", "kind": "prewarm", "state": "done",
        "created_at": now - ocimanage._RETENTION_S - 100,
        "finished_at": now - ocimanage._RETENTION_S - 50,
    })
    ledger = _ledger(state)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(json.dumps({"version": 1, "jobs": records}))

    m = ocimanage.PrewarmManager()
    m.load_ledger()
    assert len(m.list()) == 5
    assert m.get("ancient") is None
    assert m.get("p19") is not None and m.get("p00") is None
    assert len(json.loads(ledger.read_text())["jobs"]) == 5


def test_finishing_jobs_are_pruned_to_the_bound_as_they_finish(state, upstream, monkeypatch):
    monkeypatch.setattr(ocimanage, "_HISTORY_LIMIT", 3)

    async def scenario():
        m = ocimanage.PrewarmManager()
        for i in range(8):
            job = _submit(m, f"{UP}/org/app:v{i}")
            await job.done.wait()
        return m

    m = asyncio.run(scenario())
    assert len(m.list()) == 3
    assert len(m._by_id) == 3, "pruned jobs must leave the id index too"


def test_progress_writes_are_throttled(state):
    m = ocimanage.PrewarmManager()
    writes = []
    real = ocimanage.PrewarmManager._write_ledger

    def spy(self):
        writes.append(1)
        real(self)

    ocimanage.PrewarmManager._write_ledger = spy
    try:
        m._persist(transition=True)
        for _ in range(50):
            m._persist(transition=False)
    finally:
        ocimanage.PrewarmManager._write_ledger = real
    assert len(writes) == 1


# -- verify semantics ------------------------------------------------------------------


def test_done_is_never_reported_before_verification(state, upstream, monkeypatch):
    seen: list[tuple[str, float | None]] = []
    real = ocimanage.PrewarmManager._persist

    def spy(self, transition, urgent=False):
        for j in list(self._active.values()) + self._history:
            seen.append((j.state, j.finished_at))
        return real(self, transition, urgent)

    monkeypatch.setattr(ocimanage.PrewarmManager, "_persist", spy)

    async def scenario():
        m = ocimanage.PrewarmManager()
        job = _submit(m)
        await job.done.wait()
        return job

    job = asyncio.run(scenario())
    states = [s for s, _ in seen]
    assert "verifying" in states
    assert states.index("verifying") < states.index("done")
    # Nothing observed `done` without finished_at, or finished_at before done.
    assert all((s == "done") == (f is not None) for s, f in seen), seen
    assert job.state == "done"


def test_a_closure_collected_mid_prewarm_ends_in_error_not_done(state, upstream):
    """A by-digest prewarm is referenced by no tag; a sweep mid-pull takes the
    early layers. Before the verifying step this reported `done`."""
    second = upstream.layer_digest(1)
    upstream.gate[second] = asyncio.Event()
    image = f"{UP}/org/app@{upstream.manifest_digest}"

    async def scenario():
        m = ocimanage.PrewarmManager()
        job = _submit(m, image)
        await _wait_for(lambda: second in upstream.reached)
        # The GC sweep, as far as this job can tell.
        ocistore.blob_path(UP, upstream.layer_digest(0)).unlink()
        upstream.gate[second].set()
        await job.done.wait()
        return job

    job = asyncio.run(scenario())
    assert job.state == "error"
    assert "no longer on disk" in job.error
    assert job.finished_at is not None


def test_a_by_digest_manifest_is_checked_against_the_digest_asked_for(state, upstream):
    """The header comes from the same response as the body, so checking one
    against the other proves only that the response agrees with itself."""
    asked = "sha256:" + "a" * 64

    async def scenario():
        m = ocimanage.PrewarmManager()
        job = _submit(m, f"{UP}/org/app@{asked}")
        await job.done.wait()
        return job

    job = asyncio.run(scenario())
    assert job.state == "error"
    assert "DigestMismatch" in job.error
    assert not ocistore.manifest_path(UP, upstream.manifest_digest).exists()


# -- resume ------------------------------------------------------------------------------


def test_resubmitting_an_interrupted_prewarm_resumes_it(state, upstream):
    second = upstream.layer_digest(1)
    upstream.gate[second] = asyncio.Event()

    async def first_process():
        m1 = ocimanage.PrewarmManager()
        job = _submit(m1)
        await _wait_for(lambda: second in upstream.reached)
        m1.flush()
        return m1, job

    async def scenario():
        m1, job1 = await first_process()
        # Kill: stop the task without letting it record anything further.
        for t in list(m1._tasks):
            t.cancel()
        await asyncio.sleep(0)
        m2 = ocimanage.PrewarmManager()
        m2.load_ledger()
        assert m2.get(job1.id).state == "interrupted"
        fetched_before = list(upstream.fetched)
        upstream.gate.pop(second)
        job2 = _submit(m2)
        await job2.done.wait()
        return job1, job2, fetched_before

    job1, job2, fetched_before = asyncio.run(scenario())
    assert job2.state == "done", job2.error
    assert job2.resumes == job1.id
    # Only the layer the first run did not finish was fetched again.
    assert upstream.fetched[len(fetched_before):] == [second]
    assert job2.blobs_present == 2 and job2.blobs_done == 3


def test_resubmitting_a_running_prewarm_joins_it(state, upstream):
    second = upstream.layer_digest(1)
    upstream.gate[second] = asyncio.Event()

    async def scenario():
        m = ocimanage.PrewarmManager()
        a = _submit(m)
        b = _submit(m)
        c = _submit(m, pin=True)
        upstream.gate[second].set()
        await a.done.wait()
        await c.done.wait()
        return a, b, c

    a, b, c = asyncio.run(scenario())
    assert a is b, "a second POST for a running prewarm started a duplicate"
    assert c is not a, "a pinned request must not join an unpinned prewarm"


# -- shutdown ------------------------------------------------------------------------------


def test_shutdown_records_a_running_prewarm_as_interrupted(state, upstream):
    second = upstream.layer_digest(1)
    upstream.gate[second] = asyncio.Event()

    async def scenario():
        m = ocimanage.PrewarmManager()
        job = _submit(m)
        await _wait_for(lambda: second in upstream.reached)
        await asyncio.wait_for(m.stop(), 5)
        return job

    job = asyncio.run(scenario())
    assert job.state == "interrupted"
    rec = {j["id"]: j for j in json.loads(_ledger(state).read_text())["jobs"]}[job.id]
    assert rec["state"] == "interrupted"
    assert rec["blobs_done"] == 2


def test_shutdown_stops_a_prewarm_whose_cancel_was_swallowed(state, monkeypatch):
    """httpx/anyio can swallow a cancel. The blob loop must notice anyway rather
    than go on to open the next layer's upstream request."""
    up = FakeUpstream(layers=(b"l0", b"l1", b"l2", b"l3"))
    monkeypatch.setattr(registry, "get", up.get)
    monkeypatch.setattr(ocicompat, "_ensure_blob", up.ensure_blob)
    up.swallow_cancel = True
    held = up.layer_digest(1)
    up.gate[held] = asyncio.Event()

    async def scenario():
        m = ocimanage.PrewarmManager()
        job = _submit(m)
        await _wait_for(lambda: held in up.reached)
        await asyncio.wait_for(m.stop(), 5)
        return job

    job = asyncio.run(scenario())
    assert job.state == "interrupted"
    assert up.layer_digest(2) not in up.reached, "the loop went on after a cancel"
