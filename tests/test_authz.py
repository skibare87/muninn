"""Per-key push/pull authorisation.

THIS IS THE FILE THAT MATTERS MOST IN THE REPOSITORY. Everything else being wrong
costs bytes or availability; this being wrong lets the wrong principal write to
someone else's registry under the cache's identity, unattributed, because a docker
push cannot identify itself.

So the negative cases are the point and there are more of them than positive ones.
A test suite for an authoriser that mostly proves "the right thing is allowed" is
the same mistake as a gate verified only by refusals: it cannot distinguish a
working policy from one that grants everything.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.authz import Key, Principal, Rule, decide, new_secret


def key(*rules: Rule, disabled: bool = False) -> Key:
    return Key(key_id="k1", secret_hash="x", principal="alice",
               rules=list(rules), disabled=disabled)


# --------------------------------------------------------------------------
# The grant itself
# --------------------------------------------------------------------------

def test_a_pull_rule_does_not_grant_push():
    """The asymmetry the whole feature exists for.

    Pull and push were the SAME permission until this module. An over-permissive
    pull leaks bytes; an over-permissive push writes to a third party's registry.
    """
    k = key(Rule("docker.io/*", pull=True, push=False))
    assert decide(k, "pull", "docker.io/library/redis")[0]
    assert not decide(k, "push", "docker.io/library/redis")[0]


def test_a_push_only_rule_does_not_grant_pull():
    """The reverse, which is easy to get wrong by treating push as implying pull."""
    k = key(Rule("ghcr.io/me/*", pull=False, push=True))
    assert decide(k, "push", "ghcr.io/me/app")[0]
    assert not decide(k, "pull", "ghcr.io/me/app")[0]


def test_a_bare_star_grants_everything_it_says_it_does():
    k = key(Rule("*", pull=True, push=True))
    for ref in ("docker.io/library/redis", "ghcr.io/o/r", "quay.io/a/b/c/deep"):
        assert decide(k, "pull", ref)[0]
        assert decide(k, "push", ref)[0]


def test_a_star_that_only_grants_pull_does_not_grant_push_anywhere():
    """A wildcard is not a bypass. Breadth and operation are independent axes."""
    k = key(Rule("*", pull=True, push=False))
    assert decide(k, "pull", "anything/at/all")[0]
    assert not decide(k, "push", "anything/at/all")[0]


# --------------------------------------------------------------------------
# Wildcard behaviour, including the choice that `*` spans `/`
# --------------------------------------------------------------------------

@pytest.mark.parametrize("pattern,reference,expected", [
    ("docker.io/*",        "docker.io/library/redis",  True),   # * spans /
    ("docker.io/*",        "docker.io/redis",          True),
    ("docker.io/*",        "ghcr.io/library/redis",    False),  # host is part of it
    ("ghcr.io/myorg/*",    "ghcr.io/myorg/app",        True),
    ("ghcr.io/myorg/*",    "ghcr.io/other/app",        False),  # neighbouring org
    ("ghcr.io/myorg/*",    "ghcr.io/myorgextra/app",   False),  # prefix is not a match
    ("*/library/*",        "docker.io/library/redis",  True),
    ("docker.io/lib?ary/*", "docker.io/library/redis", True),   # ? is one char
    ("docker.io/lib?/*",   "docker.io/library/redis",  False),
])
def test_wildcard_matching(pattern, reference, expected):
    k = key(Rule(pattern, pull=True))
    assert decide(k, "pull", reference)[0] is expected


def test_a_prefix_is_not_a_match_without_a_wildcard():
    """`ghcr.io/myorg` must not grant `ghcr.io/myorganisation-evil/x`.

    This is the classic substring-instead-of-boundary bug, and in an authoriser
    it hands a neighbouring namespace to the wrong principal.
    """
    k = key(Rule("ghcr.io/myorg", pull=True))
    assert decide(k, "pull", "ghcr.io/myorg")[0]
    assert not decide(k, "pull", "ghcr.io/myorganisation-evil/x")[0]
    assert not decide(k, "pull", "ghcr.io/myorg/sub")[0]


def test_matching_is_case_insensitive_and_cannot_broaden_a_pattern():
    k = key(Rule("Docker.IO/Library/*", pull=True))
    assert decide(k, "pull", "docker.io/library/redis")[0]
    # Case-insensitivity must not make an unrelated reference match.
    assert not decide(k, "pull", "docker.io/other/redis")[0]


# --------------------------------------------------------------------------
# The refusals: every path that must deny
# --------------------------------------------------------------------------

def test_no_key_is_refused():
    assert not decide(None, "pull", "docker.io/library/redis")[0]


def test_a_key_with_no_rules_grants_nothing():
    """A newly created key must be useless until someone scopes it.

    The alternative -- an empty rule list meaning "unrestricted" -- is the
    fail-open this project keeps recording, and it would make the most likely
    state of a fresh key the most privileged one.
    """
    k = key()
    assert not decide(k, "pull", "docker.io/library/redis")[0]
    assert not decide(k, "push", "docker.io/library/redis")[0]


def test_a_disabled_key_is_refused_even_with_a_star_rule():
    """Revocation must beat every grant, or revocation is advisory."""
    k = key(Rule("*", pull=True, push=True), disabled=True)
    assert not decide(k, "pull", "docker.io/library/redis")[0]
    assert not decide(k, "push", "docker.io/library/redis")[0]


def test_an_inert_rule_grants_nothing():
    """Neither pull nor push: what a UI produces when both boxes are unchecked."""
    k = key(Rule("*", pull=False, push=False))
    assert not decide(k, "pull", "x/y")[0]
    assert not decide(k, "push", "x/y")[0]


@pytest.mark.parametrize("pattern", ["", "   "])
def test_an_empty_pattern_grants_nothing(pattern):
    """An empty pattern must not behave like `*`.

    This is the shape where a blank field in a form becomes universal access.
    """
    k = key(Rule(pattern, pull=True, push=True))
    assert not decide(k, "pull", "docker.io/library/redis")[0]
    assert not decide(k, "push", "docker.io/library/redis")[0]


def test_an_empty_reference_is_refused():
    k = key(Rule("*", pull=True, push=True))
    assert not decide(k, "pull", "")[0]


# --------------------------------------------------------------------------
# Composition and secrets
# --------------------------------------------------------------------------

def test_rules_compose_and_any_sufficient_rule_grants():
    """Allow-only with no ordering: no rule's effect depends on its position."""
    k = key(Rule("docker.io/*", pull=True), Rule("ghcr.io/me/*", pull=True, push=True))
    assert decide(k, "pull", "docker.io/library/redis")[0]
    assert not decide(k, "push", "docker.io/library/redis")[0]
    assert decide(k, "push", "ghcr.io/me/app")[0]
    assert not decide(k, "push", "ghcr.io/you/app")[0]


def test_reordering_rules_cannot_change_the_outcome():
    """Explicitly asserted because an order-dependent authoriser is invisible in
    review -- it only shows up as the wrong person pushing."""
    a, b = Rule("docker.io/*", pull=True), Rule("*", pull=True, push=True)
    for rules in ((a, b), (b, a)):
        k = key(*rules)
        assert decide(k, "push", "docker.io/library/redis")[0]


def test_a_secret_is_not_reused_and_is_long_enough_to_matter():
    ids, secrets_ = set(), set()
    for _ in range(200):
        kid, sec = new_secret()
        ids.add(kid)
        secrets_.add(sec)
        assert len(sec) >= 40, "a short key secret is a guessable one"
    assert len(ids) == 200
    assert len(secrets_) == 200


def test_admin_is_stored_not_derived():
    """Derived-from-first-login means deleting the first account silently promotes
    someone, or locks the instance out of ever having an admin."""
    p = Principal(subject="sub-1", email="a@example.com", is_admin=True)
    assert p.is_admin
    assert not Principal(subject="sub-2").is_admin
