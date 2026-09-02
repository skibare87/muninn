"""A repeated Accept header must not lose its values, and a 400 is not a 404.

HTTP allows a header to appear more than once. Starlette's `Headers.get()`
returns only the FIRST occurrence, so reading Accept that way silently discards
the rest -- and nothing anywhere reports it.

regctl sends SEVEN separate `Accept:` lines: both OCI types, four docker types
and the OCI artifact type. Reading only the first forwarded
`application/vnd.oci.image.index.v1+json` alone. A registry holding a DOCKER
manifest list then has nothing acceptable to return, and one answers
`400 MANIFEST_INVALID: Schema 2 manifest not supported by client`.

That 400 was then rendered to the client as 404 -- so the client was told the
image does not exist when the REQUEST was the problem. A reader concluded "the
manifest endpoint refuses a tag the listing advertises", which is a reasonable
reading of a 404 and is not what was happening.

WHY EVERY HAND-RUN PROBE PASSED: curl and docker send ONE comma-joined Accept
line. Only a client using repeated headers loses anything, so the defect was
invisible to exactly the checks anyone would reach for.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from starlette.datastructures import Headers

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import ocicompat, registry

# What regctl v0.11.5 actually sends, measured.
REGCTL_ACCEPT = [
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.docker.distribution.manifest.v1+prettyjws",
    "application/vnd.docker.distribution.manifest.v1+json",
    "application/vnd.oci.artifact.manifest.v1+json",
]


class _Req:
    def __init__(self, values):
        self.headers = Headers(raw=[(b"accept", v.encode()) for v in values])


def test_repeated_accept_headers_are_all_forwarded():
    """The defect in one assertion: six of seven values used to vanish."""
    got = ocicompat._accept_header(_Req(REGCTL_ACCEPT))
    for value in REGCTL_ACCEPT:
        assert value in got, f"{value} was dropped from the forwarded Accept"


def test_the_docker_types_survive_specifically():
    """The ones whose absence causes the 400: a registry holding a docker
    manifest list has nothing to return without them."""
    got = ocicompat._accept_header(_Req(REGCTL_ACCEPT))
    assert "vnd.docker.distribution.manifest.list.v2+json" in got
    assert "vnd.docker.distribution.manifest.v2+json" in got


def test_a_single_comma_joined_accept_is_unchanged():
    """curl and docker send one line. That path must not regress -- it is what
    every hand-run probe uses, and it worked throughout."""
    one = "application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json"
    assert ocicompat._accept_header(_Req([one])) == one


def test_absent_accept_is_none_not_empty():
    """None and "" are different: the caller substitutes */* for None."""
    assert ocicompat._accept_header(_Req([])) is None


def test_upstream_400_is_not_reported_as_not_found():
    """A 400 is an answer about the REQUEST. 404 ends the investigation."""
    ref = registry.resolve("nvcr.io/nvidia/vllm")
    r = ocicompat._unavailable(ref, {"status": 400}, "26.07-py3", "manifest")
    assert r.status_code == 400
    assert r.headers["x-xhc-upstream-status"] == "400"


def test_upstream_500_is_502_not_404(monkeypatch):
    """An upstream fault must not read as a missing image."""
    ref = registry.resolve("ghcr.io/org/img")
    r = ocicompat._unavailable(ref, {"status": 503}, "latest", "manifest")
    assert r.status_code == 502


def test_upstream_429_is_passed_through(monkeypatch):
    """Rate limiting is not a statement about whether the image exists."""
    ref = registry.resolve("library/alpine")
    r = ocicompat._unavailable(ref, {"status": 429}, "latest", "manifest")
    assert r.status_code == 429


def test_a_genuine_upstream_404_is_still_404():
    """The negative control. Without it, a change that stopped ALL statuses
    becoming 404 would pass every test above while breaking the correct case."""
    ref = registry.resolve("library/alpine")
    r = ocicompat._unavailable(ref, {"status": 404}, "nope", "manifest")
    assert r.status_code == 404


@pytest.mark.parametrize("status", [400, 429, 503])
def test_no_upstream_status_is_silently_relabelled_as_absent(status):
    """The shape, asserted directly: distinct upstream answers must not all
    render as 'not available'."""
    ref = registry.resolve("ghcr.io/org/img")
    r = ocicompat._unavailable(ref, {"status": status}, "latest", "manifest")
    assert r.status_code != 404, f"upstream {status} still renders as 404"
