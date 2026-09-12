"""The key-management surface, and specifically its PRIVILEGE BOUNDARIES.

The happy path here is trivial and is not where the risk is. The risk is in the
four questions this file exists to answer, each of which has a wrong answer that
looks exactly like a working feature:

  1. can a logged-in user touch someone ELSE's key?
  2. can a non-admin change what their own key is allowed to do?
  3. can a non-admin make themselves an admin?
  4. does disabling a user actually stop their existing keys working?

Every one of those is a silent failure. Nothing errors, nothing logs, the UI
looks right, and the service is wrong.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.authz import Rule, new_secret


@pytest.fixture
def console(tmp_path, monkeypatch):
    """An app with login and the console mounted, and two users already in it.

    The browser session is minted directly rather than by driving a real OIDC
    round trip: what is under test here is authorisation AFTER login, and
    test_oidc.py owns the login itself. Using the real cookie module rather than
    a stub keeps the boundary honest -- a bug in signing would fail here too.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app import console as console_mod
    from app import dockerauth, ocicompat, session, webauth
    from app.authzstore import AuthzStore
    from app.config import settings

    db = tmp_path / "authz.db"
    monkeypatch.setattr(settings, "authz_db", str(db))
    monkeypatch.setattr(settings, "session_secret", "test-signing-secret")
    monkeypatch.setattr(settings, "oidc_issuer", "https://idp.example.com")
    monkeypatch.setattr(dockerauth, "_store", None)

    store = AuthzStore(db)
    admin = store.claim_or_get_principal("sub-admin", "admin@example.com")
    assert admin.is_admin, "first principal must be admin; everything below assumes it"
    store.claim_or_get_principal("sub-user", "user@example.com")

    app = FastAPI()
    app.include_router(webauth.router)
    app.include_router(console_mod.router)
    app.include_router(
        ocicompat.router,
        dependencies=[__import__("fastapi").Depends(dockerauth.require_pull_auth)],
    )
    client = TestClient(app, raise_server_exceptions=False)

    def as_user(subject: str):
        client.cookies.clear()
        client.cookies.set(
            session.COOKIE_NAME, session.issue("test-signing-secret", subject)
        )
        return client

    return client, store, as_user


# ---------------- positive controls ----------------
# Without these, every refusal below is satisfied by a console that refuses
# everyone, which is the failure mode this whole file would otherwise miss.


def test_a_user_can_create_and_list_their_own_key(console):
    client, _, as_user = console
    c = as_user("sub-user")
    created = c.post("/_console/keys", json={"label": "ci"})
    assert created.status_code == 200, created.text
    assert created.json()["secret"], "creation must return the secret once"
    listed = c.get("/_console/keys")
    assert listed.status_code == 200
    assert [k["key_id"] for k in listed.json()["keys"]] == [created.json()["key_id"]]


def test_an_admin_can_set_a_users_allowlist(console):
    client, store, as_user = console
    r = as_user("sub-admin").put(
        "/_console/users/sub-user/allowlist",
        json={"rules": [{"pattern": "docker.io/*", "pull": True, "push": False}]},
    )
    assert r.status_code == 200, r.text
    assert [x.pattern for x in store.get_principal_rules("sub-user")] == ["docker.io/*"]


# ---------------- the four boundaries ----------------


def test_a_user_cannot_touch_another_users_key(console):
    """And the refusal is 404, not 403: a 403 confirms the key id is real, which
    turns this endpoint into an enumerator for other people's key ids."""
    client, _, as_user = console
    victim = as_user("sub-admin").post("/_console/keys", json={"label": "admins"}).json()

    attacker = as_user("sub-user")
    assert attacker.delete(f"/_console/keys/{victim['key_id']}").status_code == 404
    assert attacker.post(
        f"/_console/keys/{victim['key_id']}/disabled", json={"disabled": True}
    ).status_code == 404
    # and it is still usable -- the refusal was real, not cosmetic
    assert as_user("sub-admin").get("/_console/keys").json()["keys"][0]["disabled"] is False


def test_a_non_admin_cannot_set_any_allowlist_including_their_own(console):
    """The escalation this whole design exists to prevent. If a user can write
    their own allowlist, "create a key" and "grant myself push to everything"
    are the same operation."""
    client, store, as_user = console
    c = as_user("sub-user")
    for target in ("sub-user", "sub-admin"):
        r = c.put(
            f"/_console/users/{target}/allowlist",
            json={"rules": [{"pattern": "*", "pull": True, "push": True}]},
        )
        assert r.status_code == 403, f"{target}: {r.status_code}"
        assert store.get_principal_rules(target) == [], "nothing may have been written"


def test_a_non_admin_cannot_grant_themselves_admin(console):
    client, store, as_user = console
    r = as_user("sub-user").post("/_console/users/sub-user/admin", json={"is_admin": True})
    assert r.status_code == 403
    assert not [p for p in store.list_principals()
                if p.subject == "sub-user" and p.is_admin]


def test_a_non_admin_cannot_enumerate_users(console):
    client, _, as_user = console
    assert as_user("sub-user").get("/_console/users").status_code == 403
    assert as_user("sub-user").get("/_console/users/sub-admin/keys").status_code == 403


def test_disabling_a_user_stops_their_existing_keys_on_the_wire(console):
    """Not "the API returns disabled" -- the KEY MUST STOP WORKING at /v2.

    Checking the console's own response here would be checking that the write
    happened, which is adjacent to the claim. The claim is about what a docker
    client can do afterwards.
    """
    client, store, as_user = console
    key_id, secret = new_secret()
    store.add_key(key_id, secret, "sub-user", [Rule("docker.io/*", pull=True)])
    store.set_principal_rules("sub-user", [Rule("docker.io/*", pull=True)])

    before = client.get("/v2/", auth=(key_id, secret))
    assert before.status_code == 200, "positive control: the key works to begin with"

    r = as_user("sub-admin").post(
        "/_console/users/sub-user/disabled", json={"disabled": True}
    )
    assert r.status_code == 200, r.text

    after = client.get("/v2/", auth=(key_id, secret))
    assert after.status_code == 401, "a disabled user's key must stop authenticating"


def test_an_admin_cannot_disable_themselves(console):
    """Not paternalism: on a single-admin deployment it is unrecoverable without
    a shell on the host."""
    client, _, as_user = console
    r = as_user("sub-admin").post(
        "/_console/users/sub-admin/disabled", json={"disabled": True}
    )
    assert r.status_code == 400


# ---------------- the session cookie itself ----------------


def test_no_cookie_is_not_logged_in(console):
    client, _, _ = console
    client.cookies.clear()
    assert client.get("/_console/keys").status_code == 401


def test_a_forged_cookie_is_refused(console):
    """The signature is the only thing separating "I am sub-admin" from being
    sub-admin. An unsigned or wrongly-signed payload must not be read at all."""
    import base64
    import json
    import time

    from app import session

    client, _, _ = console
    payload = base64.urlsafe_b64encode(
        json.dumps({"sub": "sub-admin", "exp": time.time() + 9999}).encode()
    ).decode().rstrip("=")

    for forged in (
        payload,                                    # no signature at all
        f"{payload}.",                              # empty signature
        f"{payload}.notasignature",                 # wrong signature
        f"{payload}.{session._sign('wrong-key', b'x')}",   # signed with another key
    ):
        client.cookies.clear()
        client.cookies.set(session.COOKIE_NAME, forged)
        assert client.get("/_console/users").status_code == 401, forged[:40]


def test_an_expired_session_is_refused(console):
    from app import session

    client, _, _ = console
    client.cookies.clear()
    client.cookies.set(
        session.COOKIE_NAME, session.issue("test-signing-secret", "sub-admin", ttl_s=-1)
    )
    assert client.get("/_console/users").status_code == 401


def test_a_session_naming_an_unknown_principal_is_refused(console):
    """A valid signature over a subject that is not in the store must not create
    one. Signup happens through a completed OIDC login, nowhere else."""
    from app import session

    client, store, _ = console
    client.cookies.clear()
    client.cookies.set(
        session.COOKIE_NAME, session.issue("test-signing-secret", "sub-nobody")
    )
    assert client.get("/_console/keys").status_code == 401
    assert "sub-nobody" not in [p.subject for p in store.list_principals()]


def test_the_session_cookie_carries_no_authority_of_its_own(console):
    """is_admin is read from the STORE on every request, never from the cookie.

    A cookie carrying a role would keep a demoted admin an admin until it
    expired -- up to XHC_SESSION_TTL after the demotion.
    """
    client, store, as_user = console
    c = as_user("sub-admin")
    assert c.get("/_console/users").status_code == 200

    store.set_admin("sub-user", True)      # so the last-admin guard permits it
    store.set_admin("sub-admin", False)

    # same cookie, no re-login
    assert c.get("/_console/users").status_code == 403


def test_the_key_hash_never_leaves_the_console(console):
    client, _, as_user = console
    c = as_user("sub-user")
    c.post("/_console/keys", json={"label": "x"})
    body = c.get("/_console/keys").text
    assert "secret_hash" not in body


def test_a_non_admin_cannot_delete_a_user(console):
    client, store, as_user = console
    r = as_user("sub-user").delete("/_console/users/sub-admin")
    assert r.status_code == 403
    assert "sub-admin" in [p.subject for p in store.list_principals()]


def test_an_admin_cannot_delete_themselves(console):
    """They lose the session's backing row mid-request, and on a single-admin
    deployment nothing restores it without a shell on the host."""
    client, store, as_user = console
    r = as_user("sub-admin").delete("/_console/users/sub-admin")
    assert r.status_code == 400
    assert "sub-admin" in [p.subject for p in store.list_principals()]


def test_an_admin_can_delete_another_user_and_their_keys_stop_working(console):
    """The positive control, asserted ON THE WIRE: the API returning 200 is a
    statement about the write, not about whether the credential still works."""
    from app.authz import Rule, new_secret

    client, store, as_user = console
    key_id, secret = new_secret()
    store.add_key(key_id, secret, "sub-user", [Rule("docker.io/*", pull=True)])
    store.set_principal_rules("sub-user", [Rule("docker.io/*", pull=True)])
    assert client.get("/v2/", auth=(key_id, secret)).status_code == 200, "positive control"

    r = as_user("sub-admin").delete("/_console/users/sub-user")
    assert r.status_code == 200, r.text
    assert client.get("/v2/", auth=(key_id, secret)).status_code == 401
