"""Behaviours OTHER TEAMS assert, pinned here so changing one goes red.

WHY THIS FILE EXISTS. Twice in one night a change of mine broke something
another team had published as a check, and both times it was found by luck:

  * `/healthz`'s BODY is matched by a blackbox monitoring module -- not just the
    200. Changing the payload shape would stop their alerts firing, and STOPPED
    ALERTING IS INVISIBLE BY CONSTRUCTION: nothing goes red, the page simply
    never pages. Found because they mentioned it in passing.
  * A bogus repository name returning 404 is the line used to validate a Muninn
    deployment. A change of mine turned it into 502, so the documented smoke
    test began telling its reader the cache was broken. Found because someone
    happened to re-run it at 3am for unrelated reasons.

The shared observation was: neither side has a mechanism for "who asserts this
behaviour", and both sides document behaviour the other depends on. Searching
their documents from here does not scale and inverts the dependency.

THIS IS THE MECHANISM, AND IT IS DELIBERATELY LOCAL. A contract someone else
depends on becomes a test in the repo that can break it. It does not need their
document to be reachable, current, or even known to me later -- it needs the
assertion to live where the change happens. Adding a consumer means adding a
case here with a comment naming who depends on it and why.

These tests are not about whether the behaviour is GOOD. They are about it not
changing silently. If one fails, the fix may well be to update the contract --
after telling the consumer named in the comment.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def client(tmp_path, monkeypatch):
    from app.config import settings

    # The directories must EXIST: /healthz stats the filesystem and correctly
    # answers 503 when it cannot, which would make these contract tests pass or
    # fail for a reason unrelated to the contract.
    (tmp_path / "cache").mkdir()
    (tmp_path / "docker").mkdir()
    monkeypatch.setattr(settings, "cache_dir", str(tmp_path / "cache"))
    monkeypatch.setattr(settings, "docker_dir", str(tmp_path / "docker"))
    from app.main import app

    return TestClient(app)


def test_healthz_body_shape_is_a_monitoring_contract(client):
    """CONSUMER: a blackbox monitoring probe matches the BODY of /healthz, not
    only the status. `ok` and `free_bytes` are load-bearing for its alerting.

    Name consumers by ROLE here, never by team or host: this repo is public, and
    a contract file is exactly where the urge to be specific about who depends
    on what is strongest. The private board carries the identities.

    Stopped alerting is invisible: nothing goes red, the page just never pages.
    Tell them before changing this payload, and never "tidy" it.
    """
    r = client.get("/healthz")
    assert r.status_code == 200
    body = json.loads(r.text)
    assert body["ok"] is True
    assert isinstance(body["free_bytes"], int), "free_bytes must stay a plain integer"


def test_healthz_is_unauthenticated(client):
    """CONSUMER: the same blackbox module, plus any orchestrator healthcheck.

    Enabling client auth must never swallow this endpoint -- "turning on auth
    silently broke Prometheus" is a fleet-wide symptom produced by a security
    feature working exactly as written.
    """
    assert client.get("/healthz").status_code == 200
    assert client.get("/metrics").status_code == 200


def test_metrics_job_up_surface_is_scrapeable(client):
    """CONSUMER: `up{job="muninn"}` and the muninn_* series on their scrape.

    Renaming or removing a series breaks their dashboards and any alert built
    on it. Adding series is safe; these names are the ones already depended on.
    """
    body = client.get("/metrics").text
    for name in ("muninn_cache_bytes", "muninn_bytes_served_total",
                 "muninn_docker_requests_total"):
        assert name in body, f"{name} is scraped externally and must not vanish"


def test_bogus_repo_name_returns_404(client, monkeypatch):
    """CONSUMER: the documented deployment smoke test -- a repository that does
    not exist must return 404, so the control can tell "cache works, image is
    absent" from "cache is broken".

    This regressed to 502 once already. Registries that refuse to leak existence
    answer 401 for a nonexistent repo, and mapping that to 502 made every
    mistyped image name accuse the infrastructure.
    """
    from app import ocicompat, registry

    ref = registry.resolve("library/definitely-not-real-xyzzy")
    # Upstream issued a token and then refused: an ANSWER, not a gateway failure.
    r = ocicompat._unavailable(
        ref, {"status": 401, "authenticated": True}, "latest", "manifest")
    assert r.status_code == 404, (
        "a nonexistent repo must be 404; 502 tells the operator their "
        "infrastructure is broken when they have simply mistyped a name"
    )
