"""Signed session cookies for the browser login.

Deliberately NOT a JWT and deliberately not a server-side session table.

Not a JWT, because this cookie is read by exactly one piece of software -- this
app -- and JWT's flexibility is entirely about interoperating with software you
do not control. The cost of that flexibility is an algorithm field in the token,
which is where `alg=none` lives. A fixed HMAC with no negotiable parameters
cannot have that bug.

Not a server-side table, because the only thing this cookie establishes is WHICH
PRINCIPAL is browsing. Every authorisation decision downstream re-reads that
principal from the store, so a stale cookie cannot outlive a disabled account:
`require_login` looks the principal up on every request and refuses a disabled
one. That is what makes a stateless cookie safe here, and it is a property of
the lookup rather than of the cookie -- if the lookup is ever removed, this
choice becomes wrong.

The cookie NEVER carries an admin flag, a key, or a rule. Those all come from
the store at use time. A cookie that carried `is_admin` would let a demoted
admin stay an admin until it expired.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass

COOKIE_NAME = "muninn_session"


@dataclass(frozen=True)
class Session:
    subject: str
    email: str = ""
    expires_at: float = 0.0


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(secret: str, payload: bytes) -> str:
    return _b64url(hmac.new(secret.encode(), payload, hashlib.sha256).digest())


def issue(secret: str, subject: str, email: str = "", ttl_s: float = 43200.0) -> str:
    """Mint a cookie value. The expiry is INSIDE the signed payload, not only in
    the Set-Cookie attribute -- a browser attribute is a request to the client,
    and a client that ignores it would otherwise hold a session forever."""
    payload = json.dumps(
        {"sub": subject, "email": email, "exp": time.time() + ttl_s},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return f"{_b64url(payload)}.{_sign(secret, payload)}"


def verify(secret: str, cookie: str | None) -> Session | None:
    """Return the session, or None for anything at all wrong with the cookie.

    Every failure returns None rather than raising, and the caller treats None
    as logged out. There is no path here that returns a Session on a value that
    did not verify -- including the empty-secret case, which is checked first so
    a misconfigured deployment cannot mint or accept a cookie signed with "".
    """
    if not secret or not cookie or "." not in cookie:
        return None
    encoded, _, signature = cookie.rpartition(".")
    try:
        payload = _unb64url(encoded)
    except (ValueError, TypeError):  # binascii.Error subclasses ValueError
        return None
    # compare_digest, not ==: a byte-at-a-time comparison leaks the prefix of a
    # valid signature, and an attacker controls how many attempts they make.
    if not hmac.compare_digest(_sign(secret, payload), signature):
        return None
    try:
        claims = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        return None
    # A signed payload that is not an object is still not one of ours. Checked
    # rather than assumed: json.loads("3") returns an int, and .get on it raises
    # inside a security check, which would surface as a 500 rather than a 401.
    if not isinstance(claims, dict):
        return None
    subject = claims.get("sub")
    expires_at = claims.get("exp")
    # A cookie with no subject or no expiry is not a valid cookie with defaults
    # filled in; it is a cookie this code did not write. Refuse it.
    if not isinstance(subject, str) or not subject:
        return None
    if not isinstance(expires_at, int | float) or expires_at <= time.time():
        return None
    email = claims.get("email")
    return Session(subject=subject, email=email if isinstance(email, str) else "",
                   expires_at=float(expires_at))
