"""Workload identity: signed JWTs from configured issuers, on the wire.

Real keys throughout -- RSA and EC pairs generated here, a JWKS served by a fake
issuer through an httpx transport -- because a verifier tested against a mocked
verifier proves nothing about signatures.

Every refusal is asserted on BOTH surfaces where the token is accepted, and
every refusal asserts the reason on the wire and that the token is not echoed.
Every allow has a refusal beside it: a test that only ever sees 200 cannot tell
a working gate from an absent one.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sys
import time
import uuid
from functools import cache
from pathlib import Path

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from jwt.algorithms import ECAlgorithm, RSAAlgorithm

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.authz import new_secret, parse_rule
from app.jwtconfig import JWTConfigError, parse

COMMIT = "a" * 40
ETAG = "b" * 64
BODY = b'{"model_type": "test"}'
BLOB = b"layer bytes"
BLOB_DIGEST = "sha256:" + hashlib.sha256(BLOB).hexdigest()

ISS_A = "https://issuer-a.example"
ISS_B = "https://issuer-b.example"
SA = "system:serviceaccount:ml:trainer"
SUBJECT = f"k8s:{SA}"


@cache
def _rsa(name: str):
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@cache
def _ec(name: str):
    return ec.generate_private_key(ec.SECP256R1())


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class FakeIssuer:
    """An OIDC issuer: a discovery document and a JWKS, and a switch to break it."""

    def __init__(self, url: str, kid: str, key_name: str):
        self.url = url
        self.private: dict[str, object] = {}
        self.down = False
        self.discovery_issuer = url
        self.discovery_jwks_uri = f"{url}/jwks"
        self.fetches: list[str] = []
        self.add_rsa(kid, key_name)

    def add_rsa(self, kid: str, key_name: str) -> None:
        self.private[kid] = _rsa(key_name)

    def add_ec(self, kid: str, key_name: str) -> None:
        self.private[kid] = _ec(key_name)

    def jwks(self) -> dict:
        keys = []
        for kid, priv in self.private.items():
            if isinstance(priv, rsa.RSAPrivateKey):
                jwk = RSAAlgorithm.to_jwk(priv.public_key(), as_dict=True)
            else:
                jwk = ECAlgorithm.to_jwk(priv.public_key(), as_dict=True)
            keys.append({**jwk, "kid": kid, "use": "sig"})
        return {"keys": keys}

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.fetches.append(request.url.path)
        if self.down:
            raise httpx.ConnectError("issuer is down", request=request)
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(200, json={"issuer": self.discovery_issuer,
                                             "jwks_uri": self.discovery_jwks_uri})
        if request.url.path == "/jwks":
            return httpx.Response(200, json=self.jwks())
        return httpx.Response(404)

    def jwks_fetches(self) -> int:
        return self.fetches.count("/jwks")

    def claims(self, **over) -> dict:
        now = int(time.time())
        base = {"iss": self.url, "aud": ["muninn"], "sub": SA, "iat": now,
                "nbf": now, "exp": now + 600}
        base.update(over)
        return {k: v for k, v in base.items() if v is not None}

    def token(self, kid: str | None = None, alg: str | None = None, *,
              signer=None, **over) -> str:
        kid = kid or next(iter(self.private))
        priv = signer or self.private.get(kid) or next(iter(self.private.values()))
        if alg is None:
            alg = "RS256" if isinstance(priv, rsa.RSAPrivateKey) else "ES256"
        return jwt.encode(self.claims(**over), priv, algorithm=alg, headers={"kid": kid})


class Clock:
    """Stands in for the `time` module inside jwtauth only. Never global:
    asyncio's own loop reads time.monotonic."""

    def __init__(self):
        self.offset = 0.0

    def time(self) -> float:
        return time.time() + self.offset

    def monotonic(self) -> float:
        return time.monotonic() + self.offset

    def advance(self, seconds: float) -> None:
        self.offset += seconds


def _config(*entries: dict) -> str:
    return json.dumps(list(entries))


A_CFG = {"issuer": ISS_A, "audience": "muninn", "subject_template": "k8s:{sub}"}
B_CFG = {"issuer": ISS_B, "audience": ["muninn", "other"],
         "subject_template": "idp:{sub}", "algorithms": ["RS256", "ES256"]}


class _Upstream:
    def __init__(self):
        self.calls: list[str] = []

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.calls.append(f"{request.method} {request.url}")
            return httpx.Response(404, json={"error": "not found upstream"})

        return httpx.MockTransport(handler)


@pytest.fixture
def world(tmp_path, monkeypatch):
    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient

    from app import authzmanage, dockerauth, hfcompat, jwtauth, manage, ocicompat, ocistore, refs
    from app.authzstore import AuthzStore
    from app.config import settings

    db = tmp_path / "authz.db"
    cache = tmp_path / "cache"
    cache.mkdir()
    for name, value in {
        "authz_db": str(db), "cache_dir": str(cache), "docker_dir": str(tmp_path / "oci"),
        "docker_enabled": True, "docker_push_enabled": False, "hf_auth": "key",
        "hf_rules": "enforce", "web_root": None, "state_dir": None,
        "synthesize_repo_info": True, "block_client_xet": False,
        "datasets_server": "https://datasets-server.invalid", "manage_token": "mt",
        "jwt_cache_ttl_s": 60.0,
        "jwt_issuers": parse(_config(A_CFG, B_CFG)),
    }.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(dockerauth, "_store", None)

    a = FakeIssuer(ISS_A, "a1", "rsa-a")
    b = FakeIssuer(ISS_B, "b1", "rsa-b")
    by_host = {"issuer-a.example": a, "issuer-b.example": b}

    def route(request: httpx.Request) -> httpx.Response:
        return by_host[request.url.host].handle(request)

    jwtauth.reset()
    monkeypatch.setattr(jwtauth, "_transport", httpx.MockTransport(route))
    clock = Clock()
    monkeypatch.setattr(jwtauth, "time", clock)

    upstream = _Upstream()
    hf_client = httpx.AsyncClient(transport=upstream.transport())
    monkeypatch.setattr(hfcompat, "get_client", lambda: hf_client)

    async def _no_metadata(*_a, **_k):
        from huggingface_hub import errors

        raise errors.EntryNotFoundError("no such file upstream")

    monkeypatch.setattr(hfcompat, "fetch_metadata", _no_metadata)

    async def _never_stale(*_a, **_k):
        return False

    monkeypatch.setattr(refs, "is_stale", _never_stale)
    hfcompat.negative_cache_clear()

    store = AuthzStore(db)

    # A model and an image layer already on disk, so an allowed pull is a 200
    # from the cache and nothing depends on an upstream.
    base = cache / "models--org--model-a"
    (base / "blobs").mkdir(parents=True)
    (base / "refs").mkdir()
    (base / "snapshots" / COMMIT).mkdir(parents=True)
    (base / "blobs" / ETAG).write_bytes(BODY)
    (base / "snapshots" / COMMIT / "config.json").symlink_to(base / "blobs" / ETAG)
    (base / "refs" / "main").write_text(COMMIT)
    blob = ocistore.blob_path("docker.io", BLOB_DIGEST)
    blob.parent.mkdir(parents=True)
    blob.write_bytes(BLOB)

    app = FastAPI()
    app.include_router(manage.router)
    app.include_router(authzmanage.router)
    app.include_router(ocicompat.router, dependencies=[Depends(dockerauth.require_pull_auth)])
    app.include_router(hfcompat.router)
    client = TestClient(app, raise_server_exceptions=False)

    class W:
        pass

    w = W()
    w.client, w.store, w.a, w.b, w.clock, w.settings = client, store, a, b, clock, settings
    w.jwtauth = jwtauth
    return w


HF_OK = f"/org/model-a/resolve/{COMMIT}/config.json"
HF_OTHER = f"/elsewhere/secret/resolve/{COMMIT}/config.json"
V2_OK = f"/v2/docker.io/library/alpine/blobs/{BLOB_DIGEST}"
V2_OTHER = f"/v2/ghcr.io/someone/else/blobs/{BLOB_DIGEST}"


def _grant(w, subject=SUBJECT, rules=("models/org/* pull", "docker.io/library/* pull")):
    w.store.create_principal(subject)
    w.store.set_principal_rules(subject, [parse_rule(r) for r in rules])


def _hf(w, token, path=HF_OK):
    return w.client.get(path, headers={"authorization": f"Bearer {token}"})


def _v2(w, token, path=V2_OK, user="jwt"):
    return w.client.get(path, auth=(user, token))


def _refused(r, reason, token):
    assert r.status_code == 401, (r.status_code, r.text)
    assert r.headers.get("x-xhc-auth-error") == reason, r.headers
    assert token not in r.text
    assert all(token not in v for v in r.headers.values())


# ---------------------------------------------------------------- allowed


def test_a_valid_token_pulls_what_its_principal_is_granted_on_hf(world):
    _grant(world)
    token = world.a.token()
    r = _hf(world, token)
    assert r.status_code == 200 and r.content == BODY
    # Same token, a repo the principal was never granted: the rules applied.
    assert _hf(world, token, HF_OTHER).status_code == 403


def test_a_valid_token_pulls_on_v2_as_the_basic_password_with_any_username(world):
    _grant(world)
    token = world.a.token()
    for user in ("jwt", "anything", ""):
        r = _v2(world, token, user=user)
        assert r.status_code == 200 and r.content == BLOB, user
    assert _v2(world, token, V2_OTHER).status_code == 403


def test_a_valid_token_is_accepted_as_bearer_on_v2(world):
    _grant(world)
    token = world.a.token()
    r = world.client.get(V2_OK, headers={"authorization": f"Bearer {token}"})
    assert r.status_code == 200 and r.content == BLOB


def test_docker_login_succeeds_with_a_token_and_fails_without_a_principal(world):
    token = world.a.token()
    _refused(world.client.get("/v2/", auth=("jwt", token)), "unknown principal", token)
    _grant(world)
    assert world.client.get("/v2/", auth=("jwt", token)).status_code == 200


def test_an_ec_signed_token_verifies(world):
    world.b.add_ec("b-ec", "ec-b")
    _grant(world, "idp:svc-ec")
    token = world.b.token("b-ec", sub="svc-ec")
    assert _hf(world, token).status_code == 200
    assert _v2(world, token).status_code == 200


def test_a_second_audience_in_the_list_is_accepted(world):
    _grant(world, "idp:svc")
    assert _hf(world, world.b.token(sub="svc", aud="other")).status_code == 200


# ---------------------------------------------------------------- refused


def _hs256_with_public_key(issuer: FakeIssuer, kid: str) -> str:
    """The classic confusion: HMAC-sign with the RSA PUBLIC key as the secret.

    Built by hand, because PyJWT rightly refuses to produce it.
    """
    pem = issuer.private[kid].public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": kid}).encode())
    payload = _b64(json.dumps(issuer.claims()).encode())
    sig = hmac.new(pem, f"{header}.{payload}".encode(), hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64(sig)}"


def _alg_none(issuer: FakeIssuer) -> str:
    header = _b64(json.dumps({"alg": "none", "typ": "JWT", "kid": "a1"}).encode())
    payload = _b64(json.dumps(issuer.claims()).encode())
    return f"{header}.{payload}."


def _cases(w):
    a, b = w.a, w.b
    b_kid = next(iter(b.private))
    return {
        "expired": (a.token(exp=int(time.time()) - 3600, iat=int(time.time()) - 7200,
                            nbf=int(time.time()) - 7200), "token expired"),
        "no exp": (a.token(exp=None), "token is missing a required claim"),
        "not yet valid": (a.token(nbf=int(time.time()) + 3600), "token not yet valid"),
        "unconfigured issuer": (a.token(iss="https://evil.example"), "unknown issuer"),
        "wrong audience": (a.token(aud="kubernetes"), "wrong audience"),
        "no audience": (a.token(aud=None), "token is missing a required claim"),
        "bad signature": (a.token("a1", signer=_rsa("attacker")), "bad signature"),
        "alg none": (_alg_none(a), "algorithm not allowed"),
        "hs256 with the public key": (_hs256_with_public_key(a, "a1"),
                                      "algorithm not allowed"),
        "unknown kid": (jwt.encode(a.claims(), a.private["a1"], algorithm="RS256",
                                   headers={"kid": "nope"}), "unknown signing key"),
        # A's own key and A's iss, but B's kid: kids are looked up per issuer.
        "issuer A with issuer B's kid": (
            jwt.encode(a.claims(), a.private["a1"], algorithm="RS256",
                       headers={"kid": b_kid}), "unknown signing key"),
        # B's key and kid, claiming to be A.
        "issuer B's key claiming issuer A": (
            jwt.encode(a.claims(), b.private[b_kid], algorithm="RS256",
                       headers={"kid": b_kid}), "unknown signing key"),
        "PS256 not in B's allowlist": (b.token(alg="PS256", sub="svc"),
                                       "algorithm not allowed"),
        "no kid": (jwt.encode(a.claims(), a.private["a1"], algorithm="RS256"),
                   "unknown signing key"),
    }


CASES = ["expired", "no exp", "not yet valid", "unconfigured issuer", "wrong audience",
         "no audience", "bad signature", "alg none", "hs256 with the public key",
         "unknown kid", "issuer A with issuer B's kid", "issuer B's key claiming issuer A",
         "PS256 not in B's allowlist", "no kid"]


@pytest.mark.parametrize("case", CASES)
def test_every_rejection_is_refused_on_both_surfaces_with_its_reason(world, case):
    _grant(world)
    _grant(world, "idp:svc")
    token, reason = _cases(world)[case]
    _refused(_hf(world, token), reason, token)
    _refused(_v2(world, token), reason, token)
    _refused(world.client.get(V2_OK, headers={"authorization": f"Bearer {token}"}),
             reason, token)


def test_a_non_string_subject_is_refused(world):
    token = jwt.encode({**world.a.claims(), "sub": 42}, world.a.private["a1"],
                       algorithm="RS256", headers={"kid": "a1"})
    r = _hf(world, token)
    assert r.status_code == 401
    assert r.headers["x-xhc-auth-error"] in ("malformed token", "invalid subject",
                                              "invalid token")


def test_an_ec_key_cannot_verify_an_rs256_token(world):
    """Key type must match alg, checked before PyJWT is asked."""
    world.a.add_ec("a-ec", "ec-a")
    header = _b64(json.dumps({"alg": "RS256", "kid": "a-ec"}).encode())
    payload = _b64(json.dumps(world.a.claims()).encode())
    token = f"{header}.{payload}.{_b64(b'x' * 256)}"
    _refused(_hf(world, token), "algorithm not allowed", token)


def test_the_refusal_reason_is_in_the_hf_error_message_huggingface_hub_prints(world):
    token = world.a.token(aud="kubernetes")
    r = _hf(world, token)
    assert r.headers["x-error-message"] == "workload token refused: wrong audience"


# ---------------------------------------------------------------- principals


def test_an_unknown_principal_is_refused(world):
    token = world.a.token()
    _refused(_hf(world, token), "unknown principal", token)
    _refused(_v2(world, token), "unknown principal", token)
    assert world.store.get_principal(SUBJECT) is None


def test_auto_create_makes_an_empty_non_admin_principal_that_is_refused_the_pull(
        world, monkeypatch):
    cfg = {**A_CFG, "auto_create": True}
    monkeypatch.setattr(world.settings, "jwt_issuers", parse(_config(cfg, B_CFG)))
    assert world.store.list_principals() == []   # empty store: the first-admin case
    token = world.a.token()
    r = _hf(world, token)
    assert r.status_code == 403, "authenticated, but granted nothing"
    p = world.store.get_principal(SUBJECT)
    assert p is not None and not p.is_admin and not p.disabled
    assert world.store.get_principal_rules(SUBJECT) == []
    assert _v2(world, token).status_code == 403
    # An administrator grants it; the same token now pulls.
    world.store.set_principal_rules(SUBJECT, [parse_rule("models/org/* pull")])
    assert _hf(world, token).status_code == 200


def test_a_disabled_principal_is_refused_on_the_next_request(world):
    _grant(world)
    token = world.a.token()
    assert _hf(world, token).status_code == 200   # verified AND cached now
    world.store.set_principal_disabled(SUBJECT, True)
    _refused(_hf(world, token), "principal disabled", token)
    _refused(_v2(world, token), "principal disabled", token)


def test_a_principal_disabled_by_another_connection_is_refused(world):
    """Out-of-band administration (authzctl, a second process) must be seen
    through the verification cache, via PRAGMA data_version."""
    from app.authzstore import AuthzStore

    _grant(world)
    token = world.a.token()
    assert _v2(world, token).status_code == 200
    AuthzStore(world.settings.authz_db).set_principal_disabled(SUBJECT, True)
    _refused(_v2(world, token), "principal disabled", token)


def test_a_deleted_principal_is_refused_on_the_next_request(world):
    _grant(world)
    token = world.a.token()
    assert _hf(world, token).status_code == 200
    world.store.delete_principal(SUBJECT)
    _refused(_hf(world, token), "unknown principal", token)


def test_a_token_has_no_scope_and_carries_its_principals_full_grant(world):
    _grant(world)
    key = world.store.principal_credential(SUBJECT)
    assert key is not None and key.scope == [] and key.key_id == f"jwt:{SUBJECT}"


# ---------------------------------------------------------------- key sets


def test_the_jwks_refetch_on_unknown_kids_is_rate_limited(world):
    _grant(world)
    assert _hf(world, world.a.token()).status_code == 200
    assert world.a.jwks_fetches() == 1
    for _ in range(50):
        forged = jwt.encode(world.a.claims(), _rsa("attacker"), algorithm="RS256",
                            headers={"kid": uuid.uuid4().hex})
        assert _hf(world, forged).status_code == 401
    assert world.a.jwks_fetches() == 1, "a flood of invented kids reached the issuer"
    world.clock.advance(world.jwtauth.MIN_REFETCH_S + 1)
    for _ in range(50):
        forged = jwt.encode(world.a.claims(), _rsa("attacker"), algorithm="RS256",
                            headers={"kid": uuid.uuid4().hex})
        _hf(world, forged)
    assert world.a.jwks_fetches() == 2, "one refetch per window, not one per token"


def test_a_rotated_key_is_picked_up_on_its_first_token_after_the_window(world):
    _grant(world)
    assert _hf(world, world.a.token()).status_code == 200
    world.a.add_rsa("a2", "rsa-a2")
    world.clock.advance(world.jwtauth.MIN_REFETCH_S + 1)
    assert _hf(world, world.a.token("a2")).status_code == 200


def test_an_issuer_down_with_a_warm_key_set_keeps_verifying(world):
    _grant(world)
    assert _hf(world, world.a.token()).status_code == 200
    world.a.down = True
    # Past the periodic refresh, so the next token forces a (failing) refetch.
    world.clock.advance(world.jwtauth.JWKS_REFRESH_S + 1)
    before = len(world.a.fetches)
    fresh = world.a.token(jti="new")          # a different token: not cached
    assert _hf(world, fresh).status_code == 200
    assert len(world.a.fetches) > before, "the refresh was attempted and failed"


def test_an_issuer_down_with_a_cold_key_set_is_refused_and_not_hammered(world):
    _grant(world)
    world.a.down = True
    token = world.a.token()
    _refused(_hf(world, token), "issuer keys unavailable", token)
    attempts = len(world.a.fetches)
    for _ in range(20):
        _refused(_hf(world, world.a.token(jti=uuid.uuid4().hex)),
                 "issuer keys unavailable", token)
    assert len(world.a.fetches) == attempts, "a down issuer was retried per request"


def test_a_fetch_timeout_is_a_failure_not_a_hang(world, monkeypatch):
    def slow(request):
        raise httpx.ReadTimeout("timed out", request=request)

    monkeypatch.setattr(world.jwtauth, "_transport", httpx.MockTransport(slow))
    _grant(world)
    token = world.a.token()
    _refused(_hf(world, token), "issuer keys unavailable", token)


def test_a_discovery_document_naming_another_issuer_is_refused(world):
    world.a.discovery_issuer = "https://evil.example"
    _grant(world)
    token = world.a.token()
    _refused(_hf(world, token), "issuer keys unavailable", token)
    assert world.a.jwks_fetches() == 0


@pytest.mark.parametrize("uri", ["file:///etc/passwd", "http://issuer-a.example/jwks"])
def test_a_discovery_document_cannot_point_at_a_file_or_plaintext(world, uri):
    world.a.discovery_jwks_uri = uri
    _grant(world)
    token = world.a.token()
    _refused(_hf(world, token), "issuer keys unavailable", token)


def test_a_file_jwks_is_loaded_at_startup_and_used(world, tmp_path, monkeypatch):
    path = tmp_path / "jwks.json"
    path.write_text(json.dumps(world.a.jwks()))
    cfg = {**A_CFG, "jwks_uri": f"file://{path}"}
    monkeypatch.setattr(world.settings, "jwt_issuers", parse(_config(cfg)))
    world.jwtauth.load()
    world.a.down = True   # never consulted
    _grant(world)
    assert _hf(world, world.a.token()).status_code == 200
    assert world.a.fetches == []


def test_a_missing_or_empty_file_jwks_refuses_to_start(world, tmp_path, monkeypatch):
    cfg = {**A_CFG, "jwks_uri": f"file://{tmp_path}/absent.json"}
    monkeypatch.setattr(world.settings, "jwt_issuers", parse(_config(cfg)))
    with pytest.raises(JWTConfigError, match="absent.json"):
        world.jwtauth.load()
    (tmp_path / "empty.json").write_text('{"keys": [{"kty": "oct", "kid": "x", "k": "c2VjcmV0"}]}')
    cfg = {**A_CFG, "jwks_uri": f"file://{tmp_path}/empty.json"}
    monkeypatch.setattr(world.settings, "jwt_issuers", parse(_config(cfg)))
    with pytest.raises(JWTConfigError, match="no usable signing key"):
        world.jwtauth.load()


# ---------------------------------------------------------------- the cache


def test_one_token_is_verified_once_not_once_per_request(world, monkeypatch):
    _grant(world)
    calls = []
    real = world.jwtauth.jwt.decode

    def counting(token, *a, **k):
        if k.get("options", {}).get("verify_signature", True):
            calls.append(1)
        return real(token, *a, **k)

    monkeypatch.setattr(world.jwtauth.jwt, "decode", counting)
    token = world.a.token()
    for _ in range(20):
        assert _hf(world, token).status_code == 200
    assert len(calls) == 1


def test_the_verification_cache_never_outlives_exp(world, monkeypatch):
    """Real time, not the fake clock: exp is checked by PyJWT against the wall
    clock, so only real elapsed time proves the cached answer is not reused."""
    monkeypatch.setattr(world.settings, "jwt_issuers",
                        parse(_config({**A_CFG, "leeway_s": 0})))
    _grant(world)
    exp = int(time.time()) + 2
    token = world.a.token(exp=exp)
    assert _hf(world, token).status_code == 200
    digest = hashlib.sha256(token.encode()).digest()
    assert world.jwtauth._verified[digest][1] <= exp
    while time.time() <= exp + 0.05:
        time.sleep(0.05)
    _refused(_hf(world, token), "token expired", token)


def test_the_verification_cache_expires_at_its_ttl(world):
    _grant(world)
    token = world.a.token()
    assert _hf(world, token).status_code == 200
    digest = hashlib.sha256(token.encode()).digest()
    assert digest in world.jwtauth._verified
    world.clock.advance(61)
    assert world.jwtauth._cache_get(digest) is None


# ---------------------------------------------------------------- boundaries


def test_a_valid_token_is_refused_on_cache_management(world):
    """/_cache takes XHC_MANAGE_TOKEN and nothing else."""
    _grant(world)
    token = world.a.token()
    assert _hf(world, token).status_code == 200    # positive control: token is good
    h = {"authorization": f"Bearer {token}"}
    assert world.client.get("/_cache/status", headers=h).status_code == 401
    assert world.client.get("/_cache/authz/principals", headers=h).status_code == 401
    assert world.client.get(
        "/_cache/status", headers={"authorization": "Bearer mt"}).status_code == 200


def test_a_key_still_works_unchanged_alongside_jwt_config(world):
    world.store.create_principal("svc:ci")
    world.store.set_principal_rules("svc:ci", [parse_rule("*")])
    key_id, secret = new_secret()
    world.store.add_key(key_id, secret, "svc:ci", [])
    assert world.client.get(HF_OK, headers={
        "authorization": f"Bearer {key_id}:{secret}"}).status_code == 200
    assert world.client.get(V2_OK, auth=(key_id, secret)).status_code == 200
    assert world.client.get(V2_OK, auth=(key_id, "wrong")).status_code == 401


def test_a_jwt_shaped_password_is_never_tried_as_a_key(world):
    """The reason on the wire is the JWT verifier's, not a generic key failure."""
    token = world.a.token(iss="https://evil.example")
    r = _v2(world, token, user="deadbeefdeadbeef")
    _refused(r, "unknown issuer", token)


def test_without_jwt_config_a_jwt_is_just_an_unknown_key(world, monkeypatch):
    monkeypatch.setattr(world.settings, "jwt_issuers", ())
    _grant(world)
    token = world.a.token()
    r = _v2(world, token)
    assert r.status_code == 401 and "x-xhc-auth-error" not in r.headers
    assert _hf(world, token).status_code == 401


def test_subjects_are_encoded_so_they_are_storable_and_cannot_collide(world, monkeypatch):
    cfg = {"issuer": ISS_A, "audience": "muninn", "subject_template": "gha:{sub}"}
    (issuer,) = parse(_config(cfg))
    assert issuer.map_subject("repo:org/name:ref:refs/heads/main") == \
        "gha:repo:org%2Fname:ref:refs%2Fheads%2Fmain"
    assert issuer.map_subject("a%2Fb") != issuer.map_subject("a/b")
    monkeypatch.setattr(world.settings, "jwt_issuers", (issuer,))
    subject = issuer.map_subject("repo:org/name:ref:refs/heads/main")
    _grant(world, subject)
    token = world.a.token(sub="repo:org/name:ref:refs/heads/main")
    assert _hf(world, token).status_code == 200


# ---------------------------------------------------------------- configuration


@pytest.mark.parametrize("entry,message", [
    ({"issuer": ISS_A, "subject_template": "k8s:{sub}"}, "'audience' is required"),
    ({**A_CFG, "audience": []}, "'audience' is required"),
    ({**A_CFG, "algorithms": ["none"]}, "must not contain 'none'"),
    ({**A_CFG, "algorithms": ["HS256"]}, "HMAC algorithms are not supported"),
    ({**A_CFG, "algorithms": ["RS257"]}, "unknown 'RS257'"),
    ({**A_CFG, "audiance": "x"}, "unknown key"),
    ({**A_CFG, "subject_template": "{sub}"}, "literal prefix"),
    ({**A_CFG, "subject_template": "k8s:"}, "exactly once"),
    ({**A_CFG, "subject_template": "k8s/{sub}"}, "must not contain '/'"),
    ({**A_CFG, "subject_template": "k8s:{name}"}, "unknown placeholder"),
    ({"audience": "m", "subject_template": "k8s:{sub}"}, "'issuer' is required"),
    ({**A_CFG, "issuer": "http://plain.example"}, "set 'jwks_uri'"),
    ({**A_CFG, "jwks_uri": "http://plain.example/jwks"}, "must be https"),
    ({**A_CFG, "jwks_uri": "file://relative/path"}, "must be https"),
    ({**A_CFG, "leeway_s": 3600}, "'leeway_s' must be between"),
    ({**A_CFG, "auto_create": "yes"}, "true or false"),
    ({**A_CFG, "ca_file": "relative.pem"}, "absolute path"),
])
def test_malformed_issuer_config_is_refused_by_name(entry, message):
    with pytest.raises(JWTConfigError, match=message):
        parse(_config(entry))


def test_duplicate_issuers_and_colliding_prefixes_are_refused():
    with pytest.raises(JWTConfigError, match="twice"):
        parse(_config(A_CFG, A_CFG))
    with pytest.raises(JWTConfigError, match="one a prefix of the other"):
        parse(_config(A_CFG, {**B_CFG, "subject_template": "k8s:x{sub}"}))
    with pytest.raises(JWTConfigError, match="not valid JSON"):
        parse("[{")


def test_unset_is_off_and_an_explicit_issuer_needs_the_store(monkeypatch):
    from app.config import Settings

    monkeypatch.delenv("XHC_JWT_ISSUERS", raising=False)
    assert Settings.from_env().jwt_issuers == ()
    monkeypatch.setenv("XHC_JWT_ISSUERS", _config(A_CFG))
    monkeypatch.delenv("XHC_AUTHZ_DB", raising=False)
    with pytest.raises(ValueError, match="XHC_JWT_ISSUERS needs XHC_AUTHZ_DB"):
        Settings.from_env()
