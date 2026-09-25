"""A prewarm reads from the object-store tier before the Hub.

Refilling a cache after losing its disk is done by re-running prewarms, so this
is the path the tier exists for. A prewarm lists the revision, fills every
sha256 (LFS/Xet) file it names from the tier -- verify-first, under
huggingface_hub's per-blob lock, at most XHC_SNAPSHOT_MAX_WORKERS at a time --
and then runs snapshot_download as before, which only links what landed and
fetches the rest from the Hub.

The fake Hub is a real HTTP server and logs every request, so "nothing came
from the Hub" is asserted on what the Hub saw rather than on what the cache
says it did.
"""

from __future__ import annotations

import asyncio
import hashlib
import http.server
import json
import sys
import threading
from pathlib import Path
from typing import ClassVar
from urllib.parse import unquote, urlparse

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tierfake import FakeS3

from app import cachefs, jobs, metrics, tier
from app.config import TierSettings, settings

REPO = "acme/model"
COMMIT = "2" * 40
FILES = {
    "config.json": b'{"model_type": "toy"}',
    "tokenizer.json": b'{"vocab": ["a", "b"]}',
    "model-00001-of-00002.safetensors": b"W" * 20_000,
    "model-00002-of-00002.safetensors": b"X" * 30_000,
}
SHARDS = [n for n in FILES if n.endswith(".safetensors")]
SMALL = [n for n in FILES if n not in SHARDS]


# Captured at import: the fake Hub runs in this process, and a test counting the
# app's content-hash passes must not count the fake's own.
_REAL_SHA256 = hashlib.sha256


def _sha256(b: bytes) -> str:
    return _REAL_SHA256(b).hexdigest()


def _git_blob_id(b: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(b) + b, usedforsecurity=False).hexdigest()


def _etag(name: str) -> str:
    # What the Hub really does: LFS files carry their sha256, the rest their
    # git blob id.
    return _sha256(FILES[name]) if name in SHARDS else _git_blob_id(FILES[name])


class _Hub(http.server.BaseHTTPRequestHandler):
    log: ClassVar[list[tuple[str, str]]] = []

    def _info(self) -> None:
        siblings = []
        for n, b in FILES.items():
            s = {"rfilename": n, "size": len(b), "blobId": _git_blob_id(b)}
            if n in SHARDS:
                s["lfs"] = {"sha256": _sha256(b), "size": len(b), "pointerSize": 134}
            siblings.append(s)
        body = json.dumps({"id": REPO, "sha": COMMIT, "siblings": siblings}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve(self, with_body: bool) -> None:
        path = unquote(urlparse(self.path).path)
        if path.startswith(f"/api/models/{REPO}/revision/"):
            type(self).log.append((self.command, "INFO"))
            return self._info()
        prefix = f"/{REPO}/resolve/"
        name = path[len(prefix):].partition("/")[2] if path.startswith(prefix) else path
        type(self).log.append((self.command, name))
        if name not in FILES:
            self.send_response(404)
            self.send_header("content-length", "0")
            self.end_headers()
            return
        body = FILES[name]
        self.send_response(200)
        for k, v in {
            "x-repo-commit": COMMIT, "etag": f'"{_etag(name)}"',
            "x-linked-etag": f'"{_etag(name)}"', "x-linked-size": str(len(body)),
            "content-length": str(len(body)), "accept-ranges": "bytes",
        }.items():
            self.send_header(k, v)
        self.end_headers()
        if with_body:
            self.wfile.write(body)

    def do_HEAD(self):
        self._serve(False)

    def do_GET(self):
        self._serve(True)

    def log_message(self, *a):
        return


@pytest.fixture
def world(tmp_path, monkeypatch):
    _Hub.log = []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Hub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    (tmp_path / "cache").mkdir()
    monkeypatch.setattr(settings, "cache_dir", str(tmp_path / "cache"))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings, "upstream", f"http://127.0.0.1:{srv.server_address[1]}")
    monkeypatch.setattr(settings, "hf_token", None)
    monkeypatch.setattr(settings, "hf_verify_ingest", True)
    monkeypatch.setattr(settings, "snapshot_max_workers", 2)
    monkeypatch.setattr(settings, "tier", TierSettings(
        scheme="s3", bucket="bkt", prefix="pfx", endpoint="http://tier.test", region="auto",
        path_style=True, credentials="static", access_key_id="AKID", secret_access_key="S",
        index_key=None,
    ))
    monkeypatch.setattr(tier, "BACKOFF_S", (0,))
    tier.reset_for_tests()
    metrics.reset()
    cachefs.invalidate_view()
    fake = FakeS3()
    tier.use_client(tier.build_client(httpx.AsyncClient(transport=fake.transport())))
    for n in SHARDS:
        fake.seed(tier.hf_content_key("model", REPO, _sha256(FILES[n])), FILES[n])
    try:
        yield fake
    finally:
        tier.reset_for_tests()
        cachefs.invalidate_view()
        srv.shutdown()
        srv.server_close()


def _prewarm() -> jobs.Job:
    async def go():
        m = jobs.JobManager()
        job = await m.ensure_snapshot("model", REPO, "main")
        await job.done.wait()
        # let the write-back scheduling run, then upload inline
        for _ in range(200):
            if not [t for t in m._tasks if not t.done() and t is not m._progress_task]:
                break
            await asyncio.sleep(0.01)
        await tier.drain()
        return job

    return asyncio.run(go())


def _hub_gets() -> set[str]:
    return {name for method, name in _Hub.log if method == "GET" and name != "INFO"}


def _blob(name: str) -> Path:
    return Path(settings.cache_dir) / "models--acme--model" / "blobs" / _etag(name)


def test_a_prewarm_on_an_empty_disk_refills_shards_from_the_tier(world, monkeypatch):
    real = hashlib.sha256
    hashed = {"bytes": 0}

    class Counting:
        def __init__(self, data=b""):
            self._h = real()
            if data:
                self.update(data)

        def update(self, b):
            if len(b) >= 1024:  # content chunks, not signing strings
                hashed["bytes"] += len(b)
            self._h.update(b)

        def hexdigest(self):
            return self._h.hexdigest()

        def digest(self):
            return self._h.digest()

    monkeypatch.setattr(hashlib, "sha256", Counting)
    job = _prewarm()

    assert job.state == "done", job.error
    gets = _hub_gets()
    assert not (gets & set(SHARDS)), f"shards were fetched from the Hub: {gets}"
    assert set(SMALL) <= gets, "known positive: git-blob files still come from the Hub"
    for n in FILES:
        assert _blob(n).read_bytes() == FILES[n]
        link = Path(settings.cache_dir) / "models--acme--model" / "snapshots" / COMMIT / n
        assert link.resolve() == _blob(n).resolve()

    shard_bytes = sum(len(FILES[n]) for n in SHARDS)
    assert job.verify["verified_at_tier_read"] == 2
    assert job.verify["new_verified"] == 4, job.verify  # 2 git-blob + 2 tier
    assert job.verify["mismatched"] == 0
    assert hashed["bytes"] == shard_bytes, (
        f"{hashed['bytes']} sha256 bytes for {shard_bytes} bytes of shards: one pass only"
    )
    snap = metrics.snapshot()
    assert snap["tier_bytes_read"] == shard_bytes
    assert snap["tier_verify"]["verified"] == 2
    assert snap["bytes_ingested"] == sum(len(FILES[n]) for n in SMALL)
    # Not uploaded back: they came from there. Not even a HEAD for them.
    shard_keys = {tier.hf_content_key("model", REPO, _sha256(FILES[n])) for n in SHARDS}
    assert not [r for r in world.requests if r[0] in ("PUT", "HEAD") and r[1] in shard_keys]


def test_a_wrong_shard_in_the_tier_falls_back_to_the_hub_and_the_job_is_done(world):
    """Same-length wrong bytes for ONE shard: that shard comes from the Hub, the
    other from the tier, and the prewarm still ends done with correct bytes.

    Deleting the hash comparison in tier._fill_blob turns this red; done once by
    hand and recorded in the change's description."""
    bad = SHARDS[0]
    good = SHARDS[1]
    wrong = bytes(b ^ 0x21 for b in FILES[bad])
    assert len(wrong) == len(FILES[bad])
    bad_key = tier.hf_content_key("model", REPO, _sha256(FILES[bad]))
    world.seed(bad_key, wrong)

    job = _prewarm()

    assert job.state == "done", job.error
    gets = _hub_gets()
    assert bad in gets, "the refused shard was fetched from the Hub"
    assert good not in gets
    assert _sha256(_blob(bad).read_bytes()) == _etag(bad)
    assert _blob(good).read_bytes() == FILES[good]
    assert job.verify["verified_at_tier_read"] == 1
    assert job.verify["mismatched"] == 0
    assert metrics.snapshot()["tier_verify"]["mismatch"] == 1
    assert tier.is_bad(bad_key) and world.objects[bad_key].body == wrong, "never deleted"


def test_tier_fills_are_bounded_by_snapshot_max_workers(world, monkeypatch):
    inflight = {"now": 0, "max": 0}
    real_fill = tier._fill_blob

    async def spy(*a, **k):
        inflight["now"] += 1
        inflight["max"] = max(inflight["max"], inflight["now"])
        try:
            await asyncio.sleep(0.05)  # make overlap observable
            return await real_fill(*a, **k)
        finally:
            inflight["now"] -= 1

    monkeypatch.setattr(tier, "_fill_blob", spy)
    monkeypatch.setattr(settings, "snapshot_max_workers", 1)
    assert _prewarm().state == "done"
    assert inflight["max"] == 1

    # Known positive: with a bound of 2 the two shards do overlap, so the
    # instrument can see concurrency when it exists.
    import shutil
    shutil.rmtree(Path(settings.cache_dir) / "models--acme--model")
    inflight["max"] = 0
    monkeypatch.setattr(settings, "snapshot_max_workers", 2)
    assert _prewarm().state == "done"
    assert inflight["max"] == 2


def test_a_prewarm_without_a_key_writes_an_unsigned_index_for_every_file(world):
    _prewarm()
    for n in FILES:
        body = world.objects[tier.hf_commit_index_key("model", REPO, COMMIT, n)].body
        entry = json.loads(body)
        assert entry["etag"] == _etag(n) and entry["auth"] == tier.AUTH_UNSIGNED


def test_allow_patterns_limit_what_is_asked_of_the_tier(world):
    async def go():
        m = jobs.JobManager()
        job = await m.ensure_snapshot("model", REPO, "main", [SHARDS[1], "*.json"])
        await job.done.wait()
        return job

    job = asyncio.run(go())
    assert job.state == "done", job.error
    gets = [r[1] for r in world.requests if r[0] == "GET" and r[1] and "/content/" in r[1]]
    assert gets == [tier.hf_content_key("model", REPO, _sha256(FILES[SHARDS[1]]))]


def test_without_a_tier_the_prewarm_is_unchanged(world, monkeypatch):
    monkeypatch.setattr(settings, "tier", None)
    job = _prewarm()
    assert job.state == "done", job.error
    assert set(FILES) <= _hub_gets()
    assert job.verify["verified_at_tier_read"] == 0 and job.verify["new_verified"] == 4
