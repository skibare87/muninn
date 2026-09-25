"""Browser login routes: /_auth/login, /_auth/callback, /_auth/logout, /_auth/me.

The whole surface is mounted only when XHC_OIDC_ISSUER is set. A deployment with
no issuer has no login routes at all -- not disabled ones that 404 with a hint,
which would still advertise the feature and still be a place to send traffic.

WHAT THIS IS NOT: it is not a second gate on /v2. A browser session never
authorises a pull or a push. Those authenticate with a key, every time, through
dockerauth. Keeping the two credential kinds disjoint is the point -- a stolen
browser cookie cannot pull images, and a leaked key cannot manage other keys.

FIRST LOGIN BECOMES ADMIN, and the claim is made inside a single IMMEDIATE
transaction in the store, not read-then-write here. Two people opening the login
page at the same instant on a fresh deployment is exactly the case a read then a
write gets wrong, and it is the case that decides who administers the service.

UNLESS THE PROVIDER DECIDES (XHC_OIDC_ADMIN_CLAIM + XHC_OIDC_ADMIN_VALUE). Then
admin is recomputed from the verified id_token at EVERY login -- granted when the
claim carries the value, revoked when it does not -- and being first grants
nothing. Precedence in that mode, highest first:

    XHC_BOOTSTRAP_ADMIN is this SUBJECT     admin, whatever the claim says
    the claim carries the value             admin
    otherwise                               NOT admin, even if it was before

The bootstrap sits above the claim because it is the break-glass path: a claim
mapping broken at the provider must not be able to lock out the operator who
would fix it. In this mode it is therefore a STANDING grant rather than the
creation-only one it is otherwise, and belongs unset outside an emergency.
Being standing, it matches the SUBJECT ONLY: a grant re-applied at every login
must not key on an email, which most providers let the user edit. (Config
refuses an email-shaped value in this mode; _sync_admin does not rely on that.)

A revocation at the provider reaches Muninn at that user's NEXT LOGIN; nothing
here polls the provider. Once it is in the store it applies to every session on
its next request, because admin is never in the cookie (require_login re-reads
the principal). So an existing session is bounded by XHC_SESSION_TTL, and that
bound is documented rather than hidden.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from . import authzstore, dockerauth, oidc, session
from .config import settings

log = logging.getLogger("xhc.webauth")

router = APIRouter(prefix="/_auth", tags=["auth"])

_client: oidc.OIDCClient | None = None


def client() -> oidc.OIDCClient:
    """The OIDC client, built once. Only reachable when login is configured."""
    global _client  # noqa: PLW0603 - module-level singleton
    if _client is None:
        _client = oidc.OIDCClient(
            issuer=settings.oidc_issuer or "",
            client_id=settings.oidc_client_id or "",
            client_secret=settings.oidc_client_secret or "",
            redirect_uri=settings.oidc_redirect_uri or "",
            scopes=settings.oidc_scopes,
            discovery_url=settings.oidc_discovery_url,
            pkce=settings.oidc_pkce,
        )
    return _client


def enabled() -> bool:
    return bool(settings.oidc_issuer)


def admin_from_idp() -> bool:
    """Whether the identity provider, not this store, decides who is admin."""
    return bool(settings.oidc_admin_claim and settings.oidc_admin_value)


# One diagnostic per process for an id_token without the configured claim. Once,
# because a provider that omits the claim for every non-admin would otherwise
# log on every ordinary login; at all, because a provider that NEVER emits it
# (not in the scopes, not mapped into the id_token, a typo in the name) looks
# exactly like "nobody is an admin" and needs saying somewhere.
_missing_claim_logged = False


def _claim_admin(claims: dict) -> bool:
    global _missing_claim_logged  # noqa: PLW0603 - once-per-process latch
    name = settings.oidc_admin_claim or ""
    value = oidc.claim_at(claims, name)
    if value is oidc.ABSENT:
        if not _missing_claim_logged:
            _missing_claim_logged = True
            # Claim NAMES only. Values can be personal data and group lists.
            log.warning(
                "XHC_OIDC_ADMIN_CLAIM %r is absent from the id_token, so this "
                "login is not admin. Claims present: %s. If no login ever carries "
                "it, the provider is not putting it in the id_token (check its "
                "scopes and mappers). Logged once per process.",
                name, ", ".join(sorted(claims)) or "(none)",
            )
        return False
    return oidc.claim_grants(value, settings.oidc_admin_value or "")


def _bootstrap_names(subject: str) -> bool:
    """Claim mode only: XHC_BOOTSTRAP_ADMIN as a standing grant, SUBJECT ONLY.

    Never the email. Outside claim mode the store also accepts an email, but
    only at creation; here the grant is re-applied at every login, and an
    email the user can change at their provider would make it self-service.
    """
    b = settings.bootstrap_admin
    return bool(b) and b == subject


def current_session(request: Request) -> session.Session | None:
    return session.verify(
        settings.session_secret or "", request.cookies.get(session.COOKIE_NAME)
    )


def require_login(request: Request) -> authzstore.Principal:
    """The principal for this request, re-read from the STORE every time.

    Re-reading is what makes a stateless cookie safe: disabling a principal takes
    effect on their next request rather than when their cookie happens to expire.
    A cookie naming a principal who no longer exists is refused, not treated as a
    new signup -- signup happens only through a completed OIDC login.
    """
    sess = current_session(request)
    if sess is None:
        raise HTTPException(status_code=401, detail="not logged in")
    st = dockerauth.store()
    if st is None:
        raise HTTPException(status_code=503, detail="authorisation store unavailable")
    for principal in st.list_principals():
        if principal.subject == sess.subject:
            if principal.disabled:
                raise HTTPException(status_code=403, detail="account disabled")
            return principal
    raise HTTPException(status_code=401, detail="unknown principal")


def require_admin(request: Request) -> authzstore.Principal:
    principal = require_login(request)
    if not principal.is_admin:
        raise HTTPException(status_code=403, detail="admin required")
    return principal


def _set_cookie(response: Response, value: str) -> None:
    response.set_cookie(
        session.COOKIE_NAME,
        value,
        max_age=int(settings.session_ttl_s),
        httponly=True,   # the cookie is never read by page script, so deny it
        secure=True,     # the redirect_uri is required to be https, so this holds
        samesite="lax",  # "strict" would drop the cookie on the IdP's redirect back
        path="/",
    )


@router.get("/login")
async def login() -> RedirectResponse:
    """Start a login. No parameters -- in particular no `next` or `redirect_uri`.

    A destination taken from the query string is how an OAuth callback becomes an
    open redirect, and an open redirect on a callback hands the authorisation
    code to whoever supplied the target.
    """
    if not enabled():
        raise HTTPException(status_code=404, detail="login is not configured")
    try:
        url = await client().authorization_url()
    except oidc.OIDCError as exc:
        # The provider being unreachable is an outage, not a client error, and
        # the distinction matters to whoever is looking at the logs at 3am.
        log.warning("login could not start: %s", exc)
        raise HTTPException(status_code=502, detail="identity provider unavailable") from exc
    return RedirectResponse(url, status_code=302)


@router.get("/callback")
async def callback(request: Request) -> Response:
    if not enabled():
        raise HTTPException(status_code=404, detail="login is not configured")

    # The provider reporting an error is a normal outcome (the user pressed
    # "cancel"), so it is rendered as a refusal and not as a server fault.
    if request.query_params.get("error"):
        raise HTTPException(
            status_code=401,
            detail=f"login refused: {request.query_params.get('error')}",
        )

    code = request.query_params.get("code")
    state = request.query_params.get("state")
    if not code or not state:
        raise HTTPException(status_code=400, detail="missing code or state")

    try:
        identity = await client().complete(code, state)
    except oidc.OIDCError as exc:
        # Everything the OIDC module refuses lands here as 401 with a generic
        # body. Which check failed is a fact about our verification, and telling
        # an attacker whether it was the audience, the nonce or the signature
        # turns one probe into an oracle. The detail goes to the log.
        log.warning("login failed: %s", exc)
        raise HTTPException(status_code=401, detail="login failed") from exc

    st = dockerauth.store()
    if st is None:
        raise HTTPException(status_code=503, detail="authorisation store unavailable")

    if admin_from_idp():
        principal = _sync_admin(st, identity)
    else:
        principal = st.claim_or_get_principal(
            identity.subject, identity.email, settings.bootstrap_admin
        )
    if principal.disabled:
        # Authenticated, and still not allowed in. Refusing here rather than
        # issuing a cookie means a disabled account cannot get a session at all.
        raise HTTPException(status_code=403, detail="account disabled")

    log.info(
        "login: subject=%s admin=%s", identity.subject[:12] + "...", principal.is_admin
    )
    response = RedirectResponse("/", status_code=302)
    _set_cookie(
        response,
        session.issue(
            settings.session_secret or "",
            principal.subject,
            principal.email,
            settings.session_ttl_s,
        ),
    )
    return response


def _sync_admin(st: authzstore.AuthzStore, identity) -> authzstore.Principal:
    """Record what the provider says about admin for this login, and log it.

    Evaluated before the disabled check on purpose: a revocation must land on
    a disabled account too, or re-enabling it later would restore an admin
    flag the provider withdrew in the meantime.
    """
    by_claim = _claim_admin(getattr(identity, "claims", None) or {})
    by_bootstrap = _bootstrap_names(identity.subject)
    result = st.sync_admin_at_login(
        identity.subject, identity.email, by_claim or by_bootstrap
    )
    who = identity.subject[:12] + "..."
    if by_bootstrap and not by_claim:
        log.warning(
            "admin granted to %s by XHC_BOOTSTRAP_ADMIN, not by the identity "
            "provider. That is the break-glass path; unset it once the claim is fixed.",
            who,
        )
    if result.was_admin and not result.principal.is_admin:
        log.warning("admin revoked for %s: the id_token no longer carries it", who)
    elif result.was_admin is False and result.principal.is_admin:
        log.info("admin granted to %s at login", who)
    if result.admins_after == 0:
        # Allowed, and deliberately loud. Refusing would keep admin for the one
        # person whose role was just revoked. Recovery is any login carrying
        # the claim, XHC_BOOTSTRAP_ADMIN, or `authzctl grant-admin`.
        log.error(
            "this instance now has no admin: the last one logged in without "
            "XHC_OIDC_ADMIN_CLAIM=%r carrying %r. Grant the role at the identity "
            "provider and log in, set XHC_BOOTSTRAP_ADMIN, or run "
            "`python -m app.authzctl grant-admin SUBJECT`.",
            settings.oidc_admin_claim, settings.oidc_admin_value,
        )
    return result.principal


@router.post("/logout")
async def logout() -> Response:
    """POST, not GET. A GET logout can be fired by any page that embeds an
    image pointing at it, which is a nuisance rather than a breach, but it is
    free to avoid."""
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(session.COOKIE_NAME, path="/")
    return response


@router.get("/me")
async def me(request: Request) -> JSONResponse:
    """Who the browser is. Used by the homepage to decide whether to show a
    login button or a management link, so it answers for anonymous callers
    too rather than 401-ing -- being logged out is not an error."""
    if not enabled():
        return JSONResponse({"login_enabled": False, "authenticated": False})
    sess = current_session(request)
    if sess is None:
        return JSONResponse({"login_enabled": True, "authenticated": False})
    st = dockerauth.store()
    if st is None:
        return JSONResponse({"login_enabled": True, "authenticated": False})
    for principal in st.list_principals():
        if principal.subject == sess.subject and not principal.disabled:
            return JSONResponse(
                {
                    "login_enabled": True,
                    "authenticated": True,
                    "email": principal.email,
                    "is_admin": principal.is_admin,
                    # The console hides its admin toggle when this is true:
                    # the next login would overwrite whatever it set.
                    "admin_from_idp": admin_from_idp(),
                }
            )
    return JSONResponse({"login_enabled": True, "authenticated": False})
