"""Phase 2 of the object-store tier: restore from the index when the Hub cannot answer.

THE INDEX IN THESE TESTS IS SIGNED BY HAND, from the canonical form the
phase-1 writer documents, with the version string spelled out as a literal.
A reader checked only against entries written by its own writer shares that
writer's reading of the format: both could be wrong the same way and agree.
One test (test_entries_the_phase1_writer_wrote_are_restorable) closes the
other direction.

Every refusal test has a known-positive twin in the same parametrization or
module: the identical seeding with the one fact changed restores. A refusal
that has never been seen to turn into a restore is decoration.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import hmac
import http.server
import json
import logging
import sys
import threading
import time
from pathlib import Path
from typing import ClassVar
from urllib.parse import quote, unquote, urlparse

import httpx
import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tierfake import FakeS3

from app import hfcompat, metrics, refs, tier, tierrestore
from app.config import TierSettings, settings
from app.jobs import manager

REPO = "acme/archived"
COMMIT = "a" * 40
OLD_COMMIT = "b" * 40
KEY = b"an index key held only in configuration"
VERSION = "muninn-tier-index-v1"  # spelled out: not read from the code under test
FILES = {
    "config.json": b'{"model_type": "toy", "layers": 2}',
    "tokenizer.json": b'{"vocab": ["x", "y"]}',
    "model.safetensors": (b"tensor bytes " * 4000)[:48_000],
}
LFS = {"model.safetensors"}
_REAL_SHA256 = hashlib.sha256


def _git_blob_id(b: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(b) + b, usedforsecurity=False).hexdigest()


def _etag(name: str, files: dict | None = None) -> str:
    b = (files or FILES)[name]
    return _REAL_SHA256(b).hexdigest() if name in LFS else _git_blob_id(b)


# ---------------------------------------------------------------------------
# a Hub whose failure mode is chosen per test
# ---------------------------------------------------------------------------


class _Hub(http.server.BaseHTTPRequestHandler):
    status: ClassVar[int] = 200
    code: ClassVar[str | None] = None
    log: ClassVar[list[str]] = []

    def _send(self, status, headers, body=b""):
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _serve(self):
        path = unquote(urlparse(self.path).path)
        type(self).log.append(f"{self.command} {path}")
        cls = type(self)
        if cls.status != 200:
            hdrs = {"content-length": "0"}
            if cls.code:
                hdrs["x-error-code"] = cls.code
            return self._send(cls.status, hdrs)
        if path.startswith(f"/api/models/{REPO}"):
            body = json.dumps({"id": REPO, "sha": COMMIT, "siblings": [
                {"rfilename": n} for n in FILES]}).encode()
            return self._send(200, {"content-type": "application/json",
                                    "content-length": str(len(body))}, body)
        prefix = f"/{REPO}/resolve/"
        name = path[len(prefix):].partition("/")[2] if path.startswith(prefix) else ""
        if name not in FILES:
            return self._send(404, {"x-error-code": "EntryNotFound", "content-length": "0"})
        body = FILES[name]
        return self._send(200, {
            "x-repo-commit": COMMIT, "etag": f'"{_etag(name)}"',
            "x-linked-etag": f'"{_etag(name)}"', "x-linked-size": str(len(body)),
            "content-length": str(len(body)), "accept-ranges": "bytes",
        }, body)

    do_GET = do_HEAD = _serve

    def log_message(self, *a):
        return


class HubControl:
    def __init__(self):
        _Hub.status, _Hub.code, _Hub.log = 200, None, []
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Hub)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.up = True

    def answer(self, status: int, code: str | None = None):
        _Hub.status, _Hub.code = status, code

    def refuse(self):
        """Stop listening. The port stays the same, so the tier's keys (which
        carry the upstream's host) do not change; connections are refused."""
        if self.up:
            self.srv.shutdown()
            self.srv.server_close()
            self.up = False

    @property
    def log(self):
        return _Hub.log


def _tier_settings(**kw) -> TierSettings:
    base = dict(
        scheme="s3", bucket="bkt", prefix="pfx", endpoint="http://tier.test", region="auto",
        path_style=True, credentials="static", access_key_id="AKID", secret_access_key="SECRET",
        part_size=16 * 1024, index_key=KEY,
    )
    base.update(kw)
    return TierSettings(**base)


@pytest.fixture
def hub(monkeypatch):
    h = HubControl()
    monkeypatch.setattr(settings, "upstream", h.url)
    try:
        yield h
    finally:
        h.refuse()


@pytest.fixture
def fake(tmp_path, monkeypatch, hub):
    (tmp_path / "cache").mkdir()
    (tmp_path / "docker").mkdir()
    monkeypatch.setattr(settings, "cache_dir", str(tmp_path / "cache"))
    monkeypatch.setattr(settings, "docker_dir", str(tmp_path / "docker"))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings, "hf_token", None)
    monkeypatch.setattr(settings, "miss_policy", "wait")
    monkeypatch.setattr(settings, "snapshot_max_workers", 2)
    monkeypatch.setattr(tier, "BACKOFF_S", (0, 0))
    f = FakeS3()
    _install(monkeypatch, f)
    metrics.reset()
    hfcompat.negative_cache_clear()
    refs.clear()
    yield f
    tier.reset_for_tests()


def _install(monkeypatch, f: FakeS3, **kw) -> None:
    monkeypatch.setattr(settings, "tier", _tier_settings(**kw))
    tier.reset_for_tests()
    tier.use_client(tier.build_client(httpx.AsyncClient(transport=f.transport())))


def _host() -> str:
    return urlparse(settings.upstream).netloc.replace(":", "_")


def _content_key(etag: str) -> str:
    kind = "sha256" if len(etag) == 64 else "gitsha1"
    return f"pfx/v1/content/hf/{_host()}/models/{REPO}/{kind}/{etag}"


def _commit_key(commit: str, path: str) -> str:
    return f"pfx/v1/index/hf/{_host()}/models/{REPO}/commits/{commit}/{quote(path, safe='')}.json"


def _ref_key(ref: str, obs_ms: int, commit: str) -> str:
    return f"pfx/v1/index/hf/{_host()}/models/{REPO}/refs/{quote(ref, safe='')}/{obs_ms:015d}-{commit}"


def _sign(kind: str, ref_or_commit: str, path: str, value: str, size: int, obs: str = "",
          *, key: bytes = KEY, version: str = VERSION, repo: str = REPO) -> str:
    canon = json.dumps([version, kind, _host(), f"models/{repo}", ref_or_commit, path, value,
                        size, obs], separators=(",", ":"))
    return hmac.new(key, canon.encode(), hashlib.sha256).hexdigest()


def seed_file(fake: FakeS3, name: str, *, commit: str = COMMIT, files: dict | None = None,
              signed: bool = True, sig: str | None = None, content: bytes | None = None,
              version: str = VERSION, with_content: bool = True) -> None:
    files = files or FILES
    body, etag = files[name], _etag(name, files)
    if with_content:
        fake.seed(_content_key(etag), body if content is None else content)
    entry = {"etag": etag, "size": len(body), "version": VERSION}
    if signed:
        entry["auth"] = "hmac-sha256-v1"
        entry["sig"] = sig or _sign("hf-commit", commit, name, etag, len(body),
                                    version=version)
    else:
        entry["auth"] = "unsigned"
    fake.seed(_commit_key(commit, name), json.dumps(entry).encode(), "application/json")


def seed_ref(fake: FakeS3, ref: str, commit: str, obs_ms: int, *, signed: bool = True,
             sig: str | None = None) -> str:
    obs = f"{obs_ms:015d}"
    key = _ref_key(ref, obs_ms, commit)
    meta = {"x-amz-meta-observed-at": obs, "x-amz-meta-version": VERSION}
    if signed:
        meta["x-amz-meta-auth"] = "hmac-sha256-v1"
        meta["x-amz-meta-sig"] = sig or _sign("hf-ref", ref, "", commit, 0, obs)
    else:
        meta["x-amz-meta-auth"] = "unsigned"
    fake.seed(key, b"")
    fake.objects[key].metadata = meta
    return key


def seed_model(fake: FakeS3, *, signed: bool = True, obs_ms: int = 1_700_000_000_000) -> None:
    for n in FILES:
        seed_file(fake, n, signed=signed)
    seed_ref(fake, "main", COMMIT, obs_ms, signed=signed)


def _run(coro_fn):
    return asyncio.run(coro_fn())


async def _req(method: str, path: str, headers: dict | None = None) -> httpx.Response:
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://muninn") as c:
        return await c.request(method, path, headers=headers or {})


def _get(path: str, method: str = "GET", headers: dict | None = None) -> httpx.Response:
    return _run(lambda: _req(method, path, headers))


def _blob(etag: str) -> Path:
    return Path(settings.cache_dir) / f"models--{REPO.replace('/', '--')}" / "blobs" / etag


def _restore_counts() -> dict:
    return {k: v for k, v in metrics.snapshot()["tier_restore"].items() if v}


def _index_reads() -> dict:
    return {k: v for k, v in metrics.snapshot()["tier_index_reads"].items() if v}


def _iso(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, tz=dt.UTC).strftime(
        "%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


async def _settle() -> None:
    for _ in range(500):
        pending = [t for t in manager._tasks if not t.done() and t is not manager._progress_task]
        if not pending:
            break
        await asyncio.sleep(0.01)
    await tier.drain()


# ---------------------------------------------------------------------------
# which Hub failures restore, and which never do
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status,code,restores", [
    # The known positives: the Hub cannot answer, or says the repo/revision is gone.
    (404, "RepoNotFound", True),
    (404, "RevisionNotFound", True),
    (404, None, True),
    (500, None, True),
    (503, None, True),
    # The upstream said NO. Serving anyway would bypass a revocation.
    (401, None, False),
    (401, "RepoNotFound", False),  # what the public Hub sends an anonymous caller
    (403, "GatedRepo", False),
    (403, None, False),
    # The Hub answered about this revision: the file is not in it.
    (404, "EntryNotFound", False),
    (429, None, False),
])
def test_which_hub_answers_restore(fake, hub, status, code, restores):
    seed_model(fake)
    hub.answer(status, code)
    r = _get(f"/{REPO}/resolve/main/config.json")
    if restores:
        assert r.status_code == 200, r.text
        assert r.content == FILES["config.json"]
        assert r.headers["x-xhc-cache"] == "TIER-RESTORE"
        assert r.headers["x-xhc-restore-reason"] in ("not_found", "upstream_5xx")
        assert _restore_counts() == {"ok": 1}
    else:
        assert r.status_code == status, (r.status_code, r.text)
        assert r.headers.get("x-xhc-cache") != "TIER-RESTORE"
        assert _restore_counts() == {}, "the index must not even be consulted"
        assert not [o for o in fake.ops("GET") if o[1] and "/index/" in o[1]]
        assert not _blob(_etag("config.json")).exists()


def test_connection_refused_restores_head_get_info_and_tree(fake, hub):
    seed_model(fake, obs_ms=1_700_000_000_123)
    hub.refuse()

    head = _get(f"/{REPO}/resolve/main/model.safetensors", "HEAD")
    assert head.status_code == 200
    assert head.headers["etag"] == f'"{_etag("model.safetensors")}"'
    assert head.headers["content-length"] == str(len(FILES["model.safetensors"]))
    assert head.headers["x-repo-commit"] == COMMIT
    assert not _blob(_etag("model.safetensors")).exists(), "a HEAD fetches nothing"

    r = _get(f"/{REPO}/resolve/main/model.safetensors")
    assert r.status_code == 200 and r.content == FILES["model.safetensors"]
    assert r.headers["x-xhc-cache"] == "TIER-RESTORE"
    assert r.headers["x-xhc-restore-reason"] == "unreachable"
    assert r.headers["x-xhc-index-auth"] == "signed"

    info = _get(f"/api/models/{REPO}/revision/main")
    assert info.status_code == 200 and info.headers["x-xhc-cache"] == "TIER-RESTORE"
    body = info.json()
    assert body["sha"] == COMMIT and body["xhcSynthesized"] is True
    sib = {s["rfilename"]: s for s in body["siblings"]}
    assert set(sib) == set(FILES)
    assert sib["config.json"]["blobId"] == _etag("config.json")
    assert sib["model.safetensors"]["lfs"]["sha256"] == _etag("model.safetensors")

    tree = _get(f"/api/models/{REPO}/tree/main?recursive=true")
    assert tree.status_code == 200 and tree.headers["x-xhc-cache"] == "TIER-RESTORE"
    assert sorted(e["path"] for e in tree.json()) == sorted(FILES)
    # No local ref is written: the observation stays an observation, reported
    # as such on every answer, never promoted to "what the Hub said".
    assert not (_blob("x").parent.parent / "refs").exists()


def test_a_restored_ref_says_when_it_was_observed_and_a_commit_does_not(fake, hub):
    obs = 1_700_000_123_456
    seed_model(fake, obs_ms=obs)
    hub.refuse()
    by_ref = _get(f"/{REPO}/resolve/main/config.json")
    assert by_ref.headers["x-xhc-ref-observed-at"] == _iso(obs)
    assert int(by_ref.headers["x-xhc-ref-age"]) >= int(time.time() - obs / 1000) - 5
    # Pinned: a commit never moves, so there is no staleness to report.
    by_commit = _get(f"/{REPO}/resolve/{COMMIT}/tokenizer.json")
    assert by_commit.status_code == 200 and by_commit.content == FILES["tokenizer.json"]
    assert by_commit.headers["x-xhc-cache"] == "TIER-RESTORE"
    assert "x-xhc-ref-observed-at" not in by_commit.headers


def test_the_newest_observation_that_verifies_wins(fake, hub):
    old = {"config.json": b'{"old": true}'}
    seed_file(fake, "config.json", commit=OLD_COMMIT, files=old)
    seed_ref(fake, "main", OLD_COMMIT, 1_600_000_000_000)
    seed_model(fake, obs_ms=1_700_000_000_000)
    hub.refuse()
    r = _get(f"/{REPO}/resolve/main/config.json")
    assert r.headers["x-repo-commit"] == COMMIT and r.content == FILES["config.json"]
    assert r.headers["x-xhc-ref-observed-at"] == _iso(1_700_000_000_000)


# ---------------------------------------------------------------------------
# trust: signed by default, unsigned only by opt-in, bad signatures refused
# ---------------------------------------------------------------------------


def test_unsigned_entries_are_refused_without_the_opt_in(fake, hub):
    seed_model(fake, signed=False)
    hub.refuse()
    r = _get(f"/{REPO}/resolve/main/config.json")
    assert r.status_code == 502
    assert _restore_counts() == {"unsigned_refused": 1}
    assert _index_reads() == {"unsigned_refused": 1}
    assert not _blob(_etag("config.json")).exists()


def test_unsigned_entries_restore_with_the_opt_in_and_say_so_loudly(fake, hub, monkeypatch,
                                                                     caplog):
    _install(monkeypatch, fake, restore_unsigned=True)
    seed_model(fake, signed=False)
    hub.refuse()
    with caplog.at_level(logging.WARNING, logger="xhc.tier.restore"):
        r = _get(f"/{REPO}/resolve/main/config.json")
    assert r.status_code == 200 and r.content == FILES["config.json"]
    assert r.headers["x-xhc-index-auth"] == "unsigned"
    assert _restore_counts() == {"ok_unsigned": 1}
    assert any("UNSIGNED TIER RESTORE" in rec.getMessage() for rec in caplog.records)


def test_the_opt_in_is_announced_at_startup(fake, monkeypatch, caplog):
    _install(monkeypatch, fake, restore_unsigned=True)

    async def boot():
        await tier.start()
        await tier.stop()

    with caplog.at_level(logging.WARNING, logger="xhc.tier"):
        _run(boot)
    assert any("UNSIGNED INDEX ENTRIES WILL BE RESTORED" in r.getMessage()
               for r in caplog.records)


def test_without_a_key_or_the_opt_in_restore_is_refused_outright(fake, hub, monkeypatch):
    _install(monkeypatch, fake, index_key=None)
    seed_model(fake)
    hub.refuse()
    r = _get(f"/{REPO}/resolve/main/config.json")
    assert r.status_code == 502
    assert _restore_counts() == {"no_key": 1}
    assert not [o for o in fake.ops() if o[1] and "/index/" in o[1]], "no index read at all"
    assert tier.status()["restore"]["enabled"] is False


def test_a_bad_signature_is_refused_counted_and_nothing_is_served(fake, hub):
    seed_model(fake)
    seed_file(fake, "config.json", sig="0" * 64)  # overwrite with a forged entry
    hub.refuse()
    r = _get(f"/{REPO}/resolve/main/config.json")
    assert r.status_code == 502
    assert _restore_counts() == {"bad_signature": 1}
    assert _index_reads()["bad_signature"] == 1
    assert not _blob(_etag("config.json")).exists()
    # Known positive: the untouched neighbour restores.
    assert _get(f"/{REPO}/resolve/main/tokenizer.json").status_code == 200


def test_an_entry_signed_with_another_key_is_refused(fake, hub):
    seed_model(fake)
    other = _sign("hf-commit", COMMIT, "config.json", _etag("config.json"),
                  len(FILES["config.json"]), key=b"somebody else's key")
    seed_file(fake, "config.json", sig=other)
    hub.refuse()
    assert _get(f"/{REPO}/resolve/main/config.json").status_code == 502
    assert _restore_counts() == {"bad_signature": 1}


def test_the_signature_covers_the_version(fake, hub):
    """Signed over a different version string -> refused, though every other
    field is right. The version is the constant, never the entry's field."""
    seed_model(fake)
    seed_file(fake, "config.json", version="muninn-tier-index-v0")
    hub.refuse()
    assert _get(f"/{REPO}/resolve/main/config.json").status_code == 502
    assert _restore_counts() == {"bad_signature": 1}


def test_an_entry_copied_to_another_path_is_refused(fake, hub):
    """The path is taken from the REQUEST and signed; an entry for one file
    moved under another's name does not verify."""
    seed_model(fake)
    fake.objects[_commit_key(COMMIT, "config.json")] = fake.objects[
        _commit_key(COMMIT, "tokenizer.json")]
    hub.refuse()
    assert _get(f"/{REPO}/resolve/main/config.json").status_code == 502
    assert _restore_counts() == {"bad_signature": 1}


def test_an_old_observation_replayed_under_a_newer_name_is_refused(fake, hub):
    """observed_at is signed and read from the object's NAME. Copying the old
    signed observation of `main` under a newer time -- a rollback with a valid
    signature, if the time were not covered -- fails, and the walk falls back
    to the newest genuine observation."""
    old = {"config.json": b'{"old": true}'}
    seed_file(fake, "config.json", commit=OLD_COMMIT, files=old)
    seed_model(fake, obs_ms=1_700_000_000_000)
    # A genuine, correctly signed observation from long ago...
    genuine = seed_ref(fake, "main", OLD_COMMIT, 1_600_000_000_000)
    # ...copied VERBATIM (body and metadata, signature and all) under a newer
    # name. That is the attack: nothing about the object itself was altered.
    fake.objects[_ref_key("main", 1_800_000_000_000, OLD_COMMIT)] = fake.objects[genuine]
    hub.refuse()
    r = _get(f"/{REPO}/resolve/main/config.json")
    assert r.status_code == 200
    assert r.headers["x-repo-commit"] == COMMIT, "the replayed rollback was not taken"
    assert _index_reads()["bad_signature"] == 1


def test_with_only_bad_observations_the_ref_is_refused(fake, hub):
    for n in FILES:
        seed_file(fake, n)
    seed_ref(fake, "main", COMMIT, 1_700_000_000_000, sig="f" * 64)
    hub.refuse()
    assert _get(f"/{REPO}/resolve/main/config.json").status_code == 502
    assert _restore_counts() == {"bad_signature": 1}
    # Known positive: the same file by commit needs no ref and restores.
    assert _get(f"/{REPO}/resolve/{COMMIT}/config.json").status_code == 200


def test_entries_the_phase1_writer_wrote_are_restorable(fake, hub):
    """The other direction from the hand-signed tests: what the writer puts in
    the bucket, the reader accepts. Written through the real upload path."""
    async def write():
        await tier.process(tier._hf_ref_index("model", REPO, "main", COMMIT, time.time()))
        for n, b in FILES.items():
            fake.seed(_content_key(_etag(n)), b)
            await tier.process(tier._hf_commit_index("model", REPO, COMMIT, n, _etag(n), len(b)))

    _run(write)
    hub.refuse()
    r = _get(f"/{REPO}/resolve/main/model.safetensors")
    assert r.status_code == 200 and r.content == FILES["model.safetensors"]
    assert r.headers["x-xhc-index-auth"] == "signed"


# ---------------------------------------------------------------------------
# content: verified against the ETag the entry names, whatever the entry says
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["model.safetensors", "config.json"])
def test_tampered_content_is_refused_by_hash_and_nothing_is_served(fake, hub, name):
    good = FILES[name]
    wrong = bytes(b ^ 0x21 for b in good)
    assert len(wrong) == len(good) and wrong != good  # a size check cannot pass it
    seed_model(fake)
    seed_file(fake, name, content=wrong)
    hub.refuse()
    r = _get(f"/{REPO}/resolve/main/{name}")
    assert r.status_code == 502
    assert wrong not in r.content
    assert not _blob(_etag(name)).exists()
    assert _restore_counts() == {"content_mismatch": 1}
    assert metrics.snapshot()["tier_verify"]["mismatch"] == 1
    assert tier.hf_blob_key("model", REPO, _etag(name)) in tier.status()["bad_keys"]
    # Never deleted from the bucket.
    assert fake.objects[_content_key(_etag(name))].body == wrong


def test_a_head_does_not_promise_content_the_tier_does_not_hold(fake, hub):
    seed_model(fake)
    del fake.objects[_content_key(_etag("config.json"))]
    hub.refuse()
    assert _get(f"/{REPO}/resolve/main/config.json", "HEAD").status_code == 502
    assert _restore_counts() == {"content_missing": 1}
    # Known positive.
    assert _get(f"/{REPO}/resolve/main/tokenizer.json", "HEAD").status_code == 200


def test_a_file_the_index_never_held_is_not_invented(fake, hub):
    seed_model(fake)
    hub.refuse()
    r = _get(f"/{REPO}/resolve/main/processor_config.json")
    assert r.status_code == 502
    assert _restore_counts() == {"missing": 1}


def test_an_unsafe_path_in_an_unsigned_listing_is_refused(fake, hub, monkeypatch):
    _install(monkeypatch, fake, restore_unsigned=True)
    seed_model(fake, signed=False)
    evil = {"../../escape.json": b"{}"}
    seed_file(fake, "../../escape.json", files=evil, signed=False)
    hub.refuse()
    r = _get(f"/api/models/{REPO}/revision/main")
    assert r.status_code == 502
    assert _restore_counts() == {"bad_signature": 1}
    assert not (Path(settings.cache_dir).parent / "escape.json").exists()


# ---------------------------------------------------------------------------
# git-blob content: written back, read back, verified by the git-blob rule
# ---------------------------------------------------------------------------


def test_git_blob_files_are_written_back_and_read_through(fake, hub):
    etag = _etag("config.json")
    r = _run(lambda: _then_settle(_req("GET", f"/{REPO}/resolve/main/config.json")))
    assert r.status_code == 200
    stored = fake.objects[_content_key(etag)].body
    # Re-fetched from the store and hashed here, not read off the PUT.
    assert _git_blob_id(stored) == etag and stored == FILES["config.json"]
    assert _commit_key(COMMIT, "config.json") in fake.objects

    # A lost disk, the Hub still up: the Hub's HEAD names the git blob id and
    # the tier supplies the bytes.
    cachefs_root = _blob(etag).parent.parent
    import shutil

    shutil.rmtree(cachefs_root)
    hub.log.clear()
    r2 = _get(f"/{REPO}/resolve/main/config.json")
    assert r2.status_code == 200 and r2.content == FILES["config.json"]
    assert r2.headers["x-xhc-cache"] == "TIER-HIT"
    assert not [e for e in hub.log if e.startswith("GET")], "no content GET to the Hub"


def test_a_git_blob_corrupted_after_done_is_refused_at_upload(fake, tmp_path):
    body = FILES["config.json"]
    etag = _git_blob_id(body)
    p = tmp_path / etag
    p.write_bytes(bytes(b ^ 1 for b in body))
    key = tier.hf_blob_key("model", REPO, etag)
    item = tier._content(key, p, etag, name_kind="gitsha1")
    assert asyncio.run(tier.process(item)) == "verify_mismatch"
    assert key not in fake.objects
    # Known positive.
    p.write_bytes(body)
    assert asyncio.run(tier.process(tier._content(key, p, etag, name_kind="gitsha1"))) == "ok"
    assert fake.objects[key].body == body


async def _then_settle(coro):
    r = await coro
    await _settle()
    return r


def test_repo_info_from_the_hub_indexes_the_ref(fake, hub):
    """A client's snapshot_download fetches every file BY COMMIT, so without
    this its `main` would never be indexed and could never be restored."""
    r = _run(lambda: _then_settle(_req("GET", f"/api/models/{REPO}/revision/main")))
    assert r.status_code == 200
    (ref,) = [k for k in fake.objects if "/refs/main/" in k]
    assert ref.endswith(f"-{COMMIT}")


# ---------------------------------------------------------------------------
# prewarm with the Hub down
# ---------------------------------------------------------------------------


def test_a_prewarm_runs_entirely_from_the_index_when_the_hub_is_down(fake, hub):
    seed_model(fake, obs_ms=1_700_000_000_000)
    refs_before = sorted(k for k in fake.objects if "/refs/" in k)
    hub.refuse()

    async def scenario():
        job = await manager.ensure_snapshot("model", REPO, "main")
        await job.done.wait()
        await _settle()
        return job

    job = _run(scenario)
    assert job.state == "done", job.error
    snap = Path(job.result_path)
    assert snap.name == COMMIT
    for n, b in FILES.items():
        assert (snap / n).read_bytes() == b
    d = job.to_dict()
    assert d["tier_restore"]["commit"] == COMMIT
    assert d["tier_restore"]["ref_observed_at"] == _iso(1_700_000_000_000)
    # Hashed once, as they arrived; not again by the verifier.
    assert d["verify"]["verified_at_tier_read"] == len(FILES)
    assert d["verify"]["mismatched"] == 0
    # No NEW observation of `main`: nothing new was observed, and writing one
    # would stamp an old answer with today's time.
    assert sorted(k for k in fake.objects if "/refs/" in k) == refs_before
    assert _restore_counts() == {"ok": 1}


def test_a_prewarm_missing_content_fails_naming_the_file(fake, hub):
    seed_model(fake)
    del fake.objects[_content_key(_etag("tokenizer.json"))]
    hub.refuse()

    async def scenario():
        job = await manager.ensure_snapshot("model", REPO, "main")
        await job.done.wait()
        return job

    job = _run(scenario)
    assert job.state == "error"
    assert "tokenizer.json: content_missing" in job.error
    assert "tier index could not restore it" in job.error


def test_a_prewarm_the_index_cannot_serve_says_why(fake, hub, monkeypatch):
    _install(monkeypatch, fake, index_key=None)
    seed_model(fake)
    hub.refuse()

    async def scenario():
        job = await manager.ensure_snapshot("model", REPO, "main")
        await job.done.wait()
        return job

    job = _run(scenario)
    assert job.state == "error" and "no_key" in job.error


# ---------------------------------------------------------------------------
# found while building: the per-blob lock must be released where it was taken
# ---------------------------------------------------------------------------


def test_a_tier_fill_releases_its_blob_lock_on_the_thread_that_took_it(fake, monkeypatch):
    """filelock keeps its record of who holds a lock PER THREAD. A lock taken on
    one worker thread and released on another leaves the first thread
    believing it still holds it, and the next FileLock on that blob to run
    there raises "Deadlock". The end-to-end restore test hit this as an
    intermittent 500. Here the event loop's default executor runs every call
    on a fresh thread, so taking and releasing through it can never agree:
    the fill must not use it for the lock."""
    import concurrent.futures
    import itertools
    import threading as th

    import filelock

    seen: list[tuple[str, str]] = []
    real_acq, real_rel = filelock.FileLock.acquire, filelock.FileLock.release

    def acq(self, *a, **k):
        seen.append(("acquire", th.current_thread().name))
        return real_acq(self, *a, **k)

    def rel(self, *a, **k):
        seen.append(("release", th.current_thread().name))
        return real_rel(self, *a, **k)

    monkeypatch.setattr(filelock.FileLock, "acquire", acq)
    monkeypatch.setattr(filelock.FileLock, "release", rel)

    # Names, not idents: a joined thread's ident is reused by the next one, so
    # two different threads can report the same get_ident().
    counter = itertools.count()

    class FreshThreadEach(concurrent.futures.ThreadPoolExecutor):
        def submit(self, fn, *args, **kwargs):
            fut: concurrent.futures.Future = concurrent.futures.Future()

            def run():
                try:
                    fut.set_result(fn(*args, **kwargs))
                except BaseException as exc:  # noqa: BLE001 - handed to the future
                    fut.set_exception(exc)

            t = th.Thread(target=run, name=f"fresh-{next(counter)}")
            t.start()
            t.join()
            return fut

    seed_file(fake, "model.safetensors")
    etag = _etag("model.safetensors")

    async def scenario():
        asyncio.get_running_loop().set_default_executor(FreshThreadEach())
        return await tier._fill_blob("model", REPO, etag, len(FILES["model.safetensors"]))

    assert _run(scenario) is True
    assert _blob(etag).read_bytes() == FILES["model.safetensors"]
    ours = [s for s in seen if s[0] in ("acquire", "release")][:2]
    assert [s[0] for s in ours] == ["acquire", "release"], seen
    assert ours[0][1] == ours[1][1], "released on a different thread from the one that took it"


# ---------------------------------------------------------------------------
# the classifier, directly
# ---------------------------------------------------------------------------


def test_only_a_transport_failure_reads_as_unreachable():
    t = tierrestore.trigger_for_exception
    assert t(requests.exceptions.ConnectionError("refused")) == "unreachable"
    assert t(requests.exceptions.ReadTimeout("slow")) == "unreachable"
    assert t(httpx.ConnectError("refused")) == "unreachable"
    # A local fault says nothing about the Hub, and must not start a restore.
    assert t(OSError("disk full")) is None
    assert t(ValueError("parse")) is None


def test_status_reports_the_restore_mode(fake, monkeypatch):
    st = tier.status()["restore"]
    assert st["enabled"] is True and st["mode"].startswith("on, signed entries only")
    _install(monkeypatch, fake, restore_unsigned=True)
    assert "UNSIGNED" in tier.status()["restore"]["mode"]
    _install(monkeypatch, fake, restore=False)
    assert tier.status()["restore"]["enabled"] is False
