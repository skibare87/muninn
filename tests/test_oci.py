"""Offline tests for the Docker/OCI pull-through surface -- no network.

These cover the properties where a silent regression would be expensive and
hard to spot:

  * manifest bytes are served EXACTLY as received (any re-encoding changes the
    digest and breaks pull-by-digest and every signature check)
  * a blob whose bytes do not match its digest is never committed
  * policy refusals are a policy error, not an auth error (a 401 here sends
    people to `docker login` to fix something login cannot fix)
  * push verbs are refused

Live behaviour against real registries is listed in docs/SPEC-0.5.0.md §11.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("HF_HUB_CACHE", "/tmp/xhc-test-cache")
os.environ.setdefault("XHC_DOCKER_DIR", "/tmp/xhc-test-docker")

from fastapi.testclient import TestClient

from app import ocistore, policy, registry
from app.config import settings


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "docker_dir", str(tmp_path))
    ocistore.reset_stats_cache()
    return tmp_path


@pytest.fixture
def client():
    from app.main import app

    return TestClient(app)


# -- name resolution --------------------------------------------------------


@pytest.mark.parametrize(
    "name,upstream,api,repo",
    [
        ("ghcr.io/org/img", "ghcr.io", "https://ghcr.io", "org/img"),
        ("quay.io/prometheus/prometheus", "quay.io", "https://quay.io", "prometheus/prometheus"),
        # Docker Hub: docker.io is not the API host, and single-segment repos
        # carry an implicit library/.
        ("docker.io/library/nginx", "docker.io", "https://registry-1.docker.io", "library/nginx"),
        ("docker.io/nginx", "docker.io", "https://registry-1.docker.io", "library/nginx"),
        ("index.docker.io/nginx", "docker.io", "https://registry-1.docker.io", "library/nginx"),
        # No dot in the first segment, so it is part of the repo name.
        ("nginx", "docker.io", "https://registry-1.docker.io", "library/nginx"),
        ("org/img", "docker.io", "https://registry-1.docker.io", "org/img"),
        ("registry.k8s.io/pause", "registry.k8s.io", "https://registry.k8s.io", "pause"),
        ("localhost:5000/img", "localhost:5000", "https://localhost:5000", "img"),
        ("ghcr.io/a/b/c/d", "ghcr.io", "https://ghcr.io", "a/b/c/d"),
    ],
)
def test_resolve(name, upstream, api, repo):
    ref = registry.resolve(name)
    assert (ref.upstream, ref.api, ref.repo) == (upstream, api, repo)


@pytest.mark.parametrize("bad", ["", "/", "ghcr.io", "ghcr.io/"])
def test_resolve_rejects_names_with_no_repository(bad):
    with pytest.raises(registry.ResolveError):
        registry.resolve(bad)


# -- digests ----------------------------------------------------------------


def test_verify_accepts_matching_digest():
    body = b'{"schemaVersion":2}'
    ocistore.verify(body, ocistore.compute_digest(body))


def test_verify_rejects_mismatch():
    with pytest.raises(ocistore.DigestMismatch):
        ocistore.verify(b"tampered", ocistore.compute_digest(b"original"))


def test_store_manifest_refuses_bytes_that_do_not_match(store):
    with pytest.raises(ocistore.DigestMismatch):
        ocistore.store_manifest(
            "ghcr.io", ocistore.compute_digest(b"a"), b"b", "application/json"
        )
    # Nothing may be left behind by a refused write.
    assert not list(store.rglob("*.meta"))


def test_digest_regex_rejects_junk():
    assert ocistore.DIGEST_RE.match("sha256:" + "a" * 64)
    for bad in ["sha256:xyz", "md5:" + "a" * 32, "sha256:" + "A" * 64, "../../etc/passwd"]:
        assert not ocistore.DIGEST_RE.match(bad)


# -- storage layout ---------------------------------------------------------


def test_blobs_are_sharded_two_chars(store):
    d = "sha256:" + "ab" + "c" * 62
    p = ocistore.blob_path("ghcr.io", d)
    assert p.parent.name == "ab"
    assert p.name == "ab" + "c" * 62
    assert p.is_relative_to(store)


def test_crafted_repo_name_cannot_escape_the_root(store):
    p = ocistore.tag_path("../../etc", "../../root/x", "../../../tag", "fp")
    assert p.is_relative_to(store)


def test_same_digest_on_two_registries_is_stored_separately(store):
    d = ocistore.compute_digest(b"layer")
    assert ocistore.blob_path("ghcr.io", d) != ocistore.blob_path("quay.io", d)


# -- manifests are byte-exact ----------------------------------------------


def test_manifest_round_trips_byte_for_byte(store):
    # Deliberately awkward: key order and whitespace that json.dumps would not
    # reproduce. Re-encoding would change the digest and break pull-by-digest.
    body = b'{ "schemaVersion":2,\n  "zzz":1, "aaa":2  }'
    digest = ocistore.compute_digest(body)
    ocistore.store_manifest("ghcr.io", digest, body, "application/vnd.oci.image.index.v1+json")
    held = ocistore.load_manifest("ghcr.io", digest)
    assert held.body == body
    assert ocistore.compute_digest(held.body) == digest
    assert held.media_type == "application/vnd.oci.image.index.v1+json"


def test_missing_manifest_is_none(store):
    assert ocistore.load_manifest("ghcr.io", ocistore.compute_digest(b"nope")) is None


# -- accept negotiation -----------------------------------------------------


def test_accept_fingerprint_is_order_insensitive():
    a = ocistore.accept_fingerprint("application/vnd.oci.image.index.v1+json, application/json")
    b = ocistore.accept_fingerprint("application/json,application/vnd.oci.image.index.v1+json")
    assert a == b


def test_accept_fingerprint_distinguishes_different_accepts():
    assert ocistore.accept_fingerprint("application/vnd.oci.image.index.v1+json") != (
        ocistore.accept_fingerprint("application/vnd.docker.distribution.manifest.v2+json")
    )


def test_accept_fingerprint_ignores_q_params():
    assert ocistore.accept_fingerprint("application/json;q=0.9") == (
        ocistore.accept_fingerprint("application/json")
    )


# -- tag freshness ----------------------------------------------------------


def test_tag_freshness_respects_ttl(store, monkeypatch):
    monkeypatch.setattr(settings, "docker_tag_ttl_s", 300.0)
    ocistore.write_tag("ghcr.io", "org/img", "v1", "fp", "sha256:" + "a" * 64, "application/json")
    entry = ocistore.read_tag("ghcr.io", "org/img", "v1", "fp")
    assert ocistore.tag_is_fresh(entry)
    entry["checked_at"] = 0
    assert not ocistore.tag_is_fresh(entry)


def test_ttl_zero_means_never_revalidate(store, monkeypatch):
    """0 makes the cache a pure archive: mutable tags serve what was first
    cached, forever."""
    monkeypatch.setattr(settings, "docker_tag_ttl_s", 0.0)
    assert ocistore.tag_is_fresh({"checked_at": 0})


# -- policy -----------------------------------------------------------------


def _pol(**over):
    base = {
        "mode": "open",
        "registries": [],
        "allow": [],
        "deny": [],
        "scope": "ingest",
        "max_blob_bytes": None,
        "deny_registries": [],
    }
    base.update(over)
    return base


def test_open_policy_allows_anything():
    assert policy.check_docker("ghcr.io", "org/img", _pol()).allowed


def test_deny_beats_allow():
    p = _pol(mode="allowlist", registries=["ghcr.io"], allow=["ghcr.io/org/*"],
             deny=["ghcr.io/org/secret"])
    assert policy.check_docker("ghcr.io", "org/ok", p).allowed
    assert not policy.check_docker("ghcr.io", "org/secret", p).allowed


def test_registry_allowlist_applies_even_in_open_mode():
    """The host guard is the cheap, high-value one and should not require
    flipping the whole policy to allowlist."""
    p = _pol(registries=["ghcr.io", "docker.io"])
    assert policy.check_docker("ghcr.io", "org/img", p).allowed
    d = policy.check_docker("evil.example", "org/img", p)
    assert not d.allowed and "allowlist" in d.reason


def test_denied_registries_win():
    p = _pol(deny_registries=["evil.*"])
    assert not policy.check_docker("evil.example", "a/b", p).allowed


def test_allowlist_with_nothing_allowed_denies():
    assert not policy.check_docker("ghcr.io", "a/b", _pol(mode="allowlist")).allowed


def test_blob_size_cap_refuses_before_bytes_move():
    p = _pol(max_blob_bytes=1000)
    assert policy.check_blob_size(999, p).allowed
    assert not policy.check_blob_size(1001, p).allowed
    # Unknown size cannot be refused on size.
    assert policy.check_blob_size(None, p).allowed


def test_saving_hf_policy_does_not_erase_docker_policy(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "cache_dir", str(tmp_path))
    p = tmp_path / ".xhc"
    p.mkdir(parents=True, exist_ok=True)
    (p / "policy.json").write_text(
        json.dumps({"mode": "open", "allow": [], "deny": [], "scope": "ingest",
                    "docker": {"mode": "allowlist", "registries": ["ghcr.io"]}})
    )
    policy.save({"mode": "open", "allow": [], "deny": [], "scope": "ingest"})
    assert policy.load_docker()["registries"] == ["ghcr.io"]


# -- routes -----------------------------------------------------------------


def test_v2_root_is_answered_locally(client):
    r = client.get("/v2/")
    assert r.status_code == 200
    assert r.headers["docker-distribution-api-version"] == "registry/2.0"


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_push_verbs_are_refused(client, method):
    r = getattr(client, method)("/v2/ghcr.io/org/img/blobs/uploads/")
    assert r.status_code == 405
    assert r.json()["errors"][0]["code"] == "UNSUPPORTED"


def test_bad_digest_is_rejected_before_any_upstream_call(client):
    r = client.get("/v2/ghcr.io/org/img/blobs/notadigest")
    assert r.status_code == 400
    assert r.json()["errors"][0]["code"] == "DIGEST_INVALID"


def test_policy_refusal_is_403_not_401(client, store, monkeypatch):
    """A 401 would tell the user to `docker login`, which cannot fix a policy
    refusal. It must be an unambiguous policy error."""
    monkeypatch.setattr(policy, "load_docker", lambda: _pol(deny_registries=["*"]))
    r = client.get("/v2/ghcr.io/org/img/blobs/sha256:" + "a" * 64)
    assert r.status_code == 403
    assert r.headers["x-xhc-policy"] == "denied"
    assert "www-authenticate" not in {k.lower() for k in r.headers}


def test_cached_blob_is_served_without_upstream(client, store, monkeypatch):
    monkeypatch.setattr(policy, "load_docker", lambda: _pol())
    body = b"layer-bytes-here"
    digest = ocistore.compute_digest(body)
    p = ocistore.blob_path("ghcr.io", digest)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(body)
    r = client.get(f"/v2/ghcr.io/org/img/blobs/{digest}")
    assert r.status_code == 200
    assert r.content == body
    assert r.headers["docker-content-digest"] == digest


def test_cached_blob_honours_range(client, store, monkeypatch):
    monkeypatch.setattr(policy, "load_docker", lambda: _pol())
    body = b"0123456789"
    digest = ocistore.compute_digest(body)
    p = ocistore.blob_path("ghcr.io", digest)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(body)
    r = client.get(
        f"/v2/ghcr.io/org/img/blobs/{digest}", headers={"range": "bytes=2-5"}
    )
    assert r.status_code == 206
    assert r.content == b"2345"


def test_cached_manifest_head_has_no_body_but_correct_length(client, store, monkeypatch):
    monkeypatch.setattr(policy, "load_docker", lambda: _pol())
    body = b'{"schemaVersion":2,"layers":[]}'
    digest = ocistore.compute_digest(body)
    ocistore.store_manifest("ghcr.io", digest, body, "application/vnd.oci.image.manifest.v1+json")
    get = client.get(f"/v2/ghcr.io/org/img/manifests/{digest}")
    assert get.status_code == 200
    assert get.content == body
    assert get.headers["docker-content-digest"] == digest
    head = client.head(f"/v2/ghcr.io/org/img/manifests/{digest}")
    assert head.status_code == 200
    assert head.content == b""
    assert head.headers["content-length"] == str(len(body))
    assert head.headers["docker-content-digest"] == digest
    assert head.headers["content-type"] == "application/vnd.oci.image.manifest.v1+json"


# -- corruption is never committed ------------------------------------------


class _FakeStream:
    """Minimal stand-in for an httpx streaming response."""

    def __init__(self, chunks):
        self._chunks = chunks
        self.closed = False

    async def aiter_bytes(self, _n=None):
        for c in self._chunks:
            yield c

    async def aclose(self):
        self.closed = True


def test_corrupt_blob_is_discarded_and_never_committed(store):
    """The digest names the bytes. If they disagree, the bytes are wrong and
    must not reach the cache -- a bad layer served forever from disk is far
    worse than a failed pull."""
    import asyncio

    from app import ocicompat

    digest = ocistore.compute_digest(b"the-real-layer")
    final = ocistore.blob_path("ghcr.io", digest)
    job = ocicompat.BlobJob(
        id="test",
        digest=digest,
        upstream="ghcr.io",
        final_path=final,
        incomplete_path=str(final) + ".incomplete",
    )
    resp = _FakeStream([b"tampered", b"-bytes"])
    asyncio.run(ocicompat._write_blob(job, resp))

    assert job.state == "error"
    assert "expected" in (job.error or "")
    assert not final.exists(), "corrupt bytes were committed to the cache"
    assert not Path(job.incomplete_path).exists(), "partial file left behind"
    assert resp.closed


def test_good_blob_is_committed(store):
    import asyncio

    from app import ocicompat

    body = b"the-real-layer"
    digest = ocistore.compute_digest(body)
    final = ocistore.blob_path("ghcr.io", digest)
    job = ocicompat.BlobJob(
        id="test",
        digest=digest,
        upstream="ghcr.io",
        final_path=final,
        incomplete_path=str(final) + ".incomplete",
    )
    asyncio.run(ocicompat._write_blob(job, _FakeStream([body[:4], body[4:]])))
    assert job.state == "done"
    assert final.read_bytes() == body
    assert not Path(job.incomplete_path).exists()


def test_no_v2_path_reaches_the_huggingface_catch_all(client):
    """/v2/ is the Docker surface in full. A request we do not route must get an
    OCI-shaped 404 from this router, not whatever the HF handler would do with
    it."""
    r = client.get("/v2/ghcr.io/org/img/something/weird")
    assert r.status_code == 404
    assert r.json()["errors"][0]["code"] == "NOT_FOUND"
    assert r.headers["docker-distribution-api-version"] == "registry/2.0"


def test_a_manifest_we_hold_is_not_refetched_over_header_cosmetics(store):
    """Found live: a freshly prewarmed tag still cost an upstream request on every
    pull, because prewarm stored it under its own Accept fingerprint and the real
    client advertised a different set. Fingerprint stays the fast path; media-type
    compatibility is the fallback."""
    body = b'{"schemaVersion":2,"manifests":[]}'
    digest = ocistore.compute_digest(body)
    media = "application/vnd.oci.image.index.v1+json"
    ocistore.store_manifest("ghcr.io", digest, body, media)
    stored_fp = ocistore.accept_fingerprint(f"{media},application/json")
    ocistore.write_tag("ghcr.io", "org/img", "v1", stored_fp, digest, media)

    # a client advertising a DIFFERENT set that still accepts this media type
    other = f"{media},application/vnd.docker.distribution.manifest.v2+json"
    assert ocistore.accept_fingerprint(other) != stored_fp, "fingerprints must differ for the test to mean anything"
    assert ocistore.read_tag("ghcr.io", "org/img", "v1", ocistore.accept_fingerprint(other)) is None
    found = ocistore.read_tag_compatible("ghcr.io", "org/img", "v1", other)
    assert found and found["digest"] == digest


def test_a_client_that_does_not_accept_the_stored_type_gets_nothing(store):
    """The fallback must not serve a media type the client cannot handle."""
    body = b'{"schemaVersion":2}'
    digest = ocistore.compute_digest(body)
    ocistore.store_manifest("ghcr.io", digest, body, "application/vnd.oci.image.index.v1+json")
    ocistore.write_tag("ghcr.io", "org/img", "v1", "fp", digest,
                       "application/vnd.oci.image.index.v1+json")
    assert ocistore.read_tag_compatible(
        "ghcr.io", "org/img", "v1", "application/vnd.docker.distribution.manifest.v1+json") is None
