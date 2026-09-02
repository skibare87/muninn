"""A full filesystem must degrade the pull, not break it.

UNDER BUDGET IS NOT THE SAME AS HAVING ROOM. Eviction compares this cache's own
size against its own budget and never consults free space, so on a shared
filesystem anything else can fill the volume while this cache sits far under
budget. Before this, every ingest then failed MID-STREAM -- after the client
already had a 2xx -- as a truncated body and a digest mismatch, with the real
cause invisible.

And there is no fallback for the client to take. A node using this cache has had
its image reference rewritten, so the cache IS its registry; `docker pull`
against an unreachable one is `connection refused`, not a silent failover to the
internet. Only XHC_MISS_POLICY=redirect fails open on the HF surface, and the
docker surface never had an equivalent at all.

NOT eviction. The pool has no quota or reservation anywhere and Matt has ruled
that permanent, so freeing space here hands it to whoever is filling the volume:
the cache shrinks, evicts again, and ends small AND still failing, having
destroyed warm data to get there.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import ocistore
from app.config import settings


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "docker_dir", str(tmp_path / "docker"))
    (tmp_path / "docker").mkdir()
    return tmp_path / "docker"


def test_room_when_free_space_is_ample(store, monkeypatch):
    monkeypatch.setattr(settings, "docker_min_free_bytes", 1 << 30)
    monkeypatch.setattr(ocistore, "free_bytes", lambda: 100 << 30)
    assert ocistore.has_ingest_room() is True


def test_no_room_below_the_floor(store, monkeypatch):
    monkeypatch.setattr(settings, "docker_min_free_bytes", 1 << 30)
    monkeypatch.setattr(ocistore, "free_bytes", lambda: 100 << 20)  # 100 MiB
    assert ocistore.has_ingest_room() is False


def test_floor_of_zero_disables_the_check(store, monkeypatch):
    """An operator who wants the old behaviour must be able to have it, and
    must not pay a stat() per miss to get it."""
    monkeypatch.setattr(settings, "docker_min_free_bytes", 0)

    def _boom():
        raise AssertionError("free space must not be read when the floor is 0")

    monkeypatch.setattr(ocistore, "free_bytes", _boom)
    assert ocistore.has_ingest_room() is True


def test_unreadable_filesystem_keeps_caching(store, monkeypatch):
    """A DELIBERATE fail-open, and the opposite of the pin-state rule.

    Pin state fails CLOSED because resolving unknown to permissive risks
    deleting the only copy of something. Here the risk is inverted: resolving
    unknown to "no space" would silently switch every client to uncached on a
    transient stat error, degrading the whole fleet for no reason. The failure
    guarded here is a slowdown, not data loss, so unknown resolves to the
    status quo.
    """
    monkeypatch.setattr(settings, "docker_min_free_bytes", 1 << 30)
    monkeypatch.setattr(ocistore, "free_bytes", lambda: None)
    assert ocistore.has_ingest_room() is True


def test_free_bytes_returns_none_rather_than_zero_when_unreadable(monkeypatch):
    """None and 0 are different claims. Zero would read as "disk full" and stop
    all caching; None means "could not tell" and lets the caller decide."""
    monkeypatch.setattr(settings, "docker_dir", "/definitely/not/a/path/xyzzy")
    assert ocistore.free_bytes() is None


def test_exactly_at_the_floor_still_has_room(store, monkeypatch):
    """Boundary, stated rather than left to the comparison operator."""
    monkeypatch.setattr(settings, "docker_min_free_bytes", 1 << 30)
    monkeypatch.setattr(ocistore, "free_bytes", lambda: 1 << 30)
    assert ocistore.has_ingest_room() is True


# ---------------------------------------------------------------------------
# The route, not the predicate. Testing has_ingest_room() alone proves nothing
# about whether anything calls it -- the same gap that let the pending
# endpoints ship undocumented and the docs test go green.
# ---------------------------------------------------------------------------


from fastapi.testclient import TestClient

DIGEST = "sha256:" + "ab" * 32
BODY = b"layer-bytes-that-must-reach-the-client"


class _FakeStream:
    def __init__(self, status=200, body=BODY):
        self.status_code = status
        self.headers = {"content-length": str(len(body))}
        self._body = body
        self.closed = False

    async def aiter_bytes(self, n):
        yield self._body

    async def aclose(self):
        self.closed = True


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "cache_dir", str(tmp_path / "cache"))
    monkeypatch.setattr(settings, "docker_dir", str(tmp_path / "docker"))
    (tmp_path / "cache").mkdir()
    (tmp_path / "docker").mkdir()
    from app.main import app

    return TestClient(app)


def test_no_room_proxies_the_blob_and_caches_nothing(client, tmp_path, monkeypatch):
    from app import ocicompat, ocistore, registry

    monkeypatch.setattr(ocistore, "has_ingest_room", lambda: False)
    stream = _FakeStream()

    async def _open(ref, path):
        assert path == f"blobs/{DIGEST}"
        return stream

    monkeypatch.setattr(registry, "open_stream", _open)

    def _never(*a, **k):
        raise AssertionError("ingest must not start when there is no room")

    monkeypatch.setattr(ocicompat, "_ensure_blob", _never)

    r = client.get(f"/v2/ghcr.io/org/img/blobs/{DIGEST}")
    assert r.status_code == 200
    assert r.content == BODY, "the client must still get its bytes"
    assert r.headers["x-xhc-cache"] == "BYPASS-NO-SPACE", (
        "an operator must be able to tell 'cache is cold' from 'cache cannot "
        "write' -- they look identical from the client otherwise"
    )
    assert stream.closed, "the upstream response must be released"
    blobs = list((tmp_path / "docker").rglob("*.incomplete"))
    assert not blobs, "nothing may be written when the store has no room"


def test_room_available_takes_the_normal_ingest_path(client, monkeypatch):
    """The negative control. Without it, a bypass that fired ALWAYS would pass
    every test above -- a check with no state in which it takes the other
    branch."""
    from app import ocicompat, ocistore

    monkeypatch.setattr(ocistore, "has_ingest_room", lambda: True)
    called = {}

    async def _ensure(ref, digest):
        called["yes"] = True
        raise ocicompat.UpstreamError(404, "not found")

    monkeypatch.setattr(ocicompat, "_ensure_blob", _ensure)
    client.get(f"/v2/ghcr.io/org/img/blobs/{DIGEST}")
    assert called.get("yes"), "the normal ingest path must still be reachable"
