"""Headless provisioning operations, shared by /_cache/authz and the CLI.

WHY THIS EXISTS. The browser console was the only supported way to create a
credential, so anyone running the cache for CI or a cluster WITHOUT an identity
provider had no way in. This module is the one implementation of "create a
principal, set what it may do, mint it a key", and both front ends are thin:

    app/authzmanage.py   HTTP, under /_cache/authz, behind XHC_MANAGE_TOKEN
    app/authzctl.py      `python -m app.authzctl`, straight at XHC_AUTHZ_DB,
                         so an init container can provision before the server
                         starts

Two front ends over one set of functions, rather than two implementations,
because the properties that matter live here and a second copy is the one that
drifts:

  * NEVER THE FIRST-LOGIN PATH. Principals are made with create_principal and an
    explicit admin flag, never claim_or_get_principal -- a machine account must
    not become admin by being first.
  * THE SERVER GENERATES THE SECRET, with the generator the console uses. There
    is no parameter for a caller-chosen one: a chosen secret is a password, and
    the store's sha256 is only right for 256 bits of randomness.
  * THE SECRET IS RETURNED ONCE, by mint_key, and nothing else in this module
    can produce it. key_out has no secret field and no hash field.
  * NO SILENT SUCCESS. An unknown principal or key is an error, not a no-op.

Imports nothing that reads the environment, so the CLI can run in a container
whose other settings would not validate.
"""

from __future__ import annotations

import sqlite3

from . import authz
from .authzstore import AuthzStore

MAX_SUBJECT_LEN = 256
MAX_EMAIL_LEN = 256
MAX_LABEL_LEN = 200  # what the console truncates to; here it is refused instead


class ProvisionError(Exception):
    """Base. The message is safe to show the caller."""


class NotFound(ProvisionError):
    pass


class Conflict(ProvisionError):
    pass


class Invalid(ProvisionError):
    pass


def rule_out(rule: authz.Rule) -> dict:
    return {"pattern": rule.pattern, "pull": rule.pull, "push": rule.push}


def key_out(key: authz.Key) -> dict:
    """A key as any management surface sees it. NOTE what is absent: secret_hash.

    The hash is not the secret, but it is the only thing standing between a
    stolen database row and a working credential, and there is no reason for it
    to cross this boundary.
    """
    return {
        "key_id": key.key_id,
        "label": key.label,
        "principal": key.principal,
        "disabled": key.disabled,
        # What the HOLDER may do, and what THIS KEY may do of that. Reported
        # separately because a refusal is diagnosed differently depending on
        # which one stopped it.
        "rules": [rule_out(r) for r in key.rules],
        "scope": [rule_out(r) for r in key.scope],
    }


def _check_subject(subject: str) -> None:
    """Refuse rather than normalise.

    Stripping whitespace would store a subject other than the one requested,
    and the next call naming the original would 404. A `/` cannot be addressed
    in a path segment, so a principal holding one could be created and never
    managed again over HTTP.
    """
    if not subject:
        raise Invalid("subject must not be empty")
    if subject != subject.strip():
        raise Invalid("subject must not start or end with whitespace")
    if len(subject) > MAX_SUBJECT_LEN:
        raise Invalid(f"subject longer than {MAX_SUBJECT_LEN} characters")
    if "/" in subject or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in subject):
        raise Invalid("subject must not contain '/' or control characters")


def _require_principal(store: AuthzStore, subject: str) -> authz.Principal:
    principal = store.get_principal(subject)
    if principal is None:
        raise NotFound(f"no such principal: {subject}")
    return principal


def _parse(lines: list[str]) -> list[authz.Rule]:
    try:
        return authz.parse_rules(lines)
    except authz.RuleSyntaxError as exc:
        raise Invalid(str(exc)) from exc


def principal_out(store: AuthzStore, p: authz.Principal) -> dict:
    return {
        "subject": p.subject,
        "email": p.email,
        "is_admin": p.is_admin,
        "disabled": p.disabled,
        "rules": [rule_out(r) for r in store.get_principal_rules(p.subject)],
        "key_count": len(store.list_keys(p.subject)),
    }


def create_principal(
    store: AuthzStore, subject: str, email: str = "", is_admin: bool = False
) -> dict:
    _check_subject(subject)
    if len(email) > MAX_EMAIL_LEN:
        raise Invalid(f"email longer than {MAX_EMAIL_LEN} characters")
    try:
        p = store.create_principal(subject, email, is_admin=is_admin)
    except KeyError as exc:
        raise Conflict(f"principal already exists: {subject}") from exc
    return principal_out(store, p)


def list_principals(store: AuthzStore) -> list[dict]:
    return [principal_out(store, p) for p in store.list_principals()]


def delete_principal(store: AuthzStore, subject: str) -> None:
    try:
        store.delete_principal(subject)
    except KeyError as exc:
        raise NotFound(f"no such principal: {subject}") from exc
    except ValueError as exc:  # the last-admin refusal, worded by the store
        raise Conflict(str(exc)) from exc


def set_admin(store: AuthzStore, subject: str, is_admin: bool) -> dict:
    """Grant or revoke admin. Revoking the last admin is refused by the store.

    The recovery path when an instance has no admin, and it needs no login and
    no restart: the server re-reads the principal on every console request.
    With XHC_OIDC_ADMIN_CLAIM set, the principal's next login recomputes the
    flag from the provider, so this lasts until then.
    """
    try:
        store.set_admin(subject, is_admin)
    except KeyError as exc:
        raise NotFound(f"no such principal: {subject}") from exc
    except ValueError as exc:  # the last-admin refusal, worded by the store
        raise Conflict(str(exc)) from exc
    return principal_out(store, _require_principal(store, subject))


def set_rules(store: AuthzStore, subject: str, lines: list[str]) -> list[dict]:
    """Replace a principal's grant. Parsed in full before anything is written."""
    rules = _parse(lines)
    try:
        store.set_principal_rules(subject, rules)
    except KeyError as exc:
        raise NotFound(f"no such principal: {subject}") from exc
    return [rule_out(r) for r in rules]


def mint_key(
    store: AuthzStore, subject: str, label: str = "", scope: list[str] | None = None
) -> dict:
    """Mint a key. The ONLY function that returns a secret, and it does so once."""
    if len(label) > MAX_LABEL_LEN:
        raise Invalid(f"label longer than {MAX_LABEL_LEN} characters")
    rules = authz.normalise_scope(_parse(scope or []))
    _require_principal(store, subject)
    key_id, secret = authz.new_secret()
    try:
        store.add_key(key_id, secret, subject, rules, label=label)
    except sqlite3.IntegrityError as exc:
        # The principal was deleted between the check above and the insert.
        raise NotFound(f"no such principal: {subject}") from exc
    return {
        "key_id": key_id,
        "secret": secret,
        "principal": subject,
        "label": label,
        "scope": [rule_out(r) for r in rules],
    }


def list_keys(store: AuthzStore, principal: str | None = None) -> list[dict]:
    if principal is not None:
        _require_principal(store, principal)
    return [key_out(k) for k in store.list_keys(principal)]


def set_key_disabled(store: AuthzStore, key_id: str, disabled: bool) -> None:
    try:
        store.set_key_disabled(key_id, disabled)
    except KeyError as exc:
        raise NotFound(f"no such key: {key_id}") from exc


def delete_key(store: AuthzStore, key_id: str) -> None:
    try:
        store.delete_key(key_id)
    except KeyError as exc:
        raise NotFound(f"no such key: {key_id}") from exc
