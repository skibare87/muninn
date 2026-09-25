"""Headless provisioning over HTTP: /_cache/authz/*, behind XHC_MANAGE_TOKEN.

For CI and cluster deployments with no identity provider, where the browser
console cannot be reached because there is no login to put in front of it.
The operations themselves live in authzadmin.py and are shared with the CLI.

The token gate is the one every /_cache router uses (managegate.ManageRoute),
so this surface cannot drift from its neighbours. This surface was the first to
read an unset token as CLOSED; the rest of /_cache now does too. On top of the
shared gate it needs a store:

    XHC_MANAGE_TOKEN unset  404  naming the setting (the shared gate)
    token wrong or absent   401  (the shared gate)
    XHC_AUTHZ_DB unset      404  there is no store to provision

404 rather than 503 for the unconfigured cases because that is this project's
convention for a surface that is off: the console and /_auth are not mounted
at all without a login. It is nonetheless ALWAYS mounted (see main.py): an
unmounted path here falls to the Hugging Face catch-all and is proxied to the
Hub, so the 404 has to come from a route that exists and refuses.

The token is compared in constant time, by the shared gate.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from . import authzadmin, dockerauth, managegate
from .config import settings

log = logging.getLogger("xhc.authzmanage")


async def require_authz_store() -> None:
    """Runs only after the shared token gate has let the request through."""
    if not settings.authz_db or dockerauth.store() is None:
        raise HTTPException(status_code=404, detail="Not Found")


router = APIRouter(
    prefix="/_cache/authz",
    tags=["authz"],
    route_class=managegate.ManageRoute,
    dependencies=[Depends(require_authz_store)],
)


class PrincipalIn(BaseModel):
    # Unknown fields are refused rather than ignored, so a misspelt `is_admin`
    # is an error and not a silently non-admin principal the caller believes
    # is an admin -- or the reverse.
    model_config = ConfigDict(extra="forbid")
    subject: str
    email: str = ""
    # Strict: "yes", 1 or "true" are refused. Admin is granted by a literal
    # boolean somebody typed, and by nothing else.
    is_admin: StrictBool = False


class RulesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rules: list[str] = Field(
        ..., description="one rule per item: '<pattern> pull|push|pull+push'"
    )


class MintIn(BaseModel):
    # extra="forbid" is also what makes a caller-supplied `secret` a 422
    # rather than a field quietly dropped while the caller believes it was used.
    model_config = ConfigDict(extra="forbid")
    label: str = ""
    scope: list[str] = Field(default_factory=list)


class DisabledIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Strict for the same reason as is_admin: "false" as a string must not be
    # read as a revocation, or "0" as a re-enable.
    disabled: StrictBool


def _store():
    st = dockerauth.store()
    assert st is not None  # guaranteed by require_authz_store
    return st


def _raise(exc: authzadmin.ProvisionError) -> None:
    status = {
        authzadmin.NotFound: 404,
        authzadmin.Conflict: 409,
        authzadmin.Invalid: 400,
    }.get(type(exc), 400)
    raise HTTPException(status_code=status, detail=str(exc)) from exc


@router.get("/principals")
async def list_principals() -> dict:
    return {"principals": authzadmin.list_principals(_store())}


@router.post("/principals", status_code=201)
async def create_principal(body: PrincipalIn) -> dict:
    try:
        out = authzadmin.create_principal(_store(), body.subject, body.email, body.is_admin)
    except authzadmin.ProvisionError as exc:
        _raise(exc)
    log.info("principal created over /_cache/authz: %s admin=%s",
             body.subject[:12] + "...", body.is_admin)
    return out


@router.delete("/principals/{subject}")
async def delete_principal(subject: str) -> dict:
    try:
        authzadmin.delete_principal(_store(), subject)
    except authzadmin.ProvisionError as exc:
        _raise(exc)
    log.info("principal deleted over /_cache/authz: %s", subject[:12] + "...")
    return {"subject": subject, "deleted": True}


@router.put("/principals/{subject}/rules")
async def set_rules(subject: str, body: RulesIn) -> dict:
    try:
        rules = authzadmin.set_rules(_store(), subject, body.rules)
    except authzadmin.ProvisionError as exc:
        _raise(exc)
    log.info("rules set over /_cache/authz for %s: %d rule(s)", subject[:12] + "...", len(rules))
    out: dict = {"subject": subject, "rules": rules}
    if not rules:
        out["note"] = "an empty rule list grants nothing: this principal's keys can do nothing"
    return out


@router.post("/principals/{subject}/keys", status_code=201)
async def mint_key(subject: str, response: Response, body: MintIn | None = None) -> dict:
    body = body or MintIn()
    try:
        minted = authzadmin.mint_key(_store(), subject, body.label, body.scope)
    except authzadmin.ProvisionError as exc:
        _raise(exc)
    # The one response carrying a secret. Not for any cache between here and
    # the caller to keep.
    response.headers["cache-control"] = "no-store"
    log.info("key minted over /_cache/authz: %s for %s",
             minted["key_id"], subject[:12] + "...")
    return {
        **minted,
        "warning": "This is the only time the secret is shown. It is not recoverable.",
    }


@router.get("/keys")
async def list_keys(principal: str | None = None) -> dict:
    try:
        return {"keys": authzadmin.list_keys(_store(), principal)}
    except authzadmin.ProvisionError as exc:
        _raise(exc)


@router.post("/keys/{key_id}/disabled")
async def set_key_disabled(key_id: str, body: DisabledIn) -> dict:
    disabled = body.disabled
    try:
        authzadmin.set_key_disabled(_store(), key_id, disabled)
    except authzadmin.ProvisionError as exc:
        _raise(exc)
    log.info("key %s disabled=%s over /_cache/authz", key_id, disabled)
    return {"key_id": key_id, "disabled": disabled}


@router.delete("/keys/{key_id}")
async def delete_key(key_id: str) -> dict:
    try:
        authzadmin.delete_key(_store(), key_id)
    except authzadmin.ProvisionError as exc:
        _raise(exc)
    log.info("key deleted over /_cache/authz: %s", key_id)
    return {"key_id": key_id, "deleted": True}
