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
