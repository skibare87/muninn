"""A store-forward push must never deliver a manifest the target repository cannot serve.

REGISTRIES SCOPE BLOBS PER REPOSITORY. The defect: a blob already in this cache
from a PULL of repository A answered 200 to a pusher's HEAD for repository B.
That 200 is honest about the cache and says nothing about B, but the client
skipped the upload on the strength of it -- and the deferred manifest forward
waited only on blob forwards Muninn happened to be running. None was running
for that blob, so the manifest went to B while B lacked a layer, and a registry
that accepts manifests without verifying them stored an image that cannot be
pulled.

Everything here runs against tests/registryfake.py, which scopes blobs per
repository and records, at the moment of each manifest PUT, what that
repository was missing. The cache is driven over its real HTTP routes, in one
event loop, so the forwards it starts behind each 201 run as they do in
production.

FAILS ON THE TREE BEFORE THE FIX, measured: both variants of
test_a_pulled_blob_reaches_the_new_repository_before_its_manifest (the pulled
layer never reaches the target repository, and the manifest is PUT anyway), and
every other test here except test_a_500_on_a_finalise_that_did_NOT_commit_is_
still_a_failure, the complement, which the old code also got right. The
corrupt-copy test fails there only on the error code: the old upload had no
check of its own, and it was the registry that refused the bytes.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from registryfake import CONFIG_MEDIA, LAYER_MEDIA, MANIFEST_MEDIA, FakeRegistry, Image

from app import ocipush, ocistore, pushlimits, registry
from app.config import settings

UPSTREAM = "registry.test"
A = "team/base"         # where the pulled layers live
B = "team/app"          # where the new image is pushed
TOKEN = "manage-token-for-these-tests"
AUTH = {"authorization": f"Bearer {TOKEN}"}


def _sha(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


def _reset_push_state() -> None:
    ocipush._sessions.clear()
    ocipush._pinned.clear()
    ocipush._pending.clear()
    ocipush._attempts.clear()
    for name in ("_meta", "_needs", "_blocked", "_sources"):
        getattr(ocipush, name, {}).clear()
    ocipush._held["bytes"] = None


@pytest.fixture
def world(tmp_path, monkeypatch):
    reg = FakeRegistry()
    image = Image("base")
    reg.push(A, "v1", image)
    for d in ("cache", "docker"):
        (tmp_path / d).mkdir()
    monkeypatch.setattr(settings, "cache_dir", str(tmp_path / "cache"))
    monkeypatch.setattr(settings, "docker_dir", str(tmp_path / "docker"))
    monkeypatch.setattr(settings, "state_dir", None)
    monkeypatch.setattr(settings, "tier", None, raising=False)
    monkeypatch.setattr(settings, "docker_enabled", True)
    monkeypatch.setattr(settings, "docker_push_enabled", True)
    monkeypatch.setattr(settings, "docker_push_mode", "store-forward")
    monkeypatch.setattr(settings, "docker_cache_on_push", True)
    monkeypatch.setattr(settings, "docker_push_limits", None)
    monkeypatch.setattr(settings, "docker_blob_chunk", 0)
    monkeypatch.setattr(settings, "manage_token", TOKEN)
    monkeypatch.setattr(ocipush, "BACKOFF_S", (0, 0, 0, 0))
    # Parked manifests must stay parked until a test wakes them, so that what
    # moves them is the mechanism under test and not a timer.
    monkeypatch.setattr(ocipush, "DEP_RECHECK_S", (3600,), raising=False)
    pushlimits.reset()
    _reset_push_state()

    real_resolve = registry.resolve

    def resolve(name):
        ref = real_resolve(name)
        if ref.upstream == UPSTREAM:
            return registry.Ref(upstream=UPSTREAM, api=reg.url, repo=ref.repo)
        return ref

    monkeypatch.setattr(registry, "resolve", resolve)
    registry.clear_tokens()
    try:
        yield reg, image
    finally:
        _reset_push_state()
        reg.close()
        registry.clear_tokens()


def _client():
    from app.main import app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://muninn", timeout=30)


async def _settle(timeout: float = 15.0) -> None:
    """Until every forward has finished or is parked on a dependency."""
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while True:
        live = [t for k, t in ocipush._pending.items()
                if not t.done() and k not in getattr(ocipush, "_blocked", {})]
        if not live:
            await asyncio.sleep(0.05)
            live = [t for k, t in ocipush._pending.items()
                    if not t.done() and k not in getattr(ocipush, "_blocked", {})]
            if not live:
                return
        if loop.time() > end:
            raise AssertionError(f"forwards still running: {list(ocipush._pending)}")
        await asyncio.wait(live, timeout=0.2)


def _platform(image: Image) -> tuple[bytes, list[str]]:
    """The amd64 manifest of `image` and the blobs it references."""
    body, _ = image.manifests[image.platforms["amd64"]]
    doc = json.loads(body)
    return body, [doc["config"]["digest"]] + [x["digest"] for x in doc["layers"]]


async def _pull_from_a(c: httpx.AsyncClient, image: Image) -> list[str]:
    """What a docker pull of repository A leaves in this cache."""
    _, blobs = _platform(image)
    r = await c.get(f"/v2/{UPSTREAM}/{A}/manifests/{image.platforms['amd64']}",
                    headers={"accept": MANIFEST_MEDIA})
    assert r.status_code == 200, r.text
    for d in blobs:
        r = await c.get(f"/v2/{UPSTREAM}/{A}/blobs/{d}")
        assert r.status_code == 200 and _sha(r.content) == d
    for _ in range(100):
        if all(ocistore.blob_path(UPSTREAM, d).is_file() for d in blobs):
            break
        await asyncio.sleep(0.05)
    assert all(ocistore.blob_path(UPSTREAM, d).is_file() for d in blobs)
    return blobs


def _image_for_b(config_digest: str, config_size: int, layers: list[tuple[str, int]]) -> bytes:
    return json.dumps({
        "schemaVersion": 2, "mediaType": MANIFEST_MEDIA,
        "config": {"mediaType": CONFIG_MEDIA, "digest": config_digest, "size": config_size},
        "layers": [{"mediaType": LAYER_MEDIA, "digest": d, "size": n} for d, n in layers],
    }, separators=(",", ":")).encode()


async def _upload(c: httpx.AsyncClient, repo: str, data: bytes) -> None:
    r = await c.post(f"/v2/{UPSTREAM}/{repo}/blobs/uploads/")
    assert r.status_code == 202, r.text
    r = await c.put(f"{r.headers['location']}?digest={_sha(data)}", content=data)
    assert r.status_code == 201, r.text


async def _docker_push(c: httpx.AsyncClient, repo: str, blobs: dict[str, bytes],
                       manifest: bytes, tag: str) -> dict[str, int]:
    """What `docker push` does: HEAD each blob, upload only those not there,
    then PUT the manifest. Returns the status each HEAD got."""
    heads = {}
    for d, data in blobs.items():
        h = await c.head(f"/v2/{UPSTREAM}/{repo}/blobs/{d}")
        heads[d] = h.status_code
        if h.status_code != 200:
            await _upload(c, repo, data)
    r = await c.put(f"/v2/{UPSTREAM}/{repo}/manifests/{tag}", content=manifest,
                    headers={"content-type": MANIFEST_MEDIA})
    assert r.status_code == 201, r.text
    return heads


def _assert_confirmed_before_put(reg: FakeRegistry, repo: str, digests: list[str]) -> None:
    """THE ORDER: no manifest PUT to `repo` before every one of `digests` has
    been HEAD-present there."""
    puts = [i for i, e in enumerate(reg.events) if e[0] == "MANIFEST_PUT" and e[1] == repo]
    assert puts, "the manifest never reached the registry"
    first_put = puts[0]
    for d in digests:
        seen = [i for i, e in enumerate(reg.events)
                if e[:3] == ("HEAD", repo, d) and e[3] == 200]
        assert seen and seen[0] < first_put, (
            f"the manifest was PUT to {repo} before {d} was HEAD-present there: "
            f"{reg.events}")
    assert all(reg.events[i][4] == () for i in puts), \
        f"a manifest was PUT while {repo} was missing {[reg.events[i][4] for i in puts]}"


# --- the incident -------------------------------------------------------------

@pytest.mark.parametrize("how", ["mount", "upload"])
def test_a_pulled_blob_reaches_the_new_repository_before_its_manifest(world, how):
    """The incident, end to end: pull A, push an image to B that reuses A's
    layers. The client is told 200 for those layers and skips them; the
    registry must still end with them in B before the manifest arrives.

    `mount`: the layers were pulled through this cache, so it knows A holds
    them and links them across. `upload`: the cache holds the bytes but has no
    idea where they came from (a previous process pulled them), so it sends
    them."""
    reg, image = world
    new_layer = b"a layer only the new image has " * 500

    async def go():
        async with _client() as c:
            if how == "mount":
                pulled = await _pull_from_a(c, image)
            else:
                _, pulled = _platform(image)
                for d in pulled:
                    p = ocistore.blob_path(UPSTREAM, d)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(reg.state.blobs[d])
            cfg, base_layer = pulled
            blobs = {cfg: reg.state.blobs[cfg], base_layer: reg.state.blobs[base_layer],
                     _sha(new_layer): new_layer}
            manifest = _image_for_b(cfg, len(blobs[cfg]),
                                    [(base_layer, len(blobs[base_layer])),
                                     (_sha(new_layer), len(new_layer))])
            heads = await _docker_push(c, B, blobs, manifest, "v2")
            await _settle()
            return heads, pulled, manifest

    heads, pulled, manifest = asyncio.run(go())
    # The precondition that made it go wrong: the client was told these exist.
    assert all(heads[d] == 200 for d in pulled), heads
    for d in [*pulled, _sha(b"a layer only the new image has " * 500)]:
        assert reg.has_blob(B, d), f"{d} never reached {B}"
    _assert_confirmed_before_put(reg, B, [*pulled, _sha(b"a layer only the new image has " * 500)])
    assert reg.state.tags[f"{B}:v2"] == _sha(manifest)
    mounted = {(r, d) for r, d, src, status in reg.state.mounts if status == 201}
    if how == "mount":
        assert {(B, d) for d in pulled} <= mounted, reg.state.mounts
    else:
        assert not mounted, "nothing told it where to mount from"
    assert ocipush.pending() == [], ocipush.pending()


def test_the_proxy_mode_manifest_is_held_to_the_same_rule(world, monkeypatch):
    """proxy PUTs the manifest synchronously; the same HEAD-and-deliver runs
    first, inline, so the 201 it then returns is true of the target repo."""
    reg, image = world
    monkeypatch.setattr(settings, "docker_push_mode", "proxy")

    async def go():
        async with _client() as c:
            pulled = await _pull_from_a(c, image)
            body, _ = _platform(image)
            blobs = {d: reg.state.blobs[d] for d in pulled}
            heads = await _docker_push(c, B, blobs, body, "v3")
            return heads, pulled

    heads, pulled = asyncio.run(go())
    assert all(heads[d] == 200 for d in pulled)
    _assert_confirmed_before_put(reg, B, pulled)


def test_a_blob_nobody_holds_fails_the_manifest_by_name_and_it_is_never_put(world):
    reg, image = world
    ghost = _sha(b"a layer that exists nowhere")
    cfg = b'{"architecture":"amd64","os":"linux"}'

    async def go():
        async with _client() as c:
            # The client uploads the config and claims a layer it never sends.
            await _docker_push(c, B, {_sha(cfg): cfg},
                               _image_for_b(_sha(cfg), len(cfg), [(ghost, 27)]), "v1")
            await _settle()
            r = await c.get("/_cache/docker/pending", headers=AUTH)
            return r.json()["pending"]

    rows = asyncio.run(go())
    assert reg.manifest_puts() == [], "a manifest was PUT with a blob missing"
    [man] = [r for r in rows if r["kind"] == "manifest"]
    assert man["state"] == "failed"
    assert ghost in man["error"], man["error"]
    assert [n["digest"] for n in man["needs"]] == [ghost]
    assert man["needs"][0]["state"] == "missing"


# --- a finalise that errored after committing --------------------------------

def test_a_500_on_a_committed_finalise_is_success_and_the_manifest_follows(world):
    """Seen in production: the registry stored a 4 GB layer and answered 500.
    The blob forward used to fail, and the manifest with it, permanently."""
    reg, _ = world
    reg.state.finalise_fail.update(n=1, status=500, commits=True)
    layer = b"layer that the registry commits and then errors on " * 300
    cfg = b'{"architecture":"arm64","os":"linux"}'

    async def go():
        async with _client() as c:
            await _docker_push(c, B, {_sha(cfg): cfg, _sha(layer): layer},
                               _image_for_b(_sha(cfg), len(cfg), [(_sha(layer), len(layer))]),
                               "v1")
            await _settle()

    asyncio.run(go())
    assert reg.state.finalise_fail["n"] == 0, "the fault never fired; the test measured nothing"
    _assert_confirmed_before_put(reg, B, [_sha(cfg), _sha(layer)])
    assert ocipush.pending() == []


def test_a_500_on_a_finalise_that_did_NOT_commit_is_still_a_failure(world):
    """The complement: the HEAD must actually decide it, not the 5xx alone."""
    reg, _ = world
    reg.state.finalise_fail.update(n=1, status=500, commits=False)
    layer = b"this one really was not stored " * 300

    async def go():
        async with _client() as c:
            await _upload(c, B, layer)
            await _settle()

    asyncio.run(go())
    [row] = ocipush.pending()
    assert row["state"] == "failed" and "500" in row["error"], row
    assert not reg.has_blob(B, _sha(layer))


# --- a dependency failure parks the manifest; a retry delivers both ----------

def test_a_failed_blob_forward_parks_its_manifest_and_a_retry_delivers_both(world):
    reg, _ = world
    reg.state.finalise_fail.update(n=1, status=500, commits=False)
    layer = b"layer whose first forward fails " * 300
    cfg = b'{"architecture":"amd64","os":"linux","n":2}'
    manifest = _image_for_b(_sha(cfg), len(cfg), [(_sha(layer), len(layer))])
    mkey = f"{UPSTREAM}/{_sha(manifest)}"

    async def go():
        async with _client() as c:
            # The config goes first and succeeds; the layer's finalise fails.
            reg.state.finalise_fail["n"] = 0
            await _upload(c, B, cfg)
            await _settle()
            reg.state.finalise_fail["n"] = 1
            await _docker_push(c, B, {_sha(cfg): cfg, _sha(layer): layer}, manifest, "v1")
            await _settle()
            parked = {r["digest"]: r for r in
                      (await c.get("/_cache/docker/pending", headers=AUTH)).json()["pending"]}
            r = await c.post("/_cache/docker/pending/retry", headers=AUTH,
                             json={"upstream": UPSTREAM, "digest": _sha(layer)})
            assert r.status_code == 200, r.text
            retried = r.json()
            await _settle()
            return parked, retried

    parked, retried = asyncio.run(go())
    blob_row, man_row = parked[_sha(layer)], parked[_sha(manifest)]
    assert blob_row["state"] == "failed"
    assert man_row["state"] == "blocked", man_row
    assert man_row["attempts"] == 0, "a dependency failure was counted as a delivery attempt"
    assert [n["digest"] for n in man_row["needs"]] == [_sha(layer)]
    assert not [e for e in reg.manifest_puts() if e[3] == _sha(manifest)
                and e[4]], "the manifest went while its layer was missing"

    assert retried["scheduled"] == [f"{UPSTREAM}/{_sha(layer)}"]
    assert reg.has_blob(B, _sha(layer))
    _assert_confirmed_before_put(reg, B, [_sha(cfg), _sha(layer)])
    assert reg.state.tags[f"{B}:v1"] == _sha(manifest)
    assert ocipush._attempts[mkey] == 1, "the manifest's own budget was not its own"
    assert ocipush.pending() == []


# --- the retry endpoint -------------------------------------------------------

def test_retry_all_reschedules_every_failed_forward(world):
    reg, _ = world
    reg.state.finalise_fail.update(n=2, status=503, commits=False)
    blobs = [b"first failing layer " * 200, b"second failing layer " * 200]

    async def go():
        async with _client() as c:
            for b in blobs:
                await _upload(c, B, b)
            await _settle()
            before = [r["state"] for r in ocipush.pending()]
            r = await c.post("/_cache/docker/pending/retry", headers=AUTH)
            assert r.status_code == 200, r.text
            out = r.json()
            await _settle()
            return before, out

    before, out = asyncio.run(go())
    assert before == ["failed", "failed"]
    assert sorted(out["scheduled"]) == sorted(f"{UPSTREAM}/{_sha(b)}" for b in blobs)
    assert all(reg.has_blob(B, _sha(b)) for b in blobs)
    assert ocipush.pending() == []


def test_retry_requires_the_manage_token_and_404s_an_unknown_entry(world):
    async def go():
        async with _client() as c:
            anon = await c.post("/_cache/docker/pending/retry")
            wrong = await c.post("/_cache/docker/pending/retry",
                                 headers={"authorization": "Bearer nope"})
            unknown = await c.post("/_cache/docker/pending/retry", headers=AUTH,
                                   json={"upstream": UPSTREAM, "digest": _sha(b"never")})
            half = await c.post("/_cache/docker/pending/retry", headers=AUTH,
                                json={"upstream": UPSTREAM})
            empty = await c.post("/_cache/docker/pending/retry", headers=AUTH)
            return anon, wrong, unknown, half, empty

    anon, wrong, unknown, half, empty = asyncio.run(go())
    assert anon.status_code == 401 and wrong.status_code == 401
    assert unknown.status_code == 404, unknown.text
    assert half.status_code == 400, half.text
    assert empty.status_code == 200 and empty.json()["scheduled"] == []


def test_retry_of_a_parked_manifest_rechecks_it(world):
    """A manifest parked on a blob that appeared upstream by other means --
    someone else pushed it -- goes as soon as it is asked to look again."""
    reg, _ = world
    reg.state.finalise_fail.update(n=1, status=500, commits=False)
    layer = b"layer someone else will push " * 300
    cfg = b'{"architecture":"amd64","os":"linux","n":3}'
    manifest = _image_for_b(_sha(cfg), len(cfg), [(_sha(layer), len(layer))])

    async def go():
        async with _client() as c:
            reg.state.finalise_fail["n"] = 0
            await _upload(c, B, cfg)
            await _settle()
            reg.state.finalise_fail["n"] = 1        # the layer's finalise fails
            await _docker_push(c, B, {_sha(cfg): cfg, _sha(layer): layer}, manifest, "v1")
            await _settle()
            assert {r["state"] for r in ocipush.pending()} == {"failed", "blocked"}
            reg.state.repo_blobs[B].add(_sha(layer))     # pushed by someone else
            reg.state.blobs[_sha(layer)] = layer
            r = await c.post("/_cache/docker/pending/retry", headers=AUTH,
                             json={"upstream": UPSTREAM, "digest": _sha(manifest)})
            assert r.status_code == 200, r.text
            out = r.json()
            await _settle()
            return out

    out = asyncio.run(go())
    assert out["rechecked"] == [f"{UPSTREAM}/{_sha(manifest)}"]
    assert reg.state.tags[f"{B}:v1"] == _sha(manifest)
    _assert_confirmed_before_put(reg, B, [_sha(cfg), _sha(layer)])


# --- the pending view ---------------------------------------------------------

def test_the_pending_view_lists_dependencies_the_client_never_uploaded(world):
    """A layer pulled from A, skipped by the client, that cannot be delivered
    to B right now. It was never uploaded through Muninn, so before this the
    pending view had no row for it: the manifest's wait was invisible."""
    reg, image = world

    async def go():
        async with _client() as c:
            pulled = await _pull_from_a(c, image)
            body, _ = _platform(image)
            # Mount and upload both refused for now.
            reg.state.finalise_fail.update(n=99, status=500, commits=False)
            reg.state.mounts.clear()
            orig = reg.state.repo_blobs[A]
            reg.state.repo_blobs[A] = set()             # mount source gone
            await _docker_push(c, B, {d: reg.state.blobs[d] for d in pulled}, body, "v1")
            await _settle()
            r = await c.get("/_cache/docker/pending", headers=AUTH)
            reg.state.repo_blobs[A] = orig
            return pulled, r.json()["pending"]

    pulled, rows = asyncio.run(go())
    [man] = rows
    assert man["kind"] == "manifest" and man["state"] == "blocked", man
    assert sorted(n["digest"] for n in man["needs"]) == sorted(pulled), man["needs"]
    assert all(n["state"] == "blocked" and n["detail"] for n in man["needs"])
    assert reg.manifest_puts() == []


# --- the bytes sent are the bytes named -----------------------------------------

def test_a_corrupt_local_copy_is_never_committed_upstream(world, tmp_path):
    reg, _ = world
    good = b"what the digest names " * 100
    path = tmp_path / "rotted"
    path.write_bytes(good[:-1] + b"X")
    ref = registry.Ref(upstream=UPSTREAM, api=reg.url, repo=B)

    with pytest.raises(ocipush.PushError) as e:
        asyncio.run(ocipush.push_blob(ref, path, _sha(good)))
    assert e.value.code == "DIGEST_INVALID"
    assert not reg.has_blob(B, _sha(good))
    assert not reg.state.blobs.get(_sha(good))
