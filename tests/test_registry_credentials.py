"""Per-registry credential providers: Google Artifact Registry as an upstream.

Artifact Registry authenticates with a short-lived OAuth access token presented
as Basic `oauth2accesstoken:<token>` to its bearer-token endpoint. A static
login copied into a docker config.json stops working when that token expires,
so the credential for such a host comes from a PROVIDER (app/regcreds.py):
the metadata server, or a service-account key that mints tokens.

Everything here runs against fakes: tests/registryfake.py in OAuth mode (its
token endpoint accepts only the CURRENT OAuth token, and a bearer token stops
working when the OAuth token it was exchanged for is revoked), and
FakeMetadata standing in for both the metadata server and Google's token
endpoint. Nothing here talks to Google.

The test that fails on the tree before the feature, on behaviour rather than
on an import, is in test_artifact_registry_pull.py: this module imports
app.regcreds and so cannot even be collected there.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import hashlib
import json
import sys
from pathlib import Path

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from registryfake import (
    CONFIG_MEDIA,
    DOCKER_ACCEPT,
    LAYER_MEDIA,
    MANIFEST_MEDIA,
    FakeMetadata,
    FakeRegistry,
    Image,
)

from app import ocipush, pushlimits, regcreds, registry, tierrestore
from app.config import Settings, settings

AR = "us-docker.pkg.dev"
REPO = "proj/repo/img"
T0 = 1_800_000_000.0


def _sync(fn):
    """Run a coroutine test in its own event loop, as this suite does with
    asyncio.run, keeping its signature so pytest still injects fixtures."""
    @functools.wraps(fn)
    def run(*a, **kw):
        return asyncio.run(fn(*a, **kw))
    return run


def _sha(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


def _entries(provider: str = "gcp-metadata", **extra) -> tuple:
    entry = {"hosts": ["*-docker.pkg.dev", "gcr.io", "*.gcr.io"], "provider": provider}
    entry.update(extra)
    return regcreds.parse(json.dumps([entry]))


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def ar(tmp_path, monkeypatch):
    reg = FakeRegistry()
    image = Image("ar")
    reg.push(REPO, "v1", image)
    meta = FakeMetadata(reg, ttl=3600)  # ties OAuth mode on: only oauth-1 is valid
    for d in ("cache", "docker"):
        (tmp_path / d).mkdir()
    monkeypatch.setattr(settings, "cache_dir", str(tmp_path / "cache"))
    monkeypatch.setattr(settings, "docker_dir", str(tmp_path / "docker"))
    monkeypatch.setattr(settings, "state_dir", None)
    monkeypatch.setattr(settings, "tier", None, raising=False)
    monkeypatch.setattr(settings, "docker_enabled", True)
    monkeypatch.setattr(settings, "registry_auth_file", None)
    monkeypatch.setattr(settings, "registry_credentials",
                        _entries(metadata_url=meta.token_url), raising=False)
    clock = Clock()
    monkeypatch.setattr(regcreds, "now", clock)

    real_resolve = registry.resolve

    def resolve(name):
        ref = real_resolve(name)
        if ref.upstream == AR:
            return registry.Ref(upstream=AR, api=reg.url, repo=ref.repo)
        return ref

    monkeypatch.setattr(registry, "resolve", resolve)
    registry.reset_credentials()
    registry.clear_tokens()
    try:
        yield reg, meta, image, clock
    finally:
        reg.close()
        meta.close()
        registry.reset_credentials()
        registry.clear_tokens()


def _client():
    from app.main import app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://muninn", timeout=30)


async def _pull(c: httpx.AsyncClient, repo: str, tag: str, image: Image) -> None:
    """What docker does for one platform, checking every digest."""
    accept = {"accept": DOCKER_ACCEPT}
    r = await c.get(f"/v2/{AR}/{repo}/manifests/{tag}", headers=accept)
    assert r.status_code == 200, (r.status_code, r.headers, r.text)
    assert _sha(r.content) == image.index_digest
    plat = image.platforms["amd64"]
    m = await c.get(f"/v2/{AR}/{repo}/manifests/{plat}", headers=accept)
    assert m.status_code == 200 and _sha(m.content) == plat
    doc = json.loads(m.content)
    for d in [doc["config"]["digest"]] + [x["digest"] for x in doc["layers"]]:
        b = await c.get(f"/v2/{AR}/{repo}/blobs/{d}")
        assert b.status_code == 200 and _sha(b.content) == d, d


# --- pull ---------------------------------------------------------------------


@_sync
async def test_a_pull_authenticates_with_the_metadata_token_only(ar):
    reg, meta, image, _ = ar
    async with _client() as c:
        await _pull(c, REPO, "v1", image)
    # The token endpoint saw the metadata server's token, as Basic
    # oauth2accesstoken, and nothing else; one metadata fetch served the pull.
    assert reg.state.oauth_seen and set(reg.state.oauth_seen) == {"oauth-1"}
    assert sum(r.startswith("GET ") for r in meta.requests) == 1


@_sync
async def test_a_token_refreshes_before_expiry_and_a_pull_after_expiry_still_works(ar):
    reg, meta, image, clock = ar
    reg.state.bearer_ttl = 100_000  # the registry would let its token outlive ours
    async with _client() as c:
        await _pull(c, REPO, "v1", image)
        ref = registry.Ref(upstream=AR, api=reg.url, repo=REPO)
        assert registry._cached_token(ref) is not None

        # Past the OAuth token's expiry. The registry's own bearer token claims
        # another day of life; it must have been dropped with the OAuth token
        # it was exchanged for.
        clock.t += 3601
        assert registry._cached_token(ref) is None, \
            "a bearer token derived from an expired OAuth token is still cached"
        meta.rotate()  # oauth-1 is revoked at the registry; oauth-2 is current

        second = Image("ar-v2")
        reg.push(REPO, "v2", second)
        await _pull(c, REPO, "v2", second)
    assert reg.state.oauth_seen[-1] == "oauth-2"
    assert sum(r.startswith("GET ") for r in meta.requests) == 2


@_sync
async def test_refresh_happens_inside_the_slack_window_not_at_expiry(ar):
    _, meta, _, clock = ar
    p = regcreds.provider_for(AR)
    first = await p.credential()
    assert first.userpass == "oauth2accesstoken:oauth-1"
    clock.t += 3600 - 400  # still outside the 300s slack
    assert (await p.credential()).userpass == "oauth2accesstoken:oauth-1"
    meta.rotate()
    clock.t += 200  # now 200s before expiry: inside the slack
    assert (await p.credential()).userpass == "oauth2accesstoken:oauth-2"


@_sync
async def test_concurrent_refresh_is_single_flight(ar):
    _, meta, _, _ = ar
    meta.delay_s = 0.3
    p = regcreds.provider_for(AR)
    got = await asyncio.gather(*(p.credential() for _ in range(10)))
    assert {g.userpass for g in got} == {"oauth2accesstoken:oauth-1"}
    assert sum(r.startswith("GET ") for r in meta.requests) == 1


@_sync
async def test_concurrent_pulls_of_different_repositories_fetch_one_token(ar):
    reg, meta, _, _ = ar
    meta.delay_s = 0.3
    images = {f"proj/repo/img{i}": Image(f"ar{i}") for i in range(5)}
    for repo, im in images.items():
        reg.push(repo, "v1", im)
    async with _client() as c:
        await asyncio.gather(*(_pull(c, repo, "v1", im) for repo, im in images.items()))
    assert sum(r.startswith("GET ") for r in meta.requests) == 1


@pytest.mark.parametrize("how", ["500", "403", "not-json", "no-token", "refused"])
@_sync
async def test_a_failing_metadata_server_is_an_upstream_auth_failure_not_a_404(ar, how, caplog):
    reg, meta, _, _ = ar
    if how == "refused":
        meta.close()
    elif how == "not-json":
        meta.body = b"<html>proxy</html>"
    elif how == "no-token":
        meta.body = b'{"expires_in": 3600}'
    else:
        meta.status = int(how)
    caplog.set_level("WARNING", logger="xhc.registry")
    async with _client() as c:
        r = await c.get(f"/v2/{AR}/{REPO}/manifests/v1", headers={"accept": DOCKER_ACCEPT})
    assert r.status_code == 502, (r.status_code, r.text)
    assert r.headers["x-xhc-upstream-auth"] == "credential-provider-failed"
    assert "gcp-metadata" in r.json()["errors"][0]["message"]
    # Nothing was sent to the token endpoint: an anonymous exchange refused
    # there would read as "credentials rejected", the wrong fault.
    assert reg.state.oauth_seen == []
    assert any("could not produce a credential" in m for m in caplog.messages)
    assert not any("oauth-1" in m for m in caplog.messages), "a token reached the log"


def test_a_provider_failure_does_not_trigger_a_tier_restore():
    # The cache could not obtain its own credential: a local fault, which says
    # nothing about whether the registry is up.
    assert tierrestore.trigger_for_registry(
        {"status": 401, "token": "credential", "token_status": None,
         "token_transport": False}) is None


def test_a_provider_host_counts_as_configured_without_any_static_login(ar):
    assert registry.has_credentials(AR)
    assert registry.has_credentials("gcr.io")
    assert registry.has_credentials("eu.gcr.io")
    assert not registry.has_credentials("ghcr.io")


# --- the service-account provider ---------------------------------------------


def _key_file(tmp_path: Path, token_uri: str | None = None) -> tuple[Path, rsa.RSAPrivateKey]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    doc = {"type": "service_account", "project_id": "proj",
           "private_key_id": "kid-123", "private_key": pem,
           "client_email": "muninn@proj.iam.gserviceaccount.com", "client_id": "1"}
    if token_uri:
        doc["token_uri"] = token_uri
    path = tmp_path / "sa.json"
    path.write_text(json.dumps(doc))
    return path, key


def test_the_service_account_assertion_has_the_right_claims_and_is_signed_with_the_key(
        tmp_path, monkeypatch):
    path, key = _key_file(tmp_path)  # no token_uri: Google's default applies
    monkeypatch.setattr(regcreds, "now", lambda: T0)
    entry = _entries("gcp-service-account", key_file=str(path))[0]
    p = regcreds.GcpServiceAccountProvider(entry, lambda: None)
    token = p.assertion()
    header = jwt.get_unverified_header(token)
    assert header["alg"] == "RS256" and header["kid"] == "kid-123"
    claims = jwt.decode(token, key.public_key(), algorithms=["RS256"],
                        audience="https://oauth2.googleapis.com/token",
                        options={"verify_exp": False, "verify_iat": False})
    assert claims["iss"] == "muninn@proj.iam.gserviceaccount.com"
    assert claims["scope"] == "https://www.googleapis.com/auth/cloud-platform"
    assert claims["aud"] == "https://oauth2.googleapis.com/token"
    assert claims["iat"] == int(T0)
    assert claims["exp"] == int(T0) + 3600
    # Signed with THIS key: another key does not verify it.
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(token, other.public_key(), algorithms=["RS256"],
                   audience="https://oauth2.googleapis.com/token",
                   options={"verify_exp": False, "verify_iat": False})
    assert "PRIVATE" not in repr(p)


@_sync
async def test_a_pull_with_a_service_account_key_mints_and_presents_a_token(
        ar, tmp_path, monkeypatch):
    reg, meta, image, _ = ar
    path, key = _key_file(tmp_path, token_uri=meta.oauth_url)
    monkeypatch.setattr(settings, "registry_credentials",
                        _entries("gcp-service-account", key_file=str(path)))
    registry.reset_credentials()
    async with _client() as c:
        await _pull(c, REPO, "v1", image)
    assert len(meta.forms) == 1
    form = meta.forms[0]
    assert form["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
    claims = jwt.decode(form["assertion"], key.public_key(), algorithms=["RS256"],
                        audience=meta.oauth_url,
                        options={"verify_exp": False, "verify_iat": False})
    assert claims["iss"] == "muninn@proj.iam.gserviceaccount.com"
    assert set(reg.state.oauth_seen) == {"oauth-1"}


@_sync
async def test_json_key_presentation_sends_the_key_file_as_documented(tmp_path):
    path, _ = _key_file(tmp_path)
    entry = _entries("gcp-service-account", key_file=str(path), present="json-key")[0]
    p = regcreds.GcpServiceAccountProvider(entry, lambda: None)
    c = await p.credential()
    assert c.userpass == "_json_key:" + path.read_text()
    assert c.expires_at is None
    assert "PRIVATE" not in repr(c)


def test_an_unreadable_key_file_refuses_startup(tmp_path, monkeypatch):
    missing = tmp_path / "absent.json"
    monkeypatch.setattr(settings, "registry_credentials",
                        _entries("gcp-service-account", key_file=str(missing)), raising=False)
    with pytest.raises(regcreds.RegistryCredentialsError, match="absent.json"):
        regcreds.load()
    (tmp_path / "bad.json").write_text('{"type": "authorized_user"}')
    monkeypatch.setattr(settings, "registry_credentials",
                        _entries("gcp-service-account", key_file=str(tmp_path / "bad.json")))
    with pytest.raises(regcreds.RegistryCredentialsError, match="not a service-account key"):
        regcreds.load()
    regcreds.reset()


# --- push ---------------------------------------------------------------------


async def _settle(timeout: float = 15.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while True:
        live = [t for k, t in ocipush._pending.items()
                if not t.done() and k not in getattr(ocipush, "_blocked", {})]
        if not live:
            await asyncio.sleep(0.05)
            if not [t for k, t in ocipush._pending.items()
                    if not t.done() and k not in getattr(ocipush, "_blocked", {})]:
                return
        if loop.time() > end:
            raise AssertionError(f"forwards still running: {list(ocipush._pending)}")
        await asyncio.wait(live, timeout=0.2)


@_sync
async def test_a_push_through_to_an_artifact_registry_host_keeps_the_target_repo_guarantees(
        ar, monkeypatch):
    """A layer pulled from repository A, pushed as part of an image to B: B
    must hold it before its manifest arrives -- with every request to the
    registry authenticated through the metadata provider."""
    reg, meta, image, _ = ar
    monkeypatch.setattr(settings, "docker_push_enabled", True)
    monkeypatch.setattr(settings, "docker_push_mode", "store-forward")
    monkeypatch.setattr(settings, "docker_cache_on_push", True)
    monkeypatch.setattr(settings, "docker_push_limits", None)
    monkeypatch.setattr(settings, "docker_blob_chunk", 0)
    monkeypatch.setattr(ocipush, "BACKOFF_S", (0, 0, 0, 0))
    pushlimits.reset()
    ocipush.reset()
    target = "proj/repo/app"
    body, _ = image.manifests[image.platforms["amd64"]]
    doc = json.loads(body)
    config_d = doc["config"]["digest"]
    layer_d = doc["layers"][0]["digest"]
    try:
        async with _client() as c:
            await _pull(c, REPO, "v1", image)
            new_config = json.dumps({"architecture": "amd64", "os": "linux",
                                     "app": True}).encode()
            manifest = json.dumps({
                "schemaVersion": 2, "mediaType": MANIFEST_MEDIA,
                "config": {"mediaType": CONFIG_MEDIA, "digest": _sha(new_config),
                           "size": len(new_config)},
                "layers": [{"mediaType": LAYER_MEDIA, "digest": layer_d,
                            "size": doc["layers"][0]["size"]}],
            }, separators=(",", ":")).encode()
            # docker push: HEAD each blob, upload what is absent, PUT the manifest.
            for d, data in ((_sha(new_config), new_config), (layer_d, None)):
                h = await c.head(f"/v2/{AR}/{target}/blobs/{d}")
                if h.status_code != 200:
                    assert data is not None, f"{d} reported absent but was pulled"
                    r = await c.post(f"/v2/{AR}/{target}/blobs/uploads/")
                    assert r.status_code == 202, r.text
                    r = await c.put(f"{r.headers['location']}?digest={d}", content=data)
                    assert r.status_code == 201, r.text
            r = await c.put(f"/v2/{AR}/{target}/manifests/v1", content=manifest,
                            headers={"content-type": MANIFEST_MEDIA})
            assert r.status_code == 201, r.text
            await _settle()
    finally:
        ocipush.reset()
        pushlimits.reset()
    puts = reg.manifest_puts()
    assert puts and all(p[1] == target and p[4] == () for p in puts), puts
    assert reg.has_blob(target, layer_d) and reg.has_blob(target, _sha(new_config))
    assert config_d != _sha(new_config)
    assert set(reg.state.oauth_seen) == {"oauth-1"}


# --- configuration -------------------------------------------------------------


@pytest.mark.parametrize("raw, message", [
    ("not json", "not valid JSON"),
    ('{"hosts": ["gcr.io"]}', "JSON list"),
    ('[{"hosts": ["gcr.io"], "provider": "aws"}]', "'provider' must be one of"),
    ('[{"hosts": [], "provider": "gcp-metadata"}]', "non-empty list"),
    ('[{"hosts": ["https://gcr.io"], "provider": "gcp-metadata"}]', "bare host"),
    ('[{"hosts": ["gcr.*"], "provider": "gcp-metadata"}]', "leading"),
    ('[{"hosts": ["*gcr.io"], "provider": "gcp-metadata"}]', "leading"),
    ('[{"hosts": ["gcr.io"], "provider": "gcp-metadata", "keyfile": "x"}]', "unknown key"),
    ('[{"hosts": ["gcr.io"], "provider": "gcp-service-account"}]', "needs 'key_file'"),
    ('[{"hosts": ["gcr.io"], "provider": "gcp-metadata", "key_file": "/k"}]',
     "only to gcp-service-account"),
    ('[{"hosts": ["gcr.io"], "provider": "gcp-metadata"},'
     ' {"hosts": ["gcr.io"], "provider": "static"}]', "already named"),
    ('[{"hosts": ["gcr.io"], "provider": "gcp-service-account", "key_file": "/k",'
     ' "present": "password"}]', "'present' must be one of"),
])
def test_a_malformed_config_refuses_startup(monkeypatch, raw, message):
    monkeypatch.setenv("XHC_REGISTRY_CREDENTIALS", raw)
    with pytest.raises(regcreds.RegistryCredentialsError, match=message):
        Settings.from_env()


def test_unset_means_every_host_is_static(monkeypatch):
    monkeypatch.delenv("XHC_REGISTRY_CREDENTIALS", raising=False)
    assert Settings.from_env().registry_credentials == ()
    monkeypatch.setattr(settings, "registry_credentials", ())
    regcreds.reset()
    assert regcreds.provider_for("us-docker.pkg.dev") is None
    regcreds.reset()


def test_host_matching_and_precedence():
    entries = regcreds.parse(json.dumps([
        {"hosts": ["*-docker.pkg.dev"], "provider": "gcp-metadata"},
        {"hosts": ["*.gcr.io", "gcr.io"], "provider": "gcp-metadata"},
        {"hosts": ["europe-west1-docker.pkg.dev"], "provider": "static"},
        {"hosts": ["*-west1-docker.pkg.dev"], "provider": "gcp-metadata"},
    ]))
    m = regcreds.match
    assert m(entries, "us-docker.pkg.dev") is entries[0]
    assert m(entries, "US-DOCKER.PKG.DEV") is entries[0]
    assert m(entries, "gcr.io") is entries[1]
    assert m(entries, "eu.gcr.io") is entries[1]
    assert m(entries, "a.b.gcr.io") is None, "* is one label"
    assert m(entries, "docker.pkg.dev") is None
    assert m(entries, "evil.com") is None
    # exact beats every wildcard
    assert m(entries, "europe-west1-docker.pkg.dev") is entries[2]
    # the longest wildcard wins, whatever the list order
    assert m(entries, "us-west1-docker.pkg.dev") is entries[3]
    assert m(tuple(reversed(entries)), "us-west1-docker.pkg.dev") is entries[3]


def test_a_static_entry_keeps_the_docker_config(monkeypatch, tmp_path):
    auth = tmp_path / "config.json"
    auth.write_text(json.dumps({"auths": {"europe-docker.pkg.dev": {
        "auth": base64.b64encode(b"_json_key:{}").decode()}}}))
    monkeypatch.setattr(settings, "registry_auth_file", str(auth))
    monkeypatch.setattr(settings, "registry_credentials", regcreds.parse(json.dumps([
        {"hosts": ["*-docker.pkg.dev"], "provider": "gcp-metadata"},
        {"hosts": ["europe-docker.pkg.dev"], "provider": "static"}])), raising=False)
    registry.reset_credentials()
    try:
        assert regcreds.provider_for("europe-docker.pkg.dev") is None
        assert registry.has_credentials("europe-docker.pkg.dev")
        assert regcreds.provider_for("us-docker.pkg.dev").kind == "gcp-metadata"
    finally:
        registry.reset_credentials()
