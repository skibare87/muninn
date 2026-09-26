"""A model survives the Hub going away: the whole round trip, through uvicorn.

Phase A, the Hub is up: a real huggingface_hub client runs snapshot_download
through the cache. The cache ingests from the Hub and writes content and a
signed index to the bucket in the background, exactly as it does in production.

Phase B, the cache loses its disk and the Hub stops accepting connections: a
second, cold client runs the same snapshot_download. Every byte must come from
the bucket, verified, and the answers must say they were restored.

Deliberately imports NOTHING tier-specific from app/. Configuration arrives as
environment variables and the store is a real HTTP server, so this test runs
against a tree with no restore at all and fails there on BEHAVIOUR (phase B
cannot list or fetch anything) rather than on an import. Run once against the
parent commit; recorded in the change's description.
"""

from __future__ import annotations

import hashlib
import http.server
import json
import shutil
import socket
import sys
import threading
import time
from pathlib import Path
from typing import ClassVar
from urllib.parse import unquote, urlparse

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tierfake import FakeS3

from app.config import Settings, settings

REPO = "acme/survivor"
COMMIT = "5" * 40
FILES = {
    # Git-blob files: a model restored without these is not a model.
    "config.json": b'{"model_type": "toy", "hidden": 8}',
    "tokenizer.json": b'{"vocab": ["a", "b", "c"]}',
    # LFS files, named by their sha256.
    "model.safetensors": (b"weights! " * 7000)[:60_000],
}
LFS = {"model.safetensors"}
_REAL_SHA256 = hashlib.sha256


def _git_blob_id(b: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(b) + b, usedforsecurity=False).hexdigest()


def _etag(name: str) -> str:
    return _REAL_SHA256(FILES[name]).hexdigest() if name in LFS else _git_blob_id(FILES[name])


class _Hub(http.server.BaseHTTPRequestHandler):
    log: ClassVar[list[str]] = []

    def _send(self, status: int, headers: dict[str, str], body: bytes = b"") -> None:
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _serve(self) -> None:
        path = unquote(urlparse(self.path).path)
        type(self).log.append(f"{self.command} {path}")
        if path.startswith(f"/api/models/{REPO}"):
            siblings = []
            for n, b in FILES.items():
                s = {"rfilename": n, "size": len(b), "blobId": _git_blob_id(b)}
                if n in LFS:
                    s["lfs"] = {"sha256": _etag(n), "size": len(b), "pointerSize": 134}
                siblings.append(s)
            body = json.dumps({"id": REPO, "sha": COMMIT, "siblings": siblings}).encode()
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


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def world(tmp_path, monkeypatch):
    _Hub.log = []
    hub = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Hub)
    threading.Thread(target=hub.serve_forever, daemon=True).start()
    hub_url = f"http://127.0.0.1:{hub.server_address[1]}"
    fake = FakeS3("bkt")
    store_url, store = fake.serve()

    for d in ("cache", "docker"):
        (tmp_path / d).mkdir()
    monkeypatch.setattr(settings, "upstream", hub_url)
    monkeypatch.setattr(settings, "hf_token", None)
    monkeypatch.setattr(settings, "cache_dir", str(tmp_path / "cache"))
    monkeypatch.setattr(settings, "docker_dir", str(tmp_path / "docker"))
    monkeypatch.setattr(settings, "miss_policy", "wait")
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
    state = {"hub": hub}
    try:
        yield fake, hub_url, tmp_path, state
    finally:
        if state["hub"] is not None:
            hub.shutdown()
            hub.server_close()
        store.shutdown()
        store.server_close()


def _layout(hub_url: str) -> tuple[str, list[str], list[str]]:
    """The spec's layout, written out here rather than computed by the code
    under test."""
    host = hub_url.split("://", 1)[1].replace(":", "_")
    base = f"muninn/v1/content/hf/{host}/models/{REPO}"
    content = [f"{base}/{'sha256' if n in LFS else 'gitsha1'}/{_etag(n)}" for n in FILES]
    idx = f"muninn/v1/index/hf/{host}/models/{REPO}"
    index = [f"{idx}/commits/{COMMIT}/{n}.json" for n in FILES]
    return f"{idx}/refs/main/", content, index


def test_a_model_survives_the_hub_refusing_connections(world):
    import uvicorn
    from huggingface_hub import snapshot_download

    from app.main import app

    fake, hub_url, tmp_path, state = world
    refs_prefix, content_keys, index_keys = _layout(hub_url)
    port = _free_port()
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

        # --- phase A: the Hub is up; a client pulls the model through the cache.
        first = snapshot_download(REPO, endpoint=endpoint, cache_dir=tmp_path / "client-a")
        for n, b in FILES.items():
            assert (Path(first) / n).read_bytes() == b
        # Write-back is in the background: wait for content, index and the ref.
        deadline = time.time() + 20
        want = set(content_keys) | set(index_keys)
        while time.time() < deadline:
            have = set(fake.objects)
            if want <= have and any(k.startswith(refs_prefix) for k in have):
                break
            time.sleep(0.05)
        have = set(fake.objects)
        assert want <= have, f"not written back: {sorted(want - have)}"
        assert any(k.startswith(refs_prefix) for k in have), "no observation of main"

        # --- phase B: the disk is lost and the Hub refuses connections.
        shutil.rmtree(Path(settings.cache_dir) / f"models--{REPO.replace('/', '--')}")
        state["hub"].shutdown()
        state["hub"].server_close()
        state["hub"] = None
        with pytest.raises(httpx.ConnectError):
            httpx.get(f"{hub_url}/api/models/{REPO}", timeout=2)
        hub_requests_before = len(_Hub.log)

        second = snapshot_download(REPO, endpoint=endpoint, cache_dir=tmp_path / "client-b")
        for n, b in FILES.items():
            assert (Path(second) / n).read_bytes() == b, n
        assert Path(second).name == COMMIT

        r = httpx.get(f"{endpoint}/{REPO}/resolve/main/config.json")
        assert r.status_code == 200 and r.content == FILES["config.json"]
        assert r.headers.get("x-xhc-cache") == "TIER-RESTORE", dict(r.headers)
        assert r.headers.get("x-xhc-restore-reason") == "unreachable"
        assert r.headers.get("x-xhc-index-auth") == "signed"
        assert r.headers.get("x-xhc-ref-observed-at", "").endswith("Z")
        assert r.headers.get("x-repo-commit") == COMMIT
        info = httpx.get(f"{endpoint}/api/models/{REPO}/revision/main").json()
        assert info["sha"] == COMMIT and info["xhcSynthesized"] is True
        assert sorted(s["rfilename"] for s in info["siblings"]) == sorted(FILES)
        assert len(_Hub.log) == hub_requests_before, "the Hub was down; nothing reached it"
        # Nothing was deleted from the bucket, ever.
        assert not [o for o in fake.ops("DELETE") if "uploadId" not in o[2]]
    finally:
        server.should_exit = True
        thread.join(timeout=10)
