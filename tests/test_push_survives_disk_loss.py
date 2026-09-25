"""A store-forward push the client was told succeeded must survive the blob disk.

XHC_STATE_DIR exists so an operator can put blobs on disposable disk and keep
everything whose loss changes behaviour somewhere that survives it. A pending
store-forward push is exactly that kind of thing -- the client was answered 201,
the upstream does not have it yet, and the copy here is the only one anywhere --
but the obligations lived in `<docker dir>/_pending` and the bytes they refer to
under `<docker dir>/<upstream>/blobs/`. Replacing the disk lost both, silently:
the queue came back empty, which is what "everything was delivered" looks like.

Moving only the obligation files would not have fixed it. An obligation to
forward bytes that no longer exist is still a lost push, so with the state dir
set the bytes a pending push needs are held there too, until upstream confirms.

The disk-loss tests below point the docker dir at a FRESH EMPTY directory
between "runs", which is what replacing the disk looks like from inside.
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import ocipush, ocistore, pushlimits, registry
from app.config import settings

UPSTREAM = "r.example.com"
MEDIA = "application/vnd.oci.image.manifest.v1+json"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "docker_dir", str(tmp_path / "disk1"))
    monkeypatch.setattr(settings, "state_dir", None)
    monkeypatch.setattr(settings, "docker_push_enabled", True)
    monkeypatch.setattr(settings, "docker_push_mode", "store-forward")
    monkeypatch.setattr(settings, "docker_cache_on_push", True)
    monkeypatch.setattr(settings, "docker_push_limits", None)
    monkeypatch.setattr(settings, "docker_blob_chunk", 0)
    monkeypatch.setattr(settings, "docker_push_pending_max_bytes", None,
                        raising=False)
    pushlimits.reset()
    _restart()
    yield
    _restart()
    pushlimits.reset()


def _restart():
    """Memory goes; whatever is on disk stays."""
    ocipush._sessions.clear()
    ocipush._pinned.clear()
    ocipush._pending.clear()
    ocipush._attempts.clear()
    if hasattr(ocipush, "_held"):
        ocipush._held["bytes"] = None


def _ref():
    return registry.Ref(upstream=UPSTREAM, api=f"https://{UPSTREAM}", repo="team/img")


class _Resp:
    def __init__(self, code, headers=None):
        self.status_code, self.headers = code, headers or {}

    async def aread(self):
        return b""


class Upstream:
    """A registry that records what actually ARRIVED, byte for byte.

    `down` makes every request fail at the transport, so a forward can be left
    outstanding and then cut off by the end of the event loop -- the in-process
    shape of a crash with pushes in flight.
    """

    def __init__(self, monkeypatch):
        self.blobs: dict[str, bytes] = {}
        self.manifests: dict[str, bytes] = {}
        self.down = False
        monkeypatch.setattr(ocipush, "BACKOFF_S", (3600,))
        monkeypatch.setattr(registry, "request", self.request)
        monkeypatch.setattr(registry, "request_absolute", self.request_absolute)

    async def request(self, ref, method, path, headers=None, content=None):
        if self.down:
            import httpx
            raise httpx.ConnectError("upstream is down")
        if method == "HEAD":
            digest = path.rsplit("/", 1)[1]
            return _Resp(200 if digest in self.blobs else 404)
        if method == "POST":
            return _Resp(202, {"location": f"https://{UPSTREAM}/upload/1"})
        if method == "PUT" and path.startswith("manifests/"):
            body = content() if callable(content) else content
            self.manifests[path.split("/", 1)[1]] = body
            return _Resp(201)
        raise AssertionError(f"unexpected {method} {path}")

    async def request_absolute(self, ref, method, url, headers=None, content=None):
        assert method == "PUT", method
        digest = url.split("digest=", 1)[1]
        data = b""
        async for chunk in content():
            data += chunk
        assert "sha256:" + hashlib.sha256(data).hexdigest() == digest, \
            "the upstream was sent bytes that do not match their digest"
        self.blobs[digest] = data
        return _Resp(201)


LAYER = b"layer bytes that exist nowhere else" * 100
CONFIG = b'{"architecture":"amd64"}'


def _manifest() -> bytes:
    return json.dumps({
        "schemaVersion": 2, "mediaType": MEDIA,
        "config": {"digest": "sha256:" + hashlib.sha256(CONFIG).hexdigest()},
        "layers": [{"digest": "sha256:" + hashlib.sha256(LAYER).hexdigest()}],
    }).encode()


async def _push_image():
    """What a docker client does in store-forward: blobs, then the manifest."""
    digests = []
    for data in (CONFIG, LAYER):
        up = ocipush.begin(_ref())
        await ocipush.append(up, data)
        await ocipush.finalise_blob(up, up.computed)
        digests.append(up.computed)
    await ocipush.push_manifest(_ref(), _manifest(), MEDIA, "v1")
    return digests


def _accept_push_while_upstream_is_down(upstream):
    """Run one: the client is told 201 for every part; nothing is delivered;
    the process then ends with the forwards outstanding."""
    upstream.down = True
    asyncio.run(_push_image())
    assert not upstream.blobs and not upstream.manifests


def _resume_and_drain():
    async def go():
        await ocipush.resume()
        tasks = list(ocipush._pending.values())
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 10)
        return ocipush.pending()
    return asyncio.run(go())


def _replace_the_disk(tmp_path, monkeypatch, name="disk2"):
    fresh = tmp_path / name
    fresh.mkdir()
    monkeypatch.setattr(settings, "docker_dir", str(fresh))
    return fresh


def _assert_delivered(upstream):
    cfg = "sha256:" + hashlib.sha256(CONFIG).hexdigest()
    layer = "sha256:" + hashlib.sha256(LAYER).hexdigest()
    assert upstream.blobs.get(layer) == LAYER, "the layer the client was told was pushed never arrived"
    assert upstream.blobs.get(cfg) == CONFIG, "the config blob never arrived"
    assert upstream.manifests.get("v1") == _manifest(), "the manifest never arrived"


# --- the defect --------------------------------------------------------------

def test_a_pending_push_survives_replacing_the_blob_disk(tmp_path, monkeypatch):
    """THE test. Seen failing against v0.9.25: the fresh disk had no `_pending`
    and no blobs, resume found nothing, and the upstream never got the image."""
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    upstream = Upstream(monkeypatch)
    _accept_push_while_upstream_is_down(upstream)

    _restart()
    _replace_the_disk(tmp_path, monkeypatch)
    upstream.down = False
    _resume_and_drain()

    _assert_delivered(upstream)


def test_after_delivery_nothing_is_left_owed_or_held(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setattr(settings, "state_dir", str(state))
    upstream = Upstream(monkeypatch)
    _accept_push_while_upstream_is_down(upstream)
    _restart()
    fresh = _replace_the_disk(tmp_path, monkeypatch)
    upstream.down = False
    _resume_and_drain()

    left = [p.name for p in (state / "oci" / "pending").iterdir()]
    assert left == [], f"delivered pushes are still held on the state volume: {left}"
    assert not ocipush._pinned
    # CACHE_ON_PUSH=1: the image is back in the (new) cache, as it would have
    # been had the disk never been replaced.
    layer = "sha256:" + hashlib.sha256(LAYER).hexdigest()
    assert ocistore.blob_path(UPSTREAM, layer).read_bytes() == LAYER
    assert ocistore.load_manifest(UPSTREAM, ocistore.compute_digest(_manifest()))
    assert fresh.is_dir()


def test_pending_content_is_back_in_the_cache_while_it_is_still_owed(tmp_path, monkeypatch):
    """Between restart and delivery, a pull from this cache must still find the
    image -- and a client HEAD must still get the 200 that ensure_forward
    depends on."""
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    upstream = Upstream(monkeypatch)
    _accept_push_while_upstream_is_down(upstream)
    _restart()
    _replace_the_disk(tmp_path, monkeypatch)

    async def go():
        await ocipush.resume()
        # The upstream is still down, so every forward sits in its backoff.
        # The restore runs in a thread; give it a bounded moment.
        layer = "sha256:" + hashlib.sha256(LAYER).hexdigest()
        for _ in range(500):
            if ocistore.blob_path(UPSTREAM, layer).is_file():
                break
            await asyncio.sleep(0.01)
        assert ocipush.pending(), "nothing is owed any more; the test is measuring the wrong window"
        assert not upstream.blobs
        return (ocistore.blob_path(UPSTREAM, layer).read_bytes() == LAYER,
                ocistore.load_manifest(UPSTREAM, ocistore.compute_digest(_manifest())),
                ocistore.read_tag(UPSTREAM, "team/img", "v1",
                                  ocistore.accept_fingerprint(MEDIA)))

    blob_back, manifest_back, tag_back = asyncio.run(go())
    assert blob_back and manifest_back and tag_back


# --- restart without disk loss ------------------------------------------------

@pytest.mark.parametrize("state", [False, True], ids=["state-dir-unset", "state-dir-set"])
def test_a_restart_resumes_and_forwards(tmp_path, monkeypatch, state):
    if state:
        monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    upstream = Upstream(monkeypatch)
    _accept_push_while_upstream_is_down(upstream)
    _restart()
    upstream.down = False
    pend = _resume_and_drain()
    _assert_delivered(upstream)
    assert all(p["state"] == "done" for p in pend) or not pend


def test_state_dir_unset_keeps_the_old_layout(tmp_path, monkeypatch):
    """Unchanged behaviour: obligations in <docker dir>/_pending, bytes only in
    the cache, nothing written anywhere else."""
    upstream = Upstream(monkeypatch)
    _accept_push_while_upstream_is_down(upstream)
    disk = Path(settings.docker_dir)
    names = sorted(p.name for p in (disk / "_pending").iterdir())
    assert len(names) == 3 and all(n.endswith(".json") for n in names), names
    assert not (tmp_path / "state").exists()


def test_without_a_state_dir_disk_loss_still_loses_it(tmp_path, monkeypatch):
    """Documented, not fixed: with XHC_STATE_DIR unset the docker dir IS the
    durable storage, and there is nothing else to survive on. Pinned here so
    the README's claim about what survives what cannot drift from the code."""
    upstream = Upstream(monkeypatch)
    _accept_push_while_upstream_is_down(upstream)
    _restart()
    _replace_the_disk(tmp_path, monkeypatch)
    upstream.down = False
    _resume_and_drain()
    assert not upstream.blobs and not upstream.manifests


# --- migration ------------------------------------------------------------------

def _old_style_obligations(disk: Path):
    """What v0.9.25 left behind: markers in <docker dir>/_pending, a manifest
    marker WITHOUT its body, and the bytes only in the cache."""
    pend = disk / "_pending"
    pend.mkdir(parents=True)
    for data in (CONFIG, LAYER):
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        p = ocistore.blob_path(UPSTREAM, digest)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        key = f"{UPSTREAM}/{digest}"
        (pend / (key.replace("/", "_").replace(":", "_") + ".json")).write_text(json.dumps(
            {"kind": "blob", "upstream": UPSTREAM, "api": f"https://{UPSTREAM}",
             "repo": "team/img", "digest": digest}))
    body = _manifest()
    mdigest = ocistore.compute_digest(body)
    ocistore.store_manifest(UPSTREAM, mdigest, body, MEDIA)
    key = f"{UPSTREAM}/{mdigest}"
    (pend / (key.replace("/", "_").replace(":", "_") + ".json")).write_text(json.dumps(
        {"kind": "manifest", "upstream": UPSTREAM, "api": f"https://{UPSTREAM}",
         "repo": "team/img", "digest": mdigest, "reference": "v1"}))


def test_upgrade_migrates_old_obligations_and_their_bytes(tmp_path, monkeypatch):
    """An operator who sets XHC_STATE_DIR with pushes already pending: the first
    boot moves the obligations AND holds their bytes, so the disk can be
    replaced straight after and nothing is lost."""
    disk = Path(settings.docker_dir)
    _old_style_obligations(disk)
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    upstream = Upstream(monkeypatch)
    upstream.down = True

    async def boot():
        await ocipush.resume()
    asyncio.run(boot())                       # boot one: migrate, fail to deliver

    assert not list((disk / "_pending").glob("*.json")), \
        "old markers left in place would be forwarded AGAIN if the state dir were unset"
    _restart()
    _replace_the_disk(tmp_path, monkeypatch)
    upstream.down = False
    _resume_and_drain()                       # boot two, on a fresh disk
    _assert_delivered(upstream)


def test_an_old_obligation_whose_bytes_are_gone_is_kept_not_dropped(tmp_path, monkeypatch):
    """Migration cannot hold bytes that are already missing. It still moves the
    marker, so the loss stays visible as a failed forward rather than
    disappearing."""
    disk = Path(settings.docker_dir)
    _old_style_obligations(disk)
    layer = "sha256:" + hashlib.sha256(LAYER).hexdigest()
    ocistore.blob_path(UPSTREAM, layer).unlink()
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    Upstream(monkeypatch)
    pend = _resume_and_drain()
    failed = [p for p in pend if p["digest"] == layer]
    assert failed and failed[0]["state"] == "failed", pend
    assert list((tmp_path / "state" / "oci" / "pending").glob("*.json"))


# --- the bound --------------------------------------------------------------------

def test_a_full_pending_area_REFUSES_the_push_rather_than_dropping_it(tmp_path, monkeypatch):
    """Honest back-pressure: over the bound the client gets an error and nothing
    is recorded, pinned or held. Never accept-then-drop."""
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings, "docker_push_pending_max_bytes", len(LAYER) + 1000)
    upstream = Upstream(monkeypatch)
    upstream.down = True

    async def go():
        first = ocipush.begin(_ref())
        await ocipush.append(first, LAYER)
        await ocipush.finalise_blob(first, first.computed)       # fits
        second = ocipush.begin(_ref())
        await ocipush.append(second, LAYER[::-1])
        with pytest.raises(ocipush.PushError) as exc:
            await ocipush.finalise_blob(second, second.computed)
        return first.computed, second.computed, exc.value

    first, second, err = asyncio.run(go())
    assert err.status == 507, err.status
    assert "XHC_DOCKER_PUSH_PENDING_MAX_SIZE" in err.message
    assert not ocipush.is_pinned(UPSTREAM, second)
    assert not ocistore.blob_path(UPSTREAM, second).exists()
    held = [p.name for p in (tmp_path / "state" / "oci" / "pending").iterdir()]
    assert not any(second.split(":")[1] in n for n in held), held
    assert any(first.split(":")[1] in n for n in held), held


def test_space_is_released_once_a_push_is_delivered(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(settings, "docker_push_pending_max_bytes", len(LAYER) + 1000)
    upstream = Upstream(monkeypatch)

    async def go():
        for data in (LAYER, LAYER[::-1]):
            up = ocipush.begin(_ref())
            await ocipush.append(up, data)
            await ocipush.finalise_blob(up, up.computed)
            await ocipush._pending[f"{UPSTREAM}/{up.computed}"]

    asyncio.run(go())
    assert len(upstream.blobs) == 2


def test_a_hold_that_cannot_be_written_refuses_the_push(tmp_path, monkeypatch):
    """ENOSPC on the state volume is the same answer as over the bound."""
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    Upstream(monkeypatch)

    def no_space(*a, **k):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(ocipush, "_copy_verified", no_space)
    monkeypatch.setattr(os, "link", lambda *a, **k: (_ for _ in ()).throw(
        OSError(errno.EXDEV, "cross-device link")))

    async def go():
        up = ocipush.begin(_ref())
        await ocipush.append(up, LAYER)
        with pytest.raises(ocipush.PushError) as exc:
            await ocipush.finalise_blob(up, up.computed)
        return up.computed, exc.value

    digest, err = asyncio.run(go())
    assert err.status == 507
    assert not ocipush.is_pinned(UPSTREAM, digest)
    assert not ocipush._pending
    assert not list((tmp_path / "state" / "oci" / "pending").glob("*"))


def test_the_cross_filesystem_copy_is_verified(tmp_path, monkeypatch):
    """A copy that does not hash to the digest never becomes a hold."""
    src = tmp_path / "src"
    src.write_bytes(LAYER)
    dest = tmp_path / "held" / "x.blob"
    dest.parent.mkdir()
    with pytest.raises(OSError):
        ocipush._copy_verified(src, dest, "sha256:" + "0" * 64)
    assert not dest.exists() and not list(dest.parent.iterdir())
    ocipush._copy_verified(src, dest, "sha256:" + hashlib.sha256(LAYER).hexdigest())
    assert dest.read_bytes() == LAYER


def test_disk_loss_also_survives_when_the_hold_is_a_copy(tmp_path, monkeypatch):
    """Same as the headline test with hard links unavailable, i.e. the state
    dir on a different filesystem from the blobs -- the case that matters."""
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(os, "link", lambda *a, **k: (_ for _ in ()).throw(
        OSError(errno.EXDEV, "cross-device link")))
    upstream = Upstream(monkeypatch)
    _accept_push_while_upstream_is_down(upstream)
    _restart()
    _replace_the_disk(tmp_path, monkeypatch)
    upstream.down = False
    _resume_and_drain()
    _assert_delivered(upstream)


def test_a_record_that_cannot_be_written_refuses_the_push(tmp_path, monkeypatch):
    """With the state dir set, the old "log it and accept anyway" is exactly the
    loss the operator configured against. Refuse, and leave nothing held."""
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    Upstream(monkeypatch)
    real = ocipush._write_durable

    def refuse_json(path, data):
        if path.name.endswith(".json"):
            raise OSError(errno.EIO, "I/O error")
        return real(path, data)

    monkeypatch.setattr(ocipush, "_write_durable", refuse_json)

    async def go():
        up = ocipush.begin(_ref())
        await ocipush.append(up, LAYER)
        with pytest.raises(ocipush.PushError) as exc:
            await ocipush.finalise_blob(up, up.computed)
        with pytest.raises(ocipush.PushError):
            await ocipush.push_manifest(_ref(), _manifest(), MEDIA, "v1")
        return up.computed, exc.value

    digest, err = asyncio.run(go())
    assert err.status == 503
    assert not ocipush.is_pinned(UPSTREAM, digest) and not ocipush._pending
    assert not ocistore.blob_path(UPSTREAM, digest).exists()
    assert not ocistore.load_manifest(UPSTREAM, ocistore.compute_digest(_manifest()))


def test_a_migration_that_cannot_hold_the_bytes_refuses_to_start(tmp_path, monkeypatch):
    """Fail closed, and keep the old record where it was."""
    from app import statedir

    disk = Path(settings.docker_dir)
    _old_style_obligations(disk)
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    monkeypatch.setattr(os, "link", lambda *a, **k: (_ for _ in ()).throw(
        OSError(errno.EXDEV, "cross-device link")))

    def no_space(*a, **k):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(ocipush, "_copy_verified", no_space)
    with pytest.raises(statedir.StateDirError):
        asyncio.run(ocipush.resume())
    # A blob that could not be held is still recorded in the OLD place, and
    # only there -- never moved without its bytes.
    held_first = sorted((disk / "_pending").glob("*.json"))
    assert held_first, "the record of an unheld push was removed"
    for marker in held_first:
        assert not (tmp_path / "state" / "oci" / "pending" / marker.name).exists()


def test_a_manifest_whose_body_is_gone_stays_visible_as_failed(tmp_path, monkeypatch):
    """This used to CLEAR the obligation: an acknowledged push erased with only a
    log line. It now stays pinned and shows as failed until an operator abandons it."""
    disk = Path(settings.docker_dir)
    _old_style_obligations(disk)
    mdigest = ocistore.compute_digest(_manifest())
    ocistore.manifest_path(UPSTREAM, mdigest).unlink()
    Upstream(monkeypatch)
    pend = _resume_and_drain()
    row = [p for p in pend if p["digest"] == mdigest]
    assert row and row[0]["state"] == "failed" and row[0]["pinned"], pend
    assert list((disk / "_pending").glob("*.json"))
