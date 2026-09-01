"""Optional client-facing auth on the pull surface (an internal issue).

A GATE, not per-client isolation. Passing it means you may use this cache;
everyone who passes sees everything it holds. That is the maintainer's ruling made
enforceable rather than weakened -- per-client authorization is impossible here
because a cached HIT consults no credentials at all, so any scheme promising
"A cannot read what B pulled" would be enforced on the miss and silently absent
on every hit after it.

the maintainer ruled htpasswd with bcrypt over a single shared secret (2026-09-01),
because a docker pull cannot send an identifying header, ocicompat records no
principal, and the mesh gateway masquerades -- so per-client credentials are the
ONLY mechanism by which this cache can ever know which node pulled what.

The tests that matter most here are the NEGATIVE ones: that an unreadable
credential file refuses to start rather than serving open, and that the gate
does not leak onto /healthz or /metrics. A gate that silently disappears is
worse than no gate, and a gate that swallows the health endpoints breaks
monitoring the moment anyone enables it.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import bcrypt
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import dockerauth
from app.config import settings


def _htpasswd(tmp_path, entries):
    p = tmp_path / "htpasswd"
    p.write_text("".join(
        f"{u}:{bcrypt.hashpw(pw.encode(), bcrypt.gensalt(rounds=4)).decode()}\n"
        for u, pw in entries))
    return str(p)


def _basic(user, password):
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


@pytest.fixture(autouse=True)
def _restore():
    auth, htp = settings.docker_auth, settings.docker_htpasswd
    yield
    settings.docker_auth, settings.docker_htpasswd = auth, htp
    dockerauth._users = None


def _enable(tmp_path, entries=(("hiro", "s3cret"),)):
    settings.docker_auth = "basic"
    settings.docker_htpasswd = _htpasswd(tmp_path, entries)
    dockerauth.load()


# --- the default must not change for anyone -------------------------------

def test_default_none_is_wide_open(tmp_path):
    settings.docker_auth = "none"
    settings.docker_htpasswd = None
    dockerauth.load()
    assert dockerauth._users is None
    assert dockerauth._check(None) is False  # not consulted; gate is bypassed


# --- fail closed ----------------------------------------------------------

def test_unreadable_file_refuses_to_start(tmp_path):
    """The an internal issue shape: unknown must never resolve to permissive."""
    settings.docker_auth = "basic"
    settings.docker_htpasswd = str(tmp_path / "does-not-exist")
    with pytest.raises(dockerauth.HtpasswdError, match="unreadable"):
        dockerauth.load()


def test_basic_without_a_file_refuses_to_start():
    settings.docker_auth = "basic"
    settings.docker_htpasswd = None
    with pytest.raises(dockerauth.HtpasswdError, match="requires XHC_DOCKER_HTPASSWD"):
        dockerauth.load()


def test_empty_file_refuses_to_start(tmp_path):
    """Zero users is not 'no auth' -- it is a file that will admit nobody."""
    p = tmp_path / "htpasswd"
    p.write_text("# only a comment\n\n")
    settings.docker_auth = "basic"
    settings.docker_htpasswd = str(p)
    with pytest.raises(dockerauth.HtpasswdError, match="no usable entries"):
        dockerauth.load()


def test_non_bcrypt_entry_is_refused(tmp_path):
    """Accepting a weak format silently would make a weak file look configured."""
    p = tmp_path / "htpasswd"
    p.write_text("legacy:$apr1$abcdefgh$0123456789012345678901\n")
    settings.docker_auth = "basic"
    settings.docker_htpasswd = str(p)
    with pytest.raises(dockerauth.HtpasswdError, match="not bcrypt"):
        dockerauth.load()


# --- the gate itself ------------------------------------------------------

def test_correct_credentials_pass(tmp_path):
    _enable(tmp_path)
    assert dockerauth._check(_basic("hiro", "s3cret")) is True


def test_wrong_password_fails(tmp_path):
    _enable(tmp_path)
    assert dockerauth._check(_basic("hiro", "wrong")) is False


def test_unknown_user_fails(tmp_path):
    _enable(tmp_path)
    assert dockerauth._check(_basic("nobody", "s3cret")) is False


def test_missing_and_malformed_headers_fail(tmp_path):
    _enable(tmp_path)
    for header in (None, "", "Bearer abc", "Basic", "Basic !!!not-base64!!!",
                   "Basic " + base64.b64encode(b"no-colon").decode()):
        assert dockerauth._check(header) is False, header


def test_multiple_users_are_independent(tmp_path):
    """The point of per-host credentials: one node's secret is not another's."""
    _enable(tmp_path, (("hiro", "aaa"), ("mimir", "bbb")))
    assert dockerauth._check(_basic("hiro", "aaa")) is True
    assert dockerauth._check(_basic("mimir", "bbb")) is True
    assert dockerauth._check(_basic("hiro", "bbb")) is False, "credentials crossed over"


def test_a_user_can_be_revoked_without_touching_the_others(tmp_path):
    """Rotation is the reason this is htpasswd and not one shared secret."""
    _enable(tmp_path, (("hiro", "aaa"), ("mimir", "bbb")))
    settings.docker_htpasswd = _htpasswd(tmp_path, (("mimir", "bbb"),))
    dockerauth.load()
    assert dockerauth._check(_basic("hiro", "aaa")) is False
    assert dockerauth._check(_basic("mimir", "bbb")) is True


# --- the gate must land on /v2/* and NOWHERE else --------------------------
#
# This is the operationally dangerous half. A gate that swallows /healthz or
# /metrics breaks liveness checks and Prometheus scraping the moment anyone
# enables it -- a fleet-wide symptom caused by a security feature that is
# working exactly as written. A blanket reverse-proxy rule gets this wrong by
# default, which is the main reason this is native rather than delegated.

from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    # The app's lifespan mkdirs the cache roots; point them somewhere writable
    # so these tests exercise the ROUTING of the gate, not the filesystem.
    monkeypatch.setattr(settings, "cache_dir", str(tmp_path / "cache"))
    monkeypatch.setattr(settings, "docker_dir", str(tmp_path / "docker"))
    from app.main import app

    return TestClient(app)


def test_v2_is_challenged_when_auth_is_on(tmp_path, client):
    _enable(tmp_path)
    r = client.get("/v2/")
    assert r.status_code == 401
    # The challenge is what makes `docker login` work, and it is also what
    # distinguishes a MUNINN 401 from an upstream auth failure, which is a 502
    # carrying x-xhc-upstream-auth and never a challenge (an internal issue).
    assert r.headers["www-authenticate"] == 'Basic realm="muninn"'


def test_v2_passes_with_credentials(tmp_path, client):
    _enable(tmp_path)
    r = client.get("/v2/", headers={"authorization": _basic("hiro", "s3cret")})
    assert r.status_code == 200


def test_healthz_and_metrics_are_never_gated(tmp_path, client):
    """Unauthenticated by design. Enabling client auth must not touch them."""
    _enable(tmp_path)
    for path in ("/healthz", "/metrics"):
        r = client.get(path)
        assert r.status_code != 401, f"{path} got swallowed by the pull gate"


def test_the_pull_credential_does_not_open_the_management_api(tmp_path, client, monkeypatch):
    """Two credentials, two surfaces, no crossover.

    /_cache has its own XHC_MANAGE_TOKEN. A pull credential must not satisfy it,
    or enabling client auth would quietly widen management access.
    """
    _enable(tmp_path)
    monkeypatch.setattr(settings, "manage_token", "management-secret")
    r = client.get("/_cache/images",
                   headers={"authorization": _basic("hiro", "s3cret")})
    assert r.status_code == 401


def test_v2_is_open_when_auth_is_off(client):
    """The default. Nothing changes for an existing deployment."""
    settings.docker_auth = "none"
    dockerauth.load()
    assert client.get("/v2/").status_code == 200
