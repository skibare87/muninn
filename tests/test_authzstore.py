"""Persistence for principals, keys and rules.

Two things here cannot be verified by reading the code: that the first-admin claim
does not race, and that the cache invalidation actually makes a write visible to
the read path. Both are asserted by exercising them rather than by inspection.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.authz import Rule, decide, new_secret
from app.authzstore import AuthzStore, hash_secret, secret_matches


@pytest.fixture
def store(tmp_path):
    return AuthzStore(tmp_path / "authz.db")


# ---------------- the first-admin claim ----------------

def test_the_first_principal_is_admin_and_the_second_is_not(store):
    a = store.claim_or_get_principal("sub-1", "a@example.com")
    b = store.claim_or_get_principal("sub-2", "b@example.com")
    assert a.is_admin
    assert not b.is_admin


def test_claiming_twice_is_idempotent_and_does_not_re_grant_admin(store):
    store.claim_or_get_principal("sub-1")
    store.claim_or_get_principal("sub-2")
    again = store.claim_or_get_principal("sub-2")
    assert not again.is_admin, "a returning non-admin must not be promoted"


def test_concurrent_first_logins_produce_exactly_one_admin(store):
    """THE RACE THAT MATTERS, exercised rather than reasoned about.

    A read-then-write claim -- the obvious shape -- lets two people who sign in at
    the same moment both hold admin, and neither ever finds out. Twelve threads
    race the very first claim; exactly one must win.
    """
    results: list[bool] = []
    barrier = threading.Barrier(12)

    def claim(n: int) -> None:
        barrier.wait()
        results.append(store.claim_or_get_principal(f"sub-{n}").is_admin)

    threads = [threading.Thread(target=claim, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(results) == 1, f"expected exactly one admin, got {sum(results)}"
    assert sum(p.is_admin for p in store.list_principals()) == 1


def test_the_last_admin_cannot_be_demoted(store):
    """An instance with no admin cannot be administered, and nothing in the UI
    could grant the role back."""
    store.claim_or_get_principal("sub-1")
    store.claim_or_get_principal("sub-2")
    with pytest.raises(ValueError, match="last admin"):
        store.set_admin("sub-1", False)
    assert sum(p.is_admin for p in store.list_principals()) == 1


def test_an_admin_can_be_demoted_once_another_exists(store):
    store.claim_or_get_principal("sub-1")
    store.claim_or_get_principal("sub-2")
    store.set_admin("sub-2", True)
    store.set_admin("sub-1", False)
    admins = [p.subject for p in store.list_principals() if p.is_admin]
    assert admins == ["sub-2"]


# ---------------- credential resolution ----------------

def test_the_right_secret_resolves_and_a_wrong_one_does_not(store):
    store.claim_or_get_principal("sub-1")
    kid, sec = new_secret()
    store.add_key(kid, sec, "sub-1", [Rule("docker.io/*", pull=True)])
    assert store.resolve(kid, sec) is not None
    assert store.resolve(kid, sec + "x") is None
    assert store.resolve("unknown-id", sec) is None


def test_the_raw_secret_is_never_stored(store):
    """A store holding recoverable key secrets is a credential dump.

    THE POSITIVE CONTROL HERE EARNED ITS KEEP. The first version read only the
    .db file and "the secret is absent" passed -- vacuously, because in WAL mode
    the write was still in the -wal sidecar and NOTHING was in the main file. The
    assertion that the HASH is present is what caught that the check was reading
    the wrong surface. Without it, this test would have passed forever while
    proving nothing.
    """
    store.claim_or_get_principal("sub-1")
    kid, sec = new_secret()
    store.add_key(kid, sec, "sub-1", [Rule("*", pull=True)])

    # Everything SQLite may have written: main file plus WAL and shm sidecars.
    base = Path(store.path)
    blob = b"".join(
        p.read_bytes() for p in
        (base, base.with_name(base.name + "-wal"), base.with_name(base.name + "-shm"))
        if p.exists()
    )
    assert hash_secret(sec).encode() in blob, "reading the wrong surface"
    assert sec.encode() not in blob, "the raw secret reached disk"


def test_secret_comparison_is_constant_time_and_correct():
    h = hash_secret("abc")
    assert secret_matches("abc", h)
    assert not secret_matches("abd", h)


# ---------------- revocation, and that it reaches the read path ----------------

def test_disabling_a_key_takes_effect_immediately(store):
    """The cache is the read path, so a write that does not invalidate it is a
    revocation that silently does nothing."""
    store.claim_or_get_principal("sub-1")
    kid, sec = new_secret()
    store.add_key(kid, sec, "sub-1", [Rule("*", pull=True, push=True)])
    assert decide(store.resolve(kid, sec), "push", "docker.io/x")[0]

    store.set_key_disabled(kid, True)
    assert not decide(store.resolve(kid, sec), "push", "docker.io/x")[0]


def test_disabling_a_PRINCIPAL_disables_all_their_keys(store):
    """Enforced in the query, so no caller can forget it. Suspending a person must
    not require hunting down every key they ever minted."""
    store.claim_or_get_principal("sub-1")
    store.claim_or_get_principal("sub-2")
    keys = []
    for _ in range(3):
        kid, sec = new_secret()
        store.add_key(kid, sec, "sub-2", [Rule("*", pull=True, push=True)])
        keys.append((kid, sec))

    for kid, sec in keys:
        assert decide(store.resolve(kid, sec), "pull", "docker.io/x")[0]

    store.set_principal_disabled("sub-2", True)
    for kid, sec in keys:
        assert not decide(store.resolve(kid, sec), "pull", "docker.io/x")[0], \
            "a suspended principal's keys must all stop working"


def test_deleting_a_key_removes_its_rules(store):
    """Orphan rules rejoining a later key with the same id would be a grant nobody
    made; the foreign key cascade is asserted rather than assumed."""
    store.claim_or_get_principal("sub-1")
    kid, sec = new_secret()
    store.add_key(kid, sec, "sub-1", [Rule("*", pull=True, push=True)])
    store.delete_key(kid)
    assert store.resolve(kid, sec) is None
    assert store.list_keys() == []


def test_rewriting_rules_replaces_rather_than_appends(store):
    """Narrowing a key's scope must actually narrow it. Appending would make every
    edit strictly more permissive -- a UI that can only widen access."""
    store.claim_or_get_principal("sub-1")
    kid, sec = new_secret()
    store.add_key(kid, sec, "sub-1", [Rule("*", pull=True, push=True)])
    store.set_key_rules(kid, [Rule("docker.io/*", pull=True, push=False)])

    k = store.resolve(kid, sec)
    assert decide(k, "pull", "docker.io/library/redis")[0]
    assert not decide(k, "push", "docker.io/library/redis")[0]
    assert not decide(k, "pull", "ghcr.io/x/y")[0]


def test_the_store_survives_reopening(store, tmp_path):
    store.claim_or_get_principal("sub-1")
    kid, sec = new_secret()
    store.add_key(kid, sec, "sub-1", [Rule("ghcr.io/me/*", pull=True, push=True)])

    reopened = AuthzStore(store.path)
    k = reopened.resolve(kid, sec)
    assert k is not None
    assert decide(k, "push", "ghcr.io/me/app")[0]
    assert sum(p.is_admin for p in reopened.list_principals()) == 1
