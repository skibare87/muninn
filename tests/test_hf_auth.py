"""A credential on the Hugging Face surface, and what reaches the Hub.

Two separate properties, and conflating them is how the feature ships broken:

  INBOUND   a client must present a ravencache key to use this surface at all
  OUTBOUND  that key must NEVER leave this process. The cache authenticates to
            the Hub as itself, with its own token.

The outbound half is the one that fails loudly on the first real user and
silently on every user before them: a client authenticating to us with
`HF_TOKEN=<key_id>:<secret>` -- which is how Hugging Face's own tooling sends a
credential -- would otherwise have that key forwarded to a third party.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.authz import Rule, new_secret


@pytest.fixture
def hf(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app import dockerauth
    from app.authzstore import AuthzStore
    from app.config import settings

    (tmp_path / "cache").mkdir()
    (tmp_path / "www").mkdir()
    (tmp_path / "www" / "index.html").write_text("<h1>muninn</h1>")
    (tmp_path / "www" / "favicon.ico").write_bytes(b"\x00icon")

    db = tmp_path / "authz.db"
    monkeypatch.setattr(settings, "authz_db", str(db))
    monkeypatch.setattr(settings, "cache_dir", str(tmp_path / "cache"))
    monkeypatch.setattr(settings, "web_root", str(tmp_path / "www"))
    monkeypatch.setattr(settings, "hf_auth", "key")
    monkeypatch.setattr(dockerauth, "_store", None)

    store = AuthzStore(db)
    store.claim_or_get_principal("sub-1")
    key_id, secret = new_secret()
    store.add_key(key_id, secret, "sub-1", [Rule("*", pull=True)])

    import app.main as main

    return TestClient(main.app, raise_server_exceptions=False), (key_id, secret)


# ---------------- inbound: the gate ----------------


def test_the_homepage_stays_public(hf):
    """A cache whose front page 401s is not a front page, and the login button
    has to render before anyone has a credential."""
    client, _ = hf
    r = client.get("/")
    assert r.status_code == 200
    assert "muninn" in r.text


def test_web_root_assets_stay_public(hf):
    """The favicon is requested by the browser before any login. If it 401s the
    tab falls back to the upstream's icon, which is the bug this whole web root
    exists to avoid."""
    client, _ = hf
    assert client.get("/favicon.ico").status_code == 200


def test_an_anonymous_hf_request_is_refused(hf):
    client, _ = hf
    for path in ("/gpt2/resolve/main/config.json", "/api/models/gpt2", "/anything/at/all"):
        r = client.get(path)
        assert r.status_code == 401, f"{path} -> {r.status_code}"
        assert "basic" in r.headers.get("www-authenticate", "").lower()


def test_the_refusal_tells_an_hf_user_what_to_set(hf):
    """`huggingface_hub` users have an HF_TOKEN, not a username and password.
    A bare 401 sends them to look for a login form that does not exist."""
    client, _ = hf
    body = client.get("/api/models/gpt2").json()
    assert "HF_TOKEN" in body.get("hint", "")


def test_a_bearer_credential_is_accepted(hf):
    """THE COMPATIBILITY REQUIREMENT. Hugging Face's tooling has no concept of a
    username: HF_TOKEN arrives as `Authorization: Bearer <one opaque string>`.
    So the key must be presentable that way."""
    client, (key_id, secret) = hf
    r = client.get("/api/models/gpt2", headers={"authorization": f"Bearer {key_id}:{secret}"})
    assert r.status_code != 401, r.text


def test_basic_is_accepted_too(hf):
    """curl, requests and a browser send Basic, exactly as /v2 already expects."""
    client, (key_id, secret) = hf
    r = client.get("/api/models/gpt2", auth=(key_id, secret))
    assert r.status_code != 401, r.text


def test_a_wrong_secret_is_refused(hf):
    client, (key_id, _) = hf
    assert client.get("/api/models/gpt2",
                      headers={"authorization": f"Bearer {key_id}:wrong"}).status_code == 401
    assert client.get("/api/models/gpt2", auth=(key_id, "wrong")).status_code == 401


def test_a_disabled_key_is_refused(hf):
    """Revocation has to reach this surface too, not only /v2."""
    from app import dockerauth

    client, (key_id, secret) = hf
    dockerauth.store().set_key_disabled(key_id, True)
    assert client.get("/api/models/gpt2",
                      headers={"authorization": f"Bearer {key_id}:{secret}"}).status_code == 401


def test_the_gate_is_off_by_default(tmp_path, monkeypatch):
    """Opt-in. An existing deployment that changes no variables is untouched."""
    from app.config import Settings

    monkeypatch.delenv("XHC_HF_AUTH", raising=False)
    assert Settings.from_env().hf_auth == "none"


def test_key_auth_without_a_key_store_refuses_to_start(monkeypatch):
    """Otherwise opting into the hardening produces a surface that refuses
    everyone -- worse than the exposure it was meant to close."""
    from app.config import Settings

    monkeypatch.setenv("XHC_HF_AUTH", "key")
    monkeypatch.delenv("XHC_AUTHZ_DB", raising=False)
    with pytest.raises(ValueError, match="XHC_AUTHZ_DB"):
        Settings.from_env()


# ---------------- outbound: what reaches the Hub ----------------


def test_the_clients_credential_NEVER_reaches_the_hub():
    """THE LEAK. A client authenticates to us with a ravencache key, sent as an
    HF_TOKEN because that is the only shape Hugging Face's tooling has. If that
    header were forwarded, our own credential would be handed to a third party
    -- and the request would fail there anyway, since a ravencache key is not a
    Hub token.
    """
    from app import hfcompat
    from app.config import settings

    client_sent = {
        "authorization": "Bearer muninn-key-id:muninn-secret",
        "accept": "application/json",
    }
    settings.hf_token = "hf_the_caches_own_token"
    try:
        out = hfcompat._apply_upstream_auth(dict(client_sent))
    finally:
        settings.hf_token = None

    assert out["authorization"] == "Bearer hf_the_caches_own_token"
    assert "muninn-secret" not in str(out), "the client's key must not survive"
    assert out["accept"] == "application/json", "other headers must pass through"


def test_with_no_hub_token_the_header_is_simply_absent():
    """Not left as the client's. An anonymous upstream request is correct; one
    carrying somebody else's credential is not."""
    from app import hfcompat
    from app.config import settings

    settings.hf_token = None
    out = hfcompat._apply_upstream_auth({"authorization": "Bearer someones-key", "x": "1"})
    assert "authorization" not in out
    assert out["x"] == "1"


def test_the_cache_does_not_borrow_a_users_hub_entitlement():
    """A user with a REAL Hub token gets no more through the cache than the
    cache itself can reach. That is deliberate for shared storage: anything
    fetched is served to everyone whose rules cover the path, so borrowing one
    user's entitlement would launder it to all of them.
    """
    from app import hfcompat
    from app.config import settings

    settings.hf_token = "hf_cache_token"
    try:
        out = hfcompat._apply_upstream_auth({"authorization": "Bearer hf_a_real_user_token"})
    finally:
        settings.hf_token = None
    assert out["authorization"] == "Bearer hf_cache_token"
