"""Admin taken from an identity-provider claim, re-evaluated at every login.

THE PROPERTY THIS FILE EXISTS FOR: revoking the role at the provider removes
admin here. Granting is the easy half and a feature that only grants would pass
most of these tests while failing the one that matters -- so the demotion tests
are the point, and the grant tests are their positive controls.

The login is driven through the real /_auth/callback route with the provider
stubbed at `webauth.client()`. What is under test is what the callback DOES
with a verified identity; test_oidc.py owns verifying it, including that
`complete()` carries the claims through.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CLAIM = "realm_access.roles"
VALUE = "muninn-admin"
SECRET = "test-signing-secret"


class _FakeProvider:
    """Stands in for OIDCClient.complete(): returns whatever identity the test
    queued, as if the provider had issued and we had verified that id_token."""

    def __init__(self) -> None:
        self.next: SimpleNamespace | None = None

    async def complete(self, code: str, state: str):
        assert self.next is not None, "test did not queue an identity"
        ident, self.next = self.next, None
        return ident


@pytest.fixture
def login_app(tmp_path, monkeypatch):
    """Returns a factory: login_app(claim=..., value=..., bootstrap=...) ->
    (client, store, login, as_session)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app import console as console_mod
    from app import dockerauth, webauth
    from app.authzstore import AuthzStore
    from app.config import settings

    def make(claim=None, value=None, bootstrap=None):
        db = tmp_path / "authz.db"
        monkeypatch.setattr(settings, "authz_db", str(db))
        monkeypatch.setattr(settings, "session_secret", SECRET)
        monkeypatch.setattr(settings, "oidc_issuer", "https://idp.example.com")
        monkeypatch.setattr(settings, "bootstrap_admin", bootstrap)
        # raising=False so this file can be run against a tree that predates
        # the settings, and fail on BEHAVIOUR there rather than on setup.
        monkeypatch.setattr(settings, "oidc_admin_claim", claim, raising=False)
        monkeypatch.setattr(settings, "oidc_admin_value", value, raising=False)
        monkeypatch.setattr(dockerauth, "_store", None)
        provider = _FakeProvider()
        monkeypatch.setattr(webauth, "_client", provider)
        if hasattr(webauth, "_missing_claim_logged"):
            monkeypatch.setattr(webauth, "_missing_claim_logged", False)

        store = AuthzStore(db)
        app = FastAPI()
        app.include_router(webauth.router)
        app.include_router(console_mod.router)
        client = TestClient(app, raise_server_exceptions=False)

        def login(subject, claims=None, email=""):
            """Complete a login. Returns the session cookie value it set."""
            provider.next = SimpleNamespace(
                subject=subject, email=email, name="", claims=dict(claims or {})
            )
            client.cookies.clear()
            r = client.get("/_auth/callback?code=c&state=s", follow_redirects=False)
            assert r.status_code == 302, r.text
            cookie = r.cookies.get("muninn_session")
            assert cookie, "a completed login must set a session"
            return cookie

        def as_session(cookie):
            client.cookies.clear()
            client.cookies.set("muninn_session", cookie)
            return client

        return client, store, login, as_session

    return make


def _admin(store, subject) -> bool:
    p = store.get_principal(subject)
    assert p is not None, f"{subject} has no principal"
    return p.is_admin


def _roles(*roles):
    return {"realm_access": {"roles": list(roles)}}


# ---------------- grant and revoke ----------------


def test_a_login_with_the_claim_grants_admin(login_app):
    """Positive control: without it, every demotion below is satisfied by a
    feature that never grants."""
    _, store, login, _ = login_app(CLAIM, VALUE)
    store.create_principal("sub-other", is_admin=True)
    login("sub-a", _roles("reader", VALUE))
    assert _admin(store, "sub-a")


def test_a_later_login_without_the_claim_demotes(login_app):
    """THE REQUIREMENT. The role was revoked at the provider; the next login
    must take admin away. Another admin exists, so this is not the last-admin
    case -- that one is tested on its own below."""
    _, store, login, _ = login_app(CLAIM, VALUE)
    store.create_principal("sub-other", is_admin=True)
    login("sub-a", _roles(VALUE))
    assert _admin(store, "sub-a")
    login("sub-a", _roles("reader"))
    assert not _admin(store, "sub-a"), "revoking the role at the IdP must revoke admin"


def test_a_later_login_with_the_claim_promotes_an_existing_non_admin(login_app):
    _, store, login, _ = login_app(CLAIM, VALUE)
    store.create_principal("sub-other", is_admin=True)
    login("sub-a", _roles("reader"))
    assert not _admin(store, "sub-a")
    login("sub-a", _roles(VALUE))
    assert _admin(store, "sub-a")


# ---------------- claim shapes ----------------


def test_a_nested_list_claim_is_followed(login_app):
    _, store, login, _ = login_app("realm_access.roles", VALUE)
    store.create_principal("sub-other", is_admin=True)
    login("sub-a", {"realm_access": {"roles": ["x", VALUE, "y"]}})
    assert _admin(store, "sub-a")


def test_a_top_level_string_claim_matches_exactly(login_app):
    _, store, login, _ = login_app("role", VALUE)
    store.create_principal("sub-other", is_admin=True)
    login("sub-a", {"role": VALUE})
    assert _admin(store, "sub-a")
    # exact: not a prefix, not a substring, not case-folded
    for near_miss in (VALUE + "s", VALUE.upper(), "x" + VALUE, f"{VALUE} other"):
        login("sub-a", {"role": near_miss})
        assert not _admin(store, "sub-a"), near_miss


def test_a_list_claim_matches_an_element_exactly_not_a_substring(login_app):
    _, store, login, _ = login_app("groups", VALUE)
    store.create_principal("sub-other", is_admin=True)
    login("sub-a", {"groups": ["other", VALUE + "-readonly"]})
    assert not _admin(store, "sub-a")


def test_a_top_level_claim_whose_name_contains_dots_is_found(login_app):
    """Namespaced custom claims are URLs, so they contain dots. A pure
    dotted-path reading would split one and never find it."""
    name = "https://cache.example.com/roles"
    _, store, login, _ = login_app(name, VALUE)
    store.create_principal("sub-other", is_admin=True)
    login("sub-a", {name: [VALUE]})
    assert _admin(store, "sub-a")


def test_a_claim_of_another_type_is_not_admin(login_app):
    _, store, login, _ = login_app("role", "true")
    store.create_principal("sub-other", is_admin=True)
    for odd in (True, 1, {"true": 1}, None):
        login("sub-a", {"role": odd})
        assert not _admin(store, "sub-a"), repr(odd)


def test_a_missing_claim_is_not_admin_and_is_logged_once(login_app, caplog):
    """An IdP that never emits the claim must be diagnosable: the first time,
    the log says the claim was absent and which claims WERE present. Once, so
    an IdP that omits it for every non-admin does not fill the log."""
    _, store, login, _ = login_app(CLAIM, VALUE)
    store.create_principal("sub-other", is_admin=True)
    with caplog.at_level(logging.WARNING, logger="xhc.webauth"):
        login("sub-a", {"email": "a@example.com", "groups": ["g"]})
        login("sub-b", {})
        login("sub-a", {"realm_access": {}})
    assert not _admin(store, "sub-a")
    assert not _admin(store, "sub-b")
    hits = [r for r in caplog.records if CLAIM in r.getMessage()
            and "absent" in r.getMessage()]
    assert len(hits) == 1, [r.getMessage() for r in caplog.records]
    # names, never values: the claim set can carry personal data
    assert "groups" in hits[0].getMessage()
    assert "a@example.com" not in hits[0].getMessage()


# ---------------- bootstrap and the last admin ----------------


def test_the_first_login_is_not_made_admin_in_claim_mode(login_app):
    """With an IdP deciding admin, being first proves nothing -- it would hand
    the service to whoever reached the login page first after deploy."""
    _, store, login, _ = login_app(CLAIM, VALUE)
    login("sub-first", _roles("reader"))
    assert not _admin(store, "sub-first")


def test_demoting_the_last_admin_is_allowed_and_logged_loudly(login_app, caplog):
    """The chosen behaviour: the IdP wins, even for the last admin. Keeping
    admin here would mean the one person whose role was revoked is exactly
    the one who keeps it."""
    _, store, login, _ = login_app(CLAIM, VALUE)
    login("sub-a", _roles(VALUE))
    assert _admin(store, "sub-a")
    with caplog.at_level(logging.WARNING, logger="xhc.webauth"):
        login("sub-a", _roles("reader"))
    assert not _admin(store, "sub-a")
    assert not [p for p in store.list_principals() if p.is_admin]
    loud = [r for r in caplog.records if r.levelno >= logging.ERROR
            and "no admin" in r.getMessage()]
    assert loud, [r.getMessage() for r in caplog.records]


def test_zero_admins_is_recovered_by_the_idp_granting_the_role(login_app):
    _, store, login, _ = login_app(CLAIM, VALUE)
    login("sub-a", _roles(VALUE))
    login("sub-a", _roles())
    assert not [p for p in store.list_principals() if p.is_admin]
    login("sub-b", _roles(VALUE))
    assert _admin(store, "sub-b")


def test_zero_admins_is_recovered_by_authzctl_grant_admin(login_app, capsys):
    """Recovery that needs no IdP change and no restart. The session the
    recovering user already holds becomes admin on the very next request."""
    from app import authzctl
    from app.config import settings

    client, store, login, as_session = login_app(CLAIM, VALUE)
    login("sub-a", _roles(VALUE))
    cookie = login("sub-a", _roles())
    assert as_session(cookie).get("/_console/users").status_code == 403

    assert authzctl.main(["--db", settings.authz_db, "grant-admin", "sub-a"]) == 0
    assert _admin(store, "sub-a")
    assert as_session(cookie).get("/_console/users").status_code == 200


def test_bootstrap_admin_is_break_glass_in_claim_mode(login_app):
    """XHC_BOOTSTRAP_ADMIN grants admin at EVERY login in claim mode, whatever
    the claim says. That is what makes it break-glass: a broken IdP mapping
    must not be able to lock out the operator who can fix it."""
    _, store, login, _ = login_app(CLAIM, VALUE, bootstrap="sub-glass")
    login("sub-glass", {})
    assert _admin(store, "sub-glass")
    login("sub-glass", _roles("reader"))
    assert _admin(store, "sub-glass"), "the claim must not override break-glass"
    # and it grants nobody else
    login("sub-other", _roles("reader"))
    assert not _admin(store, "sub-other")


def test_bootstrap_admin_re_grants_after_an_out_of_band_demotion(login_app):
    _, store, login, _ = login_app(CLAIM, VALUE, bootstrap="glass@example.com")
    store.create_principal("sub-other", is_admin=True)
    login("sub-glass", {}, email="glass@example.com")
    assert _admin(store, "sub-glass")
    store.set_admin("sub-glass", False)
    login("sub-glass", {}, email="glass@example.com")
    assert _admin(store, "sub-glass")


# ---------------- how fast a demotion lands ----------------


def test_a_demotion_applies_to_an_existing_session_on_its_next_request(login_app):
    """Admin is not in the cookie. A session minted while admin must stop
    being admin on its next request once the store says so -- here, because
    a second login elsewhere carried no claim."""
    _, store, login, as_session = login_app(CLAIM, VALUE)
    store.create_principal("sub-other", is_admin=True)
    laptop = login("sub-a", _roles(VALUE))
    assert as_session(laptop).get("/_console/users").status_code == 200
    login("sub-a", _roles("reader"))  # e.g. a second browser, after revocation
    assert as_session(laptop).get("/_console/users").status_code == 403


def test_a_demotion_recorded_out_of_band_applies_on_the_next_request(login_app):
    from app import authzctl
    from app.config import settings

    _, store, login, as_session = login_app(CLAIM, VALUE)
    store.create_principal("sub-other", is_admin=True)
    cookie = login("sub-a", _roles(VALUE))
    assert as_session(cookie).get("/_console/users").status_code == 200
    assert authzctl.main(["--db", settings.authz_db, "revoke-admin", "sub-a"]) == 0
    assert as_session(cookie).get("/_console/users").status_code == 403


def test_the_session_cookie_is_the_bound_on_an_idp_revocation(login_app):
    """Documented, not fixed: Muninn learns of a revocation at the provider
    only at the next login, so an existing session is bounded by
    XHC_SESSION_TTL. The cookie's own expiry is what enforces that bound."""
    from app import session

    _, store, login, as_session = login_app(CLAIM, VALUE)
    store.create_principal("sub-other", is_admin=True)
    login("sub-a", _roles(VALUE))
    expired = session.issue(SECRET, "sub-a", ttl_s=-1)
    assert session.verify(SECRET, expired) is None
    assert as_session(expired).get("/_console/users").status_code == 401


# ---------------- the console in claim mode ----------------


def test_the_console_refuses_the_admin_toggle_in_claim_mode(login_app):
    """A change the next login silently undoes is a confident wrong answer.
    Refused, with the reason, rather than accepted and reverted."""
    _, store, login, as_session = login_app(CLAIM, VALUE)
    admin = login("sub-a", _roles(VALUE))
    login("sub-b", _roles())
    r = as_session(admin).post("/_console/users/sub-b/admin", json={"is_admin": True})
    assert r.status_code == 409, r.text
    assert "XHC_OIDC_ADMIN_CLAIM" in r.text
    assert not _admin(store, "sub-b")


def test_me_says_admin_comes_from_the_idp(login_app):
    _, _, login, as_session = login_app(CLAIM, VALUE)
    cookie = login("sub-a", _roles(VALUE))
    me = as_session(cookie).get("/_auth/me").json()
    assert me["is_admin"] is True
    assert me["admin_from_idp"] is True


def test_a_bearer_credential_is_never_a_console_admin(login_app):
    """Workload JWTs authenticate /v2 and the HF surface, never the console:
    the console reads only the session cookie, so a bearer token -- even one
    for a principal flagged admin in the store -- is simply not logged in."""
    client, store, _, _ = login_app(CLAIM, VALUE)
    store.create_principal("k8s:ns:sa", is_admin=True)
    client.cookies.clear()
    r = client.get("/_console/users", headers={"Authorization": "Bearer x.y.z"})
    assert r.status_code == 401


# ---------------- unconfigured is unchanged ----------------


def test_unconfigured_the_first_login_is_still_admin_and_claims_are_ignored(login_app):
    client, store, login, as_session = login_app()
    first = login("sub-first", {})
    login("sub-second", _roles(VALUE))
    assert _admin(store, "sub-first")
    assert not _admin(store, "sub-second"), "a claim must mean nothing unconfigured"
    login("sub-first", {})
    assert _admin(store, "sub-first"), "a later login must not re-evaluate anything"
    me = as_session(first).get("/_auth/me").json()
    assert me.get("admin_from_idp", False) is False
    # and the console toggle still works
    r = as_session(first).post("/_console/users/sub-second/admin", json={"is_admin": True})
    assert r.status_code == 200, r.text
    assert _admin(store, "sub-second")


def test_unconfigured_bootstrap_is_still_first_creation_only(login_app):
    _, store, login, _ = login_app(bootstrap="sub-b")
    login("sub-a", {})
    login("sub-b", {})
    assert _admin(store, "sub-b")
    store.set_admin("sub-b", False)
    login("sub-b", {})
    assert not _admin(store, "sub-b"), "outside claim mode it must not re-promote"


# ---------------- configuration ----------------

_LOGIN_ENV = {
    "XHC_OIDC_ISSUER": "https://idp.example.com",
    "XHC_OIDC_CLIENT_ID": "cid",
    "XHC_OIDC_CLIENT_SECRET": "csec",
    "XHC_OIDC_REDIRECT_URI": "https://cache.example/_auth/callback",
    "XHC_SESSION_SECRET": "signing",
    "XHC_AUTHZ_DB": "/srv/authz.db",
}


@pytest.mark.parametrize("present,absent", [
    ("XHC_OIDC_ADMIN_CLAIM", "XHC_OIDC_ADMIN_VALUE"),
    ("XHC_OIDC_ADMIN_VALUE", "XHC_OIDC_ADMIN_CLAIM"),
])
def test_half_configured_is_refused_at_startup(present, absent, monkeypatch):
    from app.config import Settings

    for k, v in _LOGIN_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv(present, "x")
    monkeypatch.delenv(absent, raising=False)
    with pytest.raises(ValueError, match=absent):
        Settings.from_env()


def test_an_admin_claim_without_a_login_is_refused_at_startup(monkeypatch):
    from app.config import Settings

    monkeypatch.delenv("XHC_OIDC_ISSUER", raising=False)
    monkeypatch.setenv("XHC_OIDC_ADMIN_CLAIM", "groups")
    monkeypatch.setenv("XHC_OIDC_ADMIN_VALUE", "admins")
    with pytest.raises(ValueError, match="XHC_OIDC_ISSUER"):
        Settings.from_env()


def test_both_set_reaches_settings(monkeypatch):
    from app.config import Settings

    for k, v in _LOGIN_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("XHC_OIDC_ADMIN_CLAIM", "realm_access.roles")
    monkeypatch.setenv("XHC_OIDC_ADMIN_VALUE", "muninn-admin")
    s = Settings.from_env()
    assert (s.oidc_admin_claim, s.oidc_admin_value) == ("realm_access.roles", "muninn-admin")


# ---------------- authzctl ----------------


def test_authzctl_grant_and_revoke_admin(tmp_path, capsys):
    from app import authzctl
    from app.authzstore import AuthzStore

    db = str(tmp_path / "a.db")
    store = AuthzStore(db)
    store.create_principal("sub-a", is_admin=True)
    store.create_principal("sub-b")
    assert authzctl.main(["--db", db, "grant-admin", "sub-b"]) == 0
    assert store.get_principal("sub-b").is_admin
    assert authzctl.main(["--db", db, "revoke-admin", "sub-b"]) == 0
    assert not store.get_principal("sub-b").is_admin


def test_authzctl_refuses_to_revoke_the_last_admin(tmp_path, capsys):
    from app import authzctl
    from app.authzstore import AuthzStore

    db = str(tmp_path / "a.db")
    store = AuthzStore(db)
    store.create_principal("sub-a", is_admin=True)
    assert authzctl.main(["--db", db, "revoke-admin", "sub-a"]) == 1
    assert "last admin" in capsys.readouterr().err
    assert store.get_principal("sub-a").is_admin


def test_authzctl_grant_admin_to_an_unknown_subject_is_an_error(tmp_path, capsys):
    """An UPDATE matching no rows succeeds; a break-glass command that reports
    success after promoting nobody is the worst possible time to find that."""
    from app import authzctl
    from app.authzstore import AuthzStore

    db = str(tmp_path / "a.db")
    AuthzStore(db)
    assert authzctl.main(["--db", db, "grant-admin", "sub-typo"]) == 1
    assert "no such principal" in capsys.readouterr().err
