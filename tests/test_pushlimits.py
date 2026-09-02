"""Per-registry blob chunking policy (an internal issue).

The number a registry will accept is not advertised by the OCI protocol, so
anyone pushing to a size-limited registry has to know it out of band -- which is
why regctl needs hand-configuring per host and a plain `docker push` fails
outright. Muninn's job is to absorb that so the client never learns the number.

The tests that carry the weight are the precedence order and the adaptive floor.
Precedence, because a global fallback silently overriding a per-host entry would
be invisible until someone's large layer started failing. The floor, because
halving forever turns a non-size problem into an infinite retry that looks like
a hang.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import pushlimits
from app.config import settings


@pytest.fixture(autouse=True)
def _reset():
    limits, chunk = settings.docker_push_limits, settings.docker_blob_chunk
    pushlimits.reset()
    yield
    settings.docker_push_limits, settings.docker_blob_chunk = limits, chunk
    pushlimits.reset()


def _limits_file(tmp_path, hosts):
    p = tmp_path / "push-limits.json"
    p.write_text(json.dumps({"hosts": hosts}))
    settings.docker_push_limits = str(p)
    settings.docker_blob_chunk = 0
    pushlimits.reset()
    return p


# --- the default is no chunking, because the problem is sparse -------------

def test_unconfigured_means_monolithic_put():
    settings.docker_push_limits = None
    settings.docker_blob_chunk = 0
    pushlimits.reset()
    assert pushlimits.for_upstream("registry.example.com").chunks is False


# --- the regctl-format file ------------------------------------------------

def test_reads_regctl_format(tmp_path):
    _limits_file(tmp_path, {"registry.example.com":
                            {"blobChunk": 16777216, "blobMax": 16777216}})
    lim = pushlimits.for_upstream("registry.example.com")
    assert lim.chunk == 16777216 and lim.threshold == 16777216 and lim.chunks


def test_a_host_with_no_entry_is_unaffected(tmp_path):
    """The real fleet file has limits for two of three registries."""
    _limits_file(tmp_path, {"registry.example.com": {"blobChunk": 16777216}})
    assert pushlimits.for_upstream("registry.example.net").chunks is False


def test_credentials_in_the_file_are_ignored(tmp_path):
    """regctl's format carries user/pass. A second credential source is a
    second thing to get wrong, so only the size fields are read."""
    _limits_file(tmp_path, {"r.example.com": {"user": "u", "pass": "p",
                                              "blobChunk": 8388608}})
    lim = pushlimits.for_upstream("r.example.com")
    assert lim.chunk == 8388608
    assert not hasattr(lim, "user") and not hasattr(lim, "pass")


def test_an_unreadable_file_warns_and_falls_back_rather_than_refusing(tmp_path):
    """Deliberately not fatal.

    A missing limits file means pushes fall back to no chunking, which fails
    VISIBLY at the registry with a 413. It widens no access and corrupts
    nothing, so refusing to start over a performance hint would impose a
    requirement where a warning does the job.
    """
    settings.docker_push_limits = str(tmp_path / "absent.json")
    settings.docker_blob_chunk = 4194304
    pushlimits.reset()
    assert pushlimits.for_upstream("r.example.com").chunk == 4194304


# --- precedence ------------------------------------------------------------

def test_per_host_file_beats_the_global_fallback(tmp_path):
    _limits_file(tmp_path, {"r.example.com": {"blobChunk": 16777216}})
    settings.docker_blob_chunk = 4194304
    assert pushlimits.for_upstream("r.example.com").chunk == 16777216, \
        "the global fallback overrode a specific entry"
    assert pushlimits.for_upstream("other.example.com").chunk == 4194304


def test_global_fallback_applies_where_nothing_is_configured():
    settings.docker_push_limits = None
    settings.docker_blob_chunk = 16777216
    pushlimits.reset()
    assert pushlimits.for_upstream("anything").chunk == 16777216


# --- adaptive discovery ----------------------------------------------------

def test_learns_from_a_413_and_halves():
    settings.docker_push_limits = None
    settings.docker_blob_chunk = 0
    pushlimits.reset()
    nxt = pushlimits.learn("r.example.com", 33554432)
    assert nxt == 16777216
    assert pushlimits.for_upstream("r.example.com").chunk == 16777216


def test_a_rejected_monolithic_put_starts_somewhere_sane():
    """No prior size to halve from: a monolithic PUT was refused."""
    settings.docker_push_limits = None
    settings.docker_blob_chunk = 0
    pushlimits.reset()
    assert pushlimits.learn("r.example.com", 0) == 16 * 1024 * 1024


def test_halving_stops_at_a_floor():
    """Below the floor this is not a size limit, and retrying forever turns a
    different problem into something that looks like a hang."""
    settings.docker_push_limits = None
    pushlimits.reset()
    assert pushlimits.learn("r.example.com", pushlimits.MIN_CHUNK) is None
    assert pushlimits.learn("r.example.com", 1) is None


def test_configuration_beats_what_was_learned(tmp_path):
    """A learned value is a guess; a configured one is a decision."""
    pushlimits.learn("r.example.com", 33554432)
    _limits_file(tmp_path, {"r.example.com": {"blobChunk": 8388608}})
    assert pushlimits.for_upstream("r.example.com").chunk == 8388608


def test_learned_values_are_not_persisted(tmp_path):
    """A value discovered on one deployment is not a fact about the registry.

    Writing it to disk would turn a guess into configuration nobody remembers
    making, and it would outlive the proxy setting that caused it.
    """
    p = _limits_file(tmp_path, {})
    pushlimits.learn("r.example.com", 33554432)
    assert "r.example.com" not in json.loads(p.read_text())["hosts"]
