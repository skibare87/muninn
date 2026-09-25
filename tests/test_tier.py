"""The object-store second tier (XHC_TIER2), phase 1, against a fake store.

The fake (tests/tierfake.py) shares its author's reading of the S3 protocol,
so it can only confirm this client agrees with itself about the wire. The
independent checks are the published SigV4 vectors below and the MinIO suite
(tests/test_tier_minio.py). What this module pins is BEHAVIOUR: what is served,
what is linked, what is uploaded, and when.

Every test that asserts a zero has a twin, or an earlier step, that produces a
non-zero from a known positive -- a check that has never been seen to fail is
decoration.
"""

from __future__ import annotations

import asyncio
import builtins
import hashlib
import hmac
import http.server
import json
import os
import sys
import threading
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tierfake import FakeS3

from app import hfcompat, jobs, metrics, ocistore, refs, registry, s3client, tier
from app.config import Settings, TierSettings, settings
from app.jobs import manager

REPO = "acme/weights"
FILENAME = "model.safetensors"
COMMIT = "c" * 40
TRUE_BYTES = (b"the bytes the Hub actually holds. " * 1300)[:40_000]
ETAG = hashlib.sha256(TRUE_BYTES).hexdigest()
# Same LENGTH, different bytes: a size check cannot pass these by accident.
WRONG_BYTES = bytes(b ^ 0x5A for b in TRUE_BYTES)
assert len(WRONG_BYTES) == len(TRUE_BYTES) and WRONG_BYTES != TRUE_BYTES
INDEX_KEY = b"k" * 32


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


class _Hub(http.server.BaseHTTPRequestHandler):
    """A minimal Hub resolve endpoint that counts HEADs and GETs."""

    etag = ETAG
    body = TRUE_BYTES
    get_status = 200
    heads = 0
    gets = 0

    def _headers(self):
        return {
            "x-repo-commit": COMMIT,
            "etag": f'"{type(self).etag}"',
            "x-linked-etag": f'"{type(self).etag}"',
            "content-length": str(len(type(self).body)),
            "x-linked-size": str(len(type(self).body)),
            "accept-ranges": "bytes",
            "content-type": "application/octet-stream",
        }

    def do_HEAD(self):
        type(self).heads += 1
        self.send_response(200)
        for k, v in self._headers().items():
            self.send_header(k, v)
        self.end_headers()

    def do_GET(self):
        type(self).gets += 1
        if type(self).get_status != 200:
            self.send_response(type(self).get_status)
            self.send_header("content-length", "0")
            self.end_headers()
            return
        self.send_response(200)
        for k, v in self._headers().items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(type(self).body)

    def log_message(self, *a):
        return


@pytest.fixture
def hub(monkeypatch):
    _Hub.etag, _Hub.body, _Hub.get_status, _Hub.heads, _Hub.gets = ETAG, TRUE_BYTES, 200, 0, 0
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Hub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    monkeypatch.setattr(settings, "upstream", url)
    try:
        yield _Hub
    finally:
        srv.shutdown()
        srv.server_close()


def _tier_settings(**kw) -> TierSettings:
    base = dict(
        scheme="s3", bucket="bkt", prefix="pfx", endpoint="http://tier.test", region="auto",
        path_style=True, credentials="static", access_key_id="AKID", secret_access_key="SECRET",
        part_size=16 * 1024, index_key=INDEX_KEY,
    )
    base.update(kw)
    return TierSettings(**base)


@pytest.fixture
def fake(tmp_path, monkeypatch):
    (tmp_path / "cache").mkdir()
    (tmp_path / "docker").mkdir()
    monkeypatch.setattr(settings, "cache_dir", str(tmp_path / "cache"))
    monkeypatch.setattr(settings, "docker_dir", str(tmp_path / "docker"))
    monkeypatch.setattr(settings, "tier", _tier_settings())
    monkeypatch.setattr(settings, "miss_policy", "stream")
    monkeypatch.setattr(settings, "stream_poll_interval_s", 0.01)
    monkeypatch.setattr(tier, "BACKOFF_S", (0, 0))
    tier.reset_for_tests()
    metrics.reset()
    hfcompat.negative_cache_clear()
    ocistore.reset_stats_cache()
    f = FakeS3()
    tier.use_client(tier.build_client(httpx.AsyncClient(transport=f.transport())))
    yield f
    tier.reset_for_tests()


def _set_tier(monkeypatch, fake: FakeS3, **kw) -> None:
    monkeypatch.setattr(settings, "tier", _tier_settings(**kw))
    tier.reset_for_tests()
    tier.use_client(tier.build_client(httpx.AsyncClient(transport=fake.transport())))


async def _settle() -> None:
    """Let the job's background write-back scheduling finish, then upload."""
    for _ in range(500):
        pending = [t for t in manager._tasks if not t.done() and t is not manager._progress_task]
        if not pending:
            break
        await asyncio.sleep(0.01)
    await tier.drain()


async def _close_clients() -> None:
    await hfcompat.close_client()
    await refs.close_client()


def _run(coro_fn):
    async def main():
        try:
            return await coro_fn()
        finally:
            await _close_clients()

    return asyncio.run(main())


async def _get(path: str, headers: dict | None = None) -> httpx.Response:
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://muninn") as c:
        return await c.get(path, headers=headers or {})


def _hf_key(etag: str = ETAG) -> str:
    return tier.hf_content_key("model", REPO, etag)


def _blob(etag: str = ETAG) -> Path:
    return Path(settings.cache_dir) / "models--acme--weights" / "blobs" / etag


def _snapshot_link() -> Path:
    return Path(settings.cache_dir) / "models--acme--weights" / "snapshots" / COMMIT / FILENAME


RESOLVE = f"/{REPO}/resolve/main/{FILENAME}"


# ---------------------------------------------------------------------------
# SigV4: an independent reference, not our own reading of the spec
# ---------------------------------------------------------------------------


def test_sigv4_matches_the_published_aws_vectors():
    """AWS's published test-suite values. A signer tested only against a fake
    we wrote would share our reading of the specification."""
    common = dict(access_key="AKIDEXAMPLE", secret_key="wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
                  region="us-east-1", service="service", amz_date="20150830T123600Z",
                  payload_hash=s3client.EMPTY_SHA256, sign_payload_header=False)
    # aws-sig-v4-test-suite: get-vanilla
    a = s3client.sign_v4(method="GET", host="example.amazonaws.com", canonical_uri="/",
                         query=None, headers={}, **common)
    assert a["authorization"].endswith(
        "Signature=5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31")
    # get-vanilla-query-order-key-case: query keys are sorted
    b = s3client.sign_v4(method="GET", host="example.amazonaws.com", canonical_uri="/",
                         query={"Param2": "value2", "Param1": "value1"}, headers={}, **common)
    assert b["authorization"].endswith(
        "Signature=b97d918cfa904a5beff61c982a1b6f458b799221646efd99d3219ec94cdf2500")
    # The S3 developer guide's GET Object example (with Range and the
    # x-amz-content-sha256 header S3 requires).
    c = s3client.sign_v4(
        method="GET", host="examplebucket.s3.amazonaws.com", canonical_uri="/test.txt",
        query=None, headers={"range": "bytes=0-9"}, payload_hash=s3client.EMPTY_SHA256,
        access_key="AKIAIOSFODNN7EXAMPLE", secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        region="us-east-1", service="s3", amz_date="20130524T000000Z",
    )
    assert c["authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, "
        "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, "
        "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    )


# ---------------------------------------------------------------------------
# configuration: fails on the arguments, before any I/O
# ---------------------------------------------------------------------------


def _clear_tier_env(monkeypatch):
    for k in list(os.environ):
        if k.startswith("XHC_TIER2") or k.startswith("AWS_"):
            monkeypatch.delenv(k, raising=False)


def test_unset_means_off(monkeypatch):
    _clear_tier_env(monkeypatch)
    assert Settings.from_env().tier is None


def test_static_needs_keys_and_never_reads_aws_env(monkeypatch):
    _clear_tier_env(monkeypatch)
    monkeypatch.setenv("XHC_TIER2", "s3://b/p")
    # An ambient AWS credential must not silently become the tier's.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ambient")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "ambient")
    with pytest.raises(ValueError, match="AWS_\\* is never read"):
        Settings.from_env()


def test_key_and_key_file_together_are_refused(monkeypatch, tmp_path):
    _clear_tier_env(monkeypatch)
    f = tmp_path / "id"
    f.write_text("from-file\n")
    monkeypatch.setenv("XHC_TIER2", "s3://b")
    monkeypatch.setenv("XHC_TIER2_ACCESS_KEY_ID", "direct")
    monkeypatch.setenv("XHC_TIER2_ACCESS_KEY_ID_FILE", str(f))
    monkeypatch.setenv("XHC_TIER2_SECRET_ACCESS_KEY", "s")
    with pytest.raises(ValueError, match="not both"):
        Settings.from_env()
    # Known positive: the file form alone works, and is stripped.
    monkeypatch.delenv("XHC_TIER2_ACCESS_KEY_ID")
    assert Settings.from_env().tier.access_key_id == "from-file"


def test_gs_defaults_to_metadata_credentials_and_the_xml_api(monkeypatch):
    _clear_tier_env(monkeypatch)
    monkeypatch.setenv("XHC_TIER2", "gs://bucket/pre")
    t = Settings.from_env().tier
    assert (t.credentials, t.endpoint, t.path_style) == (
        "gcp-metadata", "https://storage.googleapis.com", True)
    # The amz checksum header is off by default for GCS, which has its own.
    assert t.checksum_header is False


def test_bad_values_are_refused(monkeypatch):
    _clear_tier_env(monkeypatch)
    monkeypatch.setenv("XHC_TIER2_ACCESS_KEY_ID", "a")
    monkeypatch.setenv("XHC_TIER2_SECRET_ACCESS_KEY", "b")
    for var, val in (("XHC_TIER2", "http://nope"), ("XHC_TIER2_READ_MODE", "fast"),
                     ("XHC_TIER2_PART_SIZE", "1M"), ("XHC_TIER2_CREDENTIALS", "irsa")):
        monkeypatch.setenv("XHC_TIER2", "s3://b")
        monkeypatch.setenv(var, val)
        with pytest.raises(ValueError):
            Settings.from_env()
        monkeypatch.delenv(var)


# ---------------------------------------------------------------------------
# the startup probe
# ---------------------------------------------------------------------------


def test_probe_ok_against_a_well_behaved_store(fake):
    tier.reset_for_tests()
    tier.use_client(tier.build_client(httpx.AsyncClient(transport=fake.transport())),
                    healthy=False)
    res = asyncio.run(tier.probe())
    assert res["ok"], res
    assert tier.readable()
    # It PUT and GOT its probe object, and deleted nothing.
    assert fake.ops("PUT") and not fake.ops("DELETE")


def test_probe_refuses_403_for_a_missing_key(fake):
    """Without list permission S3 answers 403 for a missing key, and every
    tier miss would then look like an auth failure. The probe must refuse."""
    fake.missing_status = 403
    tier.reset_for_tests()
    tier.use_client(tier.build_client(httpx.AsyncClient(transport=fake.transport())),
                    healthy=False)
    res = asyncio.run(tier.probe())
    assert not res["ok"]
    assert "403" in res["error"] and "list permission" in res["error"]
    assert not tier.readable()


def test_probe_reports_a_dead_credential(fake):
    fake.status_for_all = 401
    tier.reset_for_tests()
    tier.use_client(tier.build_client(httpx.AsyncClient(transport=fake.transport())),
                    healthy=False)
    res = asyncio.run(tier.probe())
    assert not res["ok"] and "401" in res["error"]


def test_no_bucket_level_call_is_ever_made(fake, hub):
    """Across a probe, a hit, a miss, an upload and a reconcile, every request
    names a key -- except ListObjectsV2, which lists under a prefix."""

    async def scenario():
        await tier.probe()
        fake.seed(_hf_key(), TRUE_BYTES)
        await _get(RESOLVE)
        await tier.reconcile()
        await tier.drain()

    _run(scenario)
    assert fake.requests, "known positive: the fake saw traffic"
    for method, key, query in fake.requests:
        assert key is not None or query.get("list-type") == "2", (method, key, query)


# ---------------------------------------------------------------------------
# HF read-through
# ---------------------------------------------------------------------------


def test_known_positive_tier_hit_serves_without_an_upstream_get(fake, hub):
    fake.seed(_hf_key(), TRUE_BYTES)

    async def scenario():
        return await _get(RESOLVE)

    r = _run(scenario)
    assert r.status_code == 200
    assert r.content == TRUE_BYTES
    assert r.headers["x-xhc-cache"] == "TIER-HIT"
    assert hub.gets == 0, "the content must come from the tier, not the Hub"
    assert hub.heads >= 1, "the Hub HEAD still runs: it is where the expected hash comes from"
    snap = metrics.snapshot()
    assert snap["tier_verify"]["verified"] == 1
    assert snap["tier_requests"]["hf|blob|hit"] == 1
    assert snap["tier_bytes_read"] == len(TRUE_BYTES)
    assert snap["bytes_ingested"] == 0, "tier bytes are not the upstream leg"
    assert _blob().read_bytes() == TRUE_BYTES


@pytest.mark.parametrize("policy", ["stream", "wait"])
def test_ACCEPTANCE_wrong_bytes_of_the_same_length_are_never_served_or_linked(
        fake, hub, monkeypatch, policy):
    """The spec's acceptance test 1.

    Seed the tier key for sha256 X with DIFFERENT bytes of the SAME length.
    With the Hub's GET failing, the request must fail rather than serve those
    bytes, nothing may be linked, the mismatch must be counted, the object must
    NOT be deleted from the tier, and a retry must succeed from the Hub.

    Deleting the hash comparison in tier.fill_hf_blob turns this red; that was
    done once by hand and is recorded in the change's description.
    """
    monkeypatch.setattr(settings, "miss_policy", policy)
    fake.seed(_hf_key(), WRONG_BYTES)
    hub.get_status = 403  # the Hub's content GET fails on the first attempt

    async def scenario():
        first = await _get(RESOLVE)
        state = {
            "blob": _blob().exists(),
            "link": _snapshot_link().exists() or _snapshot_link().is_symlink(),
            # name -> bytes, read NOW: the retry below consumes these files.
            "stray": {p.name: p.read_bytes() for p in _blob().parent.glob("*")}
            if _blob().parent.exists() else {},
        }
        hub.get_status = 200
        tier_gets_before = len([r for r in fake.ops("GET") if r[1] == _hf_key()])
        retry = await _get(RESOLVE)
        tier_gets_after = len([r for r in fake.ops("GET") if r[1] == _hf_key()])
        return first, state, retry, tier_gets_before, tier_gets_after

    first, state, retry, before, after = _run(scenario)

    # Under verify-first a job the tier answered is always waited for, so both
    # policies answer 502 rather than a truncated 2xx.
    assert first.status_code == 502
    assert len(first.content) < len(TRUE_BYTES)
    assert WRONG_BYTES[:64] not in first.content, "no tier byte may reach the client"
    assert not state["blob"], "blobs/X must not exist"
    assert not state["link"], "nothing may be linked"
    assert not any(n.endswith(".tier.incomplete") for n in state["stray"]), state["stray"]
    for name, data in state["stray"].items():  # e.g. huggingface_hub's own .incomplete
        assert WRONG_BYTES[:64] not in data, name
    assert metrics.snapshot()["tier_verify"]["mismatch"] == 1
    assert _hf_key() in fake.objects, "Muninn never deletes from the tier"
    assert not fake.ops("DELETE")

    assert retry.status_code == 200
    assert retry.content == TRUE_BYTES
    assert hub.gets >= 2, "the retry came from the Hub"
    assert after == before, "a key marked bad is not read again"


def test_wrong_bytes_fall_through_to_the_hub_in_the_same_request(fake, hub):
    """The same mismatch with a healthy Hub: the client still gets the right
    bytes, from the Hub, in one request -- and the header says it was not a hit."""
    fake.seed(_hf_key(), WRONG_BYTES)
    r = _run(lambda: _get(RESOLVE))
    assert r.status_code == 200
    assert r.content == TRUE_BYTES
    assert r.headers["x-xhc-cache"] == "MISS-WAIT"
    assert hub.gets == 1
    assert hashlib.sha256(_blob().read_bytes()).hexdigest() == ETAG
    assert metrics.snapshot()["tier_verify"]["mismatch"] == 1


def test_one_hash_pass_on_a_tier_read(fake, hub, monkeypatch):
    """HARD REQUIREMENT: hash while fetching, rename on a match, never re-read.

    Every sha256 update of a content-sized chunk is counted, process-wide. With
    XHC_HF_VERIFY on (the default), a second pass would come from the ingest
    verifier re-hashing the blob; so the verifier is spied on too.
    """
    assert settings.hf_verify_ingest
    real = hashlib.sha256
    hashed = {"bytes": 0}

    class Counting:
        def __init__(self, data=b""):
            self._h = real()
            if data:
                self.update(data)

        def update(self, b):
            if len(b) >= 1024:  # content chunks; not signing strings or keys
                hashed["bytes"] += len(b)
            self._h.update(b)

        def hexdigest(self):
            return self._h.hexdigest()

        def digest(self):
            return self._h.digest()

    monkeypatch.setattr(hashlib, "sha256", Counting)
    monkeypatch.setattr(tier, "CHUNK", 8192)  # several chunks, not one
    verifier_calls = []
    real_verify = jobs.verify_ingested
    monkeypatch.setattr(jobs, "verify_ingested",
                        lambda p: verifier_calls.append(p) or real_verify(p))
    fake.seed(_hf_key(), TRUE_BYTES)

    r = _run(lambda: _get(RESOLVE))
    assert r.headers["x-xhc-cache"] == "TIER-HIT"
    assert hashed["bytes"] == len(TRUE_BYTES), (
        f"{hashed['bytes']} bytes hashed for a {len(TRUE_BYTES)}-byte object: "
        "exactly one pass is allowed"
    )
    assert verifier_calls == [], "the ingest verifier must not re-read a tier-verified blob"


def test_one_hash_pass_known_positive_upstream_ingest_is_hashed_by_the_verifier(
        fake, hub, monkeypatch):
    """The counter above can see a pass: an upstream ingest is hashed once by
    the verifier (and not by the tier, which missed)."""
    real = hashlib.sha256
    hashed = {"bytes": 0}

    class Counting:
        def __init__(self, data=b""):
            self._h = real()
            if data:
                self.update(data)

        def update(self, b):
            if len(b) >= 1024:
                hashed["bytes"] += len(b)
            self._h.update(b)

        def hexdigest(self):
            return self._h.hexdigest()

    monkeypatch.setattr(hashlib, "sha256", Counting)
    monkeypatch.setattr(settings, "tier", None)  # no write-back hashing either
    r = _run(lambda: _get(RESOLVE))
    assert r.status_code == 200
    assert hashed["bytes"] == len(TRUE_BYTES)


def test_stream_mode_serves_from_the_tier_and_says_so(fake, hub, monkeypatch):
    _set_tier(monkeypatch, fake, read_mode="stream")
    fake.seed(_hf_key(), TRUE_BYTES)
    r = _run(lambda: _get(RESOLVE))
    assert r.status_code == 200 and r.content == TRUE_BYTES
    assert r.headers["x-xhc-cache"] == "TIER-STREAM"
    assert hub.gets == 0


def test_min_size_skips_the_tier(fake, hub, monkeypatch):
    _set_tier(monkeypatch, fake, min_size=len(TRUE_BYTES) + 1)
    fake.seed(_hf_key(), TRUE_BYTES)
    r = _run(lambda: _get(RESOLVE))
    assert r.headers["x-xhc-cache"] == "MISS-STREAM"
    assert not [x for x in fake.ops("GET") if x[1] == _hf_key()]


def test_a_tier_outage_falls_through_to_the_hub(fake, hub):
    fake.down = True
    r = _run(lambda: _get(RESOLVE))
    assert r.status_code == 200 and r.content == TRUE_BYTES
    assert r.headers["x-xhc-cache"] == "MISS-STREAM"
    assert metrics.snapshot()["tier_requests"]["hf|blob|error"] == 1
    assert tier.readable(), "a transport error is not a dead credential"


def test_a_401_disables_the_tier_until_the_next_probe(fake, hub):
    fake.status_for_all = 401
    r = _run(lambda: _get(RESOLVE))
    assert r.status_code == 200 and r.content == TRUE_BYTES
    assert not tier.readable()
    assert "401" in tier.status()["last_error"]


# ---------------------------------------------------------------------------
# write-back
# ---------------------------------------------------------------------------


def test_write_back_after_done_uploads_content_and_index(fake, hub):
    r = _run(lambda: _settle_after(_get(RESOLVE)))
    assert r.status_code == 200
    stored = fake.objects[_hf_key()].body
    # Re-fetched from the store, not read off the PUT response.
    assert hashlib.sha256(stored).hexdigest() == ETAG
    assert metrics.snapshot()["tier_upload"]["ok"] == 1
    assert metrics.snapshot()["tier_bytes_written"] >= len(TRUE_BYTES)

    idx_key = tier.hf_commit_index_key("model", REPO, COMMIT, FILENAME)
    idx = json.loads(fake.objects[idx_key].body)
    assert (idx["etag"], idx["size"]) == (ETAG, len(TRUE_BYTES))
    canon = json.dumps([tier.INDEX_VERSION, "hf-commit", tier.hf_host(), f"models/{REPO}",
                        COMMIT, FILENAME, ETAG, len(TRUE_BYTES), ""], separators=(",", ":"))
    assert idx["sig"] == hmac.new(INDEX_KEY, canon.encode(), hashlib.sha256).hexdigest()
    assert idx["auth"] == tier.AUTH_SIGNED and idx["version"] == tier.INDEX_VERSION

    refs_ = [k for k in fake.objects if "/refs/main/" in k]
    assert len(refs_) == 1 and refs_[0].endswith(f"-{COMMIT}")
    meta = fake.objects[refs_[0]].metadata
    assert meta["x-amz-meta-sig"] and meta["x-amz-meta-observed-at"]
    assert meta["x-amz-meta-auth"] == tier.AUTH_SIGNED
    assert fake.objects[refs_[0]].body == b""
    writes = metrics.snapshot()["tier_index_writes"]
    assert writes["signed"] == 2 and writes["unsigned"] == 0


async def _settle_after(coro):
    r = await coro
    await _settle()
    return r


def test_without_a_key_the_index_is_written_unsigned_and_says_so(fake, hub, monkeypatch):
    """Phase 1 writes the index regardless, so a later phase can restore what
    this one ingested -- if its policy accepts unsigned entries. The object
    itself says it is unsigned; nothing has to infer it from a missing field."""
    _set_tier(monkeypatch, fake, index_key=None)
    _run(lambda: _settle_after(_get(RESOLVE)))
    assert _hf_key() in fake.objects

    idx = json.loads(fake.objects[tier.hf_commit_index_key("model", REPO, COMMIT, FILENAME)].body)
    assert idx == {"etag": ETAG, "size": len(TRUE_BYTES), "auth": tier.AUTH_UNSIGNED,
                   "version": tier.INDEX_VERSION}
    (ref,) = [k for k in fake.objects if "/refs/main/" in k]
    meta = fake.objects[ref].metadata
    assert meta["x-amz-meta-auth"] == tier.AUTH_UNSIGNED
    assert "x-amz-meta-sig" not in meta and meta["x-amz-meta-observed-at"]
    writes = metrics.snapshot()["tier_index_writes"]
    assert writes["unsigned"] == 2 and writes["signed"] == 0
    assert tier.status()["index"].startswith("unsigned")


def test_an_existing_index_entry_is_skipped_not_rewritten(fake, hub):
    idx_key = tier.hf_commit_index_key("model", REPO, COMMIT, FILENAME)
    fake.seed(idx_key, b'{"placed": "earlier"}')
    _run(lambda: _settle_after(_get(RESOLVE)))
    assert fake.objects[idx_key].body == b'{"placed": "earlier"}'
    assert metrics.snapshot()["tier_index_writes"]["skipped_exists"] == 1


def test_nothing_is_uploaded_from_a_job_that_ends_in_error(fake, hub):
    """The Hub serves bytes that contradict their ETag: verification fails and
    the job ends in `error`. Nothing may reach the tier."""
    hub.etag = hashlib.sha256(b"something else").hexdigest()

    async def scenario():
        r = await _get(RESOLVE)
        await _settle()
        return r

    _run(scenario)
    assert not fake.ops("PUT"), [x for x in fake.ops("PUT")]
    assert not fake.ops("POST")
    assert metrics.snapshot()["tier_upload"]["ok"] == 0


def test_nothing_is_uploaded_while_verifying_only_after_done(fake, hub, monkeypatch):
    gate = threading.Event()
    entered = threading.Event()
    real_verify = jobs.verify_ingested

    def slow_verify(p):
        entered.set()
        gate.wait(10)
        return real_verify(p)

    monkeypatch.setattr(jobs, "verify_ingested", slow_verify)
    monkeypatch.setattr(settings, "miss_policy", "wait")

    async def scenario():
        req = asyncio.create_task(_get(RESOLVE))
        while not entered.is_set():
            await asyncio.sleep(0.01)
        job = next(j for j in manager.list() if j.filename == FILENAME and j.state == "verifying")
        await asyncio.sleep(0.1)
        during = (job.state, list(fake.ops("PUT")), tier.queue_depth())
        gate.set()
        await req
        await _settle()
        return during

    state, puts, depth = _run(scenario)
    assert state == "verifying"
    assert puts == [] and depth == 0, "nothing may be uploaded before `done`"
    assert _hf_key() in fake.objects, "known positive: it was uploaded once done"


def test_a_local_blob_corrupted_after_done_is_refused_at_upload(fake, tmp_path):
    p = tmp_path / "blob"
    p.write_bytes(TRUE_BYTES[:-1] + b"X")
    res = asyncio.run(tier.process(tier._content(_hf_key(), p, ETAG)))
    assert res == "verify_mismatch"
    assert _hf_key() not in fake.objects
    assert metrics.snapshot()["tier_upload"]["verify_mismatch"] == 1
    # Known positive: the uncorrupted file uploads.
    p.write_bytes(TRUE_BYTES)
    assert asyncio.run(tier.process(tier._content(_hf_key(), p, ETAG))) == "ok"


def test_write_back_skips_objects_that_already_exist(fake, tmp_path):
    p = tmp_path / "blob"
    p.write_bytes(TRUE_BYTES)
    fake.seed(_hf_key(), TRUE_BYTES)
    assert asyncio.run(tier.process(tier._content(_hf_key(), p, ETAG))) == "skipped_exists"
    assert not fake.ops("PUT")


def test_evicted_before_upload_is_dropped_and_counted(fake, tmp_path):
    res = asyncio.run(tier.process(tier._content(_hf_key(), tmp_path / "gone", ETAG)))
    assert res == "skipped_evicted"
    assert metrics.snapshot()["tier_upload"]["skipped_evicted"] == 1


def test_queue_overflow_is_dropped_and_counted(fake, monkeypatch, tmp_path):
    _set_tier(monkeypatch, fake, queue_max=1)
    assert tier.enqueue(tier._content("a", tmp_path / "a", ETAG))
    assert not tier.enqueue(tier._content("b", tmp_path / "b", ETAG))
    assert metrics.snapshot()["tier_upload"]["dropped_queue_full"] == 1


class _CountingFile:
    def __init__(self, fh, counter):
        self._fh, self._c = fh, counter

    def read(self, n=-1):
        data = self._fh.read(n)
        self._c["bytes"] += len(data)
        return data

    def close(self):
        self._fh.close()


def _count_reads(monkeypatch, path: Path) -> dict:
    counter = {"opens": 0, "bytes": 0}

    def _open(p, mode="r", *a, **k):
        fh = builtins.open(p, mode, *a, **k)  # noqa: SIM115 - returned to the caller
        if Path(p) == path and "r" in mode:
            counter["opens"] += 1
            return _CountingFile(fh, counter)
        return fh

    monkeypatch.setattr(tier, "open", _open, raising=False)
    return counter


def _count_hashing(monkeypatch) -> dict:
    real = hashlib.sha256
    hashed = {"bytes": 0}

    class Counting:
        def __init__(self, data=b""):
            self._h = real()
            if data:
                self.update(data)

        def update(self, b):
            hashed["bytes"] += len(b)
            self._h.update(b)

        def hexdigest(self):
            return self._h.hexdigest()

    monkeypatch.setattr(hashlib, "sha256", Counting)
    return hashed


@pytest.mark.parametrize("size_factor", [0.5, 4.3])  # single PUT, then multipart
def test_one_read_and_one_hash_pass_on_upload(fake, tmp_path, monkeypatch, size_factor):
    """Upload write-back reads each byte of the local file ONCE and hashes it
    ONCE -- no pre-pass. A multipart part that fails in transit is re-sent
    from memory, never re-read from disk."""
    part = settings.tier.part_size
    data = os.urandom(int(part * size_factor))
    sha = hashlib.sha256(data).hexdigest()
    p = tmp_path / sha
    p.write_bytes(data)
    key = tier.hf_content_key("model", REPO, sha)
    if size_factor > 1:
        fake.fail_part_once = {2}
    reads = _count_reads(monkeypatch, p)
    hashed = _count_hashing(monkeypatch)

    assert asyncio.run(tier.process(tier._content(key, p, sha))) == "ok"
    assert reads == {"opens": 1, "bytes": len(data)}
    assert hashed["bytes"] == len(data)
    assert fake.objects[key].body == data
    if size_factor > 1:
        parts = [r for r in fake.ops("PUT") if "partNumber" in r[2]]
        assert len({r[2]["partNumber"] for r in parts}) == 5
        assert len(parts) == 6, "part 2 was retried once"
        assert fake.ops("POST"), "CreateMultipartUpload and CompleteMultipartUpload"


def test_multipart_mismatch_aborts_and_creates_no_object(fake, tmp_path):
    data = os.urandom(settings.tier.part_size * 3)
    p = tmp_path / "blob"
    p.write_bytes(data)
    claimed = hashlib.sha256(b"not these bytes").hexdigest()
    key = tier.hf_content_key("model", REPO, claimed)
    assert asyncio.run(tier.process(tier._content(key, p, claimed))) == "verify_mismatch"
    assert key not in fake.objects
    aborts = [r for r in fake.ops("DELETE") if "uploadId" in r[2]]
    assert len(aborts) == 1, "the incomplete multipart upload is aborted"


def test_single_put_sends_the_name_as_the_payload_hash(fake, tmp_path):
    """So a store that checks x-amz-content-sha256 refuses bytes that do not
    match their name, whatever this process believes."""
    small = TRUE_BYTES[:1000]  # below the part size: a single PUT
    sha = hashlib.sha256(small).hexdigest()
    p = tmp_path / "blob"
    p.write_bytes(small)
    key = tier.hf_content_key("model", REPO, sha)

    seen = {}
    real_put = tier._s.client.put

    async def spy(key, body, **kw):
        seen.update(kw)
        return await real_put(key, body, **kw)

    tier._s.client.put = spy
    assert asyncio.run(tier.process(tier._content(key, p, sha))) == "ok"
    assert seen["sha256_hex"] == sha and seen["checksum_header"] is True


# ---------------------------------------------------------------------------
# eviction never touches the tier
# ---------------------------------------------------------------------------


def test_local_eviction_never_deletes_from_the_tier(fake, hub, monkeypatch):
    from app import cachefs

    async def scenario():
        await _settle_after(_get(RESOLVE))
        before = len(fake.requests)
        monkeypatch.setattr(settings, "capacity_bytes", 1)  # everything is over budget
        res = await cachefs.evict()
        cachefs.delete_repo_sync("model", REPO)
        return before, res

    before, res = _run(scenario)
    assert res.get("evicted"), "known positive: eviction really removed something"
    assert not _blob().exists()
    assert len(fake.requests) == before, "eviction made no tier request at all"
    assert _hf_key() in fake.objects
    assert not fake.ops("DELETE")


def test_the_client_has_no_way_to_delete_an_object():
    """Structural: the only DELETE the client can send is AbortMultipartUpload."""
    src = (Path(__file__).resolve().parent.parent / "app" / "s3client.py").read_text()
    assert src.count('"DELETE"') == 1
    assert 'self._request("DELETE", key, query={"uploadId": upload_id})' in src
    tsrc = (Path(__file__).resolve().parent.parent / "app" / "tier.py").read_text()
    assert '"DELETE"' not in tsrc and ".delete(" not in tsrc


# ---------------------------------------------------------------------------
# reconciler: the bucket is the durable record
# ---------------------------------------------------------------------------


def test_reconciler_uploads_what_a_lost_queue_never_did(fake, hub, monkeypatch):
    """A restart with a non-empty queue loses the queue. The next reconcile
    finds the gap by LISTing the tier and uploads it."""

    async def scenario():
        await _get(RESOLVE)
        await _settle_after(asyncio.sleep(0))
        # Simulate the restart: the queue (and everything uploaded) is gone.
        fake.objects.clear()
        tier.reset_for_tests()
        tier.use_client(tier.build_client(httpx.AsyncClient(transport=fake.transport())))
        summary = await tier.reconcile()
        await tier.drain()
        return summary

    summary = _run(scenario)
    assert summary["ok"], summary
    assert summary["content_enqueued"] == 1
    assert hashlib.sha256(fake.objects[_hf_key()].body).hexdigest() == ETAG
    assert tier.hf_commit_index_key("model", REPO, COMMIT, FILENAME) in fake.objects
    assert [k for k in fake.objects if "/refs/main/" in k]
    # A second reconcile finds nothing to do, and reports the tier's size.
    again = asyncio.run(tier.reconcile())
    assert again["content_enqueued"] == 0 and again.get("index_enqueued") == 0
    assert again["tier_objects"] == 1 and again["tier_bytes"] == len(TRUE_BYTES)
    g = tier.gauges()
    assert g["muninn_tier_objects"] == 1 and "muninn_tier_last_reconcile_timestamp" in g


# ---------------------------------------------------------------------------
# OCI
# ---------------------------------------------------------------------------

OCI_BODY = (b"layer bytes " * 4000)[:40_000]
OCI_DIGEST = "sha256:" + hashlib.sha256(OCI_BODY).hexdigest()
OCI_WRONG = bytes(b ^ 0x33 for b in OCI_BODY)


class _Upstream:
    def __init__(self, body=OCI_BODY, status=200):
        self.status_code = status
        self.headers = {"content-length": str(len(body))}
        self._body = body

    async def aiter_bytes(self, n=None):
        yield self._body

    async def aclose(self):
        pass


def _oci_upstream(monkeypatch):
    calls = []

    async def _open(ref, path, headers=None):
        calls.append(path)
        return _Upstream()

    monkeypatch.setattr(registry, "open_stream", _open)
    monkeypatch.setattr(ocistore, "has_ingest_room", lambda: True)
    return calls


def test_oci_blob_known_positive_from_the_tier(fake, monkeypatch):
    calls = _oci_upstream(monkeypatch)
    fake.seed(tier.oci_blob_key("ghcr.io", OCI_DIGEST), OCI_BODY)
    r = _run(lambda: _get(f"/v2/ghcr.io/org/img/blobs/{OCI_DIGEST}"))
    assert r.status_code == 200 and r.content == OCI_BODY
    assert r.headers["x-xhc-cache"] == "TIER-HIT"
    assert calls == [], "the upstream registry was not asked"
    assert ocistore.blob_path("ghcr.io", OCI_DIGEST).read_bytes() == OCI_BODY


def test_oci_blob_wrong_bytes_same_length_fall_through_to_the_registry(fake, monkeypatch):
    calls = _oci_upstream(monkeypatch)
    key = tier.oci_blob_key("ghcr.io", OCI_DIGEST)
    fake.seed(key, OCI_WRONG)
    r = _run(lambda: _get(f"/v2/ghcr.io/org/img/blobs/{OCI_DIGEST}"))
    assert r.status_code == 200 and r.content == OCI_BODY
    assert r.headers.get("x-xhc-cache") == "MISS"
    assert calls == [f"blobs/{OCI_DIGEST}"]
    assert metrics.snapshot()["tier_verify"]["mismatch"] == 1
    assert tier.is_bad(key) and key in fake.objects


def test_oci_blob_ingested_from_the_registry_is_written_back(fake, monkeypatch):
    _oci_upstream(monkeypatch)

    async def scenario():
        r = await _get(f"/v2/ghcr.io/org/img/blobs/{OCI_DIGEST}")
        for _ in range(200):
            if tier.queue_depth():
                break
            await asyncio.sleep(0.01)
        await tier.drain()
        return r

    r = _run(scenario)
    assert r.content == OCI_BODY
    key = tier.oci_blob_key("ghcr.io", OCI_DIGEST)
    assert "sha256:" + hashlib.sha256(fake.objects[key].body).hexdigest() == OCI_DIGEST


MANIFEST = b'{"schemaVersion":2,"mediaType":"application/vnd.oci.image.manifest.v1+json"}'
MANIFEST_DIGEST = "sha256:" + hashlib.sha256(MANIFEST).hexdigest()
MEDIA = "application/vnd.oci.image.manifest.v1+json"


def _registry_manifest():
    from types import SimpleNamespace

    return SimpleNamespace(status_code=200, content=MANIFEST, headers={
        "content-type": MEDIA, "docker-content-digest": MANIFEST_DIGEST})


def test_oci_manifest_by_digest_from_the_tier(fake, monkeypatch):
    async def _never(*a, **k):
        raise AssertionError("the registry must not be asked")

    monkeypatch.setattr(registry, "get", _never)
    fake.seed(tier.oci_manifest_key("ghcr.io", MANIFEST_DIGEST), MANIFEST, content_type=MEDIA)
    r = _run(lambda: _get(f"/v2/ghcr.io/org/img/manifests/{MANIFEST_DIGEST}"))
    assert r.status_code == 200
    assert r.content == MANIFEST, "verbatim bytes"
    assert r.headers["content-type"].startswith(MEDIA)
    assert r.headers["x-xhc-cache"] == "TIER-HIT"


def test_oci_manifest_wrong_bytes_are_refused_and_the_registry_answers(fake, monkeypatch):
    wrong = MANIFEST.replace(b"2", b"3")
    assert len(wrong) == len(MANIFEST)

    async def _get_upstream(ref, path, headers=None, out=None, **k):
        return _registry_manifest()

    monkeypatch.setattr(registry, "get", _get_upstream)
    fake.seed(tier.oci_manifest_key("ghcr.io", MANIFEST_DIGEST), wrong, content_type=MEDIA)
    r = _run(lambda: _get(f"/v2/ghcr.io/org/img/manifests/{MANIFEST_DIGEST}"))
    assert r.status_code == 200 and r.content == MANIFEST
    assert r.headers.get("x-xhc-cache") != "TIER-HIT"
    assert metrics.snapshot()["tier_verify"]["mismatch"] == 1


def test_oci_tag_fetch_writes_manifest_and_a_signed_tag_observation(fake, monkeypatch):
    async def _get_upstream(ref, path, headers=None, out=None, **k):
        return _registry_manifest()

    monkeypatch.setattr(registry, "get", _get_upstream)

    async def scenario():
        r = await _get("/v2/ghcr.io/org/img/manifests/v1")
        await tier.drain()
        return r

    r = _run(scenario)
    assert r.status_code == 200
    assert fake.objects[tier.oci_manifest_key("ghcr.io", MANIFEST_DIGEST)].body == MANIFEST
    tags = [k for k in fake.objects if "/index/oci/ghcr.io/tags/org/img/v1/" in k]
    assert len(tags) == 1 and tags[0].endswith(MANIFEST_DIGEST)
    assert fake.objects[tags[0]].metadata["x-amz-meta-sig"]
    assert fake.objects[tags[0]].metadata["x-amz-meta-auth"] == tier.AUTH_SIGNED


# ---------------------------------------------------------------------------
# metrics and status
# ---------------------------------------------------------------------------


def test_tier_series_exist_at_zero_and_count(fake, hub):
    body = metrics.render({})
    for needle in ('muninn_tier_requests_total{proto="hf",kind="blob",result="hit"} 0',
                   'muninn_tier_verify_total{result="mismatch"} 0',
                   'muninn_tier_upload_total{result="verify_mismatch"} 0',
                   'muninn_tier_index_writes_total{result="unsigned"} 0',
                   'muninn_tier_index_writes_total{result="signed"} 0',
                   "muninn_tier_bytes_read_total 0", "muninn_tier_bytes_written_total 0"):
        assert needle in body, needle
    fake.seed(_hf_key(), TRUE_BYTES)
    _run(lambda: _get(RESOLVE))
    body = metrics.render({})
    assert 'muninn_tier_requests_total{proto="hf",kind="blob",result="hit"} 1' in body
    assert f"muninn_tier_bytes_read_total {len(TRUE_BYTES)}" in body


def test_status_block(fake):
    st = tier.status()
    assert st["enabled"] and st["healthy"] and "never" in st["deletes"]
    assert st["index"] == "signed"


def test_gauges_are_absent_when_the_tier_is_off(monkeypatch):
    monkeypatch.setattr(settings, "tier", None)
    assert tier.gauges() == {}
    assert tier.status() == {"enabled": False}


def test_the_bytes_read_counter_ignores_heads(fake, tmp_path):
    fake.seed(_hf_key(), TRUE_BYTES)
    p = tmp_path / "b"
    p.write_bytes(TRUE_BYTES)
    asyncio.run(tier.process(tier._content(_hf_key(), p, ETAG)))  # HEAD -> exists
    snap = metrics.snapshot()
    assert snap["tier_bytes_read"] == 0 and snap["tier_bytes_written"] == 0
