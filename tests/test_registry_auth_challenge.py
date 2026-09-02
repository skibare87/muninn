"""Telling a Basic auth challenge from a Bearer one (an internal issue).

The guard in `_authed_request` exists to do exactly one thing: decide whether a
401 carries a challenge we can satisfy with the bearer token dance. It read

    if (challenge.get("Bearer") is None) and "realm" not in challenge:

and `challenge` came from a regex matching only key="value" pairs. The scheme
token -- `Basic` or `Bearer` -- has no ="value", so it was NEVER a key, so
`challenge.get("Bearer")` was ALWAYS None and the condition collapsed to "has a
realm". Every Basic challenge entered the bearer dance.

That was invisible for as long as every upstream put a URL in its realm: the
token request simply 401'd and the caller moved on. One registry sends
`Basic realm="Authorization Required"`, httpx read that as a RELATIVE url, and
urllib raised ValueError from inside httpx's cookie handling -- which
`except httpx.HTTPError` does not catch. A 500, from a guard that had never
worked, on the one upstream whose realm happened not to be a URL.

So these tests assert on the DISCRIMINATION, not on the symptom. Testing that
the 500 is gone would pass with the realm validation alone while the guard
stayed broken and every Basic challenge kept making a pointless token request.
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import registry


def _enters_bearer_dance(header: str) -> bool:
    """Mirror of the guard in _authed_request, in the same direction."""
    challenge = registry._parse_challenge(header)
    return not (challenge.get("_scheme") != "bearer" or "realm" not in challenge)


def test_scheme_is_parsed_at_all():
    # The whole defect in one assertion: the scheme was never captured.
    assert registry._parse_challenge('Basic realm="x"')["_scheme"] == "basic"
    assert registry._parse_challenge('Bearer realm="x"')["_scheme"] == "bearer"


def test_basic_challenge_never_enters_the_bearer_dance():
    # Both real-world forms, including the one whose realm is a URL and which
    # therefore failed silently rather than loudly.
    assert not _enters_bearer_dance('Basic realm="Authorization Required"')
    assert not _enters_bearer_dance('Basic realm="https://registry.example.com/v2/"')


def test_bearer_challenge_still_does():
    assert _enters_bearer_dance(
        'Bearer realm="https://auth.docker.io/token",service="registry.docker.io"'
    )


def test_bearer_without_a_realm_is_not_actionable():
    assert not _enters_bearer_dance('Bearer service="registry.docker.io"')


def test_absent_or_empty_header():
    assert not _enters_bearer_dance("")
    assert registry._parse_challenge("") == {}


def test_scheme_matching_is_case_insensitive():
    # RFC 7235 auth-scheme is case-insensitive; registries vary.
    assert _enters_bearer_dance('bearer realm="https://auth.example.com/token"')
    assert not _enters_bearer_dance('BASIC realm="https://auth.example.com/"')


def test_scheme_key_cannot_collide_with_a_real_parameter():
    # `_scheme` is underscore-prefixed precisely so a registry sending its own
    # `scheme="..."` parameter cannot overwrite our discrimination.
    parsed = registry._parse_challenge('Bearer scheme="something",realm="https://a/b"')
    assert parsed["_scheme"] == "bearer"
    assert parsed["scheme"] == "something"


def test_realm_must_be_an_absolute_http_url():
    """The second line of defence, asserted independently of the guard.

    A registry controls this string. Anything that is not an absolute http(s)
    URL must be refused BEFORE it reaches httpx, rather than caught after.
    """
    def usable(realm):
        parts = urlparse(realm)
        return parts.scheme in ("http", "https") and bool(parts.netloc)

    assert not usable("Authorization Required")   # the actual 500
    assert not usable("/token")                   # relative
    assert not usable("ftp://host/token")         # wrong scheme
    assert not usable("")
    assert usable("https://auth.docker.io/token")
    assert usable("http://registry.internal:5000/token")


# --------------------------------------------------------------------------
# The Basic RETRY (an internal issue, second half).
#
# Telling Basic from Bearer was only half the defect. `_basic_for()` -- the
# function that turns a mounted XHC_REGISTRY_AUTH_FILE into an Authorization
# header -- was called from exactly ONE place: inside _fetch_token, to
# authenticate to a bearer TOKEN ENDPOINT. No code path ever put
# `Authorization: Basic` on a registry request.
#
# So against a registry speaking plain Basic with no token endpoint, the
# credentials loaded, logged at startup, sat in memory, and were never sent.
# A documented feature that had never worked on any release. Verified
# end-to-end against the real upstream: 401 on v0.6.1, 200 with the retry,
# same credential file.
#
# These drive coroutines with asyncio.run rather than pytest-asyncio, matching
# tests/test_request_flags.py -- bare `async def` tests are SKIPPED with a
# warning, which is a green run that asserted nothing.
# --------------------------------------------------------------------------

import asyncio
import types


class _FakeResponse:
    def __init__(self, status_code, headers=None):
        self.status_code = status_code
        self.headers = headers or {}

    async def aread(self):
        return b""

    async def aclose(self):
        return None


class _FakeClient:
    """Answers 401+challenge once, then echoes whatever auth arrived next."""

    def __init__(self, challenge):
        self.challenge = challenge
        self.sent = []

    def build_request(self, method, url, headers=None, content=None):
        # `content` mirrors httpx: the push path sends bodies through the same
        # auth dance, and a fake that did not accept it would pass while the
        # real client raised.
        return types.SimpleNamespace(method=method, url=url,
                                     headers=headers or {}, content=content)

    async def send(self, req, stream=False):
        self.sent.append(dict(req.headers))
        if len(self.sent) == 1:
            return _FakeResponse(401, {"www-authenticate": self.challenge})
        return _FakeResponse(200)


def _run_authed(monkeypatch, challenge, creds):
    client = _FakeClient(challenge)
    monkeypatch.setattr(registry, "_get_client", lambda: client)
    monkeypatch.setattr(registry, "_basic_for", lambda upstream: creds)
    monkeypatch.setattr(registry, "_cached_token", lambda ref: None)
    ref = registry.Ref(
        upstream="registry.example.com",
        api="https://registry.example.com",
        repo="team/image",
    )
    resp = asyncio.run(
        registry._authed_request("GET", f"{ref.api}/v2/{ref.repo}/manifests/latest",
                                 ref, {}, stream=False)
    )
    return resp, client.sent


def test_basic_challenge_is_retried_with_credentials(monkeypatch):
    """The defect in one assertion: a second request, carrying Basic."""
    resp, sent = _run_authed(monkeypatch, 'Basic realm="Authorization Required"', "Zm9vOmJhcg==")
    assert resp.status_code == 200
    assert len(sent) == 2, "no retry was attempted"
    assert sent[1].get("authorization") == "Basic Zm9vOmJhcg=="


def test_basic_retry_works_when_the_realm_is_not_a_url(monkeypatch):
    """The realm is irrelevant to Basic -- it must not gate the retry.

    A realm that is not a URL is what crashed the bearer path. It must not now
    prevent the Basic path from working.
    """
    resp, sent = _run_authed(monkeypatch, 'Basic realm="Authorization Required"', "eDp5")
    assert resp.status_code == 200 and len(sent) == 2


def test_no_credentials_means_hand_back_the_registrys_own_401(monkeypatch):
    """Absent credentials must not be papered over with an invented answer."""
    resp, sent = _run_authed(monkeypatch, 'Basic realm="whatever"', None)
    assert resp.status_code == 401
    assert len(sent) == 1, "retried without credentials to send"


def test_credentials_are_never_sent_unasked(monkeypatch):
    """Challenge-response, not preemptive.

    Configuring an auth file must not leak credentials to an upstream that
    never challenged. The FIRST request carries no authorization.
    """
    _, sent = _run_authed(monkeypatch, 'Basic realm="x"', "c2VjcmV0")
    assert "authorization" not in sent[0]


def test_a_bearer_challenge_does_not_take_the_basic_path(monkeypatch):
    """Guards the over-correction: Bearer must still do the token dance.

    _fetch_token is stubbed to fail, so a Bearer challenge yields the original
    401. If Bearer wrongly fell into the Basic branch this would be a 200.
    """
    async def _no_token(ref, challenge):
        return None

    monkeypatch.setattr(registry, "_fetch_token", _no_token)
    resp, sent = _run_authed(
        monkeypatch, 'Bearer realm="https://auth.example.com/token"', "Zm9vOmJhcg=="
    )
    assert resp.status_code == 401
    assert len(sent) == 1
