"""Storage for principals, keys and rules, replacing the single flat htpasswd.

WHY SQLITE AND NOT A JSON FILE. A JSON file is read-modify-write, so two
simultaneous saves from a management UI lose one of them silently -- and the thing
being lost is an authorisation rule. SQLite is in the standard library, gives real
transactions, and makes the first-admin claim below expressible as something that
cannot race. No new dependency.

WHY KEY SECRETS ARE SHA-256 AND NOT BCRYPT, which looks like the wrong choice and
is not. bcrypt exists to make guessing LOW-ENTROPY human passwords expensive. A key
secret here is 32 random bytes from `secrets`, so there is nothing to guess: a work
factor buys no security against brute force that 256 bits of entropy has not
already bought. And it would cost something real -- this hash is on the hot path of
every single /v2 request, where bcrypt at cost 12 is ~250ms and would make the
registry surface unusable.

    human password, low entropy, rare verification  -> bcrypt (dockerauth.py)
    random key, high entropy, verified every request -> sha256 (here)

Both are in this codebase on purpose and the distinction is the reason. A reviewer
who "fixes" this to bcrypt for consistency will make the cache slow without making
it safer.

Comparison is constant-time regardless, because timing a hash comparison is a
different attack from guessing the input.

THE IN-MEMORY CACHE IS NOT AN OPTIMISATION, IT IS THE READ PATH. Every /v2 request
authorises, and hitting SQLite synchronously from an async handler on every request
would serialise the server on a file lock. Keys are loaded once and reloaded when a
write bumps the version counter, so the hot path is a dict lookup and one hash.
"""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

from .authz import Key, Principal, Rule

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS principals (
    subject    TEXT PRIMARY KEY,
    email      TEXT NOT NULL DEFAULT '',
    is_admin   INTEGER NOT NULL DEFAULT 0,
    disabled   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS keys (
    key_id       TEXT PRIMARY KEY,
    secret_hash  TEXT NOT NULL,
    principal    TEXT NOT NULL REFERENCES principals(subject) ON DELETE CASCADE,
    label        TEXT NOT NULL DEFAULT '',
    disabled     INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    last_used_at TEXT
);
CREATE TABLE IF NOT EXISTS rules (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    key_id  TEXT NOT NULL REFERENCES keys(key_id) ON DELETE CASCADE,
    pattern TEXT NOT NULL,
    pull    INTEGER NOT NULL DEFAULT 1,
    push    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS rules_by_key ON rules(key_id);
"""


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def secret_matches(secret: str, stored_hash: str) -> bool:
    """Constant-time. Timing a comparison is a different attack from guessing."""
    return hmac.compare_digest(hash_secret(secret), stored_hash)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class AuthzStore:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._lock = threading.Lock()
        self._cache: dict[str, Key] | None = None
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as c:
            c.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        return c

    def _invalidate(self) -> None:
        with self._lock:
            self._cache = None

    # ---------------- principals ----------------

    def claim_or_get_principal(self, subject: str, email: str = "") -> Principal:
        """Get a principal, creating it on first sight.

        THE FIRST PRINCIPAL BECOMES ADMIN, AND THE CLAIM CANNOT RACE. Two
        simultaneous first logins must not both become admin, so the existence
        check and the insert happen in ONE IMMEDIATE transaction -- SQLite
        serialises writers, so the second caller sees the first one's row and is
        created as a non-admin.

        Doing this as a read-then-write, which is the obvious shape, would let two
        people who signed in at the same moment both hold admin, and neither would
        ever find out.
        """
        with self._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT * FROM principals WHERE subject=?", (subject,)
            ).fetchone()
            if row is not None:
                return Principal(row["subject"], row["email"],
                                 bool(row["is_admin"]), bool(row["disabled"]))
            first = c.execute("SELECT COUNT(*) AS n FROM principals").fetchone()["n"] == 0
            c.execute(
                "INSERT INTO principals(subject,email,is_admin,disabled,created_at)"
                " VALUES (?,?,?,0,?)",
                (subject, email, 1 if first else 0, _now()),
            )
            return Principal(subject, email, is_admin=first, disabled=False)

    def list_principals(self) -> list[Principal]:
        with self._connect() as c:
            return [
                Principal(r["subject"], r["email"], bool(r["is_admin"]), bool(r["disabled"]))
                for r in c.execute("SELECT * FROM principals ORDER BY created_at")
            ]

    def set_admin(self, subject: str, is_admin: bool) -> None:
        """Refuses to remove the LAST admin.

        An instance with no admin cannot be administered, and nothing in the UI
        could grant the role back -- so the destructive direction is guarded while
        the safe one is not.
        """
        with self._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            if not is_admin:
                others = c.execute(
                    "SELECT COUNT(*) AS n FROM principals WHERE is_admin=1 AND subject<>?",
                    (subject,),
                ).fetchone()["n"]
                if others == 0:
                    raise ValueError(
                        "refusing to remove the last admin: the instance would "
                        "have no one able to grant the role back"
                    )
            c.execute("UPDATE principals SET is_admin=? WHERE subject=?",
                      (1 if is_admin else 0, subject))
        self._invalidate()

    def set_principal_disabled(self, subject: str, disabled: bool) -> None:
        with self._connect() as c:
            c.execute("UPDATE principals SET disabled=? WHERE subject=?",
                      (1 if disabled else 0, subject))
        self._invalidate()

    # ---------------- keys ----------------

    def add_key(self, key_id: str, secret: str, principal: str,
                rules: list[Rule], label: str = "") -> None:
        with self._connect() as c:
            c.execute(
                "INSERT INTO keys(key_id,secret_hash,principal,label,disabled,created_at)"
                " VALUES (?,?,?,?,0,?)",
                (key_id, hash_secret(secret), principal, label, _now()),
            )
            c.executemany(
                "INSERT INTO rules(key_id,pattern,pull,push) VALUES (?,?,?,?)",
                [(key_id, r.pattern, int(r.pull), int(r.push)) for r in rules],
            )
        self._invalidate()

    def set_key_rules(self, key_id: str, rules: list[Rule]) -> None:
        with self._connect() as c:
            c.execute("DELETE FROM rules WHERE key_id=?", (key_id,))
            c.executemany(
                "INSERT INTO rules(key_id,pattern,pull,push) VALUES (?,?,?,?)",
                [(key_id, r.pattern, int(r.pull), int(r.push)) for r in rules],
            )
        self._invalidate()

    def set_key_disabled(self, key_id: str, disabled: bool) -> None:
        with self._connect() as c:
            c.execute("UPDATE keys SET disabled=? WHERE key_id=?",
                      (1 if disabled else 0, key_id))
        self._invalidate()

    def delete_key(self, key_id: str) -> None:
        with self._connect() as c:
            c.execute("DELETE FROM keys WHERE key_id=?", (key_id,))
        self._invalidate()

    def list_keys(self, principal: str | None = None) -> list[Key]:
        return [
            k for k in self._all_keys().values()
            if principal is None or k.principal == principal
        ]

    # ---------------- the read path ----------------

    def _all_keys(self) -> dict[str, Key]:
        with self._lock:
            if self._cache is not None:
                return self._cache
        with self._connect() as c:
            rules: dict[str, list[Rule]] = {}
            for r in c.execute("SELECT * FROM rules"):
                rules.setdefault(r["key_id"], []).append(
                    Rule(r["pattern"], bool(r["pull"]), bool(r["push"]))
                )
            # A key whose PRINCIPAL is disabled is itself unusable. Enforced in the
            # query rather than at the call site, so no caller can forget it.
            loaded = {
                r["key_id"]: Key(
                    key_id=r["key_id"], secret_hash=r["secret_hash"],
                    principal=r["principal"], rules=rules.get(r["key_id"], []),
                    label=r["label"],
                    disabled=bool(r["disabled"]) or bool(r["p_disabled"]),
                )
                for r in c.execute(
                    "SELECT k.*, p.disabled AS p_disabled FROM keys k"
                    " JOIN principals p ON p.subject = k.principal"
                )
            }
        with self._lock:
            self._cache = loaded
        return loaded

    def resolve(self, key_id: str, secret: str) -> Key | None:
        """Resolve a presented credential. Returns None on any failure.

        Deliberately does not distinguish an unknown key from a wrong secret, in
        the return value or in timing: a caller learning that a key id is real has
        learned half of a credential.
        """
        key = self._all_keys().get(key_id)
        if key is None:
            # Compare against a dummy so an unknown id costs the same as a known
            # one. Without this, response time enumerates valid key ids.
            secret_matches(secret, hash_secret("dummy"))
            return None
        if not secret_matches(secret, key.secret_hash):
            return None
        return key
