"""Push-through: accept a docker push, land it, forward it upstream (an internal issue).

The tests that carry weight here are the ones about LYING and LOSING:

  - a client must never be told 201 for content whose digest does not match
  - `proxy` must not answer 201 until upstream really has it
  - `store-forward` must pin what it has accepted, because until the upstream
    push completes it is THE ONLY COPY and cannot be re-fetched
  - the auth retry must not send an empty body

That last one is the subtle one. `_authed_request` may send twice, and a
consumed file handle on the second attempt uploads NOTHING -- which a registry
accepts, leaving a blob that exists and is wrong. The body is a factory for
exactly that reason, and there is a test that a second call yields the same
bytes.
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import ocipush, pushlimits, registry
from app.config import settings


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "docker_dir", str(tmp_path))
    monkeypatch.setattr(settings, "docker_push_enabled", True)
    monkeypatch.setattr(settings, "docker_push_mode", "proxy")
    monkeypatch.setattr(settings, "docker_cache_on_push", True)
    monkeypatch.setattr(settings, "docker_push_limits", None)
    monkeypatch.setattr(settings, "docker_blob_chunk", 0)
    pushlimits.reset()
    ocipush._sessions.clear()
    ocipush._pinned.clear()
    ocipush._pending.clear()
    yield
    pushlimits.reset()


def _ref(upstream="r.example.com"):
    return registry.Ref(upstream=upstream, api=f"https://{upstream}", repo="team/img")


def test_digest_is_computed_as_bytes_arrive():
    up = ocipush.begin(_ref())
    ocipush.append(up, b"hello ")
    ocipush.append(up, b"world")
    assert up.offset == 11
    assert up.computed == "sha256:" + hashlib.sha256(b"hello world").hexdigest()


def test_a_mismatched_digest_is_refused_and_the_upload_discarded():
    """Never tell a client 201 for content that is not what they claimed."""
    import asyncio

    up = ocipush.begin(_ref())
    ocipush.append(up, b"payload")
    with pytest.raises(ocipush.PushError) as e:
        asyncio.run(ocipush.finalise_blob(up, "sha256:" + "0" * 64))
    assert e.value.status == 400 and e.value.code == "DIGEST_INVALID"
    assert not up.path.exists(), "rejected content left on disk"
    assert up.uuid not in ocipush._sessions


def test_an_unknown_session_is_a_clean_404():
    with pytest.raises(ocipush.PushError) as e:
        ocipush.get("no-such-uuid")
    assert e.value.status == 404 and e.value.code == "BLOB_UPLOAD_UNKNOWN"


# --- the body factory ------------------------------------------------------

async def _drain(agen):
    return b"".join([chunk async for chunk in agen])


def test_the_body_factory_yields_the_same_bytes_twice(tmp_path):
    """The auth dance re-sends. A consumed handle would upload nothing, and a
    registry would ACCEPT that -- a blob that exists and is wrong."""
    import asyncio

    blob = tmp_path / "b"
    blob.write_bytes(b"A" * 4096)
    factory = ocipush._chunk_reader(blob, 0, 4096)
    first = asyncio.run(_drain(factory()))
    second = asyncio.run(_drain(factory()))
    assert first == second == b"A" * 4096


def test_the_body_is_an_ASYNC_iterator(tmp_path):
    """httpx's AsyncClient refuses a sync iterable as a streaming body.

    The first version of this factory was a sync generator. Every unit test
    passed -- they consumed it directly -- and the real client raised
    "Attempted to send an sync request with an AsyncClient instance" on the
    first end-to-end push. Assert the property the client actually requires.
    """
    blob = tmp_path / "b"
    blob.write_bytes(b"A" * 16)
    body = ocipush._chunk_reader(blob, 0, 16)()
    assert hasattr(body, "__aiter__"), "httpx will refuse this body"
    assert not hasattr(body, "__next__")


def test_the_body_factory_respects_its_window(tmp_path):
    """Chunked upload reads a range, not the whole file."""
    import asyncio

    blob = tmp_path / "b"
    blob.write_bytes(bytes(range(256)) * 16)
    got = asyncio.run(_drain(ocipush._chunk_reader(blob, 100, 50)()))
    assert len(got) == 50
    assert got == blob.read_bytes()[100:150]


# --- store-forward pins what it has accepted -------------------------------

def test_store_forward_pins_until_confirmed(monkeypatch):
    """Until the upstream push completes this is the only copy in existence."""
    import asyncio

    monkeypatch.setattr(settings, "docker_push_mode", "store-forward")
    started = asyncio.Event()

    async def never_finishes(ref, path, digest):
        started.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(ocipush, "push_blob", never_finishes)

    async def go():
        up = ocipush.begin(_ref())
        ocipush.append(up, b"payload")
        await ocipush.finalise_blob(up, up.computed)
        await asyncio.wait_for(started.wait(), 5)
        # Asserted INSIDE the loop: asyncio.run cancels pending tasks on exit,
        # and checking afterwards would measure shutdown rather than the
        # in-flight state this test is about.
        return ocipush.is_pinned("r.example.com", up.computed)

    assert asyncio.run(go()), "an unconfirmed blob was left collectable"


def test_proxy_mode_pins_nothing(monkeypatch):
    """Upstream confirmed before the 201, so it is re-fetchable like any
    cached layer. Pinning is a property of UNCONFIRMED content, not of
    pushed content."""
    import asyncio

    async def instant(ref, path, digest):
        return None

    monkeypatch.setattr(ocipush, "push_blob", instant)

    async def go():
        up = ocipush.begin(_ref())
        ocipush.append(up, b"payload")
        await ocipush.finalise_blob(up, up.computed)
        return up.computed

    digest = asyncio.run(go())
    assert not ocipush.is_pinned("r.example.com", digest)
    assert not ocipush._pending


def test_proxy_mode_does_not_answer_before_upstream(monkeypatch):
    """The ordering that makes a 201 honest.

    finalise_blob must not return until push_blob has. If upstream raises, the
    client must learn about it rather than being told the push worked.
    """
    import asyncio

    async def refuses(ref, path, digest):
        raise ocipush.PushError(502, "UNAVAILABLE", "upstream said no")

    monkeypatch.setattr(ocipush, "push_blob", refuses)

    async def go():
        up = ocipush.begin(_ref())
        ocipush.append(up, b"payload")
        await ocipush.finalise_blob(up, up.computed)

    with pytest.raises(ocipush.PushError) as e:
        asyncio.run(go())
    assert e.value.status == 502


# --- cache-on-push ---------------------------------------------------------

def test_cache_on_push_off_keeps_nothing(monkeypatch):
    import asyncio

    from app import ocistore

    monkeypatch.setattr(settings, "docker_cache_on_push", False)

    async def instant(ref, path, digest):
        return None

    monkeypatch.setattr(ocipush, "push_blob", instant)

    async def go():
        up = ocipush.begin(_ref())
        ocipush.append(up, b"payload")
        await ocipush.finalise_blob(up, up.computed)
        return up.computed

    digest = asyncio.run(go())
    assert not ocistore.blob_path("r.example.com", digest).exists()


def test_cache_on_push_on_keeps_the_blob(monkeypatch):
    import asyncio

    from app import ocistore

    async def instant(ref, path, digest):
        return None

    monkeypatch.setattr(ocipush, "push_blob", instant)

    async def go():
        up = ocipush.begin(_ref())
        ocipush.append(up, b"the layer")
        await ocipush.finalise_blob(up, up.computed)
        return up.computed

    digest = asyncio.run(go())
    dest = ocistore.blob_path("r.example.com", digest)
    assert dest.exists() and dest.read_bytes() == b"the layer"


# --- the pending queue is visible ------------------------------------------

def test_store_forward_exposes_what_is_pending(monkeypatch):
    """An invisible queue is how a push gets silently lost."""
    import asyncio

    monkeypatch.setattr(settings, "docker_push_mode", "store-forward")
    started = asyncio.Event()

    async def slow(ref, path, digest):
        started.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(ocipush, "push_blob", slow)

    async def go():
        up = ocipush.begin(_ref())
        ocipush.append(up, b"payload")
        await ocipush.finalise_blob(up, up.computed)
        await asyncio.wait_for(started.wait(), 5)
        return ocipush.pending()

    pend = asyncio.run(go())
    assert pend and all(p["state"] == "running" for p in pend)
    assert all(p["digest"].startswith("sha256:") for p in pend)


def test_a_failed_forward_stays_pinned_and_visible(monkeypatch):
    """The bug this test found.

    The pin was released in a `finally`, so a push that FAILED or was cancelled
    at shutdown unpinned a blob the client had already been told was pushed --
    content existing nowhere else, now collectable by the next GC. A failed
    forward must leave the evidence pinned and visible, not tidy it away.
    """
    import asyncio

    monkeypatch.setattr(settings, "docker_push_mode", "store-forward")

    async def fails(ref, path, digest):
        raise ocipush.PushError(502, "UNAVAILABLE", "upstream refused")

    monkeypatch.setattr(ocipush, "push_blob", fails)

    async def go():
        up = ocipush.begin(_ref())
        ocipush.append(up, b"payload")
        await ocipush.finalise_blob(up, up.computed)
        key = f"r.example.com/{up.computed}"
        task = ocipush._pending[key]
        with pytest.raises(ocipush.PushError):
            await task
        return ocipush.is_pinned("r.example.com", up.computed), ocipush.pending()

    pinned, pend = asyncio.run(go())
    assert pinned, "a blob whose push FAILED was left collectable"
    assert pend and all(p["state"] == "failed" for p in pend), \
        "a failed push vanished from the pending view"
    assert all(p["error"] for p in pend), \
        "a failed push reported no reason -- 'failed' alone sends the reader " \
        "to the wrong place"
    assert all(p["pinned"] for p in pend), \
        "a failed push was reported as unpinned while still being the only copy"


def test_store_forward_with_cache_off_is_an_EPHEMERAL_store(monkeypatch):
    """The two settings compose; they do not conflict.

    store-forward is WHEN THE CLIENT IS ANSWERED. cache_on_push is WHETHER THE
    COPY IS KEPT. Off means the store is ephemeral: the blob survives long
    enough to be forwarded and is deleted once upstream confirms.

    An earlier version treated this as incoherent and overrode the setting with
    a warning -- substituting my judgement for the operator's configuration,
    which is exactly what a config option exists to prevent.
    """
    import asyncio

    from app import ocistore

    monkeypatch.setattr(settings, "docker_push_mode", "store-forward")
    monkeypatch.setattr(settings, "docker_cache_on_push", False)
    release = asyncio.Event()
    seen = {}

    async def gated(ref, path, digest):
        # Present while the push is in flight -- otherwise there is nothing
        # to forward.
        seen["during"] = ocistore.blob_path("r.example.com", digest).exists()
        await release.wait()

    monkeypatch.setattr(ocipush, "push_blob", gated)

    async def go():
        up = ocipush.begin(_ref())
        ocipush.append(up, b"payload")
        await ocipush.finalise_blob(up, up.computed)
        key = f"r.example.com/{up.computed}"
        await asyncio.sleep(0)
        release.set()
        await ocipush._pending[key]
        return up.computed

    digest = asyncio.run(go())
    assert seen["during"], "deleted before it could be forwarded"
    assert not ocistore.blob_path("r.example.com", digest).exists(), \
        "kept after confirmation despite XHC_DOCKER_CACHE_ON_PUSH=0"


def test_store_forward_with_cache_ON_keeps_it_after_confirming(monkeypatch):
    """The other half of the same behaviour, so neither can regress alone."""
    import asyncio

    from app import ocistore

    monkeypatch.setattr(settings, "docker_push_mode", "store-forward")
    monkeypatch.setattr(settings, "docker_cache_on_push", True)

    async def instant(ref, path, digest):
        return None

    monkeypatch.setattr(ocipush, "push_blob", instant)

    async def go():
        up = ocipush.begin(_ref())
        ocipush.append(up, b"payload")
        await ocipush.finalise_blob(up, up.computed)
        await ocipush._pending[f"r.example.com/{up.computed}"]
        return up.computed

    digest = asyncio.run(go())
    assert ocistore.blob_path("r.example.com", digest).exists()
    assert not ocipush.is_pinned("r.example.com", digest)


# --------------------------------------------------------------------------
# A RELATIVE Location header, which the OCI spec permits and zot returns.
#
# registry:2 answers with an absolute URL, so the end-to-end test that shipped
# with this feature never exercised the relative path -- push worked against
# every registry that happened to answer absolutely and CRASHED on the others
# with ValueError out of urllib, not an error the caller could act on.
#
# Second time an upstream's relative URL has broken this module. The first was
# a Basic realm that was not a URL at all. The two are handled differently and
# both are right: a relative Location is spec-legal and gets RESOLVED, while a
# relative realm is meaningless and gets REFUSED.
# --------------------------------------------------------------------------


class _Resp:
    def __init__(self, code, headers=None):
        self.status_code, self.headers = code, headers or {}

    async def aread(self):
        return b""


def _push_against(monkeypatch, tmp_path, location_header):
    """Drive push_blob against a registry that answers with `location_header`."""
    from app import registry as reg

    seen = {}

    async def fake_request(r, method, path, headers=None, content=None):
        if method == "HEAD":
            return _Resp(404)
        if method == "POST":
            return _Resp(202, {"location": location_header})
        raise AssertionError(f"unexpected {method} {path}")

    async def fake_absolute(r, method, url, headers=None, content=None):
        seen["url"] = url
        return _Resp(201)

    monkeypatch.setattr(reg, "request", fake_request)
    monkeypatch.setattr(reg, "request_absolute", fake_absolute)

    payload = b"z" * 4096
    blob = tmp_path / "layer"
    blob.write_bytes(payload)
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    ref = reg.Ref(upstream="zot.example.com", api="https://zot.example.com",
                  repo="team/img")
    asyncio.run(ocipush.push_blob(ref, blob, digest))
    return seen["url"]


def test_a_relative_location_is_resolved_against_the_session(monkeypatch, tmp_path):
    url = _push_against(monkeypatch, tmp_path, "/v2/team/img/blobs/uploads/abc-123")
    assert url.startswith("https://zot.example.com/v2/team/img/blobs/uploads/abc-123"), \
        f"a relative Location reached the client unresolved: {url!r}"


def test_an_absolute_location_is_left_alone(monkeypatch, tmp_path):
    """The complement. A fix that mangled absolute URLs would pass the test
    above and break every registry that already worked."""
    absolute = "https://zot.example.com/v2/other/path/uploads/xyz"
    url = _push_against(monkeypatch, tmp_path, absolute)
    assert url.startswith(absolute), f"an absolute Location was rewritten: {url!r}"


def test_a_location_on_a_different_host_is_honoured(monkeypatch, tmp_path):
    """Registries may hand the session to a separate upload endpoint. Resolving
    must not force it back onto the API host."""
    other = "https://uploads.zot.example.com/v2/team/img/blobs/uploads/q"
    url = _push_against(monkeypatch, tmp_path, other)
    assert url.startswith(other)
