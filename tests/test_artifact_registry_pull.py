"""A pull from a Google Artifact Registry host, configured the way an operator does it.

Deliberately imports nothing that only exists with the credential providers --
configuration goes through XHC_REGISTRY_CREDENTIALS and Settings.from_env(), as
in a deployment -- so on a tree without the feature this fails on BEHAVIOUR
rather than on an import.

FAILS ON THE TREE BEFORE THE FEATURE, measured: the variable is not read, the
static path holds nothing for `us-docker.pkg.dev`, the fake's token endpoint
refuses an exchange that presents no `oauth2accesstoken`, and the cache answers
the manifest GET with 404 MANIFEST_UNKNOWN (the token endpoint refused an
anonymous exchange, which reads as "no such repository").

The rest of the provider tests are in test_registry_credentials.py.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from registryfake import DOCKER_ACCEPT, FakeMetadata, FakeRegistry, Image

from app import registry
from app.config import Settings, settings

AR = "us-docker.pkg.dev"
REPO = "proj/repo/img"


def _sha(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


def test_a_pull_from_an_artifact_registry_host_works(tmp_path, monkeypatch):
    reg = FakeRegistry()
    image = Image("ar-pull")
    reg.push(REPO, "v1", image)
    meta = FakeMetadata(reg)  # OAuth mode: only the metadata server's token works
    for d in ("cache", "docker"):
        (tmp_path / d).mkdir()
    monkeypatch.setattr(settings, "cache_dir", str(tmp_path / "cache"))
    monkeypatch.setattr(settings, "docker_dir", str(tmp_path / "docker"))
    monkeypatch.setattr(settings, "state_dir", None)
    monkeypatch.setattr(settings, "tier", None, raising=False)
    monkeypatch.setattr(settings, "docker_enabled", True)
    monkeypatch.setattr(settings, "registry_auth_file", None)
    monkeypatch.setenv("XHC_REGISTRY_CREDENTIALS", json.dumps([
        {"hosts": ["*-docker.pkg.dev"], "provider": "gcp-metadata",
         "metadata_url": meta.token_url}]))
    monkeypatch.setattr(settings, "registry_credentials",
                        getattr(Settings.from_env(), "registry_credentials", ()),
                        raising=False)
    real_resolve = registry.resolve

    def resolve(name):
        ref = real_resolve(name)
        if ref.upstream == AR:
            return registry.Ref(upstream=AR, api=reg.url, repo=ref.repo)
        return ref

    monkeypatch.setattr(registry, "resolve", resolve)
    registry.reset_credentials()
    registry.clear_tokens()

    async def go():
        from app.main import app
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://muninn", timeout=30) as c:
            accept = {"accept": DOCKER_ACCEPT}
            r = await c.get(f"/v2/{AR}/{REPO}/manifests/v1", headers=accept)
            assert r.status_code == 200, (r.status_code, r.text)
            assert _sha(r.content) == image.index_digest
            plat = image.platforms["amd64"]
            m = await c.get(f"/v2/{AR}/{REPO}/manifests/{plat}", headers=accept)
            assert m.status_code == 200 and _sha(m.content) == plat
            doc = json.loads(m.content)
            for d in [doc["config"]["digest"]] + [x["digest"] for x in doc["layers"]]:
                b = await c.get(f"/v2/{AR}/{REPO}/blobs/{d}")
                assert b.status_code == 200 and _sha(b.content) == d, d

    try:
        asyncio.run(go())
    finally:
        reg.close()
        meta.close()
        registry.reset_credentials()
        registry.clear_tokens()
    assert set(reg.state.oauth_seen) == {"oauth-1"}
