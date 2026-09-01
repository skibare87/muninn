"""X-Muninn-Prewarm and X-Muninn-Local-Only: request-scoped handling on the
ordinary resolve path.

Both are opt-in headers rather than query parameters. A query string changes the
URL, and the URL is the cache key the client and every proxy between us agree
on; these two ask for different HANDLING of the same resource.

The property that matters for local-only is NEGATIVE and cannot be seen in a
response body: it must never reach upstream. There are three upstream calls on
the paths it can take -- ref revalidation, the etag backfill on a hit, and the
metadata fetch on a miss -- and a test that only checked the 404 would pass while
the header quietly made a Hub request on the other two.

So these tests assert on a fetch_metadata / is_stale that RAISES if called.

The coroutines are driven with asyncio.run rather than pytest-asyncio. The first
version of this file used bare `async def` tests, and pytest SKIPPED them with a
warning -- eight assertions that never executed and a green run. A test that is
silently not run is worse than a missing one, because the suite reports coverage
it does not have.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("HF_HUB_CACHE", "/tmp/xhc-test-cache")

from starlette.datastructures import Headers
from starlette.requests import Request

from app import hfcompat


def _req(headers: dict) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/x",
        "headers": Headers(headers).raw,
        "query_string": b"",
    }
    return Request(scope)


# -- the flag parser ---------------------------------------------------------


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " 1 "])
def test_flag_accepts_the_usual_affirmatives(value):
    assert hfcompat._flag(_req({"x-muninn-prewarm": value}), "x-muninn-prewarm")


@pytest.mark.parametrize("value", ["0", "false", "no", "", "maybe", "2"])
def test_flag_rejects_everything_else(value):
    """Unrecognised must mean OFF. A header that silently means 'on' for any
    non-empty value turns a typo into a behaviour change."""
    assert not hfcompat._flag(_req({"x-muninn-prewarm": value}), "x-muninn-prewarm")


def test_flag_absent_is_off():
    assert not hfcompat._flag(_req({}), "x-muninn-prewarm")
    assert not hfcompat._flag(_req({}), "x-muninn-local-only")


def test_flags_are_independent():
    r = _req({"x-muninn-local-only": "1"})
    assert hfcompat._flag(r, "x-muninn-local-only")
    assert not hfcompat._flag(r, "x-muninn-prewarm")


# -- local-only must not reach upstream, on ANY path -------------------------


@pytest.fixture
def no_upstream(monkeypatch):
    """Any upstream call is a test failure, not a slow test."""

    async def boom(*a, **k):
        raise AssertionError("local-only reached upstream")

    monkeypatch.setattr(hfcompat, "fetch_metadata", boom)
    monkeypatch.setattr(hfcompat.refs, "is_stale", boom)
    return boom


def test_local_only_miss_returns_404_without_upstream(monkeypatch, no_upstream):
    monkeypatch.setattr(hfcompat.cachefs, "resolve_local", lambda *a, **k: None)
    monkeypatch.setattr(hfcompat.policy, "load", lambda: {})
    monkeypatch.setattr(
        hfcompat.policy, "check", lambda *a, **k: type("D", (), {"allowed": True, "reason": ""})()
    )
    monkeypatch.setattr(hfcompat.policy, "enforced_on_hits", lambda *a, **k: False)

    resp = asyncio.run(
        hfcompat.serve_file(
            "model", "org/repo", "main", "f.bin", _req({"x-muninn-local-only": "1"})
        )
    )
    assert resp.status_code == 404
    assert resp.headers["x-xhc-cache"] == "MISS-LOCAL"
    assert resp.headers["cache-control"] == "no-store"


def test_local_only_and_prewarm_together_is_a_400(monkeypatch, no_upstream):
    """Contradiction, not a precedence question: prewarm needs upstream metadata
    and local-only forbids it."""
    from fastapi import HTTPException

    monkeypatch.setattr(hfcompat.cachefs, "resolve_local", lambda *a, **k: None)
    monkeypatch.setattr(hfcompat.policy, "load", lambda: {})
    monkeypatch.setattr(
        hfcompat.policy, "check", lambda *a, **k: type("D", (), {"allowed": True, "reason": ""})()
    )
    monkeypatch.setattr(hfcompat.policy, "enforced_on_hits", lambda *a, **k: False)

    with pytest.raises(HTTPException) as ei:
        asyncio.run(
            hfcompat.serve_file(
                "model",
                "org/repo",
                "main",
                "f.bin",
                _req({"x-muninn-local-only": "1", "x-muninn-prewarm": "1"}),
            )
        )
    assert ei.value.status_code == 400
    assert "mutually exclusive" in ei.value.detail
