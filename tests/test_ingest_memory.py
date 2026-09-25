"""Files-in-flight per snapshot, and the startup memory-limit warning."""

from __future__ import annotations

from app import jobs, memcheck
from app.config import settings

GIB = 1 << 30


def test_snapshot_ingest_passes_the_configured_max_workers(monkeypatch, tmp_path):
    seen = {}

    def fake_snapshot_download(**kw):
        seen.update(kw)
        return str(tmp_path)

    monkeypatch.setattr(jobs, "snapshot_download", fake_snapshot_download)
    monkeypatch.setattr(settings, "snapshot_max_workers", 3)
    mgr = jobs.JobManager()
    monkeypatch.setattr(mgr, "_record_manifest", lambda job: None)
    job = jobs.Job(id="t1", kind="snapshot", repo_type="model", repo_id="org/m", revision="main")
    mgr._download_snapshot(job)
    assert seen["max_workers"] == 3


def test_the_default_is_one_file_in_flight():
    from app.config import Settings

    assert Settings().snapshot_max_workers == 1


def test_no_limit_means_no_warning(tmp_path):
    f = tmp_path / "memory.max"
    f.write_text("max\n")
    assert memcheck.cgroup_limit(str(f)) is None


def test_an_unreadable_limit_is_unknown_not_zero(tmp_path):
    assert memcheck.cgroup_limit(str(tmp_path / "absent")) is None


def test_a_limit_below_the_measured_need_warns(monkeypatch):
    monkeypatch.delenv("HF_HUB_DISABLE_XET", raising=False)
    msg = memcheck.check(4, 8, limit=3 * GIB)
    assert msg and "XHC_INGEST_CONCURRENCY=4" in msg and "interrupted" in msg


def test_a_generous_limit_is_quiet(monkeypatch):
    monkeypatch.delenv("HF_HUB_DISABLE_XET", raising=False)
    assert memcheck.check(1, 1, limit=4 * GIB) is None


def test_xet_off_needs_far_less(monkeypatch):
    monkeypatch.setenv("HF_HUB_DISABLE_XET", "1")
    assert memcheck.check(4, 8, limit=1 * GIB) is None
    monkeypatch.delenv("HF_HUB_DISABLE_XET")
    assert memcheck.check(4, 8, limit=1 * GIB) is not None


def test_the_reported_oom_configuration_warns(monkeypatch):
    # A real deployment: one job, the old fixed 8 files in flight, a 4 GiB limit,
    # OOM-killed on a five-shard snapshot.
    monkeypatch.delenv("HF_HUB_DISABLE_XET", raising=False)
    assert memcheck.check(1, 8, limit=4 * GIB) is not None
    assert memcheck.check(1, 1, limit=4 * GIB) is None
