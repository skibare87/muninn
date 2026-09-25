"""A path Muninn owns never reaches the Hub, whatever the feature flags say.

THE DEFECT. Each Muninn surface is a router mounted before the HF catch-all.
Switch a surface off and its router is not mounted, so its paths fall through to
`/{full_path:path}` and are proxied to the upstream. A deployment with
XHC_DOCKER_ENABLED=0 and XHC_DOCS=0 answered `/v2/` and `/docs` with the Hub's
401 and HTML, carrying the Hub's headers. The operator switched the surface off;
the cache answered with somebody else's server.

"Never forwarded" is proved by a transport that FAILS THE TEST if it is used,
not by the status alone: a 404 relayed from the upstream would satisfy a status
assertion and be exactly the bug.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class _Upstream:
    """Stands in for httpx.AsyncClient.send: every outbound request, from any
    client in the process, lands here."""

    def __init__(self, allow: bool):
        self.allow = allow
        self.calls: list[str] = []

    async def send(self, client, request, **kwargs):
        self.calls.append(str(request.url))
        if not self.allow:
            raise AssertionError(f"upstream was called: {request.method} {request.url}")
        return httpx.Response(
            200,
            headers={"content-type": "application/json", "x-from": "upstream"},
            content=b'{"from": "upstream"}',
            request=request,
        )


def _build(monkeypatch, tmp_path, allow_upstream=False, **overrides):
    """Import app.main afresh under the given settings.

    Routers are mounted at import time from settings, so a surface's
    enabled/disabled state can only be exercised by re-importing. The fixture
    teardown re-imports once more under the original settings, so no other test
    module inherits this app.
    """
    from fastapi.testclient import TestClient

    from app import hfcompat
    from app.config import settings

    (tmp_path / "cache").mkdir(exist_ok=True)
    monkeypatch.setattr(settings, "cache_dir", str(tmp_path / "cache"))
    for name, value in overrides.items():
        monkeypatch.setattr(settings, name, value)
    upstream = _Upstream(allow_upstream)
    monkeypatch.setattr(httpx.AsyncClient, "send",
                        lambda self, req, **kw: upstream.send(self, req, **kw))
    monkeypatch.setattr(hfcompat, "_client", None, raising=False)
    import app.main as main

    main = importlib.reload(main)
    return TestClient(main.app, raise_server_exceptions=False), upstream


@pytest.fixture
def build(monkeypatch, tmp_path):
    def _b(**kw):
        return _build(monkeypatch, tmp_path, **kw)

    yield _b
    monkeypatch.undo()
    import app.main as main

    importlib.reload(main)


def _assert_local_404(r, upstream, setting: str):
    assert upstream.calls == [], f"forwarded upstream: {upstream.calls}"
    assert r.status_code == 404, (r.status_code, r.text[:200])
    assert setting in r.text, r.text
    assert "x-from" not in r.headers
    assert r.headers["content-type"].startswith("text/plain")


# ---------------- disabled surfaces answer locally ----------------


@pytest.mark.parametrize("path", ["/v2/", "/v2", "/v2/library/alpine/manifests/latest",
                                  "/v2/library/alpine/blobs/sha256:" + "0" * 64])
def test_v2_with_docker_disabled_is_a_local_404(build, path):
    """The reported defect. Fails against the pre-fix code: /v2/ was proxied."""
    client, upstream = build(docker_enabled=False)
    r = client.get(path)
    _assert_local_404(r, upstream, "XHC_DOCKER_ENABLED=0")
    assert "OCI registry" in r.text


def test_v2_push_methods_with_docker_disabled_stay_local(build):
    client, upstream = build(docker_enabled=False)
    for method in ("POST", "PUT", "PATCH", "DELETE", "HEAD"):
        r = client.request(method, "/v2/library/alpine/blobs/uploads/")
        assert upstream.calls == [], (method, upstream.calls)
        assert r.status_code == 404, (method, r.status_code)


def test_docker_management_with_docker_disabled_is_a_local_404(build):
    """/_cache/docker/* belongs to a router mounted only with docker on. The
    /_cache router is always mounted but has no route there."""
    client, upstream = build(docker_enabled=False)
    r = client.get("/_cache/docker/images")
    _assert_local_404(r, upstream, "XHC_DOCKER_ENABLED=0")


@pytest.mark.parametrize("path", ["/docs", "/docs/", "/redoc", "/openapi.json",
                                  "/docs/oauth2-redirect"])
def test_docs_disabled_is_a_local_404(build, path):
    client, upstream = build(docs_enabled=False)
    r = client.get(path)
    _assert_local_404(r, upstream, "XHC_DOCS=0")


@pytest.mark.parametrize("path", ["/_auth/login", "/_auth/me", "/_console/keys"])
def test_login_and_console_disabled_are_a_local_404(build, path):
    client, upstream = build(oidc_issuer=None)
    r = client.get(path)
    _assert_local_404(r, upstream, "XHC_OIDC_ISSUER")


def test_datasets_server_disabled_is_a_local_404(build):
    """XHC_DATASETS_SERVER= (empty) switches the proxy off. Its prefix is
    Muninn's invention; the Hub has nothing meaningful to say about it."""
    client, upstream = build(datasets_server="")
    r = client.get("/datasets-server/splits?dataset=org/ds")
    _assert_local_404(r, upstream, "XHC_DATASETS_SERVER")


# ---------------- enabled surfaces: unmatched methods and sub-paths ----------------


@pytest.mark.parametrize("method,path", [
    ("POST", "/healthz"),
    ("GET", "/healthz/"),
    ("POST", "/metrics"),
    ("GET", "/_cache/no-such-endpoint"),
    ("PATCH", "/_cache/status"),
    ("POST", "/datasets-server/splits"),
    ("GET", "/datasets-server"),
])
def test_unmatched_muninn_paths_never_reach_the_hub(build, method, path):
    """An always-on surface has the same hole: a method its router does not
    define reaches the catch-all, which accepts every method. The management
    token is set: without one, /_cache is OFF and its unrouted paths name the
    setting instead (tests/test_manage_gate.py)."""
    client, upstream = build(manage_token="reserved-test-token")
    r = client.request(method, path)
    assert upstream.calls == [], upstream.calls
    assert r.status_code == 404, (r.status_code, r.text[:200])
    assert "no such Muninn endpoint" in r.text


def test_dot_segments_cannot_smuggle_a_reserved_path():
    """httpx normalises `api/../v2/` to `/v2/` when it builds the upstream URL,
    so the check must match the normalised path, not the raw string."""
    from app.hfcompat import _reserved_path

    assert _reserved_path("api/../v2/").path == "v2"
    assert _reserved_path("//v2/x").path == "v2"
    assert _reserved_path("a/b/../../docs").path == "docs"
    assert _reserved_path("gpt2/resolve/main/config.json") is None


# ---------------- HF paths: the positive control ----------------


@pytest.mark.parametrize("path", [
    "/api/whoami-v2",
    "/api/spaces",
    # Deeper than an exact reservation: /docs is a single endpoint, and a Hub
    # path underneath it is not Muninn's. The next two share only a string
    # prefix with a reserved path, not a path segment.
    "/docs/hub/index",
    "/metrics-org/model/raw/main/README.md",
    "/v2x/model/raw/main/README.md",
])
def test_hf_paths_are_still_proxied(build, path):
    """THE POSITIVE CONTROL. Without it, a check that refused everything would
    pass every test above."""
    client, upstream = build(docker_enabled=False, docs_enabled=False,
                             allow_upstream=True)
    r = client.get(path)
    assert r.status_code == 200, (r.status_code, r.text[:200])
    assert r.headers.get("x-from") == "upstream"
    assert len(upstream.calls) == 1 and upstream.calls[0].endswith(path)


def test_datasets_server_enabled_still_proxies(build):
    client, upstream = build(datasets_server="https://ds.example", allow_upstream=True)
    r = client.get("/datasets-server/is-valid?dataset=org/ds")
    assert r.status_code == 200, r.text
    assert upstream.calls and upstream.calls[0].startswith("https://ds.example/is-valid")


# ---------------- enabled surfaces are unchanged ----------------


def test_enabled_surfaces_behave_as_before(build):
    client, upstream = build(docker_enabled=True, docs_enabled=True, docker_auth="none",
                             manage_token="reserved-test-token")
    r = client.get("/v2/")
    assert r.status_code == 200
    assert r.headers.get("docker-distribution-api-version") == "registry/2.0"
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").json()["info"]["title"] == "muninn"
    assert client.get("/healthz").json()["ok"] is True
    assert client.get("/metrics").status_code == 200
    status = client.get("/_cache/status")  # answered by its own router
    assert status.status_code == 401 and "Muninn endpoint" not in status.text
    status = client.get("/_cache/status",
                        headers={"authorization": "Bearer reserved-test-token"})
    assert status.status_code == 200, status.text
    assert upstream.calls == []


# ---------------- ordering against the web root and the credential gate ----------------


def test_web_root_can_still_serve_a_reserved_path_that_is_switched_off(build, tmp_path):
    """The web root runs first: an operator's own static page at /docs is a
    local answer they chose. Only the upstream is ruled out."""
    www = tmp_path / "www"
    (www / "docs").mkdir(parents=True)
    (www / "docs" / "index.html").write_text("<h1>our docs</h1>")
    client, upstream = build(docs_enabled=False, web_root=str(www))
    r = client.get("/docs")
    assert r.status_code == 200 and "our docs" in r.text
    assert upstream.calls == []


def test_reserved_paths_answer_before_the_hf_credential_gate(build, tmp_path, monkeypatch):
    """With XHC_HF_AUTH=key an anonymous /v2/ on a docker-off cache gets the
    404 naming the setting, not the HF surface's Basic challenge -- that would
    send a docker client off to log in to a registry that is not there. The
    gate still covers every HF path."""
    from app import dockerauth
    from app.authzstore import AuthzStore

    db = tmp_path / "authz.db"
    AuthzStore(db)
    monkeypatch.setattr(dockerauth, "_store", None)
    client, upstream = build(docker_enabled=False, hf_auth="key", authz_db=str(db))
    _assert_local_404(client.get("/v2/"), upstream, "XHC_DOCKER_ENABLED=0")
    r = client.get("/api/models/gpt2")
    assert r.status_code == 401
    assert upstream.calls == []
