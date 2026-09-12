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
        )
    return _client


def enabled() -> bool:
    return bool(settings.oidc_issuer)


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

    principal = st.claim_or_get_principal(identity.subject, identity.email)
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
                }
            )
    return JSONResponse({"login_enabled": True, "authenticated": False})
