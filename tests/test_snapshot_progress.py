"""A running prewarm reports the bytes it has fetched so far.

THE DEFECT. The snapshot watcher built its root with
repo_folder_name(repo_type, repo_id) -- the arguments reversed -- which names a
folder that never exists. _tree_bytes reads a missing root as 0, so every
in-flight prewarm reported downloaded_bytes 0 until it finished, and a prewarm
cut short recorded 0 as its progress. Found by an end-to-end test that waited
for progress on a running prewarm and never saw any.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import jobs
from app.config import settings


def test_watcher_sees_bytes_landing_in_the_repo_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "cache_dir", str(tmp_path))
    monkeypatch.setattr(jobs, "_SNAPSHOT_SAMPLE_S", 0.01)
    blobs = tmp_path / "models--acme--weights" / "blobs"
    blobs.mkdir(parents=True)
    (blobs / ("a" * 64 + ".incomplete")).write_bytes(b"x" * 12345)

    job = jobs.Job(id="j", kind="snapshot", repo_type="model", repo_id="acme/weights",
                   revision="main", state="running", started_at=time.time() - 1)

    async def scenario():
        watcher = asyncio.create_task(jobs.manager._watch_snapshot(job))
        try:
            for _ in range(200):
                if job.final_bytes:
                    return
                await asyncio.sleep(0.01)
        finally:
            watcher.cancel()

    asyncio.run(scenario())
    assert job.final_bytes == 12345
    assert job.downloaded_bytes() == 12345
