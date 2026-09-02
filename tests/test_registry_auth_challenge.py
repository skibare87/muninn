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

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import registry


@pytest.fixture(autouse=True)
def _isolate_remembered_upstreams():
    """`_basic_upstreams` is module-level and now persists between requests by
    design. Without this, one test teaching the module that an upstream uses
    Basic makes a later test see credentials sent unasked -- which is exactly
    the property the later test exists to protect.
    """
    registry._basic_upstreams.clear()
    yield
    registry._basic_upstreams.clear()


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
        # Model a server, not a counter: it challenges an UNAUTHENTICATED
        # request and accepts an authenticated one. Keying off the request
        # number instead made a preemptively-authenticated request still get
        # a 401, which is not something any registry does.
        if "authorization" in {k.lower() for k in req.headers}:
            return _FakeResponse(200)
        return _FakeResponse(401, {"www-authenticate": self.challenge})


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
    async def _no_token(ref, challenge, out=None):
        if out is not None:
            out["token"] = "unreachable"
        return None

    monkeypatch.setattr(registry, "_fetch_token", _no_token)
    resp, sent = _run_authed(
        monkeypatch, 'Bearer realm="https://auth.example.com/token"', "Zm9vOmJhcg=="
    )
    assert resp.status_code == 401
    assert len(sent) == 1


# --------------------------------------------------------------------------
# Preemptive Basic after a first challenge.
#
# Challenge-response means sending the whole body, being told 401, and sending
# it again. For a small body that is wasteful. For a large one it BREAKS: the
# server answers 401 and closes as soon as it has the headers, without draining
# the body, so the remaining writes fail and httpx raises ReadError -- fast,
# and nothing to do with size limits or timeouts.
#
# Measured against a server that 401s without draining: a 64 KiB body gets a
# clean 401 because it fits in socket buffers; a 3 MiB body raises ReadError.
# That is the reported asymmetry exactly -- a small config blob succeeding
# while a 3 MiB layer failed 0.6s into a freshly opened session.
# --------------------------------------------------------------------------

def test_credentials_go_preemptively_only_after_a_challenge(monkeypatch):
    """The property that keeps this safe.

    A registry that has never challenged us must still not receive
    credentials, or configuring an auth file leaks them to every upstream.
    """
    registry._basic_upstreams.clear()
    monkeypatch.setattr(registry, "_basic_for", lambda u: "dTpw")

    _, sent = _run_authed(monkeypatch, 'Basic realm="r"', "dTpw")
    assert "authorization" not in sent[0], \
        "credentials were sent to an upstream that had not asked"
    assert sent[1]["authorization"] == "Basic dTpw"
    assert "registry.example.com" in registry._basic_upstreams, \
        "the challenge was not remembered, so the next large upload still breaks"


def test_a_remembered_upstream_gets_credentials_on_the_first_request(monkeypatch):
    """The fix itself: no second send, so no body sent twice and no 401
    arriving mid-upload."""
    registry._basic_upstreams.clear()
    registry._basic_upstreams.add("registry.example.com")
    monkeypatch.setattr(registry, "_basic_for", lambda u: "dTpw")

    resp, sent = _run_authed(monkeypatch, 'Basic realm="r"', "dTpw")
    assert sent[0].get("authorization") == "Basic dTpw"
    assert len(sent) == 1, "a remembered upstream still round-tripped a challenge"


def test_clearing_tokens_also_forgets_basic_upstreams():
    registry._basic_upstreams.add("x.example.com")
    registry.clear_tokens()
    assert not registry._basic_upstreams


# ---------------------------------------------------------------------------
# Docker Hub refuses to leak existence: for a repo that does NOT exist it
# issues a perfectly valid anonymous token, then answers 401 to the request
# carrying it. `out["authenticated"]` is what lets the caller tell that
# answer apart from "the cache could not authenticate at all".
# ---------------------------------------------------------------------------


class _RefusingClient(_FakeClient):
    """Challenges, then refuses again even with a valid token -- the shape a
    registry uses to avoid confirming whether a private repo exists."""

    async def send(self, req, stream=False):
        self.sent.append(dict(req.headers))
        return _FakeResponse(401, {"www-authenticate": self.challenge})


def test_anonymous_token_then_401_still_counts_as_authenticated(monkeypatch):
    """The wiring, not the rendering. _unavailable can only tell a typo from a
    broken cache if this flag is actually set on the real bearer path."""
    client = _RefusingClient('Bearer realm="https://auth.docker.io/token",service="registry.docker.io"')
    monkeypatch.setattr(registry, "_get_client", lambda: client)
    monkeypatch.setattr(registry, "_cached_token", lambda ref: None)
    monkeypatch.setattr(registry, "_basic_for", lambda upstream: None)

    async def _token(ref, challenge, out=None):
        # `out` mirrors the real signature: _fetch_token reports WHY it failed
        # so the caller can tell a definitive refusal from no answer at all.
        if out is not None:
            out["token"] = "ok"
        return "an-anonymous-token"

    monkeypatch.setattr(registry, "_fetch_token", _token)
    ref = registry.Ref(upstream="docker.io",
                       api="https://registry-1.docker.io",
                       repo="library/definitely-not-real-xyzzy")
    out: dict = {}
    resp = asyncio.run(
        registry._authed_request("GET", f"{ref.api}/v2/{ref.repo}/manifests/latest",
                                 ref, {}, stream=False, out=out)
    )
    assert resp.status_code == 401, "the registry refused even with the token"
    assert len(client.sent) == 2, "the token was never presented"
    assert client.sent[1].get("authorization") == "Bearer an-anonymous-token"
    assert out["authenticated"] is True, (
        "a token was obtained and presented; the 401 is the registry's ANSWER, "
        "and reporting it as a gateway failure is what made a typo'd image "
        "name render as 502 Bad Gateway"
    )


def test_unsatisfiable_challenge_leaves_authenticated_false(monkeypatch):
    """No token, no credentials: the cache really could not authenticate."""
    client = _FakeClient('Basic realm="whatever"')
    monkeypatch.setattr(registry, "_get_client", lambda: client)
    monkeypatch.setattr(registry, "_basic_for", lambda upstream: None)
    monkeypatch.setattr(registry, "_cached_token", lambda ref: None)
    ref = registry.Ref(upstream="r.example.com", api="https://r.example.com",
                       repo="team/image")
    out: dict = {}
    resp = asyncio.run(
        registry._authed_request("GET", f"{ref.api}/v2/{ref.repo}/manifests/latest",
                                 ref, {}, stream=False, out=out)
    )
    assert resp.status_code == 401
    assert out["authenticated"] is False


# ---------------------------------------------------------------------------
# The wiring: _fetch_token used to return None for five distinct
# states, so the caller could not tell a definitive refusal from no answer.
# ---------------------------------------------------------------------------


class _TokenEndpointClient:
    """A client whose TOKEN endpoint answers with a chosen status."""

    def __init__(self, token_status):
        self.token_status = token_status
        self.sent = []

    def build_request(self, method, url, headers=None, content=None):
        return types.SimpleNamespace(method=method, url=url,
                                     headers=headers or {}, content=content)

    async def send(self, req, stream=False):
        self.sent.append(str(req.url))
        return _FakeResponse(401, {"www-authenticate":
                                   'Bearer realm="https://auth.example.com/token"'})

    async def get(self, url, params=None, headers=None):
        return _FakeResponse(self.token_status)


def _token_state(monkeypatch, status):
    client = _TokenEndpointClient(status)
    monkeypatch.setattr(registry, "_get_client", lambda: client)
    monkeypatch.setattr(registry, "_cached_token", lambda ref: None)
    monkeypatch.setattr(registry, "_basic_for", lambda upstream: None)
    ref = registry.Ref(upstream="ghcr.io", api="https://ghcr.io", repo="org/img")
    out: dict = {}
    asyncio.run(registry._authed_request(
        "GET", f"{ref.api}/v2/{ref.repo}/manifests/latest", ref, {},
        stream=False, out=out))
    return out


def test_token_endpoint_403_is_recorded_as_declined(monkeypatch):
    """ghcr's actual behaviour for a nonexistent repo, measured."""
    assert _token_state(monkeypatch, 403).get("token") == "declined"


def test_token_endpoint_401_is_recorded_as_declined(monkeypatch):
    assert _token_state(monkeypatch, 401).get("token") == "declined"


def test_token_endpoint_500_is_recorded_as_unreachable(monkeypatch):
    """A 5xx is NOT an answer. Collapsing it into `declined` would turn an
    upstream outage into a 404, which is the defect in reverse."""
    assert _token_state(monkeypatch, 500).get("token") == "unreachable"


def test_token_endpoint_429_is_recorded_as_unreachable(monkeypatch):
    """Rate limiting is not a statement about whether the repo exists."""
    assert _token_state(monkeypatch, 429).get("token") == "unreachable"
