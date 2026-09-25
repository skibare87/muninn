"""Muninn is read-only toward the Hugging Face Hub, in every mode.

Everything the HF surface forwards goes out with the CACHE's Hub token. So a
forwarded POST to `api/repos/create` or `.../commit/main` is a write performed as
the cache, by whoever sent it -- on an open cache, by anyone. Muninn has no push
path for Hugging Face, so the only safe answer is a local 405.

Every refusal below asserts the upstream was never called: a 405 that had
already forwarded the request would be a log line, not a refusal.
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

COMMIT = "a" * 40
ETAG = "b" * 64
BODY = b'{"model_type": "test"}'

# Every state-changing call huggingface_hub 0.34.4 makes against the Hub, by the
# HfApi method that makes it, plus the generic verbs.
WRITES = [
    ("POST", "/api/models/org/x/commit/main"),            # create_commit
    ("POST", "/api/datasets/org/x/commit/main"),
    ("POST", "/api/models/org/x/preupload/main"),         # preupload
    ("POST", "/api/repos/create"),                        # create_repo
    ("DELETE", "/api/repos/delete"),                      # delete_repo
    ("POST", "/api/repos/move"),                          # move_repo
    ("PUT", "/api/models/org/x/settings"),                # update_repo_settings
    ("POST", "/api/models/org/x/branch/dev"),             # create_branch
    ("DELETE", "/api/models/org/x/branch/dev"),           # delete_branch
    ("POST", "/api/models/org/x/tag/v1"),                 # create_tag
    ("POST", "/api/models/org/x/super-squash/main"),      # super_squash_history
    ("POST", "/api/models/org/x/lfs-files/batch"),        # permanently_delete_lfs_files
    ("POST", "/org/x.git/info/lfs/objects/batch"),        # LFS upload batch
    ("POST", "/api/models/org/x/discussions"),            # create_discussion
    ("POST", "/api/spaces/org/x/restart"),                # restart_space
    ("POST", "/api/spaces/org/x/secrets"),                # add_space_secret
    ("POST", "/api/collections"),                         # create_collection
    ("POST", "/api/validate-yaml"),                       # only on the push path
    ("POST", "/org/x/resolve/main/config.json"),
    ("PATCH", "/api/models/org/x"),
    ("OPTIONS", "/api/models/org/x"),
    ("PUT", "/org/x/resolve/main/config.json"),
]

READ_POSTS = [
    "/api/models/org/x/paths-info/main",
    "/api/datasets/org/x/paths-info/refs/pr/1",
    "/api/models/gpt2/paths-info/main",
]


class _Upstream:
    def __init__(self):
        self.calls: list[str] = []

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.calls.append(f"{request.method} {request.url.path}")
            return httpx.Response(200, json=[])

        return httpx.MockTransport(handler)


@pytest.fixture(params=["auth-none", "rules-off", "rules-enforce-star"])
def mode(request, tmp_path, monkeypatch):
    """The same guarantee under every configuration of the HF surface."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app import dockerauth, hfcompat, refs
    from app.authz import Rule, new_secret
    from app.authzstore import AuthzStore
    from app.config import settings

    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(settings, "cache_dir", str(cache))
    monkeypatch.setattr(settings, "web_root", None)
    monkeypatch.setattr(settings, "state_dir", None)
    monkeypatch.setattr(settings, "synthesize_repo_info", True)
    monkeypatch.setattr(dockerauth, "_store", None)
    headers: dict[str, str] = {}
    if request.param == "auth-none":
        monkeypatch.setattr(settings, "hf_auth", "none")
        monkeypatch.setattr(settings, "authz_db", None)
    else:
        db = tmp_path / "authz.db"
        monkeypatch.setattr(settings, "authz_db", str(db))
        monkeypatch.setattr(settings, "hf_auth", "key")
        monkeypatch.setattr(settings, "hf_rules",
                            "off" if request.param == "rules-off" else "enforce")
        store = AuthzStore(db)
        store.claim_or_get_principal("p")
        store.set_principal_rules("p", [Rule("*", pull=True, push=True)])
        key_id, secret = new_secret()
        store.add_key(key_id, secret, "p", [])
        headers = {"authorization": f"Bearer {key_id}:{secret}"}

    upstream = _Upstream()
    upstream_client = httpx.AsyncClient(transport=upstream.transport())
    monkeypatch.setattr(hfcompat, "get_client", lambda: upstream_client)

    async def _never_stale(*_a, **_k):
        return False

    monkeypatch.setattr(refs, "is_stale", _never_stale)
    app = FastAPI()
    app.include_router(hfcompat.router)
    return TestClient(app, raise_server_exceptions=False), headers, upstream, cache


@pytest.mark.parametrize("method,path", WRITES)
def test_a_write_is_refused_locally_and_never_forwarded(mode, method, path):
    client, headers, upstream, _ = mode
    r = client.request(method, path, headers=headers, content=b"{}")
    assert r.status_code == 405, f"{method} {path} -> {r.status_code} {r.text}"
    assert "read-only toward the Hugging Face Hub" in r.text
    assert upstream.calls == [], upstream.calls


@pytest.mark.parametrize("path", READ_POSTS)
def test_an_allowlisted_read_post_is_still_forwarded(mode, path):
    client, headers, upstream, _ = mode
    r = client.post(path, headers=headers, data={"paths": "config.json"})
    assert r.status_code == 200, r.text
    assert upstream.calls == [f"POST {path}"]


def test_a_get_is_still_forwarded(mode):
    client, headers, upstream, _ = mode
    r = client.get("/api/models/org/x/refs", headers=headers)
    assert r.status_code == 200
    assert upstream.calls == ["GET /api/models/org/x/refs"]


def test_a_paths_info_lookalike_is_not_a_read(mode):
    """The allowlist is a full-path match on the endpoint, not a substring."""
    client, headers, upstream, _ = mode
    for path in ("/api/models/org/x/commit/main/paths-info/x",
                 "/api/repos/create/paths-info/main"):
        assert client.post(path, headers=headers).status_code == 405, path
    assert upstream.calls == []


def _seed(cache: Path, repo_id: str, filename: str = "config.json"):
    base = cache / f"models--{repo_id.replace('/', '--')}"
    (base / "blobs").mkdir(parents=True, exist_ok=True)
    (base / "refs").mkdir(parents=True, exist_ok=True)
    snap = base / "snapshots" / COMMIT
    snap.mkdir(parents=True, exist_ok=True)
    blob = base / "blobs" / ETAG
    blob.write_bytes(BODY)
    (snap / filename).symlink_to(blob)
    (base / "refs" / "main").write_text(COMMIT)


def test_hf_hub_download_and_snapshot_download_still_work_end_to_end(mode, tmp_path,
                                                                    monkeypatch):
    """The downloads a user actually runs, over a real socket, in every mode.
    snapshot_download enumerates through repo info (synthesised from cache here,
    because the fake Hub answers 404) and then fetches each file."""
    import socket
    import threading
    import time

    import uvicorn
    from huggingface_hub import hf_hub_download, snapshot_download

    client, headers, upstream, cache = mode

    def _hub_404(request: httpx.Request) -> httpx.Response:
        upstream.calls.append(f"{request.method} {request.url.path}")
        return httpx.Response(404, json={"error": "not here"})

    from app import hfcompat

    c404 = httpx.AsyncClient(transport=httpx.MockTransport(_hub_404))
    monkeypatch.setattr(hfcompat, "get_client", lambda: c404)
    _seed(cache, "org/model-a")

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(client.app, host="127.0.0.1", port=port,
                                           log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(200):
            if server.started:
                break
            time.sleep(0.02)
        endpoint = f"http://127.0.0.1:{port}"
        token = headers.get("authorization", " ").split(" ", 1)[1] or False
        got = hf_hub_download("org/model-a", "config.json", revision=COMMIT,
                              endpoint=endpoint, token=token, cache_dir=tmp_path / "c1")
        assert Path(got).read_bytes() == BODY
        snap = snapshot_download("org/model-a", revision=COMMIT, endpoint=endpoint,
                                 token=token, cache_dir=tmp_path / "c2")
        assert (Path(snap) / "config.json").read_bytes() == BODY
        assert not any(c.split()[0] not in ("GET", "HEAD", "POST") for c in upstream.calls)
    finally:
        server.should_exit = True
        thread.join(timeout=5)
