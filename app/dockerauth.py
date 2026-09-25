"""Optional client-facing auth on the pull surface (an internal issue).

A GATE, NOT ISOLATION, and the distinction is the whole design. Passing it means
you may use this cache; everyone who passes sees everything it holds. That is
the maintainer's ruling made enforceable, not weakened:

    "I don't want the cache to check credentials, once pulled into my network,
     trusted endpoints on my network can pull it."  -- 2026-09-01

Per-client authorization is NOT possible here and must never be implied. A
cached hit consults no credentials at all: it checks the fleet-wide allow/deny
policy and serves off disk, and the store is keyed by upstream, repo and digest
with no principal in it. Any scheme promising "A cannot read what B pulled"
would be enforced on the MISS and silently absent on every HIT after it, and be
false from the first cache fill. This promises nothing it cannot keep.

WHY PER-HOST CREDENTIALS RATHER THAN ONE SHARED SECRET. A `docker pull` cannot
send an identifying header, ocicompat records no principal, and the mesh gateway
masquerades so every client arrives wearing the same address. Credentials are
therefore the ONLY mechanism by which this cache can ever know which node pulled
what -- there is no third option to add later. the maintainer's ruling, 2026-09-01.

Default is `none`: unchanged behaviour, nothing to configure, no flag day.
"""

from __future__ import annotations

import base64
import binascii
import logging
from pathlib import Path

import bcrypt
from fastapi import Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from . import authz, authzstore, jwtauth
from .config import settings

log = logging.getLogger("xhc.dockerauth")

_users: dict[str, bytes] | None = None
# Compared against when the supplied user does not exist, so a bad username and
# a bad password cost the same time. Without it, response latency reveals which
# usernames are real.
_DUMMY = bcrypt.hashpw(b"muninn-timing-equaliser", bcrypt.gensalt(rounds=10))


class HtpasswdError(RuntimeError):
    """Raised at startup only. Never resolved to 'no auth configured'."""


def _parse(text: str) -> dict[str, bytes]:
    users: dict[str, bytes] = {}
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            raise HtpasswdError(f"htpasswd line {lineno}: no ':' separator")
        user, _, digest = line.partition(":")
        if not user:
            raise HtpasswdError(f"htpasswd line {lineno}: empty username")
        # bcrypt only. Apache's other formats (crypt, MD5-apr1, plain SHA-1) are
        # broken or unsalted, and silently accepting one would make a weak file
        # look configured. Generate with `htpasswd -B`.
        if not digest.startswith(("$2a$", "$2b$", "$2y$")):
            raise HtpasswdError(
                f"htpasswd line {lineno}: user {user!r} is not bcrypt. "
                "Muninn accepts bcrypt only -- regenerate with `htpasswd -B`."
            )
        users[user] = digest.encode()
    if not users:
        raise HtpasswdError("htpasswd file contains no usable entries")
    return users


def load() -> None:
    """Called once at startup. Raises rather than degrading to open.

    An ABSENT config means the operator did not ask for auth, which is
    legitimate. An UNREADABLE credential file when auth WAS asked for means
    unknown, and resolving unknown to permissive is what disarmed pin protection
    in an internal issue. It must not be possible to lose a password file and silently
    return to an open cache.
    """
    global _users  # noqa: PLW0603 - module-level cache, loaded once
    _users = None
    if settings.docker_auth == "none":
        # THE NEGATIVE CONTROL. A credential file set while auth is off is not a
        # neutral state: it is what an operator is left with after following this
        # project's own push-through security warning, which named only the file.
        # Silence here made "correctly open" and "you tried to close it and
        # failed" identical -- and the file is never opened, so a malformed one
        # would not have complained either.
        #
        # The sibling case (auth=basic with no file) REFUSES TO START, on the
        # reasoning in this function's docstring: unknown must not resolve to
        # permissive. This direction only WARNS, because refusing would turn a
        # stale environment variable into an outage on upgrade for a live service
        # other teams deploy. Whether it should refuse is a decision recorded on
        # the ticket rather than taken here.
        if settings.docker_htpasswd:
            log.warning(
                "XHC_DOCKER_HTPASSWD is set (%s) but XHC_DOCKER_AUTH is 'none', "
                "so the file is IGNORED and /v2/* is UNAUTHENTICATED. Set "
                "XHC_DOCKER_AUTH=basic to enforce it.",
                settings.docker_htpasswd,
            )
        return
    path = settings.docker_htpasswd
    if not path:
        raise HtpasswdError(
            "XHC_DOCKER_AUTH=basic requires XHC_DOCKER_HTPASSWD. Refusing to "
            "start rather than serve an open cache that was asked to be closed."
        )
    try:
        text = Path(path).read_text()
    except OSError as exc:
        raise HtpasswdError(
            f"XHC_DOCKER_HTPASSWD {path!r} is unreadable ({exc}). Refusing to "
            "start: an unreadable credential file is UNKNOWN, not empty."
        ) from exc
    _users = _parse(text)
    log.info("client auth enabled for %d user(s) on /v2/*", len(_users))


def _check(header: str | None) -> bool:
    if not header or not header.lower().startswith("basic "):
        return False
    try:
        raw = base64.b64decode(header.split(None, 1)[1], validate=True).decode("utf-8")
    except (binascii.Error, IndexError, UnicodeDecodeError):
        return False
    user, sep, password = raw.partition(":")
    if not sep:
        return False
    assert _users is not None
    stored = _users.get(user)
    if stored is None:
        # Spend the same time as a real check so timing cannot enumerate users.
        bcrypt.checkpw(password.encode(), _DUMMY)
        return False
    # bcrypt.checkpw IS the constant-time comparison. The spec called for
    # hmac.compare_digest, which belonged to the shared-secret design the maintainer
    # ruled against -- here it would compare a hash to a password.
    return bcrypt.checkpw(password.encode(), stored)


async def require_pull_auth(
    request: Request, authorization: str | None = Header(default=None)
) -> None:
    """FastAPI dependency for the /v2/* pull surface.

    Emits WWW-Authenticate, which is what makes `docker login` work and is also
    what distinguishes a MUNINN 401 from an upstream auth failure: an upstream
    failure is a 502 carrying x-xhc-upstream-auth and never a challenge
    (an internal issue). Two actors, two fixes, two shapes on the wire.
    """
    # AUTHENTICATE first when per-key authz is on. This resolves the credential
    # onto request.state for the per-reference authorisation that follows, and it
    # replaces the htpasswd gate rather than layering on top of it -- two
    # credential stores answering the same question is how one of them silently
    # stops being consulted.
    if store() is not None:
        token = presented_jwt(authorization, basic_password=True)
        if token is not None:
            refused = await authenticate_jwt(request, token)
            if refused is None:
                return
            # The general reason, in the body and a header. The docker CLI
            # prints neither -- only the status -- so this is for curl, a
            # client's debug log and whoever reads Muninn's. The token is never
            # echoed; the specific reason is in Muninn's log.
            raise HTTPException(
                status_code=401,
                detail=f"workload token refused: {refused}",
                headers={"www-authenticate": 'Basic realm="muninn"',
                         "x-xhc-auth-error": refused},
            )
        if authenticate(request, authorization):
            return
        raise HTTPException(
            status_code=401,
            detail="authenticate to this cache",
            headers={"www-authenticate": 'Basic realm="muninn"'},
        )
    if settings.docker_auth == "none":
        return
    if _check(authorization):
        return
    raise HTTPException(
        status_code=401,
        detail="authenticate to this cache",
        headers={"www-authenticate": 'Basic realm="muninn"'},
    )


def unauthorized_response() -> Response:
    """The same 401 as a plain Response, for routes outside the dependency."""
    return Response(
        status_code=401,
        headers={"www-authenticate": 'Basic realm="muninn"',
                 "docker-distribution-api-version": "registry/2.0"},
    )

# --------------------------------------------------------------------------
# Per-key authorisation (XHC_AUTHZ_DB). Two STEPS, deliberately:
#
#   AUTHENTICATE, at the router -- who is this? Resolves the Basic credential to
#   a key. Cannot authorise, because a router-level dependency does not know
#   which operation or which repository the request is for.
#
#   AUTHORISE, at the reference -- may they do THIS to THAT? Called from
#   _resolve_or_error, which every /v2 route that names a repository already has
#   to call. Coupling it there means forgetting to authorise requires forgetting
#   to resolve the reference, which fails loudly instead of silently granting.
# --------------------------------------------------------------------------

_store: authzstore.AuthzStore | None = None


def store() -> authzstore.AuthzStore | None:
    """The authz store, opened once. None when the feature is off."""
    global _store  # noqa: PLW0603 - module-level singleton
    if _store is None and settings.authz_db:
        _store = authzstore.AuthzStore(settings.authz_db)
        log.info("per-key authorisation enabled from %s", settings.authz_db)
    return _store


def parse_basic(header: str | None) -> tuple[str, str] | None:
    """Return (user, password) from a Basic header, or None."""
    if not header or not header.lower().startswith("basic "):
        return None
    try:
        raw = base64.b64decode(header.split(None, 1)[1], validate=True).decode("utf-8")
    except (binascii.Error, IndexError, UnicodeDecodeError):
        return None
    user, sep, password = raw.partition(":")
    if not sep:
        return None
    return user, password


def authenticate(request: Request, authorization: str | None) -> bool:
    """Resolve the presented credential onto request.state.authz_key.

    Returns False when authz is ON and the credential does not resolve. Returns
    True when authz is OFF, because in that mode this function has no opinion --
    the htpasswd gate is the only control and it runs separately.
    """
    st = store()
    if st is None:
        return True
    request.state.authz_key = None
    creds = parse_basic(authorization)
    if creds is None:
        return False
    key = st.resolve(*creds)
    if key is None:
        return False
    # A disabled key -- or a key whose PRINCIPAL is disabled, which the store
    # folds into the same flag -- must fail AUTHENTICATION, not merely
    # authorisation.
    #
    # decide() already refuses it for every operation naming a repository, so
    # pulls and pushes were never at risk. What was at risk is /v2/ itself,
    # which names no repository and therefore never reaches decide(): it is the
    # endpoint `docker login` calls, so a revoked user still got a cheerful
    # "Login Succeeded" and only discovered the revocation on their first pull.
    #
    # Two authorities disagreeing about whether a credential is live is the bug,
    # independent of which surface leaks it. Found by a test asserting that
    # disabling a user stops their key ON THE WIRE rather than asserting that
    # the management API returned 200.
    if key.disabled:
        return False
    request.state.authz_key = key
    return True


def presented_jwt(header: str | None, *, basic_password: bool) -> str | None:
    """The workload JWT in an Authorization header, or None if there is none.

    None whenever XHC_JWT_ISSUERS is unset, so a deployment without it parses
    every credential exactly as before.

    `Bearer <jwt>` on both surfaces. On /v2 also as the Basic PASSWORD, because
    that is the only thing docker and containerd send: the USERNAME IS IGNORED,
    whatever it is. The token carries the identity, and a username that could
    disagree with it would be a second, weaker claim about who is asking. `jwt`
    is the documented convention, so a log line or a docker config reads
    sensibly, but nothing checks it.

    THE SHAPE DECIDES, ONCE. A value shaped like a JWT goes to the JWT verifier
    and only there: a failed JWT is never retried as a key, and a key is never
    retried as a JWT, so the reason a credential was refused is the reason of
    the one verifier that owns it. See jwtauth.looks_like_jwt for why the two
    shapes cannot overlap.
    """
    if not header or not jwtauth.enabled():
        return None
    scheme, _, value = header.partition(" ")
    scheme = scheme.strip().lower()
    value = value.strip()
    if scheme == "bearer" and jwtauth.looks_like_jwt(value):
        return value
    if scheme == "basic" and basic_password:
        creds = parse_basic(header)
        if creds is not None and jwtauth.looks_like_jwt(creds[1]):
            return creds[1]
    return None


async def authenticate_jwt(request: Request, token: str) -> str | None:
    """Verify a workload JWT and resolve its principal onto request.state.

    Returns None on success, or the GENERAL reason it was refused -- safe to
    send, never containing the token. The specific reason is logged.

    UNKNOWN PRINCIPAL IS 401, NOT 403, on both surfaces. The token is genuine,
    but a genuine token for nobody this cache knows is the same situation as an
    unknown key id: the credential does not resolve to an identity, and every
    other credential that does not resolve is a 401. A 403 would make `docker
    login` report success for a principal that does not exist -- the exact
    two-authorities disagreement authenticate() refuses for a disabled key.
    """
    request.state.authz_key = None
    st = store()
    if st is None:
        log.error("workload JWT presented but no authorisation store; refusing")
        return "authentication unavailable"
    try:
        ident = await jwtauth.verify(token)
    except jwtauth.Rejected as exc:
        log.info("workload token refused: %s", exc.detail)
        return exc.public
    key = st.principal_credential(ident.subject)
    if key is None and ident.auto_create:
        # EMPTY RULES, NEVER ADMIN. create_principal takes an explicit flag and
        # is not the first-login path, so an auto-created workload can never
        # take the first-admin grant. With no rules it authenticates and is
        # refused every pull until an administrator grants it something --
        # which is the point: it appears in the principal list, named, waiting.
        try:
            st.create_principal(ident.subject, is_admin=False)
            log.warning("auto-created principal %s from issuer %s with no rules",
                        ident.subject, ident.issuer)
        except KeyError:
            pass  # a concurrent request created it first
        key = st.principal_credential(ident.subject)
    if key is None:
        log.info("workload token for %s from %s: no such principal",
                 ident.subject, ident.issuer)
        return "unknown principal"
    if key.disabled:
        log.info("workload token for %s: principal is disabled", ident.subject)
        return "principal disabled"
    request.state.authz_key = key
    return None


def parse_hf_credential(header: str | None) -> tuple[str, str] | None:
    """Parse a credential off the Hugging Face surface. Basic OR Bearer.

    BOTH, because the two clients that arrive here send different things and
    neither is wrong. `huggingface_hub` sends `Authorization: Bearer <HF_TOKEN>`
    and has no concept of a username, so a user sets HF_TOKEN to
    `<key_id>:<secret>` and it arrives as one opaque string. curl, requests and
    a browser send Basic, exactly as the /v2 surface already expects.

    Returning None for anything unparseable, rather than raising: the caller
    turns that into a 401 with a challenge, and a malformed header is not a
    different outcome from an absent one.
    """
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    scheme = scheme.strip().lower()
    value = value.strip()
    if scheme == "basic":
        return parse_basic(header)
    if scheme == "bearer" and ":" in value:
        key_id, _, secret = value.partition(":")
        if key_id and secret:
            return key_id, secret
    return None


def authenticate_hf(request: Request, authorization: str | None) -> bool:
    """Resolve an HF-surface credential onto request.state.authz_key.

    Returns True when XHC_HF_AUTH is off -- this function has no opinion then,
    and the surface is open by configuration rather than by accident.
    """
    if settings.hf_auth != "key":
        return True
    st = store()
    if st is None:
        # XHC_HF_AUTH=key without a store is refused at startup, so reaching
        # here means the store failed to open at runtime. FAIL CLOSED: an
        # unreadable credential store means "unknown", never "allow".
        log.error("XHC_HF_AUTH=key but no authorisation store; refusing")
        return False
    request.state.authz_key = None
    creds = parse_hf_credential(authorization)
    if creds is None:
        return False
    key = st.resolve(*creds)
    if key is None or key.disabled:
        return False
    request.state.authz_key = key
    return True


async def authenticate_hf_request(request: Request) -> Response | None:
    """The HF-surface gate. None means authenticated (or the gate is off).

    A workload JWT arrives here as `Authorization: Bearer <jwt>` -- which is
    what huggingface_hub sends for HF_TOKEN, so HF_TOKEN_PATH pointing at a
    projected token file works unchanged. Only Bearer: huggingface_hub never
    sends Basic, and a browser or curl user has a key.
    """
    if settings.hf_auth != "key":
        return None
    authorization = request.headers.get("authorization")
    token = presented_jwt(authorization, basic_password=False)
    if token is not None:
        refused = await authenticate_jwt(request, token)
        if refused is None:
            return None
        return hf_unauthorized(f"workload token refused: {refused}")
    if authenticate_hf(request, authorization):
        return None
    return hf_unauthorized()


def hf_unauthorized(reason: str | None = None) -> Response:
    """401 for the HF surface.

    Carries a Basic challenge so a browser and curl prompt, and names the Bearer
    form in the body because `huggingface_hub` shows the body on failure and its
    users have an HF_TOKEN rather than a username and password.
    """
    headers = {"www-authenticate": 'Basic realm="muninn"'}
    if reason:
        # huggingface_hub prints X-Error-Message; the reason is general and
        # never contains the token.
        headers["x-error-message"] = reason
        headers["x-xhc-auth-error"] = reason.removeprefix("workload token refused: ")
    return JSONResponse(
        {"error": reason or "this cache requires a credential",
         "hint": "set HF_TOKEN to '<key_id>:<key_secret>' or to a workload token "
                 "from a trusted issuer, or use HTTP Basic"},
        status_code=401,
        headers=headers,
    )


def authorize(request: Request, operation: str, reference: str) -> Response | None:
    """Authorise one operation on one reference. None means allowed.

    FAILS CLOSED IN EVERY DIRECTION. If authz is on and no key reached
    request.state -- a route that skipped authentication, a middleware ordering
    change -- this refuses rather than assuming the gate ran. An authoriser that
    treats "I was not told who you are" as "proceed" is not an authoriser.
    """
    if store() is None:
        return None
    key = getattr(request.state, "authz_key", None)
    allowed, reason = authz.decide(key, operation, reference)  # type: ignore[arg-type]
    if allowed:
        log.debug("authz allow: %s", reason)
        return None
    # The REASON goes to the log; the client gets a status. Telling an
    # unauthorised caller which rule refused them describes the policy to
    # someone who has just failed to satisfy it.
    log.info("authz deny: %s", reason)
    if key is None:
        return unauthorized_response()
    return Response(
        status_code=403,
        headers={"docker-distribution-api-version": "registry/2.0"},
    )
