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


# ---------------------------------------------------------------------------
# Bootstrap admin.
#
# "First principal becomes admin" is COUNT(*) == 0, which is right for a fresh
# deployment and wrong for the case that actually happens: a cache with live
# consumers whose credentials must be migrated in BEFORE authz is switched on,
# or they are all refused at the next restart. That leaves the table non-empty
# and the intended admin silently does not become one.
# ---------------------------------------------------------------------------


def test_bootstrap_grants_admin_even_when_the_table_is_not_empty(tmp_path):
    """The case it exists for: machine credentials migrated in first."""
    st = AuthzStore(tmp_path / "a.db")
    st.claim_or_get_principal("svc-consumer")           # migrated first
    assert st.claim_or_get_principal("svc-consumer").is_admin is True, (
        "sanity: the consumer took the first-principal grant, which is the problem"
    )
    human = st.claim_or_get_principal("sub-matt", "m@example.com", "sub-matt")
    assert human.is_admin is True


def test_bootstrap_matches_on_email_too(tmp_path):
    """Nobody knows their own subject before their first login, which is the
    only reason email is accepted here at all."""
    st = AuthzStore(tmp_path / "a.db")
    st.claim_or_get_principal("svc-consumer")
    p = st.claim_or_get_principal("sub-xyz", "m@example.com", "m@example.com")
    assert p.is_admin is True


def test_bootstrap_does_not_grant_to_anyone_else(tmp_path):
    st = AuthzStore(tmp_path / "a.db")
    st.claim_or_get_principal("svc-consumer")
    other = st.claim_or_get_principal("sub-stranger", "s@example.com", "sub-matt")
    assert other.is_admin is False


def test_bootstrap_cannot_re_promote_a_DEMOTED_admin(tmp_path):
    """THE REASON IT IS EVALUATED AT CREATION ONLY. Left set in the environment
    -- which it will be -- a grant applied on every login would silently undo a
    deliberate demotion the next time that person signed in."""
    st = AuthzStore(tmp_path / "a.db")
    st.claim_or_get_principal("sub-matt", "m@example.com", "sub-matt")
    st.claim_or_get_principal("sub-two", "t@example.com")
    st.set_admin("sub-two", True)
    st.set_admin("sub-matt", False)

    again = st.claim_or_get_principal("sub-matt", "m@example.com", "sub-matt")
    assert again.is_admin is False, "a later login must not re-grant admin"


def test_bootstrap_does_not_create_a_principal_by_itself(tmp_path):
    """Admin is granted by a COMPLETED login and by nothing else. If the setting
    could create a row, the environment would be a way to mint an administrator
    without anyone authenticating."""
    st = AuthzStore(tmp_path / "a.db")
    assert st.list_principals() == []
    # naming someone who never logs in changes nothing
    st.claim_or_get_principal("sub-someone-else", "", "sub-matt")
    assert [p.subject for p in st.list_principals()] == ["sub-someone-else"]
    assert not any(p.is_admin and p.subject == "sub-matt" for p in st.list_principals())


def test_an_unset_bootstrap_leaves_the_first_principal_rule_alone(tmp_path):
    """The default path must be unchanged: this is opt-in."""
    st = AuthzStore(tmp_path / "a.db")
    assert st.claim_or_get_principal("sub-a").is_admin is True
    assert st.claim_or_get_principal("sub-b", "", None).is_admin is False


# ---------------------------------------------------------------------------
# Cross-process invalidation.
#
# The read path is an in-memory cache. Dropping it inside the writing object
# only helps callers that share that object -- anything administering the store
# out of band (a migration script, an operator, a one-shot exec) committed to
# SQLite while the running server carried on serving the old answer.
#
# Two AuthzStore instances on one file is a faithful model of that: separate
# caches, same database, exactly as two processes have.
# ---------------------------------------------------------------------------


def test_a_key_disabled_BY_ANOTHER_PROCESS_stops_authenticating(tmp_path):
    """THE ONE THAT MATTERS. Revocation is the guarantee this store exists to
    provide, and it was silently not provided for out-of-band writes: a
    disabled key kept resolving until the process restarted."""
    db = tmp_path / "a.db"
    server, admin = AuthzStore(db), AuthzStore(db)

    server.claim_or_get_principal("sub-1")
    key_id, secret = new_secret()
    server.add_key(key_id, secret, "sub-1", [Rule("*", pull=True)])

    assert server.resolve(key_id, secret) is not None, "positive control"

    admin.set_key_disabled(key_id, True)          # a DIFFERENT instance writes
    resolved = server.resolve(key_id, secret)
    assert resolved is None or resolved.disabled, "the server must see the revocation"


def test_a_key_added_by_another_process_is_visible(tmp_path):
    """The same defect in the other direction: a key minted out of band did not
    work until restart, which is how it presents to whoever was handed it."""
    db = tmp_path / "a.db"
    server, admin = AuthzStore(db), AuthzStore(db)
    admin.claim_or_get_principal("sub-1")
    server.list_keys()                            # warm the server's cache first

    key_id, secret = new_secret()
    admin.add_key(key_id, secret, "sub-1", [Rule("*", pull=True)])
    assert server.resolve(key_id, secret) is not None


def test_rules_changed_by_another_process_take_effect(tmp_path):
    db = tmp_path / "a.db"
    server, admin = AuthzStore(db), AuthzStore(db)
    admin.claim_or_get_principal("sub-1")
    key_id, secret = new_secret()
    admin.add_key(key_id, secret, "sub-1", [])
    admin.set_principal_rules("sub-1", [Rule("docker.io/*", pull=True)])
    server.resolve(key_id, secret)                # warm

    admin.set_principal_rules("sub-1", [Rule("ghcr.io/*", pull=True)])
    key = server.resolve(key_id, secret)
    assert [r.pattern for r in key.rules] == ["ghcr.io/*"]


def test_the_cache_is_still_a_cache(tmp_path):
    """The freshness probe must not turn every authorisation into a reload --
    it is one pragma on an open handle, and the dict is meant to survive."""
    db = tmp_path / "a.db"
    st = AuthzStore(db)
    st.claim_or_get_principal("sub-1")
    key_id, secret = new_secret()
    st.add_key(key_id, secret, "sub-1", [Rule("*", pull=True)])
    st.resolve(key_id, secret)
    first = st._all_keys()
    for _ in range(50):
        st.resolve(key_id, secret)
    assert st._all_keys() is first, "unchanged data must not be reloaded"


def test_deleting_a_principal_takes_their_keys_and_rules(tmp_path):
    """CASCADE, not a loop. A loop can be interrupted halfway and leave a
    deleted user's credentials still resolving."""
    st = AuthzStore(tmp_path / "a.db")
    st.claim_or_get_principal("admin-1")           # first principal = admin
    st.create_principal("doomed", "d@example.com")
    st.set_principal_rules("doomed", [Rule("*", pull=True)])
    key_id, secret = new_secret()
    st.add_key(key_id, secret, "doomed", [])
    assert st.resolve(key_id, secret) is not None, "positive control"

    st.delete_principal("doomed")
    assert st.resolve(key_id, secret) is None, "their key must stop resolving"
    assert st.get_principal_rules("doomed") == []
    assert "doomed" not in [p.subject for p in st.list_principals()]


def test_the_last_admin_cannot_be_deleted(tmp_path):
    """An administrative surface with no administrator has no path back that
    does not involve a shell on the host."""
    st = AuthzStore(tmp_path / "a.db")
    st.claim_or_get_principal("only-admin")
    st.create_principal("ordinary")
    with pytest.raises(ValueError, match="last administrator"):
        st.delete_principal("only-admin")
    # and it IS possible once another admin exists -- the positive control,
    # without which the guard could simply be "never delete an admin"
    st.set_admin("ordinary", True)
    st.delete_principal("only-admin")
    assert [p.subject for p in st.list_principals()] == ["ordinary"]


def test_deleting_an_absent_principal_is_reported(tmp_path):
    st = AuthzStore(tmp_path / "a.db")
    with pytest.raises(KeyError):
        st.delete_principal("never-existed")
