"""Stale `.incomplete` partials in the HF cache are reclaimed, and live ones never are.

THE DEFECT: huggingface_hub downloads into `blobs/<etag>.incomplete` and renames
it on completion; Muninn's tier fill writes `blobs/<etag>.tier.incomplete`. A
process killed mid-file leaves the partial behind. Measured with SIGKILL, on the
plain-HTTP path and on a real xet download alike: the partial, a 0-byte
`.locks/<repo>/<etag>.lock`, `refs/<rev>` and an empty `snapshots/<commit>/`.
The hub resumes an HTTP partial only if the same file is requested again; the
xet path rewrites it from offset 0 and never resumes it; nothing resumes a
`.tier.incomplete`. scan_cache_dir() counts only blobs a snapshot links to, so
the bytes used the disk and were invisible to eviction, and nothing removed them.

THE RULE, the same one v0.9.29 applied to OCI partials: a partial is removed
only when ALL of these hold --
  1. no download in THIS process owns it (a JobManager job, or a tier fill);
  2. no process holds huggingface_hub's own per-blob lock for it
     (`.locks/<repo>/<etag>.lock`, a filelock flock held for the life of the
     partial -- this is what reaches a second process sharing the cache);
  3. it has not been written for XHC_HF_PARTIAL_MAX_AGE (default 6h).

Fails against v0.9.29: every test here -- there was no sweep, no
`owns_partial`, and eviction's `used` did not include partial bytes.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import cachefs, jobs, tier
from app.config import settings

DAY = 24 * 3600
REPO = "acme/model"
FOLDER = "models--acme--model"
ETAG = "a" * 64


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "cache_dir", str(tmp_path / "cache"))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"), raising=False)
    monkeypatch.setattr(jobs.manager, "_active", {})
    monkeypatch.setattr(tier, "_filling", {}, raising=False)
    monkeypatch.setattr(cachefs, "_last_partial_sweep", None, raising=False)
    (tmp_path / "cache").mkdir()
    return tmp_path / "cache"


def _partial(cache: Path, etag: str = ETAG, suffix: str = ".incomplete",
             body: bytes = b"half-a-shard", age_s: float = DAY,
             folder: str = FOLDER) -> Path:
    blobs = cache / folder / "blobs"
    blobs.mkdir(parents=True, exist_ok=True)
    p = blobs / f"{etag}{suffix}"
    p.write_bytes(body)
    t = time.time() - age_s
    os.utime(p, (t, t))
    return p


def _job(kind: str = "file", etag: str | None = ETAG, repo: str = REPO) -> jobs.Job:
    return jobs.Job(id="j", kind=kind, repo_type="model", repo_id=repo, revision="main",
                    filename="w.safetensors" if kind == "file" else None, state="running",
                    etag=etag)


def test_a_stale_unowned_partial_is_removed_and_reported(cache, caplog):
    p = _partial(cache)
    with caplog.at_level(logging.INFO, logger="xhc.cachefs"):
        res = cachefs.sweep_hf_partials()
    assert not p.exists()
    assert res["scanned"] == 1
    assert res["removed"] == 1
    assert res["freed_bytes"] == len(b"half-a-shard")
    assert res["kept_bytes"] == 0
    msgs = [r.getMessage() for r in caplog.records]
    assert any("stale partial" in m and p.name in m for m in msgs), msgs
    assert any("1 stale partial download(s)" in m for m in msgs), msgs


def test_a_tier_partial_is_covered(cache):
    p = _partial(cache, suffix=".tier.incomplete")
    res = cachefs.sweep_hf_partials()
    assert not p.exists()
    assert res["removed"] == 1


def test_a_fresh_partial_is_kept(cache):
    p = _partial(cache, age_s=60)
    res = cachefs.sweep_hf_partials()
    assert p.exists()
    assert res["removed"] == 0 and res["kept_young"] == 1
    assert res["kept_bytes"] == p.stat().st_size


@pytest.mark.parametrize("job", [
    _job("file", ETAG),
    _job("file", None),          # a file job with no etag: owns the repo's partials
    _job("snapshot", None),      # a prewarm does not say which blobs it fetches
], ids=["file-job", "file-job-no-etag", "snapshot-job"])
def test_a_partial_owned_by_an_active_job_is_kept_even_if_old(cache, job):
    p = _partial(cache, age_s=10 * DAY)
    jobs.manager._active[job.key] = job
    res = cachefs.sweep_hf_partials()
    assert p.exists()
    assert res["removed"] == 0 and res["kept_owned"] == 1


def test_a_job_for_another_blob_or_repo_does_not_own_it(cache):
    p = _partial(cache)
    other_blob = _job("file", "b" * 64)
    other_repo = _job("snapshot", None, repo="acme/other")
    jobs.manager._active[other_blob.key] = other_blob
    jobs.manager._active[other_repo.key] = other_repo
    assert cachefs.sweep_hf_partials()["removed"] == 1
    assert not p.exists()


def test_a_partial_owned_by_an_in_flight_tier_fill_is_kept(cache):
    p = _partial(cache, suffix=".tier.incomplete", age_s=10 * DAY)
    tier._filling[(FOLDER, ETAG)] = 1
    res = cachefs.sweep_hf_partials()
    assert p.exists() and res["kept_owned"] == 1


def test_a_tier_fill_registers_for_its_whole_life(cache, monkeypatch):
    """The in-process guard is only real if the tier fill actually registers."""
    seen: list[bool] = []

    async def fake_locked(*a, **kw):
        seen.append(tier.owns_partial(FOLDER, ETAG))
        return False

    monkeypatch.setattr(tier, "readable", lambda: True)
    monkeypatch.setattr(tier, "cfg", lambda: type("C", (), {"min_size": 0, "prefix": "p"})())
    monkeypatch.setattr(tier, "_fill_blob_locked", fake_locked)
    asyncio.run(tier._fill_blob("model", REPO, ETAG, 10))
    assert seen == [True]
    assert not tier.owns_partial(FOLDER, ETAG), "a finished fill still claims the partial"


# The hub's own lock, taken exactly the way hf_hub_download takes it
# (huggingface_hub.utils.WeakFileLock over filelock), in ANOTHER process.
_HOLDER = """
import sys, time
from huggingface_hub.utils import WeakFileLock
with WeakFileLock(sys.argv[1]):
    print("locked", flush=True)
    sys.stdin.readline()
"""


def test_a_partial_whose_hub_lock_another_process_holds_is_kept(cache):
    p = _partial(cache, age_s=10 * DAY)
    lock = cachefs.hub_lock_path(FOLDER, ETAG)
    lock.parent.mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen([sys.executable, "-c", _HOLDER, str(lock)],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "locked"
        res = cachefs.sweep_hf_partials()
        assert p.exists(), "removed a partial whose writer holds the hub lock"
        assert res["removed"] == 0 and res["kept_locked"] == 1
    finally:
        holder.kill()
        holder.wait(timeout=10)
    # The writer died; the kernel dropped its lock: now it is garbage.
    assert cachefs.sweep_hf_partials()["removed"] == 1
    assert not p.exists()
    assert lock.exists(), "the hub's lock file is not the sweep's to remove"


def test_the_sweep_holds_the_hub_lock_while_it_deletes(cache, monkeypatch):
    """So no hf_hub_download can start on the blob between check and unlink."""
    p = _partial(cache)
    lock = cachefs.hub_lock_path(FOLDER, ETAG)
    probe = """
import sys, filelock
try:
    filelock.FileLock(sys.argv[1]).acquire(timeout=0)
    print("free")
except filelock.Timeout:
    print("held")
"""
    seen: list[str] = []
    real_unlink = Path.unlink

    def unlink(self, *a, **kw):
        if self == p:
            seen.append(subprocess.run([sys.executable, "-c", probe, str(lock)],
                                       capture_output=True, text=True, check=True).stdout.strip())
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(Path, "unlink", unlink)
    assert cachefs.sweep_hf_partials()["removed"] == 1
    assert seen == ["held"]


def test_dry_run_reports_but_keeps(cache):
    p = _partial(cache)
    res = cachefs.sweep_hf_partials(dry_run=True)
    assert p.exists()
    assert res["removed"] == 1 and res["dry_run"] is True


def test_the_sweep_touches_nothing_but_partials(cache):
    blobs = cache / FOLDER / "blobs"
    blobs.mkdir(parents=True)
    blob = blobs / ("c" * 64)
    blob.write_bytes(b"a whole blob")
    state = cache / ".xhc"
    state.mkdir()
    (state / "x.incomplete").write_bytes(b"not a repo")
    stray = cache / "not-a-repo" / "blobs"
    stray.mkdir(parents=True)
    (stray / "y.incomplete").write_bytes(b"not a repo either")
    t = time.time() - DAY
    for f in (blob, state / "x.incomplete", stray / "y.incomplete"):
        os.utime(f, (t, t))
    p = _partial(cache)
    res = cachefs.sweep_hf_partials()
    assert res["scanned"] == 1
    assert not p.exists()
    assert blob.exists() and (state / "x.incomplete").exists() and (stray / "y.incomplete").exists()


def test_datasets_and_spaces_are_covered(cache):
    ps = [_partial(cache, folder="datasets--acme--d"), _partial(cache, folder="spaces--acme--s")]
    assert cachefs.sweep_hf_partials()["removed"] == 2
    assert not any(p.exists() for p in ps)


def test_the_threshold_is_its_own_setting(cache, monkeypatch):
    p = _partial(cache, age_s=120)
    monkeypatch.setattr(settings, "hf_partial_max_age_s", 60.0)
    assert cachefs.sweep_hf_partials()["removed"] == 1
    assert not p.exists()


def test_the_default_threshold_is_six_hours():
    from app.config import Settings

    assert Settings().hf_partial_max_age_s == 6 * 3600


def test_the_threshold_reads_its_environment_variable(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv("XHC_HF_PARTIAL_MAX_AGE", "123")
    assert Settings.from_env().hf_partial_max_age_s == 123.0


def test_a_stop_request_ends_the_sweep_between_files(cache):
    for i in range(3):
        _partial(cache, etag=f"{i}" * 64)
    res = cachefs.sweep_hf_partials(stop=lambda: True)
    assert res["stopped"] is True and res["scanned"] == 0


# -- accounting --------------------------------------------------------------


def _no_protection(monkeypatch, capacity: int):
    monkeypatch.setattr(cachefs, "load_pins", lambda strict=False: set())
    monkeypatch.setattr(cachefs, "load_orphans", lambda strict=False: {})
    monkeypatch.setattr(cachefs, "protected_keys", lambda strict=False: set())
    monkeypatch.setattr(cachefs, "disk_stats", lambda: {
        "fs_total": 10 * capacity, "fs_used": 0, "fs_free": 10 * capacity,
        "capacity": capacity, "capacity_source": "XHC_CACHE_MAX_SIZE",
    })


def test_scan_cache_dir_cannot_see_partial_bytes_but_the_view_can(cache):
    """The measurement behind the accounting change, pinned."""
    _partial(cache, body=b"x" * 5000, age_s=0)
    view = cachefs._scan_sync()
    assert view.size_on_disk == 0, "scan_cache_dir now counts partials; revisit accounting"
    assert view.partial_bytes == 5000


def test_eviction_counts_live_partials_as_used(cache, monkeypatch):
    """A partial a download owns survives the sweep and still uses the budget."""
    _no_protection(monkeypatch, capacity=1000)
    _partial(cache, body=b"x" * 950, age_s=10 * DAY)
    job = _job("file", ETAG)
    jobs.manager._active[job.key] = job
    out = cachefs._evict_sync()
    assert out["partial_bytes"] == 950
    assert out["used_before"] == out["blob_bytes"] + 950
    assert "reason" not in out, "950 of 1000 bytes in use did not cross the 90% high water"


def test_eviction_sweeps_stale_partials_first_and_reports_them(cache, monkeypatch):
    _no_protection(monkeypatch, capacity=1000)
    p = _partial(cache, body=b"x" * 950)
    out = cachefs._evict_sync()
    assert not p.exists()
    assert out["partials"]["removed"] == 1 and out["partials"]["freed_bytes"] == 950
    assert out["partial_bytes"] == 0
    assert out["reason"] == "under high water"
    assert cachefs.last_partial_sweep()["trigger"] == "evict"


def test_status_reports_partial_bytes_and_the_last_sweep(cache):
    from app import manage

    p = _partial(cache, body=b"x" * 777, age_s=0)
    asyncio.run(cachefs.sweep_partials_async("interval"))
    cachefs.invalidate_view()
    st = asyncio.run(manage.status())
    assert st["cache"]["partial_bytes"] == 777
    assert st["cache"]["partials"]["trigger"] == "interval"
    assert st["cache"]["partials"]["kept_young"] == 1
    assert p.exists()


# -- when it runs --------------------------------------------------------------


def test_startup_sweeps_before_the_first_interval(cache, monkeypatch):
    p = _partial(cache)
    monkeypatch.setattr(settings, "evict_interval_s", 3600)

    async def scenario():
        task = asyncio.create_task(cachefs.eviction_loop())
        deadline = time.monotonic() + 5
        while p.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
    assert not p.exists(), "startup did not sweep a stale partial"
    assert cachefs.last_partial_sweep()["trigger"] == "startup"


def test_each_interval_sweeps(cache, monkeypatch):
    monkeypatch.setattr(settings, "evict_interval_s", 0)

    async def scenario():
        task = asyncio.create_task(cachefs.eviction_loop())
        await asyncio.sleep(0.2)          # startup sweep has run on an empty cache
        p = _partial(cache)
        deadline = time.monotonic() + 5
        while p.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return p

    p = asyncio.run(scenario())
    assert not p.exists(), "the interval sweep did not reclaim a stale partial"
    assert cachefs.last_partial_sweep()["trigger"] == "interval"


def test_the_loop_ends_promptly_on_cancel(cache, monkeypatch):
    """Bounded shutdown: cancelling the loop mid-sleep ends it."""
    monkeypatch.setattr(settings, "evict_interval_s", 3600)

    async def scenario():
        task = asyncio.create_task(cachefs.eviction_loop())
        await asyncio.sleep(0.1)
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=5)
        return bool(done)

    assert asyncio.run(scenario())
