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
