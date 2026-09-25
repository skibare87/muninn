"""The tier client against a REAL S3-compatible server (MinIO).

The fake in tests/tierfake.py was written by the same hands as the client, so
it shares their reading of the protocol. This suite does not: MinIO checks the
SigV4 signature, the x-amz-content-sha256 payload hash, the checksum header,
multipart part-size rules and ListObjectsV2 pagination by its own code.

WHAT THIS DOES NOT VERIFY: Cloudflare R2, Google Cloud Storage (XML API, HMAC or
bearer), Backblaze B2 or AWS itself. Each is its own implementation; MinIO
agreeing with this client says nothing about them.

Marked `minio`. Two ways to run it:

    # CI: a MinIO service container, pointed to by environment
    MUNINN_TEST_MINIO_ENDPOINT=http://127.0.0.1:9000 \\
    MUNINN_TEST_MINIO_ACCESS_KEY=... MUNINN_TEST_MINIO_SECRET_KEY=... \\
        pytest -m minio

    # Locally: with `docker` on PATH and no endpoint set, the suite starts a
    # throwaway MinIO on a random localhost port and removes it afterwards.
    pytest -m minio

It is skipped, not failed, when neither is available -- and says so -- unless
MUNINN_TEST_MINIO_REQUIRED is set, as it is in CI, where a skip would read as
a pass.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import metrics, s3client, tier
from app.config import TierSettings, settings

pytestmark = pytest.mark.minio

IMAGE = os.environ.get("MUNINN_TEST_MINIO_IMAGE", "cgr.dev/chainguard/minio@sha256:bd014394a80898e68c149f2311fdf8d5a2c2f3bb2c33b9327ae6d02b4b065ae1")
BUCKET = "muninn-tier-test"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_ready(endpoint: str, timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(f"{endpoint}/minio/health/live", timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"MinIO at {endpoint} did not become ready in {timeout}s")


@pytest.fixture(scope="module")
def minio():
    endpoint = os.environ.get("MUNINN_TEST_MINIO_ENDPOINT")
    container = None
    if endpoint:
        access = os.environ["MUNINN_TEST_MINIO_ACCESS_KEY"]
        secret = os.environ["MUNINN_TEST_MINIO_SECRET_KEY"]
    else:
        if not shutil.which("docker"):
            msg = "no MUNINN_TEST_MINIO_ENDPOINT and no docker: MinIO suite not run"
            # In CI a skip would read as a pass. There, absence is a failure.
            if os.environ.get("MUNINN_TEST_MINIO_REQUIRED"):
                pytest.fail(msg)
            pytest.skip(msg)
        port = _free_port()
        # Throwaway credentials, generated per run and never written anywhere.
        access, secret = "t" + secrets.token_hex(8), secrets.token_hex(20)
        name = f"muninn-tier-test-{secrets.token_hex(4)}"
        subprocess.run(
            ["docker", "run", "-d", "--rm", "--name", name,
             "-p", f"127.0.0.1:{port}:9000",
             "-e", f"MINIO_ROOT_USER={access}", "-e", f"MINIO_ROOT_PASSWORD={secret}",
             IMAGE, "server", "/data"],
            check=True, capture_output=True,
        )
        container = name
        endpoint = f"http://127.0.0.1:{port}"
    try:
        _wait_ready(endpoint)
        _make_bucket(endpoint, access, secret)
        yield endpoint, access, secret
    finally:
        if container:
            subprocess.run(["docker", "stop", container], capture_output=True, check=False)


def _make_bucket(endpoint: str, access: str, secret: str) -> None:
    """Test SETUP only, with the root credential. The tier client itself has no
    bucket-level call, by construction -- so this signs a PUT Bucket by hand."""
    host = endpoint.split("://", 1)[1]
    amz_date = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    hdrs = s3client.sign_v4(method="PUT", host=host, canonical_uri=f"/{BUCKET}", query=None,
                            headers={}, payload_hash=s3client.EMPTY_SHA256, access_key=access,
                            secret_key=secret, region="us-east-1", service="s3",
                            amz_date=amz_date)
    r = httpx.put(f"{endpoint}/{BUCKET}", headers=hdrs)
    assert r.status_code in (200, 409), r.text


@pytest.fixture
def live(minio, tmp_path, monkeypatch):
    endpoint, access, secret = minio
    (tmp_path / "cache").mkdir()
    monkeypatch.setattr(settings, "cache_dir", str(tmp_path / "cache"))
    monkeypatch.setattr(settings, "docker_dir", str(tmp_path / "docker"))
    prefix = f"run-{secrets.token_hex(6)}"  # isolates tests sharing the bucket
    monkeypatch.setattr(settings, "tier", TierSettings(
        scheme="s3", bucket=BUCKET, prefix=prefix, endpoint=endpoint, region="us-east-1",
        path_style=True, credentials="static", access_key_id=access, secret_access_key=secret,
        part_size=5 * 1024 * 1024, index_key=b"i" * 32, checksum_header=True,
    ))
    monkeypatch.setattr(tier, "BACKOFF_S", (0,))
    tier.reset_for_tests()
    metrics.reset()
    tier.use_client(tier.build_client(httpx.AsyncClient(timeout=60)), healthy=False)
    yield prefix
    tier.reset_for_tests()


def _run(coro):
    async def main():
        try:
            return await coro
        finally:
            await tier._s.client._client.aclose()

    return asyncio.run(main())


def test_probe_is_healthy_and_a_missing_key_is_a_404(live):
    res = _run(tier.probe())
    assert res["ok"], res


def test_probe_refuses_a_credential_that_does_not_exist(live, monkeypatch):
    """Known negative: MinIO refuses an unknown access key, so the probe must."""
    tier.use_client(s3client.S3Client(
        endpoint=settings.tier.endpoint, bucket=BUCKET, region="us-east-1", path_style=True,
        creds=s3client.StaticKeys("nobody", "wrong-secret"), client=httpx.AsyncClient()),
        healthy=False)
    res = _run(tier.probe())
    assert not res["ok"]


def test_single_put_round_trip_and_the_server_checks_the_payload_hash(live, tmp_path):
    data = os.urandom(300_000)
    sha = hashlib.sha256(data).hexdigest()
    p = tmp_path / sha
    p.write_bytes(data)
    key = tier.hf_content_key("model", "acme/w", sha)

    async def scenario():
        assert await tier.process(tier._content(key, p, sha)) == "ok"
        got = await tier._s.client.get_bytes(key)
        # A body that does not match the declared payload hash: MinIO refuses it.
        bad = await tier._s.client.put(tier.hf_content_key("model", "acme/w", "0" * 64),
                                       b"not those bytes", sha256_hex="0" * 64)
        # The checksum header alone, wrong while the payload hash is right:
        # does THIS server check x-amz-checksum-sha256? (MinIO only -- this
        # says nothing about R2 or GCS.)
        other = base64.b64encode(hashlib.sha256(b"other").digest()).decode()
        bad2 = await tier._s.client._request(
            "PUT", tier.hf_content_key("model", "acme/w", "1" * 64),
            headers={"x-amz-checksum-sha256": other}, content=b"x")
        ok2 = await tier._s.client.put(tier.hf_content_key("model", "acme/w", "2" * 64),
                                       b"x", checksum_header=True)
        return got, bad, bad2, ok2

    got, bad, bad2, ok2 = _run(scenario())
    assert got.status_code == 200
    assert hashlib.sha256(got.content).hexdigest() == sha, "re-fetched, not read off the PUT"
    assert bad.status_code == 400, s3client.describe(bad)
    assert bad2.status_code == 400, s3client.describe(bad2)
    assert ok2.status_code == 200, "known positive: a correct checksum header is accepted"


def test_multipart_upload_above_the_part_size(live, tmp_path):
    part = settings.tier.part_size
    data = os.urandom(part * 2 + 12345)  # three parts, the last one short
    sha = hashlib.sha256(data).hexdigest()
    p = tmp_path / sha
    p.write_bytes(data)
    key = tier.hf_content_key("model", "acme/w", sha)

    async def scenario():
        res = await tier.process(tier._content(key, p, sha))
        got = await tier._s.client.get_bytes(key)
        return res, got

    res, got = _run(scenario())
    assert res == "ok"
    assert hashlib.sha256(got.content).hexdigest() == sha
    assert metrics.snapshot()["tier_bytes_written"] == len(data)


def test_multipart_mismatch_is_aborted_and_leaves_no_object(live, tmp_path):
    part = settings.tier.part_size
    data = os.urandom(part + 1)
    p = tmp_path / "blob"
    p.write_bytes(data)
    claimed = hashlib.sha256(b"something else").hexdigest()
    key = tier.hf_content_key("model", "acme/w", claimed)

    async def scenario():
        res = await tier.process(tier._content(key, p, claimed))
        head = await tier._s.client.head(key)
        uploads = await tier._s.client._request("GET", None, query={"uploads": ""})
        return res, head, uploads

    res, head, uploads = _run(scenario())
    assert res == "verify_mismatch"
    assert head.status_code == 404
    # Nothing left in progress under this key: the abort reached the server.
    ns = "{http://s3.amazonaws.com/doc/2006-03-01/}"
    pending = [u.findtext(f"{ns}Key") for u in ET.fromstring(uploads.content).iter(f"{ns}Upload")]
    assert key not in pending


def test_keys_with_reserved_characters_round_trip(live):
    """Index keys carry a percent-quoted path. The signature is computed over
    the encoded path, so a single-encoding mistake shows up here as a 403."""
    key = tier.hf_commit_index_key("model", "acme/w", "c" * 40, "sub dir/a+b%.json")

    async def scenario():
        r = await tier._s.client.put(key, b"{}")
        g = await tier._s.client.get_bytes(key)
        listed = [o.key async for o in tier._s.client.list_prefix(key.rsplit("/", 1)[0] + "/")]
        return r, g, listed

    r, g, listed = _run(scenario())
    assert r.status_code == 200, s3client.describe(r)
    assert g.status_code == 200 and g.content == b"{}"
    assert listed == [key]


def test_read_through_verifies_against_the_name(live, tmp_path):
    """A wrong object of the right length is refused by the same code path the
    app uses, against a real server's bytes."""
    good = os.urandom(100_000)
    sha = hashlib.sha256(good).hexdigest()
    wrong = bytes(b ^ 1 for b in good)
    key = tier.hf_content_key("model", "acme/w", sha)
    tmp = tmp_path / "t"

    async def scenario():
        await tier.probe()
        await tier._s.client.put(key, wrong)
        resp = await tier.open_read(key, "hf", "blob", len(good))
        got, n = await tier.hash_into(resp, tmp, flush=False)
        await resp.aclose()
        return got, n

    got, n = _run(scenario())
    assert n == len(good)
    assert got != sha, "the hash, not the length, is what tells them apart"


def test_list_follows_continuation_tokens_and_reconcile_sizes_the_tier(live):
    """ListObjectsV2 against a real server, across more than one page."""
    async def scenario():
        for i in range(5):
            await tier._s.client.put(f"{tier._base()}/content/x/{i}", b"z" * i)
        paged = [o.key async for o in tier._s.client.list_prefix(
            f"{tier._base()}/content/", page_size=2)]
        return paged, await tier.reconcile()

    paged, summary = _run(scenario())
    assert paged == [f"{tier._base()}/content/x/{i}" for i in range(5)]
    assert summary["ok"], summary
    assert summary["tier_objects"] == 5 and summary["tier_bytes"] == sum(range(5))


def test_index_entries_keep_their_auth_marker_on_a_real_server(live, monkeypatch):
    """The signed/unsigned marker lives in the object: a JSON field for a commit
    entry, user metadata for a ref. Read both back from MinIO."""
    from dataclasses import replace

    async def scenario():
        out = {}
        for key_set in (True, False):
            monkeypatch.setattr(settings, "tier",
                                replace(settings.tier, index_key=b"i" * 32 if key_set else None))
            ref = tier._hf_ref_index("model", "acme/w", "main", "d" * 40, time.time())
            commit = tier._hf_commit_index("model", "acme/w", "d" * 40,
                                           f"f-{key_set}.json", "e" * 64, 3)
            await tier.process(ref)
            await tier.process(commit)
            head = await tier._s.client.head(ref.key)
            body = (await tier._s.client.get_bytes(commit.key)).json()
            out[key_set] = (head.headers.get("x-amz-meta-auth"),
                            head.headers.get("x-amz-meta-sig"), body)
        return out

    out = _run(scenario())
    auth, sig, body = out[True]
    assert auth == tier.AUTH_SIGNED and sig and body["auth"] == tier.AUTH_SIGNED and body["sig"]
    auth, sig, body = out[False]
    assert auth == tier.AUTH_UNSIGNED and sig is None
    assert body["auth"] == tier.AUTH_UNSIGNED and "sig" not in body
