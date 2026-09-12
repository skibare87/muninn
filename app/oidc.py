"""Sign-in via OpenID Connect, performed by Muninn itself.

WHY THE APPLICATION AND NOT AN EDGE PROXY. An identity proxy (Cloudflare Access,
oauth2-proxy) enforces in front of the origin, which means the origin's traffic has
to traverse it. This cache deliberately does NOT put its data path behind such a
proxy -- a large blob PUT hits body limits there -- so an edge identity product
would have forced a second, proxied hostname for the UI alone. Doing the flow here
removes that: one hostname, a login button on the homepage, and `/v2/*` untouched.

It is also the only shape that is honest in an MIT-licensed project. Depending on
one vendor's edge product would make sign-in unavailable to anyone deploying this
elsewhere. Anything speaking OIDC works: a hosted IdP, Keycloak, Authentik,
Google, GitHub via a shim, or Cloudflare Access, which speaks OIDC too.

WHAT THIS DOES NOT DO: protect /v2/*. Docker clients cannot complete an
interactive redirect flow. Humans sign in HERE and mint a key; machines present
that key as Basic auth. Two doors, two mechanisms, on purpose -- conflating them
breaks one of them.

THE FOUR THINGS THAT MAKE IT SAFE, none of which are optional:

  state   -- CSRF on the callback. Without it, an attacker can complete a login
             in a victim's browser using their own code.
  nonce   -- binds the id_token to THIS login. Without it a previously issued
             token can be replayed.
  PKCE    -- binds the code to the client that requested it. Protects a code
             intercepted in the redirect.
  aud     -- pins the token to THIS application. A signature proves the token is
             genuine; the audience proves it was issued FOR US. Verifying the
             first and not the second accepts a valid token minted for a
             different application on the same provider.

Signature verification uses the provider's published key set, matched by `kid`
rather than a cached single key, because providers rotate and serve old and new
keys together.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import time
from dataclasses import dataclass

import httpx
import jwt
from jwt import PyJWKClient

log = logging.getLogger("xhc.oidc")

# A login that has not completed in this long is abandoned, and its pending state
# is dropped. Short on purpose: this window is how long a stolen state value is
# useful, and no human takes ten minutes to click "approve".
_LOGIN_TTL_S = 600


@dataclass(frozen=True)
class Identity:
    """What a completed login establishes.

    `subject` is the stable key. `email` is a DISPLAY attribute refreshed on each
    login -- email is routable and changeable, and providers move it during
    migrations, so keying a user record on it renames the user when their address
    changes.
    """

    subject: str
    email: str = ""
    name: str = ""


class OIDCError(Exception):
    """Any failure of the flow. The message is for the LOG, never for the browser:
    a precise reason tells an attacker which of their guesses was closest."""


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class OIDCClient:
    def __init__(
        self,
        issuer: str,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        scopes: str = "openid email profile",
        *,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.issuer = issuer.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.scopes = scopes
        self._http = http
        self._meta: dict | None = None
        self._jwks: PyJWKClient | None = None
        # state -> (nonce, code_verifier, created_at). In-process by design: a
        # pending login is worthless after the callback and must not outlive a
        # restart, which would let a state from a previous process be replayed.
        self._pending: dict[str, tuple[str, str, float]] = {}

    # ---------------- provider discovery ----------------

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=15)
        return self._http

    async def metadata(self) -> dict:
        """Fetch and cache the discovery document.

        Discovery rather than hand-configured endpoints: a provider that rotates
        an endpoint should not require a config change, and every OIDC provider
        publishes this.
        """
        if self._meta is None:
            c = await self._client()
            r = await c.get(f"{self.issuer}/.well-known/openid-configuration")
            r.raise_for_status()
            self._meta = r.json()
        return self._meta

    async def _jwk_client(self) -> PyJWKClient:
        if self._jwks is None:
            meta = await self.metadata()
            # PyJWKClient matches by kid and refetches on an unknown one, which is
            # what makes key rotation survivable.
            self._jwks = PyJWKClient(meta["jwks_uri"])
        return self._jwks

    # ---------------- the flow ----------------

    def _sweep(self) -> None:
        cutoff = time.time() - _LOGIN_TTL_S
        for state in [s for s, (_, _, t) in self._pending.items() if t < cutoff]:
            self._pending.pop(state, None)

    async def authorization_url(self) -> str:
        """Begin a login. Returns the URL to send the browser to."""
        self._sweep()
        meta = await self.metadata()
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
        self._pending[state] = (nonce, verifier, time.time())

        from urllib.parse import urlencode

        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": self.scopes,
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return f"{meta['authorization_endpoint']}?{urlencode(params)}"

    async def complete(self, code: str, state: str) -> Identity:
        """Finish a login. Raises OIDCError on ANY failure.

        Single-use by construction: the pending state is popped before the token
        exchange, so a replayed callback finds nothing and is refused even if the
        code is still live at the provider.
        """
        self._sweep()
        pending = self._pending.pop(state, None)
        if pending is None:
            raise OIDCError("unknown or expired state")
        nonce, verifier, _ = pending

        meta = await self.metadata()
        c = await self._client()
        r = await c.post(
            meta["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "code_verifier": verifier,
            },
            headers={"Accept": "application/json"},
        )
        if r.status_code != 200:
            raise OIDCError(f"token endpoint returned {r.status_code}")
        token = r.json().get("id_token")
        if not token:
            raise OIDCError("no id_token in the token response")

        signing_key = (await self._jwk_client()).get_signing_key_from_jwt(token)
        try:
            claims = jwt.decode(
                token,
                signing_key.key,
                # An explicit allow-list. Never accept the token's own `alg`, which
                # is what "alg: none" and algorithm-confusion attacks rely on.
                algorithms=["RS256", "RS512", "ES256", "ES384"],
                audience=self.client_id,   # the aud check -- not optional
                issuer=meta.get("issuer", self.issuer),
                options={"require": ["exp", "iat", "aud", "iss", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise OIDCError(f"id_token rejected: {exc}") from exc

        if claims.get("nonce") != nonce:
            raise OIDCError("nonce mismatch -- possible replay")

        subject = claims.get("sub")
        if not subject:
            raise OIDCError("id_token has no sub")
        return Identity(
            subject=subject,
            email=claims.get("email", "") or "",
            name=claims.get("name", "") or "",
        )
