"""The OIDC login flow.

Every test here is about a REFUSAL, except one positive control. That weighting is
deliberate: a login flow that accepts a valid token is easy and is not where the
bugs are. The bugs are in what it ALSO accepts -- a token for another application,
a replayed callback, a forged state, a token signed with the wrong algorithm.

A real provider is not needed for any of this. The flow is exercised against a
locally generated RSA key so the tests can mint tokens that are genuinely signed
but deliberately wrong in one respect at a time.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.oidc import Identity, OIDCClient, OIDCError

ISSUER = "https://idp.example.com"
CLIENT_ID = "muninn-test"


@pytest.fixture(scope="module")
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def mint(key, *, aud=CLIENT_ID, iss=ISSUER, sub="user-1", nonce="n", **extra) -> str:
    now = int(time.time())
    claims = {"sub": sub, "aud": aud, "iss": iss, "iat": now,
              "exp": now + 300, "nonce": nonce, "email": "a@example.com"}
    claims.update(extra)
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test-key"})


class FakeClient(OIDCClient):
    """Stands in for the provider: fixed metadata, a fixed signing key, and a
    token response we control. Everything else is the real implementation."""

    def __init__(self, key, token_response):
        super().__init__(ISSUER, CLIENT_ID, "secret", "https://cache.example/cb")
        self._key = key
        self._token_response = token_response
        self._meta = {
            "issuer": ISSUER,
            "authorization_endpoint": f"{ISSUER}/authorize",
            "token_endpoint": f"{ISSUER}/token",
            "jwks_uri": f"{ISSUER}/jwks",
        }

    async def _jwk_client(self):
        pub = self._key.public_key()

        class _K:
            key = pub

        class _C:
            def get_signing_key_from_jwt(self, _token):
                return _K()

        return _C()

    async def _client(self):
        outer = self

        class _Resp:
            status_code = 200

            def json(self):
                return outer._token_response

        class _HTTP:
            async def post(self, *a, **k):
                return _Resp()

        return _HTTP()


async def begin(client: OIDCClient) -> tuple[str, str]:
    """Start a login and return (state, nonce) from the client's own pending map."""
    await client.authorization_url()
    state = next(iter(client._pending))
    nonce = client._pending[state][0]
    return state, nonce


# ---------------- the positive control ----------------

@pytest.mark.asyncio
async def test_a_correct_login_succeeds(key):
    """Without this, a flow that refuses everything would pass every test below."""
    c = FakeClient(key, {})
    state, nonce = await begin(c)
    c._token_response = {"id_token": mint(key, nonce=nonce)}
    ident = await c.complete("code", state)
    assert isinstance(ident, Identity)
    assert ident.subject == "user-1"
    assert ident.email == "a@example.com"


# ---------------- the refusals ----------------

@pytest.mark.asyncio
async def test_a_token_for_another_application_is_refused(key):
    """THE aud CHECK. A genuine, correctly signed token issued for a DIFFERENT
    application on the same provider. Verifying the signature and not the
    audience accepts it -- the token is authentic and simply not for us.
    """
    c = FakeClient(key, {})
    state, nonce = await begin(c)
    c._token_response = {"id_token": mint(key, aud="some-other-app", nonce=nonce)}
    with pytest.raises(OIDCError):
        await c.complete("code", state)


@pytest.mark.asyncio
async def test_a_token_from_another_issuer_is_refused(key):
    c = FakeClient(key, {})
    state, nonce = await begin(c)
    c._token_response = {"id_token": mint(key, iss="https://evil.example", nonce=nonce)}
    with pytest.raises(OIDCError):
        await c.complete("code", state)


@pytest.mark.asyncio
async def test_a_replayed_token_with_the_wrong_nonce_is_refused(key):
    """The nonce binds the token to THIS login. Without it, a token captured from
    an earlier session is accepted on a later one."""
    c = FakeClient(key, {})
    state, _ = await begin(c)
    c._token_response = {"id_token": mint(key, nonce="a-different-login")}
    with pytest.raises(OIDCError, match="nonce"):
        await c.complete("code", state)


@pytest.mark.asyncio
async def test_an_unknown_state_is_refused(key):
    """CSRF. A callback the server never initiated must not complete a login."""
    c = FakeClient(key, {"id_token": mint(key)})
    with pytest.raises(OIDCError, match="state"):
        await c.complete("code", "a-state-we-never-issued")


@pytest.mark.asyncio
async def test_a_state_is_single_use(key):
    """A replayed callback must fail even when the code is still live upstream."""
    c = FakeClient(key, {})
    state, nonce = await begin(c)
    c._token_response = {"id_token": mint(key, nonce=nonce)}
    await c.complete("code", state)
    with pytest.raises(OIDCError, match="state"):
        await c.complete("code", state)


@pytest.mark.asyncio
async def test_an_expired_token_is_refused(key):
    c = FakeClient(key, {})
    state, nonce = await begin(c)
    now = int(time.time())
    c._token_response = {"id_token": jwt.encode(
        {"sub": "u", "aud": CLIENT_ID, "iss": ISSUER, "iat": now - 7200,
         "exp": now - 3600, "nonce": nonce},
        key, algorithm="RS256")}
    with pytest.raises(OIDCError):
        await c.complete("code", state)


@pytest.mark.asyncio
async def test_an_unsigned_token_is_refused(key):
    """alg=none. The reason algorithms are an explicit allow-list rather than read
    from the token's own header."""
    c = FakeClient(key, {})
    state, nonce = await begin(c)
    now = int(time.time())
    c._token_response = {"id_token": jwt.encode(
        {"sub": "u", "aud": CLIENT_ID, "iss": ISSUER, "iat": now,
         "exp": now + 300, "nonce": nonce},
        key=None, algorithm="none")}
    with pytest.raises(OIDCError):
        await c.complete("code", state)


@pytest.mark.asyncio
async def test_a_token_missing_sub_is_refused(key):
    """sub is the identity. A token without one cannot name a user, and falling
    back to email would key the account on a changeable attribute."""
    c = FakeClient(key, {})
    state, nonce = await begin(c)
    now = int(time.time())
    c._token_response = {"id_token": jwt.encode(
        {"aud": CLIENT_ID, "iss": ISSUER, "iat": now, "exp": now + 300, "nonce": nonce},
        key, algorithm="RS256")}
    with pytest.raises(OIDCError):
        await c.complete("code", state)


@pytest.mark.asyncio
async def test_a_response_with_no_id_token_is_refused(key):
    c = FakeClient(key, {"access_token": "something"})
    state, _ = await begin(c)
    with pytest.raises(OIDCError, match="id_token"):
        await c.complete("code", state)


# ---------------- the request side ----------------

@pytest.mark.asyncio
async def test_the_authorization_url_carries_pkce_state_and_nonce(key):
    from urllib.parse import parse_qs, urlparse

    c = FakeClient(key, {})
    url = await c.authorization_url()
    q = parse_qs(urlparse(url).query)
    assert q["response_type"] == ["code"]
    assert q["code_challenge_method"] == ["S256"], "plain PKCE is not protection"
    assert q["code_challenge"][0]
    assert q["state"][0] and q["nonce"][0]
    assert "client_secret" not in q, "the secret must never reach the browser"


@pytest.mark.asyncio
async def test_each_login_gets_fresh_state_nonce_and_verifier(key):
    c = FakeClient(key, {})
    for _ in range(25):
        await c.authorization_url()
    states = set(c._pending)
    nonces = {n for n, _, _ in c._pending.values()}
    verifiers = {v for _, v, _ in c._pending.values()}
    assert len(states) == len(nonces) == len(verifiers) == 25


@pytest.mark.asyncio
async def test_abandoned_logins_are_swept(key, monkeypatch):
    """Pending state is how long a stolen state value stays useful."""
    import app.oidc as mod

    c = FakeClient(key, {})
    await c.authorization_url()
    assert len(c._pending) == 1
    # capture the real clock BEFORE patching -- app.oidc.time IS this module's
    # time, so a lambda calling time.time() would call its own replacement
    later = time.time() + mod._LOGIN_TTL_S + 60
    monkeypatch.setattr(mod.time, "time", lambda: later)
    await c.authorization_url()
    assert len(c._pending) == 1, "the abandoned login should have been swept"
