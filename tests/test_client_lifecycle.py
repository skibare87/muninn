"""Long-lived outbound HTTP clients across app lifespans and event loops.

An httpx.AsyncClient's pooled connections belong to the event loop that opened
them. uvicorn runs one loop per process, so in production a client made on
first use and closed at shutdown is fine. A process that runs the lifespan more
than once -- this test suite, or anything embedding the app -- runs each
startup/shutdown on a NEW loop, and a client carried over from the previous one
is unusable there and cannot be closed from there either.

These tests use the real lifespan against real local HTTP servers, so every
client really holds a pooled keep-alive connection when shutdown runs. A
MockTransport has no connections and so nothing loop-bound to trip over.
"""

from __future__ import annotations

import asyncio
import http.server
import sys
import threading
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tierfake import FakeS3

from app import hfcompat, orphans, refs, tier, webauth
from app import registry as ociregistry
from app.config import Settings, settings


class _Upstream(http.server.BaseHTTPRequestHandler):
    # HTTP/1.1 with a content-length, so httpx keeps the connection in its pool
    # and shutdown has something loop-bound to close.
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        body = b"{}"
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        return


@pytest.fixture
def world(tmp_path, monkeypatch):
    up = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    fake = FakeS3("bkt")
    store_url, store = fake.serve()

    for d in ("cache", "docker"):
        (tmp_path / d).mkdir()
    monkeypatch.setattr(settings, "cache_dir", str(tmp_path / "cache"))
    monkeypatch.setattr(settings, "docker_dir", str(tmp_path / "docker"))
    monkeypatch.setattr(settings, "state_dir", None)
    monkeypatch.setattr(settings, "docker_enabled", True)
    monkeypatch.setattr(settings, "docker_push_enabled", False)
    monkeypatch.setattr(settings, "orphan_check_interval_s", 0.0)
    monkeypatch.setattr(settings, "jwt_issuers", [])
    monkeypatch.setattr(settings, "oidc_issuer", "https://idp.invalid")
    monkeypatch.setattr(settings, "oidc_client_id", "cid")
    monkeypatch.setattr(settings, "oidc_client_secret", "csec")
    monkeypatch.setattr(settings, "oidc_redirect_uri", "https://muninn.invalid/_auth/callback")
    monkeypatch.setattr(webauth, "_client", None)
    for k, v in {
        "XHC_TIER2": "s3://bkt/muninn",
        "XHC_TIER2_ENDPOINT": store_url,
        "XHC_TIER2_ACCESS_KEY_ID": "id",
        "XHC_TIER2_SECRET_ACCESS_KEY": "secret",
        "XHC_TIER2_RECONCILE_INTERVAL": "0",
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(settings, "tier", Settings.from_env().tier)
    tier.reset_for_tests()
    fake.seed("muninn/probe-object", b"x")
    try:
        yield f"http://127.0.0.1:{up.server_address[1]}/"
    finally:
        tier.reset_for_tests()
        for srv in (up, store):
            srv.shutdown()
            srv.server_close()


async def _use_every_client(url: str, *, in_lifespan: bool = True) -> list:
    """One real request through each long-lived client, leaving a pooled
    connection in each. Returns the client objects used."""
    used = [
        hfcompat.get_client(),
        ociregistry._get_client(),
        refs._get_client(),
        orphans._get_client(),
        await webauth.client()._client(),
    ]
    for c in used:
        r = await c.get(url)
        assert r.status_code == 200
    if not in_lifespan:
        # The tier's client is built only by tier.start(), in the lifespan.
        assert tier._s.client is None
        return used
    # The tier's client, through the S3 client that wraps it.
    r = await tier._s.client.get_bytes("muninn/probe-object")
    assert r.status_code == 200, r.status_code
    used.append(tier._s.http)
    # The tier's upload workers wait on a queue; one bound to an earlier loop
    # kills them on their first get(), and stop() swallows the exception.
    await asyncio.sleep(0.05)
    dead = [t for t in tier._s.tasks if t.done()]
    assert not dead, [t.exception() for t in dead if not t.cancelled()]
    return used


async def _cycle(url: str) -> list:
    from app.main import app, lifespan

    async with lifespan(app):
        used = await _use_every_client(url)
    return used


def _on_new_loop(coro):
    """Run on a brand-new loop, failing on anything the loop's exception
    handler sees (an unretrieved task exception, a close that went wrong).
    Bounded: a request sent on a connection from a dead loop can wait forever,
    and that is a failure, not a hang."""
    coro = asyncio.wait_for(coro, timeout=20)
    loop = asyncio.new_event_loop()
    errors: list = []
    loop.set_exception_handler(lambda _l, ctx: errors.append(ctx))
    try:
        result = loop.run_until_complete(coro)
        loop.run_until_complete(loop.shutdown_asyncgens())
    finally:
        loop.close()
    assert not errors, errors
    return result


def test_startup_and_shutdown_twice_on_two_loops(world):
    first = _on_new_loop(_cycle(world))
    second = _on_new_loop(_cycle(world))
    # Each lifespan built its own clients; nothing crossed loops.
    assert not {id(c) for c in first} & {id(c) for c in second}


def test_every_client_used_in_a_lifespan_is_closed_at_shutdown(world, monkeypatch):
    # Every AsyncClient constructed anywhere during the lifespan, recorded at
    # the constructor -- independent of any registry the app keeps, so a client
    # the app does not know it holds is caught too.
    built: list[httpx.AsyncClient] = []
    real_init = httpx.AsyncClient.__init__

    def recording_init(self, *a, **kw):
        real_init(self, *a, **kw)
        built.append(self)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", recording_init)
    used = _on_new_loop(_cycle(world))

    assert len(used) == 6
    assert {id(c) for c in used} <= {id(c) for c in built}
    still_open = [c for c in built if not c.is_closed]
    assert not still_open, still_open


def test_a_client_first_used_outside_any_lifespan_does_not_break_the_next(world):
    """What a request-level test does: route handlers run with no lifespan, so
    the clients they touch are created on that test's loop and left behind.

    Those stray clients are DROPPED unclosed by the next lifespan (closing them
    would need their loop, which is gone), so this test emits one ResourceWarning
    per client under -W default. That is the stated trade, and it cannot arise
    under uvicorn, which has one loop.
    """

    async def stray():
        await _use_every_client(world, in_lifespan=False)

    _on_new_loop(stray())
    _on_new_loop(_cycle(world))
