"""With no XHC_MANAGE_TOKEN, the whole /_cache management surface is OFF.

THE DECISION. "If there is no token set it is not usable." Before this, an
unset token meant OPEN for every /_cache route except /_cache/authz: status,
repos, prewarm, pins, policy, eviction, GC, orphan deletion and the docker
management routes all served anyone who could reach the port.

THE SHAPE OF THE FIX, and what these tests pin:

  - one gate, applied by the ROUTER (its route class), so a route added to a
    /_cache router later is gated without anyone remembering to ask for it;
  - unset or blank token -> 404 naming XHC_MANAGE_TOKEN, answered locally;
  - token set, header missing or wrong -> 401;
  - no handler runs on a refusal, and nothing reaches the upstream.

The route list is ENUMERATED FROM THE REAL APP, never written out by hand: a
hand list is a copy of the route table, and it goes stale exactly when a route
is added -- which is the case this exists for.
"""

from __future__ import annotations

import importlib
import logging
import re
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TOKEN = "manage-gate-test-token"
DISABLED = "XHC_MANAGE_TOKEN is unset"


class _NoUpstream:
    """Every outbound request from any httpx client lands here and FAILS the
    test, so "nothing was forwarded" is proved by the transport rather than
    inferred from a status that a relayed upstream 404 would also satisfy."""

    def __init__(self):
        self.calls: list[str] = []

    async def send(self, client, request, **kwargs):
        self.calls.append(f"{request.method} {request.url}")
        raise AssertionError(f"upstream was called: {request.method} {request.url}")


def _import_real_app(monkeypatch, tmp_path, **overrides):
    """app.main, re-imported under these settings with docker and authz ON, so
    every /_cache router is mounted and enumerable."""
    from app import dockerauth
    from app.config import settings

    (tmp_path / "cache").mkdir(exist_ok=True)
    (tmp_path / "oci").mkdir(exist_ok=True)
    base = {
        "cache_dir": str(tmp_path / "cache"),
        "docker_dir": str(tmp_path / "oci"),
        "docker_enabled": True,
        "authz_db": str(tmp_path / "authz.db"),
        "web_root": None,
        "metrics_auth": "none",
    }
    base.update(overrides)
    for name, value in base.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(dockerauth, "_store", None)
    upstream = _NoUpstream()
    monkeypatch.setattr(httpx.AsyncClient, "send",
                        lambda self, req, **kw: upstream.send(self, req, **kw))
    import app.main as main

    return importlib.reload(main), upstream


@pytest.fixture
def real_app(monkeypatch, tmp_path):
    def _b(**kw):
        return _import_real_app(monkeypatch, tmp_path, **kw)

    yield _b
    monkeypatch.undo()
    import app.main as main

    importlib.reload(main)


def _cache_routes(app):
    """(method, concrete path, route) for every /_cache route in the app."""
    from fastapi.routing import APIRoute

    out = []
    for route in app.routes:
        path = getattr(route, "path", "")
        if not (path == "/_cache" or path.startswith("/_cache/")):
            continue
        # A non-APIRoute under /_cache (a Mount, a bare Starlette Route) would
        # not carry the route-class gate. Refuse it by name rather than let the
        # loop below skip it.
        assert isinstance(route, APIRoute), f"{path} is a {type(route).__name__}"
        concrete = re.sub(r"\{[^}]+\}", "x", path)
        for method in sorted(route.methods):
            out.append((method, concrete, route))
    return out


def _stub_handlers(app):
    """Replace every /_cache endpoint with a recorder. FastAPI calls
    `dependant.call` at request time, so this reaches the live handler."""
    ran: list[str] = []
    for _method, _path, route in _cache_routes(app):
        async def _stub(__p=route.path, **_kw):
            ran.append(__p)
            return {"stub": True}

        route.dependant.call = _stub
    return ran


# The enumeration is only evidence if it finds the routes it must: a filter
# that matched nothing would make every test below pass vacuously.
_KNOWN = {
    ("GET", "/_cache/status"), ("POST", "/_cache/prewarm"), ("GET", "/_cache/jobs"),
    ("GET", "/_cache/repos"), ("PUT", "/_cache/policy"), ("DELETE", "/_cache/orphans"),
    ("POST", "/_cache/docker/gc"), ("GET", "/_cache/docker/pins"),
    ("GET", "/_cache/authz/principals"), ("POST", "/_cache/authz/principals/x/keys"),
}


def _all_cache_routes_of_the_real_app():
    """Collected at import with docker and authz forced on for the duration.
    Only the (method, path) pairs are kept; each test re-imports the app."""
    import app.main as main
    from app.config import settings

    saved = (settings.docker_enabled, settings.authz_db)
    settings.docker_enabled, settings.authz_db = True, "/nonexistent/authz.db"
    try:
        m = importlib.reload(main)
        pairs = sorted({(meth, p) for meth, p, _ in _cache_routes(m.app)})
    finally:
        settings.docker_enabled, settings.authz_db = saved
        importlib.reload(main)
    return pairs


ROUTES = _all_cache_routes_of_the_real_app()


def test_the_enumeration_finds_every_cache_router():
    missing = _KNOWN - set(ROUTES)
    assert not missing, f"enumeration missed {missing}"
    assert len(ROUTES) >= 30, len(ROUTES)


@pytest.mark.parametrize("unset", [None, "", "   "])
@pytest.mark.parametrize("method,path", ROUTES)
def test_no_token_every_cache_route_is_a_local_404_naming_the_setting(
    real_app, method, path, unset
):
    """THE DECISION, over the real route table."""
    from fastapi.testclient import TestClient

    main, upstream = real_app(manage_token=unset)
    ran = _stub_handlers(main.app)
    client = TestClient(main.app, raise_server_exceptions=False)
    for headers in ({}, {"authorization": "Bearer "}, {"authorization": f"Bearer {unset}"},
                    {"authorization": "Bearer None"}):
        r = client.request(method, path, headers=headers, json={})
        assert r.status_code == 404, (method, path, headers, r.status_code, r.text[:200])
        assert DISABLED in r.text, r.text
        assert r.headers["content-type"].startswith("text/plain")
    assert ran == [], f"handler ran with the surface off: {ran}"
    assert upstream.calls == []


@pytest.mark.parametrize("method,path", ROUTES)
def test_with_a_token_missing_or_wrong_is_401_and_right_passes(real_app, method, path):
    from fastapi.testclient import TestClient

    main, upstream = real_app(manage_token=TOKEN)
    ran = _stub_handlers(main.app)
    client = TestClient(main.app, raise_server_exceptions=False)
    for presented in (None, "", "Bearer ", "Bearer wrong", TOKEN, f"Bearer {TOKEN}x",
                      f"Basic {TOKEN}"):
        headers = {} if presented is None else {"authorization": presented}
        r = client.request(method, path, headers=headers, json={})
        assert r.status_code == 401, (method, path, presented, r.status_code, r.text[:200])
    assert ran == []
    r = client.request(method, path, headers={"authorization": f"Bearer {TOKEN}"}, json={})
    # The stub stands in for the handler, so a 2xx means "the gate let it
    # through". A route whose body model rejects {} answers 422 instead, and
    # that is equally past the gate: the gate runs BEFORE the body is parsed
    # (test_a_malformed_body_does_not_get_past_the_gate), so a 422 is only
    # reachable with the right token.
    passed = 200 <= r.status_code < 300
    assert passed or r.status_code == 422, (method, path, r.status_code, r.text[:200])
    assert len(ran) == (1 if passed else 0)
    assert upstream.calls == []


def test_a_malformed_body_does_not_get_past_the_gate(real_app):
    """The gate runs before FastAPI parses the body: a request that would be a
    422 is still a 404 (off) or 401 (bad token), never a 422 that tells an
    anonymous caller the route exists."""
    from fastapi.testclient import TestClient

    for token, expected in ((None, 404), (TOKEN, 401)):
        main, _ = real_app(manage_token=token)
        client = TestClient(main.app, raise_server_exceptions=False)
        for path in ("/_cache/prewarm", "/_cache/docker/prewarm", "/_cache/authz/principals"):
            r = client.post(path, content=b"{not json",
                            headers={"content-type": "application/json"})
            assert r.status_code == expected, (token, path, r.status_code, r.text)


@pytest.mark.parametrize("module", ["manage", "ocimanage", "authzmanage"])
def test_a_route_added_to_a_cache_router_later_is_gated_automatically(
    monkeypatch, tmp_path, module
):
    """The router carries the gate, not the route. This adds a route with NO
    dependencies of its own and shows it refused."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app import dockerauth
    from app.config import settings

    mod = importlib.import_module(f"app.{module}")
    monkeypatch.setattr(settings, "authz_db", str(tmp_path / "authz.db"))
    monkeypatch.setattr(dockerauth, "_store", None)
    ran = []

    async def dummy():
        ran.append(1)
        return {"dummy": True}

    before = list(mod.router.routes)
    mod.router.add_api_route("/added-later-dummy", dummy, methods=["GET"])
    try:
        app = FastAPI()
        app.include_router(mod.router)
    finally:
        mod.router.routes[:] = before
    client = TestClient(app, raise_server_exceptions=False)
    path = mod.router.prefix + "/added-later-dummy"

    for unset in (None, "", "  "):
        monkeypatch.setattr(settings, "manage_token", unset)
        r = client.get(path)
        assert r.status_code == 404 and DISABLED in r.text, (r.status_code, r.text)

    monkeypatch.setattr(settings, "manage_token", TOKEN)
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"authorization": "Bearer nope"}).status_code == 401
    assert ran == []
    r = client.get(path, headers={"authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200 and r.json() == {"dummy": True}
    assert ran == [1]


@pytest.mark.parametrize("path", ["/_cache", "/_cache/", "/_cache/no-such-endpoint",
                                  "/_cache/status/extra"])
def test_unrouted_cache_paths_stay_local_and_name_the_setting_when_off(real_app, path):
    """Sub-paths no router defines reach the HF catch-all; /_cache is reserved
    there, so they are answered locally -- and with the surface off, the answer
    names the setting rather than calling it a typo."""
    from fastapi.testclient import TestClient

    main, upstream = real_app(manage_token=None)
    client = TestClient(main.app, raise_server_exceptions=False)
    for method in ("GET", "POST", "PATCH"):
        r = client.request(method, path)
        assert r.status_code == 404, (method, path, r.status_code)
        assert DISABLED in r.text, r.text
    assert upstream.calls == []


def test_healthz_and_metrics_do_not_depend_on_the_management_token(real_app):
    from fastapi.testclient import TestClient

    for token in (None, TOKEN):
        main, upstream = real_app(manage_token=token)
        client = TestClient(main.app, raise_server_exceptions=False)
        h = client.get("/healthz")
        assert h.status_code == 200 and h.json()["ok"] is True, h.text
        m = client.get("/metrics")
        assert m.status_code == 200 and "muninn_cache_bytes" in m.text, m.status_code
        assert upstream.calls == []


def test_metrics_token_mode_is_unchanged(real_app):
    """XHC_METRICS_AUTH=token keeps its own behaviour: 401 without the token,
    200 with it. Pinned so the /_cache change cannot drift into it."""
    from fastapi.testclient import TestClient

    main, _ = real_app(manage_token=TOKEN, metrics_auth="token")
    client = TestClient(main.app, raise_server_exceptions=False)
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers={"authorization": f"Bearer {TOKEN}"}).status_code == 200


def test_startup_warns_once_that_management_is_off(real_app, caplog, monkeypatch):
    from fastapi.testclient import TestClient

    main, _ = real_app(manage_token=None)
    with caplog.at_level(logging.WARNING), TestClient(main.app):
        pass
    lines = [r.getMessage() for r in caplog.records
             if r.levelno == logging.WARNING and "management API is disabled" in r.getMessage()]
    assert len(lines) == 1, lines
    assert "XHC_MANAGE_TOKEN" in lines[0]

    caplog.clear()
    main, _ = real_app(manage_token=TOKEN)
    with caplog.at_level(logging.WARNING), TestClient(main.app):
        pass
    assert not [r for r in caplog.records if "management API is disabled" in r.getMessage()]
