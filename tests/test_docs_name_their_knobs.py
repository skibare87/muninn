"""Every setting the code reads is named in the README (an internal issue follow-up).

a colleague was told to enable store-and-forward, could not do it from the
documentation, and was about to reverse-engineer the variable names out of the
running container. the maintainer: "that's a failure in its documentation".

THE SHAPE, in a colleague's words: documentation that describes BEHAVIOUR without
naming the KNOB reads as complete to its author, because the author already
knows the knob. It surfaced twice on the same page, and both times only when
someone tried to act on it -- which is far too late, and by then the reader's
cheapest next move is to guess from the source.

Proofreading does not catch this: the prose is correct, it is just unusable.
A mechanical check does.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _knobs() -> set[str]:
    """Every XHC_ variable config.py actually reads from the environment."""
    return set(re.findall(r'"(XHC_[A-Z0-9_]+)"', (ROOT / "app" / "config.py").read_text()))


def test_every_environment_variable_is_named_in_the_readme():
    readme = (ROOT / "README.md").read_text()
    missing = sorted(k for k in _knobs() if k not in readme)
    assert not missing, (
        "settings the code reads but the README never names, so a reader can "
        f"only find them by reading the source: {missing}"
    )


def test_enumerated_settings_document_their_accepted_values():
    """A name alone is not enough for a setting with a fixed vocabulary.

    `XHC_DOCKER_PUSH_MODE` named without `store-forward` beside it is exactly
    the gap a colleague hit: they knew a mode existed and could not spell it.
    """
    readme = (ROOT / "README.md").read_text()
    for knob, values in {
        "XHC_DOCKER_PUSH_MODE": ["proxy", "store-forward"],
        "XHC_DOCKER_AUTH": ["none", "basic"],
        "XHC_DOCKER_POLICY": ["open", "allowlist"],
        "XHC_ORPHAN_POLICY": ["retain", "evict"],
    }.items():
        assert knob in readme, f"{knob} is not in the README at all"
        for v in values:
            assert v in readme, f"{knob} is named but its value {v!r} is not"


def test_the_shipped_compose_is_a_general_example_not_someone_s_deployment():
    """It is a public repo and the example was written for one machine.

    the maintainer: "this is public but you wrote it for me alone". It carried an 80T
    array, a 70T cache budget, a 16G memory limit and absolute paths from one
    NAS -- meaningless to anyone cloning it, and actively misleading as a
    starting point.
    """
    compose = (ROOT / "docker-compose.yml").read_text()
    for leak in ("80T", "70T", "/mnt/nvme", "NAS"):
        assert leak not in compose, f"the example still carries {leak!r} from one deployment"


def test_the_shipped_compose_covers_the_docker_side():
    """It shipped with ZERO XHC_DOCKER_* settings while OCI caching was a
    headline feature, so copying it gave you none of the registry half."""
    compose = (ROOT / "docker-compose.yml").read_text()
    for knob in ("XHC_DOCKER_ENABLED", "XHC_DOCKER_MAX_SIZE", "XHC_DOCKER_POLICY",
                 "XHC_REGISTRY_AUTH_FILE", "XHC_DOCKER_PUSH",
                 "XHC_DOCKER_PUSH_MODE", "XHC_DOCKER_CACHE_ON_PUSH",
                 "XHC_DOCKER_PUSH_LIMITS"):
        assert knob in compose, f"{knob} is not in the shipped example"


def test_the_regctl_file_credential_caveat_is_documented():
    """regctl's format carries user/pass and Muninn reads neither.

    Someone who mounts their real regctl config needs to know that, and
    someone deciding what to mount needs to know a limits-only file suffices.
    """
    readme = (ROOT / "README.md").read_text()
    assert "ignores any credentials" in readme or "limits-only" in readme, \
        "the README does not say credentials in the limits file are ignored"


# ---------------------------------------------------------------------------
# THE GUARD ABOVE GENERALISED TO THE INSTANCES THAT PROMPTED IT, WHICH IS ITS
# OWN DEFECT.
#
# Both examples that caused it to be written were environment variables, so it
# encodes "env vars must be documented". The actual shape is broader:
#
#     ANYTHING A READER MUST NAME IN ORDER TO USE MUST BE NAMED IN THE DOCS.
#
# Config variables are one instance. ROUTES are another, and that is the one
# that got through: `/_cache/docker/pending` shipped and stayed undocumented
# for three releases while this file went green -- and the docs described "the
# pending view" as the reason store-forward is safe to enable, so the mitigation
# was unreachable by anyone who had not read the router.
#
# The management surface is read off app.routes rather than a hand-maintained
# list, because a list beside code that changes is a list that drifts.
# ---------------------------------------------------------------------------

# Excluded deliberately, each with the reason. An exclusion list that grows
# silently rebuilds the original problem, so these are named individually
# rather than matched by a broad pattern.
_NOT_READER_FACING = {
    # OCI protocol surface: a docker client calls these, never a human, and
    # they are specified by the distribution spec rather than by this project.
    "/v2", "/v2/",
    # Catch-alls, not endpoints.
    "/v2/{rest:path}", "/{full_path:path}",
    # FastAPI's own, not ours.
    "/docs", "/docs/oauth2-redirect", "/openapi.json", "/redoc",
}


def _management_routes() -> set[str]:
    """Every non-protocol path this app serves, from the router itself."""
    from app.main import app

    paths = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        if not path or not getattr(route, "methods", None):
            continue
        if path in _NOT_READER_FACING or path.startswith("/v2/{name:path}"):
            continue
        paths.add(path)
    return paths


def test_every_management_endpoint_is_named_in_the_readme():
    """A route a reader must type is exactly as undocumentable as a variable
    they must set, and this file did not know that until it was bitten."""
    readme = (ROOT / "README.md").read_text()
    missing = sorted(p for p in _management_routes()
                     if p.split("{")[0].rstrip("/") not in readme)
    assert not missing, (
        "endpoints the app serves but the README never names, so a reader can "
        f"only find them by reading the router: {missing}"
    )


def test_the_exclusion_list_does_not_silently_cover_everything():
    """The negative control for the test above.

    If a refactor renamed the management prefix, `_management_routes()` could
    return an empty set and the check above would pass vacuously -- a sweep
    that has never been shown to find a known instance. Assert it still sees
    the surface it exists to police.
    """
    routes = _management_routes()
    assert len(routes) >= 10, f"the route sweep found almost nothing: {sorted(routes)}"
    for expected in ("/healthz", "/metrics", "/_cache/docker/pending"):
        assert expected in routes, f"{expected} vanished from the swept set"
