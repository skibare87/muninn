"""Principals, keys, and per-key push/pull authorisation with wildcards.

WHAT THIS CHANGES. Until now Muninn's client auth was a GATE: any valid credential
could pull, and could push to any upstream the global allow-list permitted. There
was no notion of a principal and no per-caller scope. This module is the first
thing that makes the cache genuinely multi-principal, which is why it is written
and tested in isolation before any of it is wired to an identity provider or a UI.

THE DECISION IS DELIBERATELY BORING AND ALLOW-ONLY. A rule GRANTS; nothing denies.
Any matching rule that carries the operation is sufficient. There is no precedence
order to get wrong, no deny-overrides-allow subtlety, and no rule whose effect
depends on its position in the list -- because an authorisation bug that depends on
ordering is invisible in review and only shows up as the wrong person pushing.

WHAT A RULE MATCHES AGAINST is the full reference `<upstream>/<repo>`, e.g.
`docker.io/library/redis`. NOT the tag, and NOT the digest: authorisation is about
which repository you may read or write, and a scheme that varied by tag would let
`:latest` be more privileged than `:v2`.

WILDCARDS. `*` matches any run of characters INCLUDING `/`, so `docker.io/*` covers
`docker.io/library/redis` and a bare `*` is everything. Matching `/` was a choice:
per-segment wildcards mean `docker.io/*` silently fails to cover the nested repos
that are the normal case, and the failure direction is an operator widening the
rule until it works, which lands them at `*`. A `?` matches exactly one character.

CASE. Registry hosts are case-insensitive and repository paths are not, but no
registry in practice uses case to distinguish repos, and treating a rule as
case-sensitive means `Docker.io/*` silently grants nothing. Matching is
case-insensitive, deliberately, and it is stated here because silently granting
LESS than intended is the safe direction and silently granting more is not -- this
choice is the safe one only because it cannot broaden a rule beyond its pattern.
"""

from __future__ import annotations

import fnmatch
import re
import secrets
from dataclasses import dataclass, field
from typing import Literal

Operation = Literal["pull", "push"]

# A key id is shown in UIs and logs; the secret never is. Split so that a log line
# can name WHICH key acted without the log becoming a credential store.
_KEY_ID_BYTES = 8
_KEY_SECRET_BYTES = 32


@dataclass(frozen=True)
class Rule:
    """One grant. `pattern` is matched against `<upstream>/<repo>`.

    A rule with neither pull nor push is inert rather than an error: it is what a
    UI produces when someone unchecks both boxes, and refusing it would turn a
    harmless no-op into a failed save.
    """

    pattern: str
    pull: bool = True
    push: bool = False

    def grants(self, operation: Operation, reference: str) -> bool:
        if operation == "pull" and not self.pull:
            return False
        if operation == "push" and not self.push:
            return False
        return _matches(self.pattern, reference)


@dataclass
class Key:
    """A credential a machine presents. The secret is stored HASHED, never raw."""

    key_id: str
    secret_hash: str
    principal: str
    rules: list[Rule] = field(default_factory=list)
    label: str = ""
    disabled: bool = False

    def allows(self, operation: Operation, reference: str) -> bool:
        if self.disabled:
            return False
        return any(r.grants(operation, reference) for r in self.rules)


@dataclass
class Principal:
    """A human, identified by whatever the identity provider calls stable.

    `is_admin` is STORED rather than derived from being first to log in. Deriving
    it would mean the admin changes when the first account is deleted, or that
    deleting your own account silently promotes someone else -- and an operator who
    removes their own account should not be able to lock the instance out of ever
    having an admin again.
    """

    subject: str
    email: str = ""
    is_admin: bool = False
    disabled: bool = False


def _matches(pattern: str, reference: str) -> bool:
    """Wildcard match of a rule pattern against `<upstream>/<repo>`.

    Uses fnmatch semantics where `*` spans `/`, via translate() so that the
    behaviour is a regex we control rather than fnmatch's platform-dependent
    case handling -- fnmatch.fnmatch() lowercases according to the OS on some
    platforms, which would make authorisation differ between a developer's
    machine and the container.
    """
    if not pattern or not reference:
        return False
    regex = fnmatch.translate(pattern.strip().lower())
    return re.match(regex, reference.strip().lower()) is not None


def new_secret() -> tuple[str, str]:
    """Return (key_id, secret). The secret is returned ONCE and never stored raw.

    Shown to the user at creation and unrecoverable afterwards, which is the same
    contract as a cloud provider's API key -- and the reason the UI has to say so
    at the moment of creation rather than in documentation nobody opens.
    """
    return secrets.token_hex(_KEY_ID_BYTES), secrets.token_urlsafe(_KEY_SECRET_BYTES)


def decide(key: Key | None, operation: Operation, reference: str) -> tuple[bool, str]:
    """Authorise one operation. Returns (allowed, reason).

    The reason is for the LOG, not for the client: telling an unauthorised caller
    which rule refused them describes the policy to someone who has just failed to
    satisfy it. The wire gets a status; the operator gets the sentence.
    """
    if key is None:
        return False, "no key presented"
    if key.disabled:
        return False, f"key {key.key_id} is disabled"
    if not key.rules:
        return False, f"key {key.key_id} has no rules"
    if key.allows(operation, reference):
        return True, f"key {key.key_id} allows {operation} on {reference}"
    return False, f"key {key.key_id} has no rule granting {operation} on {reference}"
