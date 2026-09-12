"""Per-key authorisation as it behaves ON THE WIRE, not in isolation.

app/authz.py proves the decision function is right. This proves the decision is
actually CONSULTED, for the right operation, on every route that names a
repository — which is a different claim and the one that can silently regress.

THE COUPLING IS THE DESIGN. Authorisation happens inside _resolve_or_error, which
every /v2 route naming a repository must already call. A route cannot skip
authorisation without also failing to resolve its reference, which fails loudly.
The alternative — a separate authorize() call each route must remember — is a
fail-open waiting for one distracted edit, and these tests would not catch it
because a forgotten call looks exactly like an allowed request.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.authz import Rule, new_secret


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """A running app with authz enabled and one pull-only key."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app import dockerauth, ocicompat
    from app.authzstore import AuthzStore
    from app.config import settings

    db = tmp_path / "authz.db"
    monkeypatch.setattr(settings, "authz_db", str(db))
    monkeypatch.setattr(settings, "docker_enabled", True)
    monkeypatch.setattr(settings, "docker_push_enabled", True)
    # The push path writes policy state and staging files. Without these the
    # handlers 500 on a PermissionError against the default /cache, which looks
    # exactly like an authorisation failure in a test about authorisation.
    monkeypatch.setattr(settings, "cache_dir", str(tmp_path / "cache"))
    monkeypatch.setattr(settings, "docker_dir", str(tmp_path / "oci"))
    monkeypatch.setattr(dockerauth, "_store", None)  # reopen against tmp db

    store = AuthzStore(db)
    store.claim_or_get_principal("sub-1", "a@example.com")
    puller, puller_secret = new_secret()
    store.add_key(puller, puller_secret, "sub-1",
                  [Rule("docker.io/*", pull=True, push=False)])
    pusher, pusher_secret = new_secret()
    store.add_key(pusher, pusher_secret, "sub-1",
                  [Rule("docker.io/*", pull=True, push=True)])

    app = FastAPI()
    app.include_router(ocicompat.router,
                       dependencies=[__import__("fastapi").Depends(dockerauth.require_pull_auth)])
    return TestClient(app, raise_server_exceptions=False), \
        (puller, puller_secret), (pusher, pusher_secret)


def test_no_credential_is_refused(wired):
    client, _, _ = wired
    r = client.get("/v2/docker.io/library/alpine/tags/list")
    assert r.status_code == 401
    assert "basic" in r.headers.get("www-authenticate", "").lower()


def test_an_unknown_key_is_refused(wired):
    client, _, _ = wired
    r = client.get("/v2/docker.io/library/alpine/tags/list", auth=("nope", "nope"))
    assert r.status_code == 401


def test_a_pull_key_is_refused_on_a_PUSH_route(wired):
    """THE POINT OF THE WHOLE FEATURE. Pull and push were the same permission
    until this change; a pull-only key reaching a push route must be 403, not 202.
    """
    client, puller, _ = wired
    r = client.post("/v2/docker.io/library/alpine/blobs/uploads/", auth=puller)
    assert r.status_code == 403, f"a pull-only key started an upload: {r.status_code}"


def test_a_pull_key_is_refused_on_a_MANIFEST_PUSH(wired):
    """The route whose operation I initially mis-wired as 'pull'.

    It was caught only because the mapping was printed while refactoring. A
    manifest PUT is how a tag actually moves, so classifying it as a pull would
    have let any pull-only key overwrite any tag it could read.
    """
    client, puller, _ = wired
    r = client.put("/v2/docker.io/library/alpine/manifests/latest",
                   auth=puller, content=b"{}")
    assert r.status_code == 403, f"a pull-only key pushed a manifest: {r.status_code}"


def test_a_push_key_is_not_refused_by_authorisation_on_a_push_route(wired):
    """POSITIVE CONTROL. Without this, a blanket 403 would pass every test above.

    It asserts only that authorisation did not refuse -- the request may still
    fail downstream on a real upstream, which is not what is under test here.
    """
    client, _, pusher = wired
    r = client.post("/v2/docker.io/library/alpine/blobs/uploads/", auth=pusher)
    assert r.status_code != 403, "the push key was refused by authorisation"
    assert r.status_code != 401


def test_a_key_is_refused_on_a_repository_outside_its_rules(wired):
    """Breadth is enforced, not just operation."""
    client, puller, _ = wired
    r = client.get("/v2/ghcr.io/someone/else/tags/list", auth=puller)
    assert r.status_code == 403


def test_every_v2_route_that_names_a_repo_authorises_it(wired):
    """A SOURCE-LEVEL GUARD, because a missing call cannot be seen from outside.

    A route that forgets to authorise returns a normal response, which is
    indistinguishable from an allowed one. So this asserts the structural
    property instead: every call that resolves a name passes a request and an
    explicit operation, and none relies on the default.
    """
    import re

    src = Path(__import__("app.ocicompat", fromlist=["x"]).__file__).read_text()
    calls = re.findall(r"_resolve_or_error\(name[^)]*\)", src)
    assert calls, "no resolve calls found -- the guard is looking in the wrong place"
    for call in calls:
        assert "request" in call, f"resolve without a request cannot authorise: {call}"
        assert '"pull"' in call or '"push"' in call, \
            f"resolve must name its operation explicitly, not inherit a default: {call}"


# ---------------------------------------------------------------------------
# Upload-session hijacking.
#
# A push is four requests and only the FIRST is authorised against a
# repository: POST /blobs/uploads/ resolves the name and checks the rule. PATCH
# and PUT carry a session uuid, and the repository they write to comes from the
# session rather than from their own path -- so authorising their path would
# authorise something they do not use.
#
# That leaves the session itself as the thing to protect, which is what these
# tests are about. They are the reason `_resolve_or_error` being the coupling
# point is NOT sufficient on its own.
# ---------------------------------------------------------------------------


def _open_upload(client, cred):
    r = client.post("/v2/docker.io/library/alpine/blobs/uploads/", auth=cred)
    assert r.status_code == 202, r.text
    return r.headers["docker-upload-uuid"]


def test_a_second_key_cannot_continue_an_upload_it_did_not_open(wired):
    """The isolation break this binding exists to close.

    Both keys here are legitimate and authenticated. The attacker's key has no
    push rule at all, so it could never have OPENED this session -- and without
    binding it can still finish one, writing bytes into a repository it was
    refused. On a shared cache that is one tenant completing another's push.
    """
    client, puller, pusher = wired
    uuid = _open_upload(client, pusher)

    hijack = client.patch(
        f"/v2/docker.io/library/alpine/blobs/uploads/{uuid}",
        auth=puller, content=b"malicious",
    )
    assert hijack.status_code == 404, hijack.text

    finish = client.put(
        f"/v2/docker.io/library/alpine/blobs/uploads/{uuid}"
        "?digest=sha256:" + "0" * 64,
        auth=puller, content=b"malicious",
    )
    assert finish.status_code == 404, finish.text


def test_an_unknown_session_and_a_stolen_one_are_indistinguishable(wired):
    """Same status, same code. Telling the caller a uuid is real but not theirs
    confirms someone else's session is live, which is the first half of
    hijacking it."""
    client, puller, pusher = wired
    stolen = _open_upload(client, pusher)

    absent = "00000000-0000-4000-8000-000000000000"
    a = client.patch(f"/v2/docker.io/library/alpine/blobs/uploads/{stolen}",
                     auth=puller, content=b"x")
    b = client.patch(f"/v2/docker.io/library/alpine/blobs/uploads/{absent}",
                     auth=puller, content=b"x")
    assert a.status_code == b.status_code == 404

    # Each body echoes the uuid THE CALLER SUPPLIED, which tells them nothing
    # they did not already have. Compare with their own input normalised out --
    # comparing the raw bodies would fail on that echo and would be asserting
    # something stricter than the property, which is that the two cases are
    # indistinguishable to the caller.
    assert a.json()["errors"][0]["code"] == b.json()["errors"][0]["code"]
    assert (
        a.text.replace(stolen, "UUID") == b.text.replace(absent, "UUID")
    ), "a stolen uuid must be reported exactly as an absent one"


def test_the_opening_key_can_still_finish_its_own_upload(wired):
    """THE POSITIVE CONTROL. Without it, the two tests above are satisfied by a
    binding that refuses every PATCH and PUT, which would break all pushes and
    still look like a passing security test."""
    import hashlib

    client, _, pusher = wired
    uuid = _open_upload(client, pusher)

    body = b"a legitimate layer"
    cont = client.patch(f"/v2/docker.io/library/alpine/blobs/uploads/{uuid}",
                        auth=pusher, content=body)
    assert cont.status_code == 202, cont.text
    # The PUT forwards upstream, which these tests do not reach -- what is being
    # asserted is that the session was ACCEPTED, i.e. not a 404 from the binding.
    done = client.put(
        f"/v2/docker.io/library/alpine/blobs/uploads/{uuid}"
        "?digest=sha256:" + hashlib.sha256(body).hexdigest(),
        auth=pusher, content=b"",
    )
    assert done.status_code != 404, (
        "the opening key must not be refused its own session: " + done.text
    )
