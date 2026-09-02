"""Push-through against a REAL registry:2 (an internal issue).

Skipped unless a registry is reachable. To run it:

    docker run -d --name muninn-push-test -p 127.0.0.1:5100:5000 registry:2
    .venv/bin/pytest -q tests/test_ocipush_integration.py

WHY THIS EXISTS AND THE UNIT TESTS ARE NOT ENOUGH. The first version of the
upload body factory was a SYNC generator. Every unit test passed -- they
consumed the iterator directly -- and httpx's AsyncClient rejects a sync
iterable outright: "Attempted to send an sync request with an AsyncClient
instance". The bug was invisible to every test that did not go through the real
client to a real registry, and it would have failed the very first push.

So this asserts the things only a real registry can answer: that a chunked
upload reassembles into the same bytes, that a monolithic PUT is accepted, that
an existing blob is not re-uploaded, and that a manifest lands and is
retrievable.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import ocipush, pushlimits, registry
from app.config import settings

REGISTRY = "127.0.0.1:5100"


def _reachable() -> bool:
    try:
        return httpx.get(f"http://{REGISTRY}/v2/", timeout=2).status_code == 200
    except Exception:  # noqa: BLE001 - any failure means "not available"
        return False


pytestmark = pytest.mark.skipif(
    not _reachable(),
    reason=f"no registry:2 on {REGISTRY} -- see this module's docstring",
)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "docker_dir", str(tmp_path))
    monkeypatch.setattr(settings, "docker_push_enabled", True)
    monkeypatch.setattr(settings, "docker_cache_on_push", True)
    monkeypatch.setattr(settings, "docker_push_limits", None)
    monkeypatch.setattr(settings, "docker_blob_chunk", 0)
    pushlimits.reset()
    yield tmp_path
    pushlimits.reset()


def _ref(repo="test/img"):
    return registry.Ref(upstream=REGISTRY, api=f"http://{REGISTRY}", repo=repo)


def _blob(tmp_path, name, fill, size):
    payload = fill * size
    p = tmp_path / name
    p.write_bytes(payload)
    return p, "sha256:" + hashlib.sha256(payload).hexdigest()


def test_monolithic_push_lands(env):
    path, digest = _blob(env, "mono", b"x", 5 * 1024 * 1024)
    ref = _ref("test/mono")

    async def go():
        await ocipush.push_blob(ref, path, digest)
        return await registry.request(ref, "HEAD", f"blobs/{digest}")

    assert asyncio.run(go()).status_code == 200


def test_chunked_push_reassembles_to_the_same_bytes(env):
    """The property that matters: chunking must not corrupt anything.

    A 5 MiB layer at 1 MiB chunks is five PATCHes plus a PUT, and the registry
    is the only thing that can confirm they were stitched back together in the
    right order.
    """
    path, digest = _blob(env, "chunked", b"y", 5 * 1024 * 1024)
    ref = _ref("test/chunked")
    settings.docker_blob_chunk = 1024 * 1024
    pushlimits.reset()

    async def go():
        await ocipush.push_blob(ref, path, digest)
        got = await registry.request(ref, "GET", f"blobs/{digest}")
        return got.status_code, hashlib.sha256(got.content).hexdigest()

    status, sha = asyncio.run(go())
    assert status == 200
    assert f"sha256:{sha}" == digest, "chunked upload corrupted the blob"


def test_an_existing_blob_is_not_re_uploaded(env, monkeypatch):
    """What makes a second push of a similar image fast."""
    path, digest = _blob(env, "dedupe", b"z", 1024 * 1024)
    ref = _ref("test/dedupe")

    async def go():
        await ocipush.push_blob(ref, path, digest)
        sessions = {"n": 0}
        original = registry.request

        async def counting(r, method, p, headers=None, content=None):
            if method == "POST":
                sessions["n"] += 1
            return await original(r, method, p, headers, content)

        monkeypatch.setattr(registry, "request", counting)
        await ocipush.push_blob(ref, path, digest)
        return sessions["n"]

    assert asyncio.run(go()) == 0, "re-uploaded a blob the registry already had"


def test_manifest_push_lands_and_is_retrievable(env):
    path, config_digest = _blob(env, "cfg", b"c", 512)
    ref = _ref("test/manifest")
    media = "application/vnd.oci.image.manifest.v1+json"

    async def go():
        await ocipush.push_blob(ref, path, config_digest)
        body = json.dumps({
            "schemaVersion": 2,
            "mediaType": media,
            "config": {
                "mediaType": "application/vnd.oci.image.config.v1+json",
                "size": 512,
                "digest": config_digest,
            },
            "layers": [],
        }).encode()
        digest = await ocipush.push_manifest(ref, body, media, "latest")
        got = await registry.request(ref, "GET", "manifests/latest",
                                     {"accept": media})
        return digest, got.status_code

    digest, status = asyncio.run(go())
    assert status == 200
    assert digest.startswith("sha256:")
