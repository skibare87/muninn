"""Self-service key management, behind the browser login.

WHO MAY DO WHAT, and it is the whole security model of this file:

    any logged-in user   create, list, disable and delete THEIR OWN keys
    admin only           see users, set a user's allowlist, grant/revoke admin,
                         disable a user, and act on any key

A user manages the CREDENTIALS they hold. An admin decides what those
credentials may do. Those are deliberately different powers: if a user could
edit their own rules, "create a key" would be indistinguishable from "grant
myself push to everything", and the allowlist would be decoration.

Every handler that touches a key routes through `_owned_key`, which is the
single place ownership is decided. One choke point rather than a check repeated
in five handlers, because the bug in this shape is always the handler where
someone forgot.

THE SECRET IS RETURNED ONCE, at creation, and is not recoverable afterwards --
the store keeps only a hash. That is not an inconvenience to work around with a
"show key" endpoint; it is the property that makes a database copy less than a
full compromise.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Body, HTTPException, Request
from pydantic import BaseModel, Field

from . import authz, dockerauth, webauth

log = logging.getLogger("xhc.console")

router = APIRouter(prefix="/_console", tags=["console"])


class RuleIn(BaseModel):
    pattern: str = Field(..., max_length=512)
    pull: bool = True
    push: bool = False


class AllowlistIn(BaseModel):
    rules: list[RuleIn]


def _store():
    st = dockerauth.store()
    if st is None:
        raise HTTPException(status_code=503, detail="authorisation store unavailable")
    return st


def _rule_out(rule: authz.Rule) -> dict:
    return {"pattern": rule.pattern, "pull": rule.pull, "push": rule.push}


def _key_out(key: authz.Key) -> dict:
    """A key as the UI sees it. NOTE what is absent: secret_hash.

    The hash is not the secret, but it is the only thing standing between a
    stolen database row and a working credential, and there is no reason for it
    to cross this boundary.
    """
    return {
        "key_id": key.key_id,
        "label": key.label,
        "principal": key.principal,
        "disabled": key.disabled,
        "rules": [_rule_out(r) for r in key.rules],
    }


def _owned_key(request: Request, key_id: str) -> authz.Key:
    """The key, if this caller may act on it. THE ownership choke point.

    A key belonging to someone else is reported as 404, not 403. 403 confirms
    the key exists, which turns this endpoint into a way to enumerate other
    users' key ids.
    """
    principal = webauth.require_login(request)
    for key in _store().list_keys():
        if key.key_id == key_id:
            if key.principal != principal.subject and not principal.is_admin:
                raise HTTPException(status_code=404, detail="no such key")
            return key
    raise HTTPException(status_code=404, detail="no such key")


# ---------------- a user's own keys ----------------


@router.get("/keys")
async def list_my_keys(request: Request) -> dict:
    principal = webauth.require_login(request)
    keys = _store().list_keys(principal.subject)
    return {
        "keys": [_key_out(k) for k in keys],
        # Surfaced because a key with no rules authenticates and authorises
        # NOTHING, and a user who is not told that will file it as a bug in the
        # cache. The empty-rule-list-grants-nothing behaviour is correct; it is
        # silently correct, which is the problem.
        "allowlist": [_rule_out(r) for r in _store().get_principal_rules(principal.subject)],
        "note": (
            "A key can only do what your allowlist permits. An empty allowlist "
            "grants nothing -- ask an administrator to set one."
        ),
    }


@router.post("/keys")
async def create_my_key(request: Request, label: str = Body("", embed=True)) -> dict:
    principal = webauth.require_login(request)
    key_id, secret = authz.new_secret()
    # No key-specific rules: a new key's authority is its owner's allowlist and
    # nothing more. Passed explicitly rather than defaulted, so that "a user
    # created a key" can never be the act that widens what they may do.
    _store().add_key(key_id, secret, principal.subject, [], label=label[:200])
    log.info("key created: %s for %s", key_id, principal.subject[:12] + "...")
    return {
        "key_id": key_id,
        "secret": secret,
        "label": label[:200],
        "warning": "This is the only time the secret is shown. It is not recoverable.",
        "docker_login": f"docker login <this-host> -u {key_id} --password-stdin",
    }


@router.post("/keys/{key_id}/disabled")
async def set_my_key_disabled(
    request: Request, key_id: str, disabled: bool = Body(..., embed=True)
) -> dict:
    key = _owned_key(request, key_id)
    _store().set_key_disabled(key.key_id, disabled)
    return {"key_id": key.key_id, "disabled": disabled}


@router.delete("/keys/{key_id}")
async def delete_my_key(request: Request, key_id: str) -> dict:
    key = _owned_key(request, key_id)
    _store().delete_key(key.key_id)
    log.info("key deleted: %s", key.key_id)
    return {"key_id": key.key_id, "deleted": True}


# ---------------- administration ----------------


@router.get("/users")
async def list_users(request: Request) -> dict:
    webauth.require_admin(request)
    st = _store()
    return {
        "users": [
            {
                "subject": p.subject,
                "email": p.email,
                "is_admin": p.is_admin,
                "disabled": p.disabled,
                "allowlist": [_rule_out(r) for r in st.get_principal_rules(p.subject)],
                "key_count": len(st.list_keys(p.subject)),
            }
            for p in st.list_principals()
        ]
    }


@router.put("/users/{subject}/allowlist")
async def set_user_allowlist(
    request: Request, subject: str, body: AllowlistIn
) -> dict:
    """Replace a user's allowlist. `*` for both pull and push is "anything,
    anywhere", which is what Matt asked for as the permissive case -- and it is
    spelled out rather than implied, so nobody reaches it by leaving a field
    blank."""
    webauth.require_admin(request)
    parsed = [authz.Rule(r.pattern, r.pull, r.push) for r in body.rules]
    try:
        _store().set_principal_rules(subject, parsed)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="no such user") from exc
    log.info("allowlist set for %s: %d rule(s)", subject[:12] + "...", len(parsed))
    return {"subject": subject, "allowlist": [_rule_out(r) for r in parsed]}


@router.post("/users/{subject}/admin")
async def set_user_admin(
    request: Request, subject: str, is_admin: bool = Body(..., embed=True)
) -> dict:
    webauth.require_admin(request)
    try:
        # The store refuses to remove the last admin. That check lives there
        # rather than here because it is a property of the data, and a second
        # copy of it in this handler would be the copy that goes stale.
        _store().set_admin(subject, is_admin)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"subject": subject, "is_admin": is_admin}


@router.post("/users/{subject}/disabled")
async def set_user_disabled(
    request: Request, subject: str, disabled: bool = Body(..., embed=True)
) -> dict:
    """Disabling a user disables every key they hold, in the same act.

    That is enforced in the store's read query rather than by walking their keys
    here -- a loop that disables keys one at a time can be interrupted halfway,
    and leaves a disabled user holding working credentials.
    """
    admin = webauth.require_admin(request)
    if subject == admin.subject and disabled:
        # Not paternalism: an admin who disables themselves cannot re-enable
        # themselves, and on a single-admin deployment that locks everyone out
        # of administration permanently with no path back but a shell on the box.
        raise HTTPException(status_code=400, detail="an admin cannot disable themselves")
    try:
        _store().set_principal_disabled(subject, disabled)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="no such user") from exc
    log.info("user %s disabled=%s", subject[:12] + "...", disabled)
    return {"subject": subject, "disabled": disabled}


@router.get("/users/{subject}/keys")
async def list_user_keys(request: Request, subject: str) -> dict:
    webauth.require_admin(request)
    return {"keys": [_key_out(k) for k in _store().list_keys(subject)]}
