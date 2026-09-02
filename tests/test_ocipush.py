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
import json
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
    asyncio.run(ocipush.append(up, b"hello "))
    asyncio.run(ocipush.append(up, b"world"))
    assert up.offset == 11
    assert up.computed == "sha256:" + hashlib.sha256(b"hello world").hexdigest()


def test_a_mismatched_digest_is_refused_and_the_upload_discarded():
    """Never tell a client 201 for content that is not what they claimed."""
    import asyncio

    up = ocipush.begin(_ref())
    asyncio.run(ocipush.append(up, b"payload"))
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
        await ocipush.append(up, b"payload")
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
        await ocipush.append(up, b"payload")
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
        await ocipush.append(up, b"payload")
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
        await ocipush.append(up, b"payload")
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
        await ocipush.append(up, b"the layer")
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
        await ocipush.append(up, b"payload")
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
        await ocipush.append(up, b"payload")
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
        await ocipush.append(up, b"payload")
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
        await ocipush.append(up, b"payload")
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


# --------------------------------------------------------------------------
# store-forward is EVENTUALLY consistent, not immediately consistent.
#
# Two defects came from treating it as the latter. Both were silent: the queue
# was empty and nothing logged an error, because from Muninn's side nothing had
# failed -- nothing had been asked.
# --------------------------------------------------------------------------

def test_manifests_are_deferred_in_store_forward(monkeypatch, tmp_path):
    """The manifest path was forwarding SYNCHRONOUSLY in both modes.

    The tell was that a client received the registry's own 400: a deferred
    forward cannot return an upstream status, so receiving one proves no
    deferral happened.
    """
    from app import ocistore
    from app import registry as reg

    monkeypatch.setattr(settings, "docker_push_mode", "store-forward")
    sent = asyncio.Event()

    async def upstream(r, method, path, headers=None, content=None):
        sent.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(reg, "request", upstream)
    body = b'{"schemaVersion":2,"layers":[]}'

    async def go():
        digest = await ocipush.push_manifest(
            _ref(), body, "application/vnd.oci.image.manifest.v1+json", "latest")
        # Returned WITHOUT the upstream having answered.
        return digest, sent.is_set(), ocipush.pending()

    digest, upstream_answered, pend = asyncio.run(go())
    assert digest == ocistore.compute_digest(body)
    assert not upstream_answered, "the manifest was forwarded synchronously"
    assert pend, "the deferred manifest was never enqueued"


def test_a_deferred_manifest_waits_for_its_blobs(monkeypatch, tmp_path):
    """A manifest is only valid once its blobs are upstream, so the deferred
    forward must be ordered after them -- pushing it early is what the registry
    rejected with a 400."""
    from app import registry as reg

    monkeypatch.setattr(settings, "docker_push_mode", "store-forward")
    order = []
    blob_done = asyncio.Event()

    async def slow_blob(ref, path, digest):
        await blob_done.wait()
        order.append("blob")

    async def upstream(r, method, path, headers=None, content=None):
        order.append("manifest")
        return type("R", (), {"status_code": 201, "headers": {}})()

    monkeypatch.setattr(ocipush, "push_blob", slow_blob)
    monkeypatch.setattr(reg, "request", upstream)

    cfg = "sha256:" + "a" * 64
    body = json.dumps({"schemaVersion": 2, "config": {"digest": cfg},
                       "layers": []}).encode()

    async def go():
        up = ocipush.begin(_ref())
        await ocipush.append(up, b"cfg")
        # Pretend the config blob is the one being forwarded.
        ocipush._pending[f"r.example.com/{cfg}"] = asyncio.create_task(
            slow_blob(_ref(), up.path, cfg))
        await ocipush.push_manifest(
            _ref(), body, "application/vnd.oci.image.manifest.v1+json", "t")
        await asyncio.sleep(0)
        blob_done.set()
        await asyncio.gather(*[t for t in ocipush._pending.values()],
                             return_exceptions=True)
        return order

    got = asyncio.run(go())
    assert got and got[0] == "blob", \
        f"the manifest was delivered before its blobs: {got}"


def test_a_held_but_unconfirmed_blob_gets_its_forward_rescheduled(monkeypatch, tmp_path):
    """The quiet half.

    A client that sees 200 skips the upload, so a blob left over from a failed
    forward would never be re-enqueued: no upload, no enqueue, empty queue, no
    error. ensure_forward closes that.
    """
    scheduled = []

    async def fake_forward(ref, digest, key):
        scheduled.append(digest)

    monkeypatch.setattr(ocipush, "_forward_later", fake_forward)
    ref = _ref()
    digest = "sha256:" + "b" * 64

    async def go():
        # Not held at all -- nothing to deliver, nothing scheduled.
        first = ocipush.ensure_forward(ref, digest)
        # Held and unconfirmed, with no attempt in flight.
        ocipush._pinned.add(f"{ref.upstream}/{digest}")
        second = ocipush.ensure_forward(ref, digest)
        await asyncio.sleep(0)
        return first, second

    first, second = asyncio.run(go())
    assert first is False, "scheduled a forward for a blob we do not hold"
    assert second is True, "an unconfirmed blob was never rescheduled"
    assert scheduled == [digest]


def test_a_confirmed_cached_blob_is_not_rescheduled(monkeypatch, tmp_path):
    """The complement. A blob cached from a PULL was fetched from that upstream
    and is already there -- re-pushing every cached layer on every HEAD would
    be a self-inflicted stampede."""
    ref = _ref()
    digest = "sha256:" + "c" * 64
    assert ocipush.ensure_forward(ref, digest) is False


# --------------------------------------------------------------------------
# A transient transport failure is not a terminal one.
#
# Retry was ASYMMETRIC and backwards. A 401 already recovered -- the auth dance
# re-sends after a challenge -- while a dropped connection went straight to
# failed and pinned forever. The transport error is the one that says nothing
# about whether the request was acceptable, so it is the one that should retry.
# --------------------------------------------------------------------------

def test_a_read_error_is_retried_and_can_succeed(monkeypatch):
    import httpx

    monkeypatch.setattr(ocipush, "BACKOFF_S", (0, 0, 0, 0))
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ReadError("")
        return "delivered"

    got = asyncio.run(ocipush._with_retries(flaky, what="x", key="k"))
    assert got == "delivered"
    assert calls["n"] == 3, "a transient transport error was not retried"


def test_retries_are_bounded(monkeypatch):
    """Retrying forever turns a broken upstream into an invisible hot loop."""
    import httpx

    monkeypatch.setattr(ocipush, "BACKOFF_S", (0, 0, 0, 0))
    calls = {"n": 0}

    async def always():
        calls["n"] += 1
        raise httpx.ConnectError("")

    with pytest.raises(httpx.ConnectError):
        asyncio.run(ocipush._with_retries(always, what="x", key="k"))
    assert calls["n"] == ocipush.MAX_ATTEMPTS


def test_an_http_answer_is_NOT_retried(monkeypatch):
    """A registry that says 400 will say 400 again.

    Retrying an answer burns the upstream and delays the operator learning
    something true. Only the transport layer is retried.
    """
    monkeypatch.setattr(ocipush, "BACKOFF_S", (0, 0, 0, 0))
    calls = {"n": 0}

    async def refused():
        calls["n"] += 1
        raise ocipush.PushError(502, "UNAVAILABLE", "upstream rejected it: 400")

    with pytest.raises(ocipush.PushError):
        asyncio.run(ocipush._with_retries(refused, what="x", key="k"))
    assert calls["n"] == 1, "an HTTP answer was retried as though transient"


# --- the reason must never be empty ---------------------------------------

def test_an_exception_with_no_message_still_reports_a_reason():
    """str(httpx.ReadError()) is the EMPTY STRING.

    The pending view reported {"state":"failed","error":""} -- a failure with
    no cause, which sends an operator to the container logs for one word.
    """
    import httpx

    assert ocipush.describe_exc(httpx.ReadError("")) == "httpx.ReadError"
    assert "timed out" in ocipush.describe_exc(httpx.ReadTimeout("timed out"))
    assert ocipush.describe_exc(ValueError("")) == "ValueError"


def test_pending_distinguishes_retrying_from_given_up(monkeypatch):
    """Both showed as the same state, so "it is still trying" and "it has
    stopped" were indistinguishable to anyone polling."""
    import httpx

    monkeypatch.setattr(settings, "docker_push_mode", "store-forward")
    monkeypatch.setattr(ocipush, "BACKOFF_S", (0, 0, 0, 0))

    async def always_fails(ref, path, digest):
        raise httpx.ReadError("")

    monkeypatch.setattr(ocipush, "push_blob", always_fails)

    async def go():
        up = ocipush.begin(_ref())
        await ocipush.append(up, b"payload")
        await ocipush.finalise_blob(up, up.computed)
        key = f"r.example.com/{up.computed}"
        with pytest.raises(httpx.ReadError):
            await ocipush._pending[key]
        return ocipush.pending()

    pend = asyncio.run(go())
    rec = pend[0]
    assert rec["state"] == "failed"
    assert rec["error"] == "httpx.ReadError", f"empty reason: {rec['error']!r}"
    assert rec["attempts"] == ocipush.MAX_ATTEMPTS
    assert rec["pinned"], "gave up AND unpinned -- that would lose the only copy"


# --------------------------------------------------------------------------
# An outstanding forward must survive a restart.
#
# The queue lived only in memory. A restart emptied it while the bytes stayed
# on disk: the client had been told 201, the upstream did not have it, the blob
# was pinned, and the only record of the obligation was gone. An operator
# polling saw an empty queue -- which is exactly what they see when everything
# succeeded. EMPTY MEANT "ALL DELIVERED" AND "WE FORGOT" AT THE SAME TIME.
#
# And nothing could reconstruct it: after a restart a blob landed by a failed
# push is byte-identical on disk to one cached from a pull. Both are files
# under <upstream>/blobs/. So "report it at startup" was not implementable
# without persisting the obligation itself.
# --------------------------------------------------------------------------

def test_an_outstanding_forward_is_recorded_on_disk(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "docker_push_mode", "store-forward")
    started = asyncio.Event()

    async def slow(ref, path, digest):
        started.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(ocipush, "push_blob", slow)

    async def go():
        up = ocipush.begin(_ref())
        await ocipush.append(up, b"payload")
        await ocipush.finalise_blob(up, up.computed)
        await asyncio.wait_for(started.wait(), 5)
        return up.computed

    digest = asyncio.run(go())
    markers = list((tmp_path / "_pending").glob("*.json"))
    assert markers, "an accepted forward left no durable record"
    rec = json.loads(markers[0].read_text())
    assert rec["digest"] == digest and rec["kind"] == "blob"
    assert rec["upstream"] == "r.example.com"


def test_recovery_re_enqueues_what_a_previous_run_owed(monkeypatch, tmp_path):
    """The restart case, simulated by clearing the in-memory state only."""
    monkeypatch.setattr(settings, "docker_push_mode", "store-forward")
    scheduled = []

    async def capture(ref, digest, key):
        scheduled.append((ref.upstream, digest))

    async def slow(ref, path, digest):
        await asyncio.sleep(3600)

    monkeypatch.setattr(ocipush, "push_blob", slow)

    async def enqueue():
        up = ocipush.begin(_ref())
        await ocipush.append(up, b"payload")
        await ocipush.finalise_blob(up, up.computed)
        return up.computed

    digest = asyncio.run(enqueue())

    # THE RESTART: memory goes, disk stays.
    ocipush._pending.clear()
    ocipush._pinned.clear()
    ocipush._attempts.clear()
    assert ocipush.pending() == [], "precondition: the queue looks empty"

    monkeypatch.setattr(ocipush, "_forward_later", capture)
    asyncio.run(ocipush.resume())
    assert scheduled == [("r.example.com", digest)], \
        "an obligation from a previous run was silently dropped"
    assert ocipush.is_pinned("r.example.com", digest), \
        "recovered content was left collectable"


def test_a_completed_forward_leaves_no_obligation(monkeypatch, tmp_path):
    """The complement. A marker that outlived its forward would resurrect a
    delivered blob on every restart, forever."""
    monkeypatch.setattr(settings, "docker_push_mode", "store-forward")

    async def instant(ref, path, digest):
        return None

    monkeypatch.setattr(ocipush, "push_blob", instant)

    async def go():
        up = ocipush.begin(_ref())
        await ocipush.append(up, b"payload")
        await ocipush.finalise_blob(up, up.computed)
        await ocipush._pending[f"r.example.com/{up.computed}"]

    asyncio.run(go())
    assert not list((tmp_path / "_pending").glob("*.json")), \
        "a delivered forward left a marker that will resurrect it"


def test_abandoning_a_forward_also_drops_its_marker(monkeypatch, tmp_path):
    """An operator's decision to give up must outlive the process, exactly as
    the obligation does -- otherwise an abandoned forward returns at the next
    restart."""
    monkeypatch.setattr(settings, "docker_push_mode", "store-forward")

    async def fails(ref, path, digest):
        raise RuntimeError("upstream refused")

    monkeypatch.setattr(ocipush, "push_blob", fails)

    async def go():
        up = ocipush.begin(_ref())
        await ocipush.append(up, b"payload")
        await ocipush.finalise_blob(up, up.computed)
        with pytest.raises(RuntimeError):
            await ocipush._pending[f"r.example.com/{up.computed}"]
        return up.computed

    digest = asyncio.run(go())
    assert list((tmp_path / "_pending").glob("*.json")), "a failed forward lost its record"
    ocipush.abandon("r.example.com", digest)
    assert not list((tmp_path / "_pending").glob("*.json"))
