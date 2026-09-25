"""The tier through the whole app: environment, lifespan, probe, HTTP, SigV4.

Deliberately imports NOTHING tier-specific from app/. Configuration arrives the
way an operator supplies it -- environment variables -- and the store is a real
HTTP server. So this test can be run against a tree with no tier at all, and
there it fails on BEHAVIOUR (the file comes from the Hub, not the bucket)
rather than on an import. That was done once, against the parent commit, and is
recorded in the change's description.
"""

from __future__ import annotations

import hashlib
import http.server
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient
from tierfake import FakeS3

from app.config import Settings, settings

REPO = "acme/weights"
FILENAME = "model.safetensors"
COMMIT = "e" * 40
BODY = (b"weights that live in the bucket. " * 2000)[:50_000]
ETAG = hashlib.sha256(BODY).hexdigest()


class _Hub(http.server.BaseHTTPRequestHandler):
    gets = 0

    def _h(self):
        for k, v in {
            "x-repo-commit": COMMIT, "etag": f'"{ETAG}"', "x-linked-etag": f'"{ETAG}"',
            "content-length": str(len(BODY)), "x-linked-size": str(len(BODY)),
            "accept-ranges": "bytes",
        }.items():
            self.send_header(k, v)

    def do_HEAD(self):
        self.send_response(200)
        self._h()
        self.end_headers()

    def do_GET(self):
        type(self).gets += 1
        self.send_response(200)
        self._h()
        self.end_headers()
        self.wfile.write(BODY)

    def log_message(self, *a):
        return


@pytest.fixture
def world(tmp_path, monkeypatch):
    _Hub.gets = 0
    hub = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Hub)
    threading.Thread(target=hub.serve_forever, daemon=True).start()
    hub_url = f"http://127.0.0.1:{hub.server_address[1]}"
    fake = FakeS3("bkt")
    store_url, store = fake.serve()

    for d in ("cache", "docker"):
        (tmp_path / d).mkdir()
    monkeypatch.setattr(settings, "upstream", hub_url)
    monkeypatch.setattr(settings, "cache_dir", str(tmp_path / "cache"))
    monkeypatch.setattr(settings, "docker_dir", str(tmp_path / "docker"))
    monkeypatch.setattr(settings, "miss_policy", "stream")
    monkeypatch.setattr(settings, "orphan_check_interval_s", 0.0)
    monkeypatch.setattr(settings, "state_dir", None)
    # Module-level HTTP clients left behind by earlier tests are bound to event
    # loops that no longer exist, and this test runs the lifespan, whose
    # shutdown closes them. Start from none.
    from app import hfcompat, orphans, refs, registry

    for mod in (hfcompat, orphans, refs, registry):
        monkeypatch.setattr(mod, "_client", None)

    # Exactly what an operator would set.
    for k, v in {
        "XHC_TIER2": "s3://bkt/muninn",
        "XHC_TIER2_ENDPOINT": store_url,
        "XHC_TIER2_ACCESS_KEY_ID": "e2e-id",
        "XHC_TIER2_SECRET_ACCESS_KEY": "e2e-secret",
        "XHC_TIER2_RECONCILE_INTERVAL": "0",
    }.items():
        monkeypatch.setenv(k, v)
    # raising=False: on a tree without the feature there is no such field, and
    # the test must still run and fail on behaviour.
    monkeypatch.setattr(settings, "tier", getattr(Settings.from_env(), "tier", None),
                        raising=False)

    # The spec's layout, written out here rather than computed by the code
    # under test: <prefix>/v1/content/hf/<hf-host>/<repo_type>s/<org>/<name>/sha256/<etag>
    host = hub_url.split("://", 1)[1].replace(":", "_")
    fake.seed(f"muninn/v1/content/hf/{host}/models/{REPO}/sha256/{ETAG}", BODY)
    try:
        yield fake
    finally:
        hub.shutdown()
        hub.server_close()
        store.shutdown()
        store.server_close()


def test_a_cold_disk_is_refilled_from_the_bucket_not_the_hub(world, monkeypatch):
    from app.config import settings
    from app.main import app

    # /_cache/status is off without a management token.
    monkeypatch.setattr(settings, "manage_token", "tier-test-token")
    auth = {"authorization": "Bearer tier-test-token"}
    with TestClient(app) as client:
        # The probe runs in the background at boot; wait for it to report.
        deadline = time.time() + 10
        while time.time() < deadline:
            st = client.get("/_cache/status", headers=auth).json()
            if (st.get("tier") or {}).get("healthy"):
                break
            time.sleep(0.05)
        r = client.get(f"/{REPO}/resolve/main/{FILENAME}")

    assert r.status_code == 200
    assert r.content == BODY
    assert r.headers.get("x-xhc-cache") == "TIER-HIT", r.headers.get("x-xhc-cache")
    assert _Hub.gets == 0, "the bytes must come from the bucket, not the Hub"
    signed = [h for h in world.requests if h[0] == "GET" and h[1] and "/content/" in h[1]]
    assert signed, "the app read the bucket over HTTP, SigV4-signed (the fake refuses unsigned)"
