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
import re
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


# The consumer's blackbox module matches this against the raw body. Copied from
# their live configuration rather than inferred, so it is what actually fires.
HEALTHZ_BODY_RE = re.compile(r'"ok"[ \t\n]*:[ \t\n]*true')


def test_healthz_body_matches_the_probes_actual_regexp(client):
    """CONSUMER: a blackbox probe with
    `fail_if_body_not_matches_regexp: "\"ok\"[[:space:]]*:[[:space:]]*true"`.

    The pin is NARROWER than "body shape". The key must be literally `ok` and
    the value must be the JSON boolean `true`. Every one of these passes review
    and fails the probe:

        {"ok": 1}          {"ok": "true"}
        {"status": "ok"}   {"healthy": true}

    Whitespace between them is tolerated; nothing else is.
    """
    r = client.get("/healthz")
    assert HEALTHZ_BODY_RE.search(r.text), (
        f"body {r.text!r} does not match the probe's regexp -- their alert "
        "would stop firing, and stopped alerting is invisible"
    )


def test_healthz_status_is_exactly_200(client):
    """CONSUMER: the same probe, `valid_status_codes: [200]`. A 204 fails."""
    assert client.get("/healthz").status_code == 200


def test_healthz_and_metrics_are_served_locally_not_proxied(client):
    """CONSUMER: the load-bearing one, in their words.

    Muninn proxies everything that is not /v2/* or /_cache/* to the Hub, so a
    path like /healthcheck or /status returns 200 with ~70 KB of Hub HTML and
    reads HEALTHY NO MATTER WHAT. If a refactor moved /healthz or /metrics
    behind that catch-all, a status-code probe would go green forever against
    Hub HTML while the cache was dead.

    Their body regexp is the only thing standing between that and a
    permanently-green broken cache. This asserts the separation itself: these
    two paths must be answered locally, in their own format, not by the proxy.
    """
    h = client.get("/healthz")
    assert h.headers["content-type"].startswith("application/json")
    assert len(h.text) < 4096, "a large body here means the proxy answered"

    m = client.get("/metrics")
    assert "muninn_" in m.text, "the local exposition, not a proxied page"
    assert not m.text.lstrip().startswith("<"), "HTML here means the proxy answered"


def test_healthz_and_metrics_are_not_wired_to_a_shared_readiness_gate(client, monkeypatch):
    """CONSUMER: two SEPARATE alerts, deliberately, because one must not be
    inferable from the other.

    In 0.5.0 /metrics returned 500 for the entire life of every ingest while
    /healthz was green and docker reported "Up (healthy)". Wiring them to a
    shared readiness check would silently destroy that.

    WHAT THIS PINS is the absence of a shared gate: each endpoint reaches its
    own verdict, and neither consults the other. It deliberately does NOT
    assert that one survives the other's failure -- see the test below, which
    records a dependency they really do share.
    """
    from app import cachefs

    monkeypatch.setattr(cachefs, "disk_stats",
                        lambda: (_ for _ in ()).throw(OSError("simulated")))
    # healthz reaches its own verdict and renders it as 503 rather than raising.
    assert client.get("/healthz").status_code == 503


def test_healthz_and_metrics_share_a_disk_dependency(client, monkeypatch):
    """NOT A CONTRACT -- a recorded fact that qualifies one, and it is the
    reason this file is worth keeping honest.

    /healthz and /metrics are separate handlers with no shared readiness gate,
    but they BOTH call cachefs.disk_stats(). /healthz catches OSError and
    answers 503; /metrics does not catch it, so the request fails and the
    scrape fails with it.

    So for THIS failure mode the two alerts are not independent signals: a
    filesystem fault fires the healthz alert AND takes `up{job="muninn"}` to 0.
    That is not wrong -- both alerting is arguably correct -- but a consumer
    treating them as independent evidence should know they have a common cause.

    Pinned so the coupling cannot deepen unnoticed, and so that if /metrics is
    ever made to degrade gracefully, this test is where that decision surfaces
    rather than being an incidental behaviour change.
    """
    import pytest as _pytest

    from app import cachefs

    monkeypatch.setattr(cachefs, "disk_stats",
                        lambda: (_ for _ in ()).throw(OSError("simulated")))
    assert client.get("/healthz").status_code == 503
    with _pytest.raises(OSError):
        client.get("/metrics")


def test_durable_gauges_are_named_and_are_not_seeded_counters(client):
    """CONSUMER: their alert calls these "the only signal that distinguishes a
    working cache from a fully bypassed one", which makes the NAMES contractual.

    And the inverse of the counter-seeding change: these must NOT be seeded to
    zero at startup. A gauge reading 0 because the process restarted, rather
    than because the cache is empty, is worse than one that is absent.
    """
    from app import metrics

    body = client.get("/metrics").text
    for name in ("muninn_cache_bytes", "muninn_cache_files", "muninn_cache_repos"):
        assert name in body, f"{name} is alerted on externally"
    seeded = {r for r, _ in metrics._DOCKER_SERIES} | set(metrics._REQUEST_SERIES)
    assert not {"muninn_cache_bytes", "muninn_cache_files", "muninn_cache_repos"} & seeded


def test_path_prefix_resolution_is_a_fleet_wide_promise():
    """CONSUMER: every rewritten image reference on the fleet, and all four
    bare-name forms are documented to tenants as a promise.

    WIDEST BLAST RADIUS AND THE SLOWEST FAILURE SIGNAL on this list: a change
    here breaks pulls fleet-wide with no alert at all, because a node that
    bypasses the cache is invisible to the cache by construction.
    """
    from app import registry

    for name in ("ubuntu", "library/ubuntu", "docker.io/ubuntu",
                 "docker.io/library/ubuntu"):
        ref = registry.resolve(name)
        assert ref.upstream == "docker.io", name
        assert ref.repo == "library/ubuntu", name

    # A dot, a colon, or the literal `localhost` in the first segment makes it
    # the upstream. Anything else is part of the repository name.
    assert registry.resolve("ghcr.io/org/img").upstream == "ghcr.io"
    assert registry.resolve("localhost:5000/img").upstream == "localhost:5000"
    assert registry.resolve("localhost/img").upstream == "localhost"
    assert registry.resolve("org/team/img").upstream == "docker.io"


def test_push_defaults_to_off():
    """CONSUMER: relied upon rather than asserted, and worth pinning for that
    reason. Push is enabled deliberately on one deployment, where the start-up
    WARNING makes the blast radius explicit.

    If the default ever flipped, any Muninn anyone stands up becomes a write
    path into every registry in its auth file, under the cache's identity.
    """
    from app.config import Settings

    assert Settings.docker_push_enabled is False


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
