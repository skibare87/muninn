"""XHC_DOCKER_TAG_TTL has three regimes and used to expose two.

    N        trust a tag->digest mapping for N seconds
    0        NEVER revalidate -- serve whatever was first cached, forever
    always   revalidate on every request        <- did not exist

`0` meaning NEVER is the trap. It is the value an operator reaches for when they
want the strictest behaviour, and it selected the loosest -- so the knob failed
toward staleness, silently, in the direction of "I thought I turned checking on".
The only way to get always-check was an arbitrarily small positive TTL, which
worked by accident of the comparison rather than by intent.

`0` is unchanged, because deployments rely on it. `always` is a new spelling
rather than a redefinition, so no existing deployment changes behaviour.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, ocistore
from app.config import Settings, settings


def _entry(age_s: float) -> dict:
    import time
    return {"checked_at": time.time() - age_s, "digest": "sha256:" + "0" * 64}


def test_always_never_serves_without_revalidating(monkeypatch):
    """The gap this closes: a mapping checked one millisecond ago is stale."""
    monkeypatch.setattr(settings, "docker_tag_ttl_s", config.ALWAYS_REVALIDATE)
    assert ocistore.tag_is_fresh(_entry(0.001)) is False


def test_zero_still_means_never_revalidate(monkeypatch):
    """Unchanged and load-bearing. Someone is relying on 0 today, and a
    redefinition would silently start hitting upstream on every request."""
    monkeypatch.setattr(settings, "docker_tag_ttl_s", 0.0)
    assert ocistore.tag_is_fresh(_entry(10_000_000)) is True


def test_a_positive_ttl_still_expires(monkeypatch):
    monkeypatch.setattr(settings, "docker_tag_ttl_s", 300.0)
    assert ocistore.tag_is_fresh(_entry(299)) is True
    assert ocistore.tag_is_fresh(_entry(301)) is False


@pytest.mark.parametrize("raw", ["always", "ALWAYS", " Always ", "revalidate", "0s"])
def test_the_spellings_someone_would_actually_try(monkeypatch, raw):
    monkeypatch.setenv("XHC_DOCKER_TAG_TTL", raw)
    assert Settings.from_env().docker_tag_ttl_s == config.ALWAYS_REVALIDATE


def test_a_negative_number_means_always_not_never(monkeypatch):
    """-1 is what someone guesses when they want "no caching". Treating it as
    `never` -- which the old `ttl <= 0` did -- is the exact surprise this
    exists to remove, and it fails toward staleness."""
    monkeypatch.setenv("XHC_DOCKER_TAG_TTL", "-1")
    assert Settings.from_env().docker_tag_ttl_s == config.ALWAYS_REVALIDATE


def test_unset_keeps_the_documented_default(monkeypatch):
    monkeypatch.delenv("XHC_DOCKER_TAG_TTL", raising=False)
    assert Settings.from_env().docker_tag_ttl_s == 300.0


def test_zero_from_the_environment_is_still_never(monkeypatch):
    """The parse and the comparison must agree: `0` survives as 0.0 rather than
    being swept into the always sentinel."""
    monkeypatch.setenv("XHC_DOCKER_TAG_TTL", "0")
    resolved = Settings.from_env().docker_tag_ttl_s
    assert resolved == 0.0
    monkeypatch.setattr(settings, "docker_tag_ttl_s", resolved)
    assert ocistore.tag_is_fresh(_entry(10_000_000)) is True
