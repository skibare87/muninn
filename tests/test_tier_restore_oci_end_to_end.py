"""An image survives its registry going away: the whole round trip, through uvicorn.

Phase A, the registry is up: a client pulls a multi-arch tag through the cache
the way docker does -- HEAD the tag, GET it, GET the platform manifest by
digest, GET the config and layer. The cache writes manifests, blobs and a
signed tag observation to the bucket in the background, as in production.

Phase B, the cache loses its disk and the registry refuses connections: the
same pull again. The tag must be answered from the bucket's index, say so, and
every digest must verify -- the index, the platform manifest and the blobs.

Deliberately imports NOTHING tier-specific from app/. Configuration arrives as
environment variables and the store is a real HTTP server, so this test runs
against a tree with no OCI restore at all and fails there on BEHAVIOUR (phase
B's pull by tag gets an error) rather than on an import.
"""

from __future__ import annotations

import hashlib
import shutil
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from registryfake import DOCKER_ACCEPT, FakeRegistry, Image, free_port
from tierfake import FakeS3

from app import registry
from app.config import Settings, settings

UPSTREAM = "registry.test"
REPO = "team/app"
NAME = f"{UPSTREAM}/{REPO}"


def _sha(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


@pytest.fixture
def world(tmp_path, monkeypatch):
    reg = FakeRegistry()
    image = Image("e2e")
    reg.push(REPO, "v1", image)
    fake = FakeS3("bkt")
    store_url, store = fake.serve()
    for d in ("cache", "docker"):
        (tmp_path / d).mkdir()
    monkeypatch.setattr(settings, "cache_dir", str(tmp_path / "cache"))
    monkeypatch.setattr(settings, "docker_dir", str(tmp_path / "docker"))
    monkeypatch.setattr(settings, "orphan_check_interval_s", 0.0)
    monkeypatch.setattr(settings, "state_dir", None)
    monkeypatch.setattr(settings, "manage_token", "restore-e2e-token")
    for k, v in {
        "XHC_TIER2": "s3://bkt/muninn",
        "XHC_TIER2_ENDPOINT": store_url,
        "XHC_TIER2_ACCESS_KEY_ID": "e2e-id",
        "XHC_TIER2_SECRET_ACCESS_KEY": "e2e-secret",
        "XHC_TIER2_RECONCILE_INTERVAL": "0",
        "XHC_TIER2_INDEX_KEY": "an-index-key-from-configuration",
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(settings, "tier", getattr(Settings.from_env(), "tier", None),
                        raising=False)
    real_resolve = registry.resolve

    def resolve(name):
        ref = real_resolve(name)
        if ref.upstream == UPSTREAM:
            return registry.Ref(upstream=UPSTREAM, api=reg.url, repo=ref.repo)
        return ref

    monkeypatch.setattr(registry, "resolve", resolve)
    registry.clear_tokens()
    try:
        yield fake, reg, image, tmp_path
    finally:
        reg.close()
        store.shutdown()
        store.server_close()
        registry.clear_tokens()


def _pull(endpoint: str, image: Image) -> dict:
    """What docker does for one platform, asserting every digest on the way."""
    accept = {"accept": DOCKER_ACCEPT}
    head = httpx.head(f"{endpoint}/v2/{NAME}/manifests/v1", headers=accept)
    assert head.status_code == 200, (head.status_code, head.text)
    assert head.headers["docker-content-digest"] == image.index_digest
    r = httpx.get(f"{endpoint}/v2/{NAME}/manifests/v1", headers=accept)
    assert r.status_code == 200, (r.status_code, r.text)
    assert _sha(r.content) == r.headers["docker-content-digest"] == image.index_digest
    assert r.headers["content-type"].startswith("application/vnd.oci.image.index.v1+json")
    plat = image.platforms["amd64"]
    m = httpx.get(f"{endpoint}/v2/{NAME}/manifests/{plat}", headers=accept)
    assert m.status_code == 200 and _sha(m.content) == plat
    for blob in image.blobs:
        if blob not in m.text:
            continue
        b = httpx.get(f"{endpoint}/v2/{NAME}/blobs/{blob}")
        assert b.status_code == 200 and _sha(b.content) == blob, blob
    return {"head": head, "tag": r, "platform": m}


def test_an_image_survives_the_registry_refusing_connections(world):
    import uvicorn

    from app.main import app

    fake, reg, image, tmp_path = world
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{port}"
    auth = {"authorization": "Bearer restore-e2e-token"}
    try:
        for _ in range(250):
            if server.started:
                break
            time.sleep(0.02)
        deadline = time.time() + 10
        while time.time() < deadline:
            st = httpx.get(f"{endpoint}/_cache/status", headers=auth).json()
            if (st.get("tier") or {}).get("healthy"):
                break
            time.sleep(0.05)

        # --- phase A: the registry is up.
        first = _pull(endpoint, image)
        assert first["tag"].headers.get("x-xhc-cache") != "TIER-RESTORE"
        tag_prefix = f"muninn/v1/index/oci/{UPSTREAM}/tags/{REPO}/v1/"
        plat = image.platforms["amd64"]
        plat_doc = image.manifests[plat][0]
        wanted_blobs = [d for d in image.blobs if d in plat_doc.decode()]
        want = {f"muninn/v1/content/oci/{UPSTREAM}/manifests/sha256/{d[7:9]}/{d[7:]}"
                for d in (image.index_digest, plat)}
        want |= {f"muninn/v1/content/oci/{UPSTREAM}/blobs/sha256/{d[7:9]}/{d[7:]}"
                 for d in wanted_blobs}
        deadline = time.time() + 20
        while time.time() < deadline:
            have = set(fake.objects)
            if want <= have and any(k.startswith(tag_prefix) for k in have):
                break
            time.sleep(0.05)
        have = set(fake.objects)
        assert want <= have, f"not written back: {sorted(want - have)}"
        observed = [k for k in have if k.startswith(tag_prefix)]
        assert observed and observed[0].endswith(image.index_digest), observed

        # --- phase B: the disk is lost and the registry refuses connections.
        for p in Path(settings.docker_dir).iterdir():
            if p.is_dir():
                shutil.rmtree(p)
        reg.refuse()
        with pytest.raises(httpx.ConnectError):
            httpx.get(f"{reg.url}/v2/", timeout=2)
        registry_requests = len(reg.log)

        second = _pull(endpoint, image)
        for which in ("head", "tag"):
            h = second[which].headers
            assert h.get("x-xhc-cache") == "TIER-RESTORE", (which, dict(h))
            assert h.get("x-xhc-restore-reason") == "unreachable"
            assert h.get("x-xhc-index-auth") == "signed"
            assert h.get("x-xhc-ref-observed-at", "").endswith("Z")
            assert int(h.get("x-xhc-ref-age")) >= 0
        assert second["head"].content == b""
        assert second["platform"].headers.get("x-xhc-cache") == "TIER-HIT"
        assert len(reg.log) == registry_requests, "the registry was down; nothing reached it"
        assert not [o for o in fake.ops("DELETE") if "uploadId" not in o[2]]
    finally:
        server.should_exit = True
        thread.join(timeout=10)
