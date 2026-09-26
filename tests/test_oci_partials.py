"""Stale `.incomplete` blobs in the OCI store are reclaimed, and live ones never are.

THE DEFECT: a blob download writes `<digest>.incomplete` (or
`<digest>.tier.incomplete`) and renames it into place on a digest match. A
process killed mid-download leaves that file behind, and the GC walk skipped
every `.incomplete` name, so it was never reclaimed: dead bytes on the array,
counted by nothing that looks for garbage.

THE RULE: a partial is removed only when ALL of these hold --
  1. no download in THIS process owns it (the in-process single-flight table);
  2. no process holds its advisory lock (every writer takes one for the life of
     the file, and the kernel drops it when the writer dies -- this is what
     reaches a second process sharing the cache dir);
  3. it has not been written to for XHC_DOCKER_PARTIAL_MAX_AGE (default 6h),
     the backstop for filesystems where the lock is not honoured.

Tests that fail against v0.9.28: every test that calls sweep_partials or reads
the GC's `partials` summary -- there was no sweep.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import ocicompat, ocigc, ocistore
from app.config import settings

UP = "ghcr.io"
DAY = 24 * 3600


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "docker_dir", str(tmp_path / "docker"))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"), raising=False)
    ocistore.reset_stats_cache()
    return tmp_path


def _partial(body: bytes = b"half-a-layer", suffix: str = ".incomplete",
             age_s: float = DAY) -> tuple[str, Path]:
    digest = ocistore.compute_digest(body + b"-full")
    final = ocistore.blob_path(UP, digest)
    final.parent.mkdir(parents=True, exist_ok=True)
    p = Path(str(final) + suffix)
    p.write_bytes(body)
    t = time.time() - age_s
    os.utime(p, (t, t))
    return digest, p


def test_a_stale_orphaned_partial_is_removed_and_reported(store, caplog):
    _, p = _partial()
    with caplog.at_level(logging.INFO, logger="xhc.ocigc"):
        res = ocigc.collect()
    assert not p.exists()
    assert res["partials"]["removed"] == 1
    assert res["partials"]["freed_bytes"] == len(b"half-a-layer")
    assert res["partials"]["scanned"] == 1
    assert any("stale partial" in r.getMessage() and p.name in r.getMessage()
               for r in caplog.records), [r.getMessage() for r in caplog.records]


def test_a_tier_partial_is_swept_too(store):
    _, p = _partial(suffix=".tier.incomplete")
    res = ocigc.sweep_partials()
    assert not p.exists()
    assert res["removed"] == 1


def test_a_fresh_partial_is_kept(store):
    _, p = _partial(age_s=60)
    res = ocigc.sweep_partials()
    assert p.exists()
    assert res["removed"] == 0 and res["kept_young"] == 1


def test_a_partial_owned_by_an_active_download_is_kept_even_if_old(store, monkeypatch):
    digest, p = _partial(age_s=10 * DAY)
    monkeypatch.setitem(ocicompat._inflight, (UP, digest), object())
    res = ocigc.sweep_partials()
    assert p.exists()
    assert res["removed"] == 0 and res["kept_owned"] == 1


def test_a_partial_locked_by_another_writer_is_kept_even_if_old(store):
    """What a second process sharing the cache dir looks like from here: the
    file is not in our single-flight table, but its writer holds the lock."""
    _, p = _partial(age_s=10 * DAY)
    with open(p, "r+b") as other:
        fcntl.flock(other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        res = ocigc.sweep_partials()
        assert p.exists()
        assert res["removed"] == 0 and res["kept_locked"] == 1
    # The writer died (its descriptor closed): now it is garbage.
    assert ocigc.sweep_partials()["removed"] == 1
    assert not p.exists()


def test_dry_run_reports_but_keeps(store):
    _, p = _partial()
    res = ocigc.collect(dry_run=True)
    assert p.exists()
    assert res["partials"]["removed"] == 1


def test_the_sweep_touches_nothing_but_partials(store):
    body = b"a whole blob"
    d = ocistore.compute_digest(body)
    blob = ocistore.blob_path(UP, d)
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_bytes(body)
    t = time.time() - DAY
    os.utime(blob, (t, t))
    _, p = _partial()
    ocigc.sweep_partials()
    assert blob.exists() and not p.exists()


def test_the_threshold_is_configurable(store, monkeypatch):
    _, p = _partial(age_s=120)
    monkeypatch.setattr(settings, "docker_partial_max_age_s", 60.0)
    assert ocigc.sweep_partials()["removed"] == 1
    assert not p.exists()


def test_the_writer_holds_the_lock_for_the_life_of_the_file(store):
    """The sweep's cross-process guard is only real if writers take the lock."""
    body = b"x" * 4096
    digest = ocistore.compute_digest(body)
    final = ocistore.blob_path(UP, digest)
    job = ocicompat.BlobJob(id="t", digest=digest, upstream=UP, final_path=final,
                            incomplete_path=str(final) + ".incomplete")
    seen: list[bool] = []

    async def chunks(_n):
        yield body[:2048]
        seen.append(ocistore.partial_is_locked(Path(job.incomplete_path)))
        yield body[2048:]

    async def aclose():
        return None

    resp = SimpleNamespace(aiter_bytes=chunks, aclose=aclose)
    asyncio.run(ocicompat._write_blob(job, resp))
    assert job.state == "done", job.error
    assert seen == [True], "a live download's partial was claimable"
    assert final.is_file()


def test_startup_sweeps_before_the_first_interval(store, monkeypatch):
    _, p = _partial()
    monkeypatch.setattr(settings, "docker_enabled", True)
    monkeypatch.setattr(settings, "evict_interval_s", 3600)

    async def scenario():
        task = asyncio.create_task(ocigc.gc_loop())
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
