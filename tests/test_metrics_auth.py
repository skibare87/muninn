"""Who may read /metrics.

The reason this is a setting and not a fix: /metrics is an existing monitoring
contract. Gating it unconditionally would stop somebody's alerting, and stopped
alerting is invisible by construction -- nothing goes red, the page simply never
fires. So the default stays open and the gate is opted into by the operator who
can also update their scrape config.

Which means BOTH behaviours are correct behaviours, and both need testing: a
test suite that only covered the gated case would make the default look like an
oversight to the next reader.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.config import settings

    # Created, not just named: /healthz reports a missing cache dir as 503,
    # which is correct behaviour and would make the assertions below fail for a
    # reason that has nothing to do with the metrics gate.
    (tmp_path / "cache").mkdir()
    (tmp_path / "oci").mkdir()
    monkeypatch.setattr(settings, "cache_dir", str(tmp_path / "cache"))
    monkeypatch.setattr(settings, "docker_dir", str(tmp_path / "oci"))
    import app.main as main

    return TestClient(main.app, raise_server_exceptions=False), settings


def test_metrics_is_open_by_default(client):
    """The default, asserted so the next reader can see it is a decision."""
    c, settings = client
    assert settings.metrics_auth == "none"
    r = c.get("/metrics")
    assert r.status_code == 200
    assert "muninn_cache_bytes" in r.text


def test_a_token_gate_refuses_an_anonymous_scrape(client, monkeypatch):
    c, settings = client
    monkeypatch.setattr(settings, "metrics_auth", "token")
    monkeypatch.setattr(settings, "manage_token", "s3kret")
    assert c.get("/metrics").status_code == 401


def test_a_token_gate_refuses_a_wrong_token(client, monkeypatch):
    c, settings = client
    monkeypatch.setattr(settings, "metrics_auth", "token")
    monkeypatch.setattr(settings, "manage_token", "s3kret")
    for header in ("Bearer wrong", "s3kret", "Basic s3kret", "Bearer "):
        assert c.get("/metrics", headers={"authorization": header}).status_code == 401, header


def test_a_token_gate_admits_the_right_token(client, monkeypatch):
    """THE POSITIVE CONTROL. Three refusals above are equally satisfied by a
    gate that refuses everyone -- which would silently kill the monitoring this
    whole design is arranged around not breaking."""
    c, settings = client
    monkeypatch.setattr(settings, "metrics_auth", "token")
    monkeypatch.setattr(settings, "manage_token", "s3kret")
    r = c.get("/metrics", headers={"authorization": "Bearer s3kret"})
    assert r.status_code == 200
    assert "muninn_cache_bytes" in r.text


def test_healthz_is_never_gated_by_this(client, monkeypatch):
    """/healthz's BODY is a separate monitoring contract, matched by a blackbox
    probe. XHC_METRICS_AUTH must not reach it -- a liveness check that needs a
    credential is a liveness check that reports the credential's health."""
    c, settings = client
    monkeypatch.setattr(settings, "metrics_auth", "token")
    monkeypatch.setattr(settings, "manage_token", "s3kret")
    r = c.get("/healthz")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_asking_for_a_gate_with_no_credential_refuses_to_start(monkeypatch):
    """Fails on the ARGUMENTS, before any I/O. Otherwise the server starts and
    /metrics refuses everyone, including the monitoring that depends on it --
    the exact outcome the default exists to avoid, reached by opting in."""
    from app.config import Settings

    monkeypatch.setenv("XHC_METRICS_AUTH", "token")
    monkeypatch.delenv("XHC_MANAGE_TOKEN", raising=False)
    with pytest.raises(ValueError, match="XHC_MANAGE_TOKEN"):
        Settings.from_env()


def test_an_unknown_mode_refuses_to_start(monkeypatch):
    """A typo must not silently mean "open". XHC_METRICS_AUTH=tokne reading as
    unauthenticated is how a hardening step becomes a no-op nobody notices."""
    from app.config import Settings

    monkeypatch.setenv("XHC_METRICS_AUTH", "tokne")
    monkeypatch.setenv("XHC_MANAGE_TOKEN", "x")
    with pytest.raises(ValueError, match="none|token"):
        Settings.from_env()
