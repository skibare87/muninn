"""Workload identity: accept signed JWTs from configured OIDC issuers.

A pod, a CI job or a client-credentials service presents a short-lived token its
platform minted -- a Kubernetes projected service-account token, a Keycloak
client_credentials token, a GitHub Actions OIDC token -- instead of a static key.
The token authenticates as a PRINCIPAL in XHC_AUTHZ_DB, and that principal's
rules apply exactly as they do for a key. Off unless XHC_JWT_ISSUERS is set.

WHAT IS CHECKED, in order, and every check is a refusal:

  shape       three base64url segments, bounded length
  header      alg in THIS ISSUER's allowlist (never `none`, never HS*), a kid,
              no `crit` extensions. `jku`, `x5u` and `jwk` headers are IGNORED:
              a token never gets to say where its own key lives.
  issuer      the unverified `iss` selects a CONFIGURED issuer by exact match.
              Nothing else is tried.
  key         the kid is looked up in that issuer's JWKS only, and the key's type
              must match the alg (an RSA key for RS/PS, the right curve for ES).
  signature   PyJWT, with exactly the one alg already checked.
  claims      iss exact, aud contains a configured audience, exp REQUIRED,
              nbf/iat honoured with the issuer's leeway.
  subject     the identity claim is a non-empty string; it maps through the
              issuer's template to a principal subject.

THE TRUST ANCHOR IS CONFIGURATION, as in oidc.py and for the same reason: the
discovery document is fetched over the network, so its `issuer` is checked
against the configured one rather than used as the expected value, and a
document without `jwks_uri` is refused by name. A jwks_uri learned from the
network must be https -- a document cannot point this at a local file.

This does NOT reuse oidc.OIDCClient's key handling, deliberately, and the rules
above are that module's rules carried across. Its discovery requires
authorization and token endpoints that a workload issuer does not have (a
Kubernetes cluster publishes neither), and its PyJWKClient refetches on every
unknown kid with no rate limit and no way to keep a warm key set when the issuer
is down. Both are right for a login that happens once a day; neither is right on
the hot path of every blob request.

WHY THE VERIFIED RESULT IS CACHED, AND WHAT IS NOT. A pod pulling a 400-file
snapshot presents one token 400 times. The verified IDENTITY is remembered per
token (keyed by its SHA-256, never the token) until min(exp, XHC_JWT_CACHE_TTL).
What the principal may do is NOT cached here: it is read on every request from
the store's snapshot, which refreshes on PRAGMA data_version -- so disabling or
deleting the principal is refused on the very next request, exactly as for a key.

FAILS CLOSED. An issuer that cannot be reached with no key set in hand refuses
every token from it. With a key set in hand it keeps verifying with that set --
an issuer blip must not become a cache outage -- and says so in the log.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import jwt
from jwt.algorithms import get_default_algorithms

from .config import settings
from .jwtconfig import IssuerConfig, JWTConfigError

log = logging.getLogger("xhc.jwtauth")

# A token longer than this is refused before any parsing. Kubernetes tokens are
# ~1 KB; this is generous and still bounds what a garbage header costs.
MAX_TOKEN_LEN = 16384
MAX_SUBJECT_LEN = 256  # the store's own limit (authzadmin.MAX_SUBJECT_LEN)
MAX_JWKS_BYTES = 1 << 20
# How long a fetched key set is trusted before it is refreshed in line. A
# refresh that fails keeps the old set.
JWKS_REFRESH_S = 600.0
# The floor between two fetch ATTEMPTS for one issuer, whatever prompted them: an
# unknown kid, an expired set, or a previous failure. This is what stops a flood
# of tokens with invented kids from turning this cache into a load generator
# pointed at the issuer, and what stops a down issuer costing every request a
# timeout.
MIN_REFETCH_S = 30.0
MAX_CACHE_ENTRIES = 10000

_JWT_SHAPE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*$")

# Test seam: an httpx transport used for every fetch when set.
_transport: httpx.AsyncBaseTransport | None = None


class Rejected(Exception):
    """A token that did not verify.

    `public` is the general reason, safe to send: it names which check failed
    (expired, wrong audience, unknown issuer) so a workload's operator can fix
    it, and never echoes the token or the key material. `detail` is for the log.
    """

    def __init__(self, public: str, detail: str = "") -> None:
        super().__init__(public)
        self.public = public
        self.detail = detail or public


@dataclass(frozen=True)
class Identity:
    issuer: str
    subject: str        # the MAPPED principal subject
    exp: float
    auto_create: bool


def enabled() -> bool:
    return bool(settings.jwt_issuers)


def looks_like_jwt(value: str) -> bool:
    """A compact JWS: three base64url segments.

    UNAMBIGUOUS AGAINST A KEY. A key credential is `<key_id>:<secret>`, or a
    secret alone as a Basic password; key ids are hex and secrets are
    token_urlsafe, so neither ever contains a `.`, and a JWT never contains a
    `:`. The shape alone decides which verifier runs -- nothing is tried twice.

    The signature segment may be EMPTY. An unsigned (`alg: none`) token has
    one, and classifying it as a JWT is what gets it refused with the JWT
    verifier's reason ("algorithm not allowed") rather than as a key that
    happened not to resolve.
    """
    return len(value) <= MAX_TOKEN_LEN and _JWT_SHAPE.match(value) is not None


# ---------------------------------------------------------------- key sets


def _key_matches_alg(jwk: dict, alg: str) -> bool:
    kty = jwk.get("kty")
    if alg.startswith(("RS", "PS")):
        return kty == "RSA"
    if alg.startswith("ES"):
        want = {"ES256": "P-256", "ES384": "P-384", "ES512": "P-521"}.get(alg)
        return kty == "EC" and jwk.get("crv") == want
    if alg == "EdDSA":
        return kty == "OKP" and jwk.get("crv") in ("Ed25519", "Ed448")
    return False


@dataclass
class _Key:
    jwk: dict
    key: object


def _parse_jwks(raw: bytes, source: str) -> dict[str, _Key]:
    """Parse a key set. Raises ValueError when it holds no usable key.

    Unusable entries are skipped with a log line rather than failing the set:
    issuers publish encryption keys and keys for algorithms nobody here uses.
    A set with NO usable key is an error, never an empty set that replaces a
    good one.
    """
    if len(raw) > MAX_JWKS_BYTES:
        raise ValueError(f"key set at {source} is larger than {MAX_JWKS_BYTES} bytes")
    data = json.loads(raw)
    keys = data.get("keys") if isinstance(data, dict) else None
    if not isinstance(keys, list):
        raise ValueError(f"{source} is not a JWK set: no 'keys' list")
    out: dict[str, _Key] = {}
    for entry in keys:
        if not isinstance(entry, dict):
            continue
        kid = entry.get("kid")
        if not isinstance(kid, str) or not kid:
            log.info("jwks %s: skipping a key with no kid", source)
            continue
        if entry.get("kty") not in ("RSA", "EC", "OKP"):
            # `oct` is a symmetric secret. Never a verification key here.
            log.info("jwks %s: skipping kid %s of type %r", source, kid, entry.get("kty"))
            continue
        if entry.get("use", "sig") != "sig":
            continue
        try:
            parsed = jwt.PyJWK(entry)
        except (jwt.PyJWTError, ValueError, TypeError, KeyError) as exc:
            log.info("jwks %s: skipping kid %s: %s", source, kid, exc)
            continue
        size = getattr(parsed.key, "key_size", None)
        if entry["kty"] == "RSA" and isinstance(size, int) and size < 2048:
            log.warning("jwks %s: skipping kid %s: RSA key of %d bits", source, kid, size)
            continue
        out[kid] = _Key(jwk=entry, key=parsed.key)
    if not out:
        raise ValueError(f"key set at {source} contains no usable signing key")
    return out


@dataclass
class _IssuerState:
    cfg: IssuerConfig
    keys: dict[str, _Key] = field(default_factory=dict)
    fetched_at: float = 0.0          # last SUCCESSFUL fetch
    attempted_at: float = -1e18      # last attempt, successful or not
    jwks_uri: str | None = None      # resolved: configured, or from discovery
    lock: asyncio.Lock | None = None

    def _lock(self) -> asyncio.Lock:
        if self.lock is None:
            self.lock = asyncio.Lock()
        return self.lock

    async def _get(self, url: str) -> bytes:
        cfg = self.cfg
        headers = {"accept": "application/json"}
        if cfg.fetch_token_file:
            # Re-read on every fetch: a projected token rotates underneath us.
            token = Path(cfg.fetch_token_file).read_text().strip()
            headers["authorization"] = f"Bearer {token}"
        async with httpx.AsyncClient(
            timeout=cfg.fetch_timeout_s,
            verify=cfg.ca_file or True,
            transport=_transport,
            follow_redirects=False,
        ) as client:
            r = await client.get(url, headers=headers)
        if r.status_code != 200:
            raise ValueError(f"GET {url} returned {r.status_code}")
        return r.content

    async def _resolve_jwks_uri(self) -> str:
        if self.jwks_uri:
            return self.jwks_uri
        cfg = self.cfg
        if cfg.jwks_uri:
            self.jwks_uri = cfg.jwks_uri
            return self.jwks_uri
        url = cfg.issuer.rstrip("/") + "/.well-known/openid-configuration"
        meta = json.loads(await self._get(url))
        if not isinstance(meta, dict):
            raise ValueError(f"discovery document at {url} is not a JSON object")
        # THE DOCUMENT DOES NOT GET TO NAME ITS OWN ISSUER. Checked against the
        # configured one, exactly -- see oidc.OIDCClient.metadata.
        if meta.get("issuer") != cfg.issuer:
            raise ValueError(f"discovery document at {url} declares issuer "
                             f"{meta.get('issuer')!r}, configured issuer is {cfg.issuer!r}")
        uri = meta.get("jwks_uri")
        if not isinstance(uri, str) or not uri:
            raise ValueError(f"discovery document at {url} has no jwks_uri")
        parts = urlsplit(uri)
        if parts.scheme != "https" or not parts.netloc:
            # A network document must not be able to point this at a file, or
            # at plaintext.
            raise ValueError(f"discovery document at {url} names jwks_uri {uri!r}, "
                             "which is not https")
        self.jwks_uri = uri
        return uri

    async def _fetch(self) -> dict[str, _Key]:
        uri = await self._resolve_jwks_uri()
        if uri.startswith("file:"):
            path = urlsplit(uri).path
            raw = await asyncio.to_thread(Path(path).read_bytes)
        else:
            raw = await self._get(uri)
        return _parse_jwks(raw, uri)

    async def refresh(self, reason: str) -> None:
        """Fetch the key set, at most once per MIN_REFETCH_S. Never raises.

        Concurrent callers share one fetch: the lock is held across it, and a
        caller that waited re-checks whether the fetch it wanted just happened.
        """
        async with self._lock():
            now = time.monotonic()
            if now - self.attempted_at < MIN_REFETCH_S:
                return
            self.attempted_at = now
            try:
                keys = await self._fetch()
            except (httpx.HTTPError, OSError, ValueError) as exc:
                if self.keys:
                    log.warning("jwt issuer %s: key refresh (%s) failed, keeping %d "
                                "cached key(s): %s", self.cfg.issuer, reason,
                                len(self.keys), exc)
                else:
                    log.error("jwt issuer %s: cannot fetch keys (%s) and none are "
                              "cached; its tokens are refused: %s",
                              self.cfg.issuer, reason, exc)
                return
            added = sorted(set(keys) - set(self.keys))
            self.keys = keys
            self.fetched_at = time.monotonic()
            log.info("jwt issuer %s: %d key(s) loaded (%s)%s", self.cfg.issuer,
                     len(keys), reason, f", new kid(s) {added}" if added else "")

    async def key_for(self, kid: str) -> _Key | None:
        if self.keys and time.monotonic() - self.fetched_at > JWKS_REFRESH_S:
            await self.refresh("periodic refresh")
        key = self.keys.get(kid)
        if key is None:
            # Rotation: a new kid is the normal signal that the issuer has a new
            # key. Rate-limited, so an invented kid costs one fetch per window
            # per issuer, not one per request.
            await self.refresh(f"unknown kid {kid[:64]!r}")
            key = self.keys.get(kid)
        return key


# ---------------------------------------------------------------- module state

_issuers: dict[str, _IssuerState] = {}
_verified: dict[bytes, tuple[Identity, float]] = {}


def reset() -> None:
    """Drop all key sets and verified tokens. For tests and for load()."""
    _issuers.clear()
    _verified.clear()


def _state(cfg: IssuerConfig) -> _IssuerState:
    st = _issuers.get(cfg.issuer)
    if st is None or st.cfg is not cfg:
        st = _issuers[cfg.issuer] = _IssuerState(cfg)
    return st


def load() -> None:
    """Startup checks that need I/O. Raises JWTConfigError; never degrades.

    A file-backed key set that is missing or holds no usable key is refused
    here, rather than discovered as every token being refused. So are a CA file
    or fetch-token file that cannot be read.
    """
    reset()
    for cfg in settings.jwt_issuers:
        for name, path in (("ca_file", cfg.ca_file), ("fetch_token_file", cfg.fetch_token_file)):
            if path and not Path(path).is_file():
                raise JWTConfigError(f"XHC_JWT_ISSUERS issuer {cfg.issuer!r}: {name} "
                                     f"{path!r} is not a readable file")
        if cfg.jwks_uri and cfg.jwks_uri.startswith("file:"):
            path = urlsplit(cfg.jwks_uri).path
            try:
                keys = _parse_jwks(Path(path).read_bytes(), cfg.jwks_uri)
            except (OSError, ValueError) as exc:
                raise JWTConfigError(f"XHC_JWT_ISSUERS issuer {cfg.issuer!r}: "
                                     f"{exc}") from exc
            st = _state(cfg)
            st.keys, st.fetched_at, st.jwks_uri = keys, time.monotonic(), cfg.jwks_uri
            st.attempted_at = time.monotonic()
        log.info("workload JWTs accepted from %s (audience %s, principal %s)",
                 cfg.issuer, list(cfg.audiences), cfg.subject_template)


async def warm() -> None:
    """Fetch every remote issuer's keys once at startup. Never raises.

    Not required -- keys are fetched on first use -- but a cache that already
    holds a key set keeps verifying through a later issuer outage, and the boot
    log then says whether each issuer was reachable.
    """
    for cfg in settings.jwt_issuers:
        st = _state(cfg)
        if not st.keys:
            await st.refresh("startup")


# ---------------------------------------------------------------- verification


def _cache_get(digest: bytes) -> Identity | None:
    hit = _verified.get(digest)
    if hit is None:
        return None
    ident, until = hit
    if time.time() >= until:
        _verified.pop(digest, None)
        return None
    return ident


def _cache_put(digest: bytes, ident: Identity) -> None:
    ttl = settings.jwt_cache_ttl_s
    if ttl <= 0:
        return
    now = time.time()
    # NEVER PAST exp. The cache is an optimisation of a check that has a
    # deadline built in; remembering the answer beyond it would accept an
    # expired token for up to a TTL.
    until = min(ident.exp, now + ttl)
    if until <= now:
        return
    if len(_verified) >= MAX_CACHE_ENTRIES:
        for k in [k for k, (_, u) in _verified.items() if u <= now]:
            _verified.pop(k, None)
        if len(_verified) >= MAX_CACHE_ENTRIES:
            _verified.clear()
    _verified[digest] = (ident, until)


def _issuer_for(iss: object) -> IssuerConfig | None:
    for cfg in settings.jwt_issuers:
        if iss == cfg.issuer:
            return cfg
    return None


_CLAIM_ERRORS: tuple[tuple[type[Exception], str], ...] = (
    (jwt.ExpiredSignatureError, "token expired"),
    (jwt.ImmatureSignatureError, "token not yet valid"),
    (jwt.InvalidAudienceError, "wrong audience"),
    (jwt.InvalidIssuerError, "wrong issuer"),
    (jwt.MissingRequiredClaimError, "token is missing a required claim"),
    (jwt.InvalidSignatureError, "bad signature"),
)


async def verify(token: str) -> Identity:
    """Verify one token. Returns its identity or raises Rejected."""
    digest = hashlib.sha256(token.encode()).digest()
    cached = _cache_get(digest)
    if cached is not None:
        return cached

    if not looks_like_jwt(token):
        raise Rejected("malformed token")
    try:
        header = jwt.get_unverified_header(token)
        unverified = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError as exc:
        raise Rejected("malformed token", f"undecodable: {exc}") from exc

    cfg = _issuer_for(unverified.get("iss"))
    if cfg is None:
        raise Rejected("unknown issuer", f"iss {str(unverified.get('iss'))[:200]!r} "
                                         "is not configured")
    alg = header.get("alg")
    if not isinstance(alg, str) or alg not in cfg.algorithms:
        raise Rejected("algorithm not allowed",
                       f"alg {str(alg)[:20]!r} is not allowed for {cfg.issuer}")
    if alg not in get_default_algorithms():
        raise Rejected("algorithm not allowed", f"alg {alg!r} is not available")
    if "crit" in header:
        raise Rejected("malformed token", "token carries 'crit' header extensions")
    kid = header.get("kid")
    if not isinstance(kid, str) or not kid:
        raise Rejected("unknown signing key", "token header has no kid")

    st = _state(cfg)
    key = await st.key_for(kid)
    if key is None:
        if not st.keys:
            raise Rejected("issuer keys unavailable",
                           f"no key set for {cfg.issuer} could be fetched")
        raise Rejected("unknown signing key", f"kid {kid[:64]!r} is not in the key set "
                                              f"of {cfg.issuer}")
    if not _key_matches_alg(key.jwk, alg) or key.jwk.get("alg", alg) != alg:
        raise Rejected("algorithm not allowed",
                       f"kid {kid[:64]!r} is a {key.jwk.get('kty')} key "
                       f"(alg {key.jwk.get('alg')!r}); token says {alg}")

    try:
        claims = jwt.decode(
            token,
            key.key,
            algorithms=[alg],
            audience=list(cfg.audiences),
            issuer=cfg.issuer,
            leeway=cfg.leeway_s,
            options={"require": ["exp", "iss", "aud"]},
        )
    except jwt.PyJWTError as exc:
        for kind, public in _CLAIM_ERRORS:
            if isinstance(exc, kind):
                raise Rejected(public, f"{cfg.issuer}: {exc}") from exc
        raise Rejected("invalid token", f"{cfg.issuer}: {exc}") from exc

    value = claims.get(cfg.subject_claim)
    if not isinstance(value, str) or not value or value != value.strip():
        raise Rejected("invalid subject", f"{cfg.issuer}: claim {cfg.subject_claim!r} "
                                          "is not a non-empty string")
    subject = cfg.map_subject(value)
    if len(subject) > MAX_SUBJECT_LEN:
        raise Rejected("invalid subject", f"{cfg.issuer}: mapped subject is longer "
                                          f"than {MAX_SUBJECT_LEN} characters")

    exp = claims["exp"]
    if isinstance(exp, bool) or not isinstance(exp, int | float):
        raise Rejected("malformed token", "exp is not a number")
    ident = Identity(issuer=cfg.issuer, subject=subject, exp=float(exp),
                     auto_create=cfg.auto_create)
    _cache_put(digest, ident)
    return ident
