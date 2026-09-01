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
from fastapi import Header, HTTPException, Response

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


async def require_pull_auth(authorization: str | None = Header(default=None)) -> None:
    """FastAPI dependency for the /v2/* pull surface.

    Emits WWW-Authenticate, which is what makes `docker login` work and is also
    what distinguishes a MUNINN 401 from an upstream auth failure: an upstream
    failure is a 502 carrying x-xhc-upstream-auth and never a challenge
    (an internal issue). Two actors, two fixes, two shapes on the wire.
    """
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
