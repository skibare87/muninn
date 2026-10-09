"""OCI tags restored from the tier's index when the registry cannot answer.

Every test pulls through the real cache (FastAPI over httpx.ASGITransport) from
a real fake registry with a separate token endpoint (tests/registryfake.py),
into a fake S3 (tests/tierfake.py). The index is written by the cache's own
phase-1 writer during a real pull; one test (test_the_signature_is_the_one_the_
writer_documents) checks a hand-computed signature against it, so the reader
and writer cannot agree on a wrong format unnoticed.

Every refusal has a known-positive twin: the same priming with the one fact
changed restores. A refusal never seen to turn into a restore is decoration.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from registryfake import DOCKER_ACCEPT, MANIFEST_MEDIA, FakeRegistry, Image
from tierfake import FakeS3

from app import metrics, ocistore, registry, tier, tierrestore
from app.config import TierSettings, settings

UPSTREAM = "registry.test"
REPO = "team/app"
NAME = f"{UPSTREAM}/{REPO}"
KEY = b"an oci index key held only in configuration"
VERSION = "muninn-tier-index-v1"  # spelled out: not read from the code under test
ACCEPT = {"accept": DOCKER_ACCEPT}


def _sha(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


def _tier_settings(**kw) -> TierSettings:
    base = dict(
        scheme="s3", bucket="bkt", prefix="pfx", endpoint="http://tier.test", region="auto",
        path_style=True, credentials="static", access_key_id="AKID", secret_access_key="SECRET",
        part_size=16 * 1024 * 1024, index_key=KEY,
    )
    base.update(kw)
    return TierSettings(**base)


def _set_tier(monkeypatch, fake: FakeS3, **kw) -> None:
    monkeypatch.setattr(settings, "tier", _tier_settings(**kw))
    tier.reset_for_tests()
    tier.use_client(tier.build_client(httpx.AsyncClient(transport=fake.transport())))


@pytest.fixture
def world(tmp_path, monkeypatch):
    reg = FakeRegistry()
    image = Image("unit")
    reg.push(REPO, "v1", image)
    for d in ("cache", "docker"):
        (tmp_path / d).mkdir()
    monkeypatch.setattr(settings, "cache_dir", str(tmp_path / "cache"))
    monkeypatch.setattr(settings, "docker_dir", str(tmp_path / "docker"))
    monkeypatch.setattr(settings, "docker_enabled", True)
    monkeypatch.setattr(ocistore, "has_ingest_room", lambda: True)
    monkeypatch.setattr(tier, "BACKOFF_S", (0, 0))
    real_resolve = registry.resolve

    def resolve(name):
        ref = real_resolve(name)
        if ref.upstream == UPSTREAM:
            return registry.Ref(upstream=UPSTREAM, api=reg.url, repo=ref.repo)
        return ref

    monkeypatch.setattr(registry, "resolve", resolve)
    registry.clear_tokens()
    metrics.reset()
    fake = FakeS3()
    _set_tier(monkeypatch, fake)
    try:
        yield SimpleNamespace(reg=reg, image=image, fake=fake, mp=monkeypatch)
    finally:
        reg.close()
        registry.clear_tokens()
        tier.reset_for_tests()


def _run(coro_fn):
    return asyncio.run(coro_fn())


async def _req(method: str, path: str, headers: dict | None = None) -> httpx.Response:
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://muninn") as c:
        return await c.request(method, path, headers=headers or {})


def _get(path: str, method: str = "GET", headers: dict | None = None) -> httpx.Response:
    return _run(lambda: _req(method, path, headers if headers is not None else ACCEPT))


def _prime(w, tag: str = "v1", platforms: tuple[str, ...] = ("amd64",)) -> None:
    """A real pull through the cache with the registry up, then write-back."""

    async def scenario():
        r = await _req("GET", f"/v2/{NAME}/manifests/{tag}", ACCEPT)
        assert r.status_code == 200, r.text
        doc = json.loads(r.content)
        for arch in platforms:
            if "manifests" not in doc:
                break
            d = w.image.platforms[arch]
            m = await _req("GET", f"/v2/{NAME}/manifests/{d}", ACCEPT)
            assert m.status_code == 200
            mdoc = json.loads(m.content)
            for b in [mdoc["config"]] + mdoc["layers"]:
                assert (await _req("GET", f"/v2/{NAME}/blobs/{b['digest']}")).status_code == 200
        for _ in range(200):
            if tier.queue_depth():
                break
            await asyncio.sleep(0.005)
        await tier.drain()

    _run(scenario)


def _drop_disk() -> None:
    for p in Path(settings.docker_dir).iterdir():
        if p.is_dir():
            shutil.rmtree(p)
    registry.clear_tokens()


def _tag_keys(fake: FakeS3, tag: str = "v1") -> list[str]:
    prefix = f"pfx/v1/index/oci/{UPSTREAM}/tags/{REPO}/{tag}/"
    return sorted(k for k in fake.objects if k.startswith(prefix))


def _restores() -> dict:
    return {k: v for k, v in metrics.snapshot()["tier_restore"].items() if v}


def _index_reads() -> dict:
    return {k: v for k, v in metrics.snapshot()["tier_index_reads"].items() if v}


def _assert_restored(r: httpx.Response, w, reason: str, auth: str = "signed") -> None:
    assert r.status_code == 200, (r.status_code, r.text)
    assert r.headers["x-xhc-cache"] == "TIER-RESTORE"
    assert r.headers["x-xhc-restore-reason"] == reason
    assert r.headers["x-xhc-index-auth"] == auth
    assert r.headers["x-xhc-ref-observed-at"].endswith("Z")
    assert int(r.headers["x-xhc-ref-age"]) >= 0
    assert r.headers["docker-content-digest"] == w.image.index_digest
    assert r.headers["content-type"].startswith("application/vnd.oci.image.index.v1+json")
    if r.request.method == "GET":
        assert _sha(r.content) == w.image.index_digest


def _assert_not_restored(r: httpx.Response) -> None:
    assert r.headers.get("x-xhc-cache") != "TIER-RESTORE", dict(r.headers)
    assert r.status_code != 200


# ---------------------------------------------------------------------------
# the trigger table, as a pure function
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("up,expected", [
    # no response from the registry: only a transport failure is an outage
    ({"transport": True}, "unreachable"),
    ({"transport": False}, None),
    ({}, None),
    # the registry answered
    ({"status": 500}, "upstream_5xx"),
    ({"status": 503, "error_code": "UNAVAILABLE"}, "upstream_5xx"),
    ({"status": 404, "error_code": "MANIFEST_UNKNOWN"}, "not_found"),
    ({"status": 404, "error_code": "NAME_UNKNOWN"}, "not_found"),
    ({"status": 404, "error_code": None}, None),
    ({"status": 404, "error_code": "BLOB_UNKNOWN"}, None),
    ({"status": 401, "error_code": "UNAUTHORIZED"}, None),
    ({"status": 401, "authenticated": True}, None),
    ({"status": 403, "error_code": "DENIED"}, None),
    ({"status": 403}, None),
    ({"status": 429, "error_code": "TOOMANYREQUESTS"}, None),
    ({"status": 400, "error_code": "MANIFEST_INVALID"}, None),
    # the bearer dance failed: the registry's 401 comes back, judged by the token layer
    ({"status": 401, "token": "declined", "token_status": 403}, None),
    ({"status": 401, "token": "declined", "token_status": 401}, None),
    ({"status": 401, "token": "unreachable", "token_status": None,
      "token_transport": True}, "unreachable"),
    ({"status": 401, "token": "unreachable", "token_status": None,
      "token_transport": False}, None),
    ({"status": 401, "token": "unreachable", "token_status": 503}, "upstream_5xx"),
    ({"status": 401, "token": "unreachable", "token_status": 429}, None),
    ({"status": 401, "token": "unreachable", "token_status": 404}, None),
    ({"status": 401, "token": "unusable", "token_status": 200}, None),
    ({"status": 401, "token": "unusable", "token_status": None}, None),
])
def test_which_registry_answers_restore(up, expected):
    assert tierrestore.trigger_for_registry(up) == expected


def test_the_hub_rule_is_unchanged_by_the_oci_one():
    """One classifier, two 404 rules. The HF rule must not have moved."""
    t = tierrestore.trigger_for_status
    assert t(404, None) == "not_found" and t(404, "RepoNotFound") == "not_found"
    assert t(404, "EntryNotFound") is None
    assert t(404, None, proto="oci") is None
    assert t(404, "MANIFEST_UNKNOWN", proto="oci") == "not_found"
    for status in (401, 403, 429):
        assert t(status) is None and t(status, proto="oci") is None


# ---------------------------------------------------------------------------
# the round trip, and each registry failure
# ---------------------------------------------------------------------------


def test_the_registry_refusing_connections_restores_the_tag(world):
    w = world
    _prime(w)
    assert _tag_keys(w.fake), "phase 1 wrote no tag observation"
    _drop_disk()
    w.reg.refuse()
    r = _get(f"/v2/{NAME}/manifests/v1")
    _assert_restored(r, w, "unreachable")
    assert r.content == w.image.index, "verbatim bytes"
    assert _restores() == {"oci|ok": 1}
    assert _index_reads() == {"oci|signed_ok": 1}


def test_head_is_answered_too_with_the_same_headers_and_no_body(world):
    w = world
    _prime(w)
    _drop_disk()
    w.reg.refuse()
    h = _get(f"/v2/{NAME}/manifests/v1", "HEAD")
    _assert_restored(h, w, "unreachable")
    assert h.content == b""
    assert int(h.headers["content-length"]) == len(w.image.index)


def test_a_multi_arch_index_restores_and_its_platform_and_blobs_follow(world):
    w = world
    _prime(w)
    _drop_disk()
    w.reg.refuse()
    _assert_restored(_get(f"/v2/{NAME}/manifests/v1"), w, "unreachable")
    plat = w.image.platforms["amd64"]
    m = _get(f"/v2/{NAME}/manifests/{plat}")
    assert m.status_code == 200 and _sha(m.content) == plat
    assert m.headers["x-xhc-cache"] == "TIER-HIT"
    doc = json.loads(m.content)
    for b in [doc["config"]] + doc["layers"]:
        r = _get(f"/v2/{NAME}/blobs/{b['digest']}", headers={})
        assert r.status_code == 200 and _sha(r.content) == b["digest"]
        assert r.headers["x-xhc-cache"] == "TIER-HIT"
    # The platform never pulled is not in the bucket: it fails by digest, honestly.
    other = _get(f"/v2/{NAME}/manifests/{w.image.platforms['arm64']}")
    assert other.status_code == 502


@pytest.mark.parametrize("status,code", [
    (404, "MANIFEST_UNKNOWN"), (404, "NAME_UNKNOWN"), (500, None), (503, "UNAVAILABLE"),
])
def test_registry_answers_that_restore(world, status, code):
    w = world
    _prime(w)
    _drop_disk()
    w.reg.answer(status, code)
    reason = "not_found" if status == 404 else "upstream_5xx"
    _assert_restored(_get(f"/v2/{NAME}/manifests/v1"), w, reason)


def test_a_tag_deleted_upstream_restores_from_its_last_observation(world):
    """The registry's real 404 for a deleted tag, not a forced one."""
    w = world
    _prime(w)
    _drop_disk()
    w.reg.delete_tag(REPO, "v1")
    _assert_restored(_get(f"/v2/{NAME}/manifests/v1"), w, "not_found")


@pytest.mark.parametrize("status,code", [
    (401, "UNAUTHORIZED"), (401, None), (403, "DENIED"), (403, None),
    (429, "TOOMANYREQUESTS"), (404, None), (404, "BLOB_UNKNOWN"),
])
def test_registry_answers_that_never_restore(world, status, code):
    w = world
    _prime(w)
    _drop_disk()
    w.reg.answer(status, code)
    _assert_not_restored(_get(f"/v2/{NAME}/manifests/v1"))
    assert not _restores(), "a refusal is not a restore attempt"
    # Known positive: the same priming, the registry down, restores.
    w.reg.refuse()
    _assert_restored(_get(f"/v2/{NAME}/manifests/v1"), w, "unreachable")


@pytest.mark.parametrize("token_status", [401, 403, 429])
def test_a_token_endpoint_that_answers_no_never_restores(world, token_status):
    """A refusal at the token endpoint (or a rate limit there) is an ANSWER."""
    w = world
    _prime(w)
    _drop_disk()
    w.reg.token_answer(token_status)
    _assert_not_restored(_get(f"/v2/{NAME}/manifests/v1"))
    assert w.reg.state.token_log, "the token endpoint was asked"
    assert not _restores()


@pytest.mark.parametrize("outage", ["refuse", "503"])
def test_a_token_endpoint_outage_restores(world, outage):
    w = world
    _prime(w)
    _drop_disk()
    if outage == "refuse":
        w.reg.refuse_tokens()
        reason = "unreachable"
    else:
        w.reg.token_answer(503)
        reason = "upstream_5xx"
    _assert_restored(_get(f"/v2/{NAME}/manifests/v1"), w, reason)


def test_a_registry_outage_with_nothing_restorable_is_502_not_404(world):
    """No answer is a gateway failure. It used to render as 404, which the
    docker CLI prints as 'not found' -- the one status it erases."""
    w = world
    w.reg.refuse()
    r = _get(f"/v2/{NAME}/manifests/never-pulled")
    assert r.status_code == 502
    assert r.headers["x-xhc-upstream-status"] == "none"
    assert _restores() == {"oci|missing": 1}


# ---------------------------------------------------------------------------
# trust
# ---------------------------------------------------------------------------


def test_unsigned_observations_are_refused_without_the_opt_in(world):
    w = world
    _set_tier(w.mp, w.fake, index_key=None)  # written unsigned
    _prime(w)
    obs = _tag_keys(w.fake)
    assert obs and w.fake.objects[obs[0]].metadata["x-amz-meta-auth"] == "unsigned"
    _drop_disk()
    w.reg.refuse()
    _set_tier(w.mp, w.fake)  # a key now: signed only
    _assert_not_restored(_get(f"/v2/{NAME}/manifests/v1"))
    assert _restores() == {"oci|unsigned_refused": 1}
    # Known positive: the opt-in, and the same unsigned entry restores.
    _set_tier(w.mp, w.fake, index_key=None, restore_unsigned=True)
    _assert_restored(_get(f"/v2/{NAME}/manifests/v1"), w, "unreachable", auth="unsigned")


def test_an_unsigned_restore_is_logged_loudly(world, caplog):
    w = world
    _set_tier(w.mp, w.fake, index_key=None, restore_unsigned=True)
    _prime(w)
    _drop_disk()
    w.reg.refuse()
    with caplog.at_level(logging.INFO, logger="xhc.tier.restore"):
        _assert_restored(_get(f"/v2/{NAME}/manifests/v1"), w, "unreachable", auth="unsigned")
    loud = [r for r in caplog.records if r.levelno == logging.WARNING
            and "UNSIGNED TIER RESTORE" in r.getMessage()]
    assert loud and w.image.index_digest in loud[0].getMessage()
    assert _restores() == {"oci|ok_unsigned": 1}


def test_the_pull_policy_applies_to_a_restore_as_to_a_miss(world):
    from app import policy

    w = world
    _prime(w)
    # The tag file survives and its manifest does not, and hits are not policed:
    # the one path where nothing earlier refuses, and only the restore's own
    # check stands between a denied repository and a manifest from the bucket.
    shutil.rmtree(ocistore.upstream_root(UPSTREAM) / "manifests")
    registry.clear_tokens()
    w.reg.refuse()
    w.mp.setattr(policy, "docker_enforced_on_hits", lambda: False)
    w.mp.setattr(policy, "check_docker",
                 lambda up, repo, pol=None: policy.Decision(False, "not this one"))
    r = _get(f"/v2/{NAME}/manifests/v1")
    assert r.status_code == 403 and r.headers.get("x-xhc-policy") == "denied"
    assert not _restores()


def test_restore_off_means_off(world):
    w = world
    _prime(w)
    _drop_disk()
    w.reg.refuse()
    _set_tier(w.mp, w.fake, restore=False)
    _assert_not_restored(_get(f"/v2/{NAME}/manifests/v1"))
    assert not _restores()


def test_a_bad_signature_counts_as_absent_and_is_counted(world):
    w = world
    _prime(w)
    _drop_disk()
    w.reg.refuse()
    _set_tier(w.mp, w.fake, index_key=b"another key entirely")
    _assert_not_restored(_get(f"/v2/{NAME}/manifests/v1"))
    assert _restores() == {"oci|bad_signature": 1}
    assert _index_reads() == {"oci|bad_signature": 1}
    assert not ocistore.manifest_path(UPSTREAM, w.image.index_digest).exists()


def test_an_observation_copied_to_another_tag_is_refused(world):
    """The signature covers the tag: v1's observation under v2's name fails."""
    w = world
    _prime(w)
    _drop_disk()
    w.reg.refuse()
    src = _tag_keys(w.fake)[0]
    dst = src.replace("/tags/team/app/v1/", "/tags/team/app/v2/")
    w.fake.objects[dst] = w.fake.objects[src]
    _assert_not_restored(_get(f"/v2/{NAME}/manifests/v2"))
    assert _restores() == {"oci|bad_signature": 1}


def test_a_newer_forged_observation_falls_back_to_the_older_genuine_one(world):
    """A tampered newest observation cannot block a restore, only fall back."""
    w = world
    _prime(w)
    _drop_disk()
    w.reg.refuse()
    src = _tag_keys(w.fake)[0]
    obs = src.rsplit("/", 1)[1].split("-", 1)[0]
    forged_digest = "sha256:" + "f" * 64
    newer = src.replace(f"{obs}-{w.image.index_digest}",
                        f"{int(obs) + 1000:015d}-{forged_digest}")
    w.fake.objects[newer] = w.fake.objects[src]
    _assert_restored(_get(f"/v2/{NAME}/manifests/v1"), w, "unreachable")
    assert _index_reads() == {"oci|bad_signature": 1, "oci|signed_ok": 1}


def test_the_signature_is_the_one_the_writer_documents(world):
    """Recomputed by hand from the canonical form, with the version spelled out:
    (version, "oci-tag", upstream, repo, tag, "", digest, 0, observed_at)."""
    w = world
    _prime(w)
    key = _tag_keys(w.fake)[0]
    obs, _, digest = key.rsplit("/", 1)[1].partition("-")
    canon = json.dumps([VERSION, "oci-tag", UPSTREAM, REPO, "v1", "", digest, 0, obs],
                       separators=(",", ":"))
    want = hmac.new(KEY, canon.encode(), hashlib.sha256).hexdigest()
    meta = w.fake.objects[key].metadata
    assert meta["x-amz-meta-sig"] == want
    assert meta["x-amz-meta-version"] == VERSION
    assert meta["x-amz-meta-observed-at"] == obs


def test_a_tampered_manifest_in_the_tier_is_refused_by_digest(world):
    w = world
    _prime(w)
    _drop_disk()
    w.reg.refuse()
    mkey = tier.oci_manifest_key(UPSTREAM, w.image.index_digest)
    body = w.fake.objects[mkey].body
    wrong = body.replace(b"amd64", b"amd65")
    assert len(wrong) == len(body) and wrong != body
    w.fake.objects[mkey].body = wrong
    r = _get(f"/v2/{NAME}/manifests/v1")
    _assert_not_restored(r)
    assert b"amd65" not in r.content
    assert _restores() == {"oci|content_mismatch": 1}
    assert tier.is_bad(mkey) and mkey in w.fake.objects, "marked bad, never deleted"
    assert not ocistore.manifest_path(UPSTREAM, w.image.index_digest).exists()


def test_an_image_whose_blobs_are_not_in_the_tier_is_not_promised(world):
    """A single-platform tag whose layers were never pulled: serving its
    manifest would promise a pull that fails on the first layer."""
    w = world
    plat = w.image.platforms["amd64"]
    w.reg.state.tags[f"{REPO}:slim"] = plat
    _prime(w, "slim")  # the manifest only, no blobs
    _drop_disk()
    w.reg.refuse()
    _assert_not_restored(_get(f"/v2/{NAME}/manifests/slim"))
    assert _restores() == {"oci|content_missing": 1}
    # Known positive: once the blobs are in the tier, it restores.
    w.reg.registry.start()
    _prime(w, "slim")
    doc = json.loads(w.image.manifests[plat][0])

    async def pull_blobs():
        for b in [doc["config"]] + doc["layers"]:
            await _req("GET", f"/v2/{NAME}/blobs/{b['digest']}")
        await tier.drain()

    _run(pull_blobs)
    _drop_disk()
    w.reg.refuse()
    r = _get(f"/v2/{NAME}/manifests/slim")
    assert r.status_code == 200 and r.headers["x-xhc-cache"] == "TIER-RESTORE"
    assert _sha(r.content) == plat and r.headers["content-type"].startswith(MANIFEST_MEDIA)


def test_a_client_that_does_not_accept_the_observed_type_gets_no_restore(world):
    w = world
    _prime(w)
    _drop_disk()
    w.reg.refuse()
    r = _get(f"/v2/{NAME}/manifests/v1",
             headers={"accept": "application/vnd.docker.distribution.manifest.v1+json"})
    _assert_not_restored(r)
    assert _restores() == {"oci|missing": 1}


def test_every_oci_restore_is_logged_at_info_with_repo_tag_digest_and_age(world, caplog):
    """The docker CLI shows none of the headers, so this line is what an
    operator has."""
    w = world
    _prime(w)
    _drop_disk()
    w.reg.refuse()
    with caplog.at_level(logging.INFO, logger="xhc.tier.restore"):
        _assert_restored(_get(f"/v2/{NAME}/manifests/v1"), w, "unreachable")
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO
            and "tier restore of" in r.getMessage()]
    assert msgs, [r.getMessage() for r in caplog.records]
    m = msgs[-1]
    assert f"{UPSTREAM}/{REPO}:v1" in m and w.image.index_digest in m and "age" in m


def test_the_metric_carries_proto(world):
    w = world
    _prime(w)
    _drop_disk()
    w.reg.refuse()
    _get(f"/v2/{NAME}/manifests/v1")
    text = metrics.render({})
    assert 'muninn_tier_restore_total{proto="oci",result="ok"} 1' in text
    assert 'muninn_tier_restore_total{proto="hf",result="ok"} 0' in text
    assert 'muninn_tier_index_reads_total{proto="oci",result="signed_ok"} 1' in text


# ---------------------------------------------------------------------------
# phase 1: is every successful pull-by-tag path recorded?
# ---------------------------------------------------------------------------


def test_a_prewarm_by_tag_records_the_tag_and_writes_the_manifests_back(world):
    """FAILS ON v0.9.33: a prewarm stored the tag locally and wrote no
    observation, and every later pull was a local HIT that never asked the
    registry, so the tag was never indexed at all."""
    from app import ocimanage

    w = world
    ref = registry.resolve(NAME)

    async def scenario():
        await ocimanage._fetch_closure(SimpleNamespace(manifests_done=0), ref, "v1")
        await tier.drain()

    _run(scenario)
    keys = _tag_keys(w.fake)
    assert len(keys) == 1 and keys[0].endswith(w.image.index_digest)
    for d in w.image.manifests:
        assert tier.oci_manifest_key(UPSTREAM, d) in w.fake.objects


def test_reconcile_backfills_a_tag_observation_that_never_reached_the_bucket(world):
    """A pull writes an observation only when the mapping MOVES, so one whose
    PUT was lost was never retried. Reconcile is the retry, stamped with when
    the registry was seen to say so -- not with now."""
    w = world
    _set_tier(w.mp, w.fake, write=False)
    _prime(w)
    assert not _tag_keys(w.fake)
    entry = ocistore.read_tag_compatible(UPSTREAM, REPO, "v1", DOCKER_ACCEPT)
    assert entry["upstream"] == UPSTREAM and entry["observed_at"]
    _set_tier(w.mp, w.fake)

    async def scenario():
        await tier.reconcile()
        await tier.drain()

    _run(scenario)
    keys = _tag_keys(w.fake)
    assert len(keys) == 1 and keys[0].endswith(w.image.index_digest)
    assert keys[0].rsplit("/", 1)[1].startswith(f"{int(entry['observed_at'] * 1000):015d}-")
    # And it restores.
    _drop_disk()
    w.reg.refuse()
    _assert_restored(_get(f"/v2/{NAME}/manifests/v1"), w, "unreachable")
    # A second reconcile writes nothing new.
    _run(scenario)
    assert len(_tag_keys(w.fake)) == 1


def test_reconcile_skips_a_mapping_the_registry_never_confirmed(world):
    """A store-forward push the registry has not accepted, or a tag file from
    before names were recorded, is not an observation."""
    w = world
    ocistore.write_tag(UPSTREAM, REPO, "pushed", "fp", w.image.index_digest,
                       "application/vnd.oci.image.index.v1+json")
    legacy = ocistore.tag_path(UPSTREAM, REPO, "legacy", "fp")
    legacy.write_text(json.dumps({"digest": w.image.index_digest, "media_type": "x",
                                  "checked_at": 1.0}))
    _run(tier.reconcile)
    _run(tier.drain)
    assert not _tag_keys(w.fake, "pushed") and not _tag_keys(w.fake, "legacy")


def test_fail_open_does_not_move_the_observation_time(world):
    """touch_tag on a fail-open serve bumps checked_at; observed_at must stay
    when the registry last SAID so, or a backfill would make it look fresh."""
    w = world
    w.mp.setattr(settings, "docker_tag_ttl_s", 0.0001)
    _prime(w)
    before = ocistore.read_tag_compatible(UPSTREAM, REPO, "v1", DOCKER_ACCEPT)
    w.reg.refuse()
    r = _get(f"/v2/{NAME}/manifests/v1")
    assert r.status_code == 200 and r.headers.get("x-xhc-cache") != "TIER-RESTORE"
    after = ocistore.read_tag_compatible(UPSTREAM, REPO, "v1", DOCKER_ACCEPT)
    assert after["observed_at"] == before["observed_at"]
    assert after["checked_at"] >= before["checked_at"]

