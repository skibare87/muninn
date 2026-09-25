"""Headless provisioning: principals, rules and keys without a browser.

The console was the only supported way to mint a credential, which left anyone
running the cache for CI or a cluster without an identity provider with no way
in at all. These tests pin the two replacements -- /_cache/authz/* behind the
manage token, and `python -m app.authzctl` against the database file -- and the
properties that make them safe to ship in a public image:

  * an UNSET manage token closes the surface. The older /_cache routes treat an
    unset token as open; a surface that mints credentials must not.
  * a created principal is never admin unless someone typed `is_admin`,
    including when it is the first row in an empty store.
  * the secret is shown once, by the mint, and exists nowhere afterwards except
    as a hash.
  * a key minted by the CLI while the server runs authenticates on the very
    next request.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TOKEN = "manage-token-for-tests"
AUTH = {"authorization": f"Bearer {TOKEN}"}

# Every new endpoint, with a body that would be VALID if the caller were
# authorised -- so a refusal cannot be a 422 wearing a 401's clothes.
ENDPOINTS = [
    ("GET", "/_cache/authz/principals", None),
    ("POST", "/_cache/authz/principals", {"subject": "svc:x"}),
    ("DELETE", "/_cache/authz/principals/svc:x", None),
    ("PUT", "/_cache/authz/principals/svc:x/rules", {"rules": ["* pull"]}),
    ("POST", "/_cache/authz/principals/svc:x/keys", {}),
    ("GET", "/_cache/authz/keys", None),
    ("POST", "/_cache/authz/keys/abc/disabled", {"disabled": True}),
    ("DELETE", "/_cache/authz/keys/abc", None),
]


@pytest.fixture
def env(tmp_path, monkeypatch):
    """An app carrying the provisioning routes, /v2 and the HF surface."""
    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient

    from app import authzmanage, dockerauth, hfcompat, ocicompat
    from app.config import settings

    db = tmp_path / "authz.db"
    monkeypatch.setattr(settings, "authz_db", str(db))
    monkeypatch.setattr(settings, "manage_token", TOKEN)
    monkeypatch.setattr(settings, "hf_auth", "key")
    monkeypatch.setattr(settings, "docker_enabled", True)
    monkeypatch.setattr(settings, "cache_dir", str(tmp_path / "cache"))
    monkeypatch.setattr(settings, "docker_dir", str(tmp_path / "oci"))
    monkeypatch.setattr(settings, "web_root", None)
    monkeypatch.setattr(dockerauth, "_store", None)
    (tmp_path / "cache").mkdir()

    app = FastAPI()
    app.include_router(authzmanage.router)
    app.include_router(ocicompat.router, dependencies=[Depends(dockerauth.require_pull_auth)])
    app.include_router(hfcompat.router)
    return TestClient(app, raise_server_exceptions=False), db


def _call(client, method, path, body=None, headers=None):
    return client.request(method, path, json=body, headers=headers or {})


def _provision(client, subject="svc:ci", rules=("docker.io/library/* pull",), scope=None):
    r = _call(client, "POST", "/_cache/authz/principals", {"subject": subject}, AUTH)
    assert r.status_code == 201, r.text
    r = _call(client, "PUT", f"/_cache/authz/principals/{subject}/rules",
              {"rules": list(rules)}, AUTH)
    assert r.status_code == 200, r.text
    body = {"label": "ci runner"}
    if scope is not None:
        body["scope"] = scope
    r = _call(client, "POST", f"/_cache/authz/principals/{subject}/keys", body, AUTH)
    assert r.status_code == 201, r.text
    minted = r.json()
    return minted["key_id"], minted["secret"]


# ---------------- the gate ----------------


@pytest.mark.parametrize("method,path,body", ENDPOINTS)
@pytest.mark.parametrize("presented", [None, "Bearer wrong", "Bearer ", "", TOKEN, "Basic x"])
def test_a_missing_wrong_or_empty_token_is_refused(env, method, path, body, presented):
    client, _ = env
    headers = {} if presented is None else {"authorization": presented}
    r = _call(client, method, path, body, headers)
    assert r.status_code == 401, f"{method} {path} with {presented!r} -> {r.status_code}"


@pytest.mark.parametrize("method,path,body", ENDPOINTS)
def test_an_unset_manage_token_closes_the_surface_rather_than_opening_it(
    env, monkeypatch, method, path, body
):
    """The existing /_cache routes read an unset token as OPEN. A route that
    mints credentials inheriting that default would be an unauthenticated
    key-minting endpoint on every deployment that never set the variable."""
    from app.config import settings

    client, _ = env
    for unset in (None, "", "   "):
        monkeypatch.setattr(settings, "manage_token", unset)
        for headers in ({}, {"authorization": "Bearer "}, {"authorization": f"Bearer {unset}"}):
            r = _call(client, method, path, body, headers)
            assert r.status_code == 404, f"{method} {path} token={unset!r} -> {r.status_code}"


@pytest.mark.parametrize("method,path,body", ENDPOINTS)
def test_the_surface_is_absent_without_an_authz_db(env, monkeypatch, method, path, body):
    from app import dockerauth
    from app.config import settings

    client, _ = env
    monkeypatch.setattr(settings, "authz_db", None)
    monkeypatch.setattr(dockerauth, "_store", None)
    r = _call(client, method, path, body, AUTH)
    assert r.status_code == 404, f"{method} {path} -> {r.status_code}"


def test_unconfigured_the_real_app_answers_404_itself_and_forwards_nothing(
    tmp_path, monkeypatch
):
    """In the REAL app, an unmounted /_cache path is not a 404: it falls to the
    Hugging Face catch-all and is proxied to the Hub, request body included.
    So "unavailable" has to be a route that exists and refuses, not a route
    that is absent."""
    from fastapi.testclient import TestClient

    from app import dockerauth, hfcompat
    from app.config import settings

    monkeypatch.setattr(settings, "manage_token", None)
    monkeypatch.setattr(settings, "authz_db", None)
    monkeypatch.setattr(settings, "hf_auth", "none")
    monkeypatch.setattr(settings, "web_root", None)
    monkeypatch.setattr(settings, "cache_dir", str(tmp_path))
    monkeypatch.setattr(dockerauth, "_store", None)
    forwarded = []

    async def _record(full_path, request):
        forwarded.append(full_path)
        from fastapi.responses import Response
        return Response(status_code=599)

    monkeypatch.setattr(hfcompat, "proxy_upstream", _record)
    import app.main as main

    client = TestClient(main.app, raise_server_exceptions=False)
    for method, path, body in ENDPOINTS:
        r = _call(client, method, path, body, AUTH)
        assert r.status_code == 404, f"{method} {path} -> {r.status_code}"
    assert forwarded == []


# ---------------- principals ----------------


def test_a_created_principal_is_not_admin_even_as_the_first_in_an_empty_store(env):
    """The first-login-becomes-admin grant belongs to the interactive login.
    A machine account created on an empty store must not take it."""
    client, db = env
    r = _call(client, "POST", "/_cache/authz/principals", {"subject": "svc:ci"}, AUTH)
    assert r.status_code == 201, r.text
    assert r.json()["is_admin"] is False
    row = sqlite3.connect(db).execute(
        "SELECT is_admin FROM principals WHERE subject='svc:ci'").fetchone()
    assert row == (0,)


def test_admin_is_granted_only_when_asked_for_explicitly(env):
    client, _ = env
    r = _call(client, "POST", "/_cache/authz/principals",
              {"subject": "ops", "email": "ops@example.com", "is_admin": True}, AUTH)
    assert r.status_code == 201 and r.json()["is_admin"] is True


def test_admin_must_be_a_real_boolean_not_a_truthy_string(env):
    client, _ = env
    r = _call(client, "POST", "/_cache/authz/principals",
              {"subject": "svc:x", "is_admin": "yes"}, AUTH)
    assert r.status_code == 422


def test_a_duplicate_subject_is_a_conflict(env):
    client, _ = env
    body = {"subject": "svc:ci"}
    assert _call(client, "POST", "/_cache/authz/principals", body, AUTH).status_code == 201
    r = _call(client, "POST", "/_cache/authz/principals", body, AUTH)
    assert r.status_code == 409, r.text


@pytest.mark.parametrize("subject", ["", " svc:ci", "svc:ci ", "a/b", "x" * 300, "a\nb"])
def test_a_subject_that_would_not_round_trip_is_refused_not_normalised(env, subject):
    client, _ = env
    r = _call(client, "POST", "/_cache/authz/principals", {"subject": subject}, AUTH)
    assert r.status_code == 400, f"{subject!r} -> {r.status_code}"


def test_unknown_principal_is_404_everywhere(env):
    client, _ = env
    for method, path, body in [
        ("PUT", "/_cache/authz/principals/ghost/rules", {"rules": ["* pull"]}),
        ("POST", "/_cache/authz/principals/ghost/keys", {}),
        ("DELETE", "/_cache/authz/principals/ghost", None),
        ("GET", "/_cache/authz/keys?principal=ghost", None),
    ]:
        r = _call(client, method, path, body, AUTH)
        assert r.status_code == 404, f"{method} {path} -> {r.status_code}"


def test_the_last_admin_cannot_be_deleted(env):
    client, _ = env
    _call(client, "POST", "/_cache/authz/principals", {"subject": "ops", "is_admin": True}, AUTH)
    r = _call(client, "DELETE", "/_cache/authz/principals/ops", None, AUTH)
    assert r.status_code == 409, r.text
    assert "admin" in r.json()["detail"]


# ---------------- rules ----------------


@pytest.mark.parametrize("bad", ["docker.io/* pul", "docker.io/* pull push", "x read"])
def test_bad_rule_syntax_is_400_with_the_line_named(env, bad):
    client, _ = env
    _call(client, "POST", "/_cache/authz/principals", {"subject": "svc:ci"}, AUTH)
    r = _call(client, "PUT", "/_cache/authz/principals/svc:ci/rules",
              {"rules": ["* pull", bad]}, AUTH)
    assert r.status_code == 400, r.text
    assert bad in r.json()["detail"]


def test_a_rejected_rule_set_changes_nothing(env):
    """A partially applied allowlist is the worst outcome: neither the old
    grant nor the new one, and a 400 that reads as "nothing happened"."""
    client, _ = env
    _call(client, "POST", "/_cache/authz/principals", {"subject": "svc:ci"}, AUTH)
    _call(client, "PUT", "/_cache/authz/principals/svc:ci/rules", {"rules": ["a.io/* pull"]}, AUTH)
    _call(client, "PUT", "/_cache/authz/principals/svc:ci/rules",
          {"rules": ["b.io/* pull", "c.io/* nonsense"]}, AUTH)
    users = _call(client, "GET", "/_cache/authz/principals", None, AUTH).json()["principals"]
    assert users[0]["rules"] == [{"pattern": "a.io/*", "pull": True, "push": False}]


def test_rule_text_matches_the_console_syntax(env):
    client, _ = env
    _call(client, "POST", "/_cache/authz/principals", {"subject": "svc:ci"}, AUTH)
    r = _call(client, "PUT", "/_cache/authz/principals/svc:ci/rules",
              {"rules": ["docker.io/library/*", "ghcr.io/me/* pull+push", "x.io/* push"]}, AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["rules"] == [
        {"pattern": "docker.io/library/*", "pull": True, "push": False},
        {"pattern": "ghcr.io/me/*", "pull": True, "push": True},
        {"pattern": "x.io/*", "pull": False, "push": True},
    ]


def test_bad_scope_syntax_is_400_and_mints_nothing(env):
    client, _ = env
    _call(client, "POST", "/_cache/authz/principals", {"subject": "svc:ci"}, AUTH)
    r = _call(client, "POST", "/_cache/authz/principals/svc:ci/keys",
              {"scope": ["docker.io/* sometimes"]}, AUTH)
    assert r.status_code == 400
    assert _call(client, "GET", "/_cache/authz/keys", None, AUTH).json()["keys"] == []


def test_a_star_scope_is_stored_as_no_limit_as_the_console_does(env):
    client, _ = env
    key_id, _ = _provision(client, scope=["*"])
    keys = _call(client, "GET", "/_cache/authz/keys", None, AUTH).json()["keys"]
    assert keys[0]["key_id"] == key_id and keys[0]["scope"] == []


# ---------------- the secret ----------------


def test_the_minted_secret_authenticates_on_v2_and_the_hf_surface(env):
    client, _ = env
    key_id, secret = _provision(client)

    assert client.get("/v2/").status_code == 401
    assert client.get("/v2/", auth=(key_id, "wrong")).status_code == 401
    assert client.get("/v2/", auth=(key_id, secret)).status_code == 200

    local = {"x-muninn-local-only": "1"}
    path = "/gpt2/resolve/main/config.json"
    assert client.get(path, headers=local).status_code == 401
    r = client.get(path, headers={**local, "authorization": f"Bearer {key_id}:{secret}"})
    assert r.status_code != 401, r.text
    r = client.get(path, headers=local, auth=(key_id, secret))
    assert r.status_code != 401, r.text


def test_the_hf_surface_consults_rules(env, monkeypatch):
    """Pins the README's statement that rules are enforced on the Hugging Face
    surface too (XHC_HF_RULES=enforce, the default). This test used to pin the
    opposite -- an empty allowlist passing the HF gate -- and its assertion was
    `!= 401`, which a 403 ALSO satisfies: it kept passing after the behaviour it
    described had gone. Every assertion here names the exact status, so it
    cannot outlive what it claims.

    An empty allowlist grants nothing on either surface; `models/...` grants the HF
    repo and nothing on /v2; XHC_HF_RULES=off restores the gate-only behaviour."""
    from app.config import settings

    client, _ = env
    commit = "a" * 40
    snap = Path(settings.cache_dir) / "models--org--model" / "snapshots" / commit
    snap.mkdir(parents=True)
    (snap / "config.json").write_text("{}")
    path = f"/org/model/resolve/{commit}/config.json"
    tags = "/v2/docker.io/library/alpine/tags/list"

    empty_id, empty_secret = _provision(client, subject="svc:empty", rules=[])
    empty = {"authorization": f"Bearer {empty_id}:{empty_secret}"}
    assert client.get(path, headers=empty).status_code == 403
    assert client.get(tags, auth=(empty_id, empty_secret)).status_code == 403

    hf_id, hf_secret = _provision(client, subject="svc:hf", rules=["models/org/* pull"])
    hf = {"authorization": f"Bearer {hf_id}:{hf_secret}"}
    assert client.get(path, headers=hf).status_code == 200
    assert client.get(tags, auth=(hf_id, hf_secret)).status_code == 403

    monkeypatch.setattr(settings, "hf_rules", "off")
    assert client.get(path, headers=empty).status_code == 200


def test_the_minted_key_is_authorised_by_the_rules_it_was_given(env):
    client, _ = env
    key_id, secret = _provision(client, rules=["docker.io/library/* pull"],
                                scope=["docker.io/library/alpine pull"])
    from app import authz, dockerauth

    key = dockerauth.store().resolve(key_id, secret)
    assert authz.decide(key, "pull", "docker.io/library/alpine")[0]
    assert not authz.decide(key, "pull", "docker.io/library/redis")[0]  # scoped away
    assert not authz.decide(key, "push", "docker.io/library/alpine")[0]  # never granted


def test_the_secret_is_never_listed_and_never_stored_in_plaintext(env):
    client, db = env
    key_id, secret = _provision(client)
    for path in ("/_cache/authz/principals", "/_cache/authz/keys",
                 "/_cache/authz/keys?principal=svc:ci"):
        r = _call(client, "GET", path, None, AUTH)
        assert r.status_code == 200
        assert secret not in r.text
        assert "secret" not in r.text  # neither the value nor its hash field
    raw = db.read_bytes()
    for side in db.parent.glob(db.name + "*"):
        raw += side.read_bytes()
    assert secret.encode() not in raw


def test_the_mint_response_is_not_cacheable(env):
    client, _ = env
    _call(client, "POST", "/_cache/authz/principals", {"subject": "svc:ci"}, AUTH)
    # No body at all: label and scope are both optional.
    r = client.post("/_cache/authz/principals/svc:ci/keys", headers=AUTH)
    assert r.status_code == 201, r.text
    assert "no-store" in r.headers.get("cache-control", "")


def test_there_is_no_way_to_supply_the_secret(env):
    """The server generates it. A caller-chosen secret is a low-entropy
    password with a sha256 on it, which is the case bcrypt exists for."""
    client, _ = env
    _call(client, "POST", "/_cache/authz/principals", {"subject": "svc:ci"}, AUTH)
    r = _call(client, "POST", "/_cache/authz/principals/svc:ci/keys",
              {"secret": "hunter2"}, AUTH)
    assert r.status_code == 422


# ---------------- revocation ----------------


def test_a_disabled_key_is_refused_and_re_enabling_restores_it(env):
    client, _ = env
    key_id, secret = _provision(client)
    r = _call(client, "POST", f"/_cache/authz/keys/{key_id}/disabled", {"disabled": True}, AUTH)
    assert r.status_code == 200
    assert client.get("/v2/", auth=(key_id, secret)).status_code == 401
    _call(client, "POST", f"/_cache/authz/keys/{key_id}/disabled", {"disabled": False}, AUTH)
    assert client.get("/v2/", auth=(key_id, secret)).status_code == 200


def test_a_deleted_key_is_refused(env):
    client, _ = env
    key_id, secret = _provision(client)
    assert _call(client, "DELETE", f"/_cache/authz/keys/{key_id}", None, AUTH).status_code == 200
    assert client.get("/v2/", auth=(key_id, secret)).status_code == 401


def test_a_deleted_principals_keys_are_refused(env):
    client, _ = env
    key_id, secret = _provision(client)
    r = _call(client, "DELETE", "/_cache/authz/principals/svc:ci", None, AUTH)
    assert r.status_code == 200
    assert client.get("/v2/", auth=(key_id, secret)).status_code == 401
    assert client.get("/gpt2/resolve/main/config.json",
                      headers={"authorization": f"Bearer {key_id}:{secret}",
                               "x-muninn-local-only": "1"}).status_code == 401


def test_acting_on_an_unknown_key_is_404_not_a_silent_200(env):
    client, _ = env
    for method, path, body in [
        ("POST", "/_cache/authz/keys/nope/disabled", {"disabled": True}),
        ("DELETE", "/_cache/authz/keys/nope", None),
    ]:
        r = _call(client, method, path, body, AUTH)
        assert r.status_code == 404, f"{method} {path} -> {r.status_code}"


def test_the_store_reports_an_unknown_key_rather_than_succeeding(tmp_path):
    """The store-level half of the test above. An UPDATE or DELETE matching no
    rows succeeds, so disabling a mistyped key id reported success while
    revoking nothing -- the same shape set_principal_disabled already guards."""
    from app.authzstore import AuthzStore

    store = AuthzStore(tmp_path / "a.db")
    with pytest.raises(KeyError):
        store.set_key_disabled("no-such-key", True)
    with pytest.raises(KeyError):
        store.delete_key("no-such-key")


# ---------------- the CLI ----------------


def _ctl(db, *args, stdin=None):
    import subprocess

    root = Path(__file__).resolve().parent.parent
    return subprocess.run(
        [sys.executable, "-m", "app.authzctl", "--db", str(db), *args],
        cwd=root, capture_output=True, text=True, input=stdin, timeout=60, check=False,
        env={k: v for k, v in os.environ.items() if not k.startswith("XHC_")},
    )


def test_cli_mint_authenticates_on_the_very_next_request_to_a_running_server(env):
    """The freshness property, across PROCESSES: the server's store is already
    open and has already served a request (so its key cache is warm), and the
    key is written by a separate process on the same file."""
    client, db = env
    assert client.get("/v2/", auth=("x", "y")).status_code == 401  # warm the cache

    assert _ctl(db, "create-principal", "svc:ci").returncode == 0
    assert _ctl(db, "set-rules", "svc:ci", "docker.io/* pull").returncode == 0
    out = _ctl(db, "mint", "svc:ci", "--label", "ci")
    assert out.returncode == 0, out.stderr
    minted = json.loads(out.stdout)

    assert client.get("/v2/", auth=(minted["key_id"], minted["secret"])).status_code == 200

    assert _ctl(db, "disable-key", minted["key_id"]).returncode == 0
    assert client.get("/v2/", auth=(minted["key_id"], minted["secret"])).status_code == 401


def test_two_store_instances_on_one_file_see_each_others_keys(tmp_path):
    from app import authzadmin
    from app.authzstore import AuthzStore

    db = tmp_path / "a.db"
    server, cli = AuthzStore(db), AuthzStore(db)
    assert server.resolve("x", "y") is None  # warm the server's cache
    authzadmin.create_principal(cli, "svc:ci")
    authzadmin.set_rules(cli, "svc:ci", ["* pull"])
    minted = authzadmin.mint_key(cli, "svc:ci")
    assert server.resolve(minted["key_id"], minted["secret"]) is not None


def test_cli_created_principal_is_not_admin_on_an_empty_store(tmp_path):
    db = tmp_path / "a.db"
    assert _ctl(db, "create-principal", "svc:ci").returncode == 0
    listing = json.loads(_ctl(db, "list").stdout)
    assert listing["principals"][0]["is_admin"] is False


def test_cli_secret_file_is_0600_and_the_secret_is_not_on_stdout(tmp_path):
    db = tmp_path / "a.db"
    _ctl(db, "create-principal", "svc:ci")
    _ctl(db, "set-rules", "svc:ci", "* pull")
    target = tmp_path / "out" / "secret"
    target.parent.mkdir()
    out = _ctl(db, "mint", "svc:ci", "--secret-file", str(target))
    assert out.returncode == 0, out.stderr
    secret = target.read_text()
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert secret and secret not in out.stdout and secret not in out.stderr
    key_id = json.loads(out.stdout)["key_id"]

    from app.authzstore import AuthzStore
    assert AuthzStore(db).resolve(key_id, secret) is not None


def test_cli_secret_file_token_format_is_what_hf_token_wants(tmp_path):
    db = tmp_path / "a.db"
    _ctl(db, "create-principal", "svc:ci")
    target = tmp_path / "token"
    out = _ctl(db, "mint", "svc:ci", "--secret-file", str(target),
               "--secret-file-format", "token")
    assert out.returncode == 0, out.stderr
    key_id, _, secret = target.read_text().partition(":")
    assert key_id == json.loads(out.stdout)["key_id"]

    from app.authzstore import AuthzStore
    assert AuthzStore(db).resolve(key_id, secret) is not None


def test_cli_refuses_to_overwrite_a_secret_file_and_mints_nothing(tmp_path):
    db = tmp_path / "a.db"
    _ctl(db, "create-principal", "svc:ci")
    target = tmp_path / "secret"
    target.write_text("existing")
    out = _ctl(db, "mint", "svc:ci", "--secret-file", str(target))
    assert out.returncode != 0
    assert target.read_text() == "existing"
    assert json.loads(_ctl(db, "list").stdout)["keys"] == []


def test_cli_only_mint_prints_a_secret(tmp_path):
    db = tmp_path / "a.db"
    _ctl(db, "create-principal", "svc:ci")
    _ctl(db, "set-rules", "svc:ci", "* pull")
    secret = json.loads(_ctl(db, "mint", "svc:ci").stdout)["secret"]
    listing = _ctl(db, "list")
    assert listing.returncode == 0
    assert secret not in listing.stdout and "secret" not in listing.stdout


def test_cli_errors_are_specific_and_nonzero(tmp_path):
    db = tmp_path / "a.db"
    r = _ctl(db, "mint", "ghost")
    assert r.returncode != 0 and "ghost" in r.stderr
    _ctl(db, "create-principal", "svc:ci")
    r = _ctl(db, "create-principal", "svc:ci")
    assert r.returncode != 0 and "exists" in r.stderr
    r = _ctl(db, "set-rules", "svc:ci", "docker.io/* pul")
    assert r.returncode != 0 and "docker.io/* pul" in r.stderr
    r = _ctl(db, "disable-key", "nope")
    assert r.returncode != 0


def test_cli_exist_ok_is_idempotent_but_not_on_a_different_admin_flag(tmp_path):
    """An init container re-runs on every pod start. Re-creating the same
    principal must succeed; re-creating it with a DIFFERENT admin flag must not
    quietly report success over a principal that does not match the request."""
    db = tmp_path / "a.db"
    assert _ctl(db, "create-principal", "svc:ci", "--exist-ok").returncode == 0
    assert _ctl(db, "create-principal", "svc:ci", "--exist-ok").returncode == 0
    r = _ctl(db, "create-principal", "svc:ci", "--exist-ok", "--admin")
    assert r.returncode != 0 and "admin" in r.stderr


def test_cli_set_rules_with_no_rules_needs_an_explicit_flag(tmp_path):
    """Emptying an allowlist revokes everything the principal can do. That
    should not be what a script produces when a variable expands to nothing."""
    db = tmp_path / "a.db"
    _ctl(db, "create-principal", "svc:ci")
    _ctl(db, "set-rules", "svc:ci", "* pull")
    assert _ctl(db, "set-rules", "svc:ci").returncode != 0
    assert json.loads(_ctl(db, "list").stdout)["principals"][0]["rules"]
    assert _ctl(db, "set-rules", "svc:ci", "--empty").returncode == 0
    assert json.loads(_ctl(db, "list").stdout)["principals"][0]["rules"] == []


def test_cli_without_a_db_refuses(tmp_path):
    import subprocess

    root = Path(__file__).resolve().parent.parent
    r = subprocess.run([sys.executable, "-m", "app.authzctl", "list"], cwd=root,
                       capture_output=True, text=True, timeout=60, check=False,
                       env={k: v for k, v in os.environ.items() if not k.startswith("XHC_")})
    assert r.returncode != 0 and "XHC_AUTHZ_DB" in r.stderr


# ---------------- docs ----------------


def test_every_provisioning_route_is_named_in_the_readme():
    """The app-wide guard in test_docs_name_their_knobs.py covers these too,
    because the router is always mounted. This one is pointed at the router
    directly so it does not depend on that mounting decision, and also names
    the CLI, which no route listing can see."""
    from app import authzmanage

    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text()
    missing = sorted({r.path for r in authzmanage.router.routes
                      if r.path.split("{")[0].rstrip("/") not in readme})
    assert not missing, missing
    assert "python -m app.authzctl" in readme


# ---------------- Hugging Face rule shapes, on every surface that saves rules ----


HF_REFUSALS = [
    ("hf/models/org/x pull", "'hf/' is not a rule prefix"),
    ("models/org/x push", "pull-only"),
    ("model/org/x pull", "unknown type prefix"),
    ("google/gemma pull", "neither a registry host nor a Hugging Face type"),
]


@pytest.mark.parametrize("line,reason", HF_REFUSALS)
def test_the_api_refuses_an_hf_rule_it_cannot_enforce_with_the_reason(env, line, reason):
    client, _ = env
    _call(client, "POST", "/_cache/authz/principals", {"subject": "svc:ci"}, AUTH)
    r = _call(client, "PUT", "/_cache/authz/principals/svc:ci/rules", {"rules": [line]}, AUTH)
    assert r.status_code == 400
    assert reason in r.text


@pytest.mark.parametrize("line,reason", HF_REFUSALS)
def test_the_cli_refuses_an_hf_rule_it_cannot_enforce_with_the_reason(env, line, reason):
    _, db = env
    assert _ctl(db, "create-principal", "svc:ci").returncode == 0
    r = _ctl(db, "set-rules", "svc:ci", line)
    assert r.returncode != 0
    assert reason in r.stderr + r.stdout


def test_the_api_and_cli_accept_the_allowlist_shape(env):
    client, db = env
    _call(client, "POST", "/_cache/authz/principals", {"subject": "svc:ci"}, AUTH)
    r = _call(client, "PUT", "/_cache/authz/principals/svc:ci/rules",
              {"rules": ["models/google/gemma-4-* pull"]}, AUTH)
    assert r.status_code == 200, r.text
    assert _ctl(db, "set-rules", "svc:ci", "datasets/org/* pull").returncode == 0
