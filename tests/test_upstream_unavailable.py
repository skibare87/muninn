"""Three upstream states that used to render as one 404 (an internal issue).

    upstream 401, no credentials configured  -> docker login on the cache host
    upstream 401, credentials rejected       -> wrong/expired/unscoped creds
    upstream 404                             -> genuinely not there

Three different fixes, potentially three different people, one status code. The
second state only became reachable in v0.6.2; before that nothing was ever sent,
so every 401 was the first case.

WHY THE STATUS MATTERS MORE THAN THE BODY. The docker CLI discards the body and
headers and prints only the status. Measured against docker 29.5.1:

    404 -> "not found"                                <- the status is ERASED
    401 -> "unexpected status ...: 401 Unauthorized"
    502 -> "unexpected status ...: 502 Bad Gateway"
    403 -> "unexpected status ...: 403 Forbidden"

404 is the only status that hides itself, which is why an auth failure must not
wear one: a colleague read "not found" and went looking for a missing image on a
registry they could see it in. A perfect explanation in a body nobody renders
fixes nothing.

401 is RESERVED for Muninn's own client-facing auth (an internal issue). "Authenticate to
the cache" and "the cache cannot authenticate upstream" are different actors
with different fixes, and giving them the same status would recreate this ticket
one layer up.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json

from app import ocicompat, registry


def _ref(upstream="registry.example.com"):
    return registry.Ref(upstream=upstream, api=f"https://{upstream}", repo="team/image")


def _body(resp):
    return json.loads(bytes(resp.body).decode())["errors"][0]


def test_upstream_404_stays_404():
    """Genuinely absent. 'not found' is the correct thing for docker to print."""
    r = ocicompat._unavailable(_ref(), 404, "latest", "manifest")
    assert r.status_code == 404
    assert _body(r)["code"] == "MANIFEST_UNKNOWN"
    assert r.headers["x-xhc-upstream-auth"] == "n/a"


def test_upstream_401_without_credentials_is_502_and_says_where_to_log_in(monkeypatch):
    monkeypatch.setattr(registry, "has_credentials", lambda upstream: False)
    r = ocicompat._unavailable(_ref(), 401, "latest", "manifest")
    assert r.status_code == 502, "404 would be erased to 'not found' by the docker CLI"
    assert r.headers["x-xhc-upstream-auth"] == "unconfigured"
    msg = _body(r)["message"]
    assert "docker login registry.example.com" in msg
    # The actor must be unambiguous: the fix is on the CACHE, not the client.
    assert "cache host" in msg
    assert "does not forward your" in msg


def test_upstream_401_with_credentials_is_a_different_diagnosis(monkeypatch):
    """Only reachable since v0.6.2. Different fix, different message."""
    monkeypatch.setattr(registry, "has_credentials", lambda upstream: True)
    r = ocicompat._unavailable(_ref(), 401, "latest", "manifest")
    assert r.status_code == 502
    assert r.headers["x-xhc-upstream-auth"] == "rejected"
    msg = _body(r)["message"]
    assert "rejected the credentials" in msg
    assert "docker login" not in msg, "wrong fix: they ARE logged in"


def test_the_two_401_states_are_actually_distinguishable(monkeypatch):
    """The point of the ticket, asserted directly rather than implied.

    Two tests each passing separately would not catch a helper that returned the
    same message for both.
    """
    monkeypatch.setattr(registry, "has_credentials", lambda upstream: False)
    unconfigured = _body(ocicompat._unavailable(_ref(), 401, "latest", "manifest"))
    monkeypatch.setattr(registry, "has_credentials", lambda upstream: True)
    rejected = _body(ocicompat._unavailable(_ref(), 401, "latest", "manifest"))
    assert unconfigured["message"] != rejected["message"]


def test_401_is_never_returned_for_an_upstream_failure(monkeypatch):
    """Reserved for Muninn's own auth (an internal issue)."""
    for configured in (True, False):
        monkeypatch.setattr(
            registry, "has_credentials",
            lambda upstream, c=configured: c,
        )
        for status in (401, 404, 500, None):
            r = ocicompat._unavailable(_ref(), status, "latest", "manifest")
            assert r.status_code != 401


def test_no_challenge_is_ever_emitted(monkeypatch):
    """A challenge invites the client to authenticate to the wrong party.

    Upstream's own realm is not proxied by Muninn, so a client that acts on it
    loops with no exit; and after an internal issue it would be indistinguishable from
    Muninn's own challenge.
    """
    for configured in (True, False):
        monkeypatch.setattr(
            registry, "has_credentials",
            lambda upstream, c=configured: c,
        )
        r = ocicompat._unavailable(_ref(), 401, "latest", "manifest")
        assert "www-authenticate" not in {k.lower() for k in r.headers}


def test_blobs_get_the_same_treatment_with_their_own_code(monkeypatch):
    """Blobs and manifests disagreed before this: blobs passed a 401 through
    (forwarding upstream's challenge), manifests collapsed to 404."""
    monkeypatch.setattr(registry, "has_credentials", lambda upstream: False)
    r = ocicompat._unavailable(_ref(), 401, "sha256:abc", "blob")
    assert r.status_code == 502
    r404 = ocicompat._unavailable(_ref(), 404, "sha256:abc", "blob")
    assert r404.status_code == 404 and _body(r404)["code"] == "BLOB_UNKNOWN"


def test_upstream_status_is_always_reported_for_logs_and_curl():
    """The status is for the docker user; the header is for everyone else."""
    r = ocicompat._unavailable(_ref(), 404, "latest", "manifest")
    assert r.headers["x-xhc-upstream-status"] == "404"
    r = ocicompat._unavailable(_ref(), None, "latest", "manifest")
    assert r.headers["x-xhc-upstream-status"] == "none"
