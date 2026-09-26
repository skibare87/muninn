"""What a killed HF ingest leaves besides `.incomplete` partials, and why each
leftover is deliberately NOT cleaned up by Muninn.

EMPTY SNAPSHOT DIRECTORIES ARE KEPT. huggingface_hub creates
`snapshots/<commit>/` and writes `refs/<revision>` BEFORE the first byte of a
file (file_download.py, `_hf_hub_download_to_cache_dir`: makedirs of the pointer
path, then `_cache_commit_hash_for_specific_revision`). A kill before any file
lands leaves an empty snapshot that `scan_cache_dir` reports as a 0-file
revision. It stays, for three reasons these tests pin:

  1. It is reported correctly. With a prewarm manifest it lists as
     `complete: false`, 0 of N files; without one, `complete: null`. Neither
     reads as a finished snapshot, which is the defect manifests exist to stop.
  2. Removing it while `refs/` still names it CORRUPTS THE WHOLE REPO in
     huggingface_hub's eyes: `scan_cache_dir` raises "Reference(s) refer to
     missing commit hashes" for that repo, drops it from the listing, and
     eviction stops seeing its blobs. Since the ref is written first, the
     referenced case is the normal case.
  3. It holds no bytes, and the next ingest of that commit reuses it.

HF-XET LOG FILES ARE PRUNED BY HF-XET ITSELF. hf-xet 1.6.0 (xet-core v1.6.0,
xet_runtime/src/config/groups/log.rs and logging/init.rs) cleans its own log
directory when it initialises logging, which it does at import: files older
than HF_XET_LOG_DIR_MAX_RETENTION_AGE (default 14 days) are deleted, and the
directory is trimmed oldest-first to HF_XET_LOG_DIR_MAX_SIZE (default 250mb),
never touching a file younger than HF_XET_LOG_DIR_MIN_DELETION_AGE (1 day) or
one whose writer process is still alive. A second pruner in Muninn would be a
second mechanism deleting the same files, so there is none; the contract test
below fails if an hf-xet upgrade drops that cleanup.

Against v0.9.30 every test here passes except the README one: nothing in the
code changed, because both leftovers turned out to be handled already. The
tests pin the reasons, so a later change that breaks one of them goes red.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from huggingface_hub import scan_cache_dir

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import cachefs, jobs, manifests, tier
from app.config import settings

REPO = "org/model"
FOLDER = "models--org--model"
COMMIT = "a" * 40
DAY = 24 * 3600
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "cache_dir", str(tmp_path / "cache"))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"), raising=False)
    monkeypatch.setattr(jobs.manager, "_active", {})
    monkeypatch.setattr(tier, "_filling", {}, raising=False)
    monkeypatch.setattr(cachefs, "_last_partial_sweep", None, raising=False)
    (tmp_path / "cache").mkdir()
    return tmp_path / "cache"


def _killed_before_first_file(cache: Path, *, ref: str | None = "main") -> Path:
    """The on-disk state huggingface_hub leaves when killed before a file lands."""
    repo = cache / FOLDER
    (repo / "blobs").mkdir(parents=True)
    snap = repo / "snapshots" / COMMIT
    snap.mkdir(parents=True)
    if ref is not None:
        (repo / "refs").mkdir()
        (repo / "refs" / ref).write_text(COMMIT)
    t = time.time() - DAY
    os.utime(snap, (t, t))
    return snap


def _revision(view: cachefs.CacheView) -> dict:
    (repo,) = [r for r in view.repos if r.repo_id == REPO]
    (rev,) = repo.revisions
    return rev


def test_a_prewarmed_empty_snapshot_lists_as_incomplete(cache):
    _killed_before_first_file(cache)
    manifests.record("model", REPO, COMMIT, {"config.json": 10, "model.safetensors": 999},
                     None)
    rev = _revision(cachefs._scan_sync())
    assert rev["nb_files"] == 0
    assert rev["complete"] is False
    assert (rev["files_present"], rev["files_expected"]) == (0, 2)
    assert rev["refs"] == ["main"]


def test_an_empty_snapshot_with_no_manifest_is_unknown_not_complete(cache):
    _killed_before_first_file(cache)
    rev = _revision(cachefs._scan_sync())
    assert rev["nb_files"] == 0
    assert rev["complete"] is None


def test_removing_a_referenced_empty_snapshot_would_corrupt_the_repo(cache):
    """The reason the sweep does not do it. If this ever stops raising, the
    decision can be revisited; until then, deleting the directory hides the
    whole repo from the listing and from eviction."""
    snap = _killed_before_first_file(cache)
    assert [r.repo_id for r in scan_cache_dir(cache).repos] == [REPO]
    snap.rmdir()
    info = scan_cache_dir(cache)
    assert info.repos == frozenset()
    assert any("refer to missing commit hashes" in str(w) for w in info.warnings)


def test_the_partial_sweep_leaves_empty_snapshots_alone(cache):
    """Idle, unowned, and past the age: still kept, referenced or not."""
    snap = _killed_before_first_file(cache)
    res = cachefs.sweep_hf_partials(max_age_s=0)
    assert snap.is_dir()
    assert res["removed"] == 0


# --- hf-xet's own log retention ------------------------------------------------

def _xet_log_name(ts: datetime, pid: int) -> str:
    # xet_runtime/src/logging/init.rs: <prefix>_<YYYYMMDD>T<HHMMSS><mmm><+/-HHMM>_<pid>.log
    return f"xet_{ts.strftime('%Y%m%dT%H%M%S')}000+0000_{pid}.log"


def test_hf_xet_prunes_its_own_log_directory(tmp_path):
    """Contract with hf-xet: importing it cleans HF_XET_CACHE/logs. Run in a
    child process because hf-xet initialises logging once per process."""
    logs = tmp_path / "xet" / "logs"
    logs.mkdir(parents=True)
    now = datetime.now(UTC)
    expired = logs / _xet_log_name(now - timedelta(days=30), 999_999)
    recent = logs / _xet_log_name(now - timedelta(days=3), 999_998)
    foreign = logs / "operator-notes.txt"
    for p in (expired, recent, foreign):
        p.write_text("x")

    env = {k: v for k, v in os.environ.items()
           if not k.startswith("HF_XET_LOG") and k != "RUST_LOG"}
    env["HF_XET_CACHE"] = str(tmp_path / "xet")
    # The cleanup runs on a background thread; give it time before exiting.
    subprocess.run([sys.executable, "-c", "import hf_xet, time; time.sleep(2)"],
                   env=env, check=True, timeout=60)

    assert not expired.exists(), (
        "hf-xet no longer removes logs past HF_XET_LOG_DIR_MAX_RETENTION_AGE; "
        "its log directory is now unbounded and needs pruning here")
    assert recent.exists(), "a log inside the retention window was deleted"
    assert foreign.exists(), "hf-xet deleted a file that is not its own log"
    own = [p for p in logs.glob("xet_*.log") if p not in (expired, recent)]
    assert own, "the child wrote no log file of its own; the test did not exercise hf-xet"


def test_the_readme_names_the_hf_xet_log_knobs():
    """The decision rests on these; an operator who wants a tighter bound, or
    no files at all, needs to find them."""
    text = (ROOT / "README.md").read_text()
    for knob in ("HF_XET_LOG_DEST", "HF_XET_LOG_DIR_MAX_RETENTION_AGE",
                 "HF_XET_LOG_DIR_MAX_SIZE", "HF_XET_LOG_DIR_MIN_DELETION_AGE",
                 "HF_XET_LOG_DIR_DISABLE_CLEANUP"):
        assert knob in text, f"README does not name {knob}"
