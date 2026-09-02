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


# ---------------------------------------------------------------------------
# A registry that refuses to leak existence answers 401 for a repo that does
# not exist. Before this, that rendered as 502 Bad Gateway -- so the single
# most common user error, a typo'd image name, accused the infrastructure.
# ---------------------------------------------------------------------------


def test_authenticated_then_refused_is_404_not_502():
    """Docker Hub issues a valid anonymous token for a nonexistent repo, then
    401s the request carrying it. We authenticated fine; the registry answered.
    That is 'not there', not 'the gateway is broken'."""
    ref = registry.resolve("library/definitely-not-real-xyzzy")
    r = ocicompat._unavailable(
        ref, {"status": 401, "authenticated": True}, "latest", "manifest")
    assert r.status_code == 404
    assert r.headers["x-xhc-upstream-auth"] == "anonymous-refused"


def test_credentials_configured_and_refused_is_still_502(monkeypatch):
    """The case the three-state work exists for must not regress: we sent real
    credentials and they were rejected, which the operator fixes on the host."""
    ref = registry.resolve("library/something")
    monkeypatch.setattr(registry, "has_credentials", lambda u: True)
    r = ocicompat._unavailable(
        ref, {"status": 401, "authenticated": True}, "latest", "manifest")
    assert r.status_code == 502
    assert r.headers["x-xhc-upstream-auth"] == "rejected"


def test_never_authenticated_is_still_502(monkeypatch):
    """Challenged and we could not answer at all -- no token, no credentials.
    The cache genuinely could not reach the registry as anyone."""
    ref = registry.resolve("library/something")
    monkeypatch.setattr(registry, "has_credentials", lambda u: False)
    r = ocicompat._unavailable(
        ref, {"status": 401, "authenticated": False}, "latest", "manifest")
    assert r.status_code == 502
    assert r.headers["x-xhc-upstream-auth"] == "unconfigured"


def test_bare_status_still_supported_for_blobs():
    """The blob path passes a bare 401 on purpose; it must not become a 404."""
    ref = registry.resolve("library/something")
    r = ocicompat._unavailable(ref, 401, "sha256:" + "0" * 64, "blob")
    assert r.status_code == 502


# ---------------------------------------------------------------------------
# Registries differ in WHERE they refuse, and the first fix only
# covered one of the two places.
#
#   Docker Hub  issues an anonymous token for a nonexistent repo, then 401s
#               the request carrying it        -> "authenticated, then refused"
#   ghcr        refuses at the token endpoint  -> never authenticates at all
#
# Measured 2026-09-02: ghcr's token endpoint returns 403 for a nonexistent repo
# and 200 for a real public one; Docker Hub returns 200 for both.
# ---------------------------------------------------------------------------


def test_token_endpoint_declined_anonymously_is_404(monkeypatch):
    """A 401/403 from the auth service is an ANSWER, not a gateway failure.

    With no credentials held it is indistinguishable from "no such repository",
    which is what the caller needs to hear. Before this, a mistyped ghcr image
    name returned 502 Bad Gateway and sent the reader to check the network.
    """
    ref = registry.resolve("ghcr.io/org/definitely-not-real-xyzzy")
    monkeypatch.setattr(registry, "has_credentials", lambda u: False)
    r = ocicompat._unavailable(
        ref, {"status": 401, "authenticated": False, "token": "declined"},
        "latest", "manifest")
    assert r.status_code == 404
    assert r.headers["x-xhc-upstream-auth"] == "anonymous-refused-at-token"


def test_token_endpoint_declined_with_credentials_is_502(monkeypatch):
    """We presented real credentials to the auth service and were refused.
    That is the operator's problem to fix and must not be softened to 404."""
    ref = registry.resolve("ghcr.io/org/private")
    monkeypatch.setattr(registry, "has_credentials", lambda u: True)
    r = ocicompat._unavailable(
        ref, {"status": 401, "authenticated": False, "token": "declined"},
        "latest", "manifest")
    assert r.status_code == 502
    assert r.headers["x-xhc-upstream-auth"] == "rejected"


def test_token_endpoint_unreachable_is_502_not_404(monkeypatch):
    """THE NEGATIVE CONTROL, and the reason it matters more than the fix.

    A token endpoint returning 500, or timing out, means we got no answer at
    all. Rendering that as 404 would convert a genuine upstream outage into
    "you typed it wrong" -- the same misdirection as the original defect, in the
    opposite direction, and harder to spot because it looks like a clean answer.
    """
    ref = registry.resolve("ghcr.io/org/img")
    monkeypatch.setattr(registry, "has_credentials", lambda u: False)
    r = ocicompat._unavailable(
        ref, {"status": 401, "authenticated": False, "token": "unreachable"},
        "latest", "manifest")
    assert r.status_code == 502
    assert r.headers["x-xhc-upstream-auth"] == "token-endpoint-unreachable"


def test_docker_hub_path_is_unaffected(monkeypatch):
    """The earlier fix must not regress: Hub authenticates anonymously and
    is then refused, which is a different branch from ghcr's."""
    ref = registry.resolve("library/definitely-not-real-xyzzy")
    monkeypatch.setattr(registry, "has_credentials", lambda u: False)
    r = ocicompat._unavailable(
        ref, {"status": 401, "authenticated": True}, "latest", "manifest")
    assert r.status_code == 404
    assert r.headers["x-xhc-upstream-auth"] == "anonymous-refused"
