"""Per-registry credential providers: XHC_REGISTRY_CREDENTIALS.

`XHC_REGISTRY_AUTH_FILE` holds STATIC logins, read once. That cannot reach a
registry whose native credential is a short-lived OAuth access token, which is
what Google Artifact Registry (`<region>-docker.pkg.dev`) and Container
Registry (`gcr.io`, `*.gcr.io`) use: a token from the GKE/GCE metadata server
lives about an hour, and a static copy of one stops working after that.

So the credential for an upstream is chosen PER HOST, by a provider:

    static               the docker config.json in XHC_REGISTRY_AUTH_FILE, as
                         before. The default for every host no entry names, so
                         a deployment that does not set this variable behaves
                         exactly as it did.
    gcp-metadata         an access token from the metadata server (workload
                         identity on GKE, the attached service account on GCE),
                         presented as Basic `oauth2accesstoken:<token>`.
    gcp-service-account  a service-account JSON key file, read once at startup
                         and held in memory. By default it MINTS an access token
                         (a JWT assertion signed with the key, exchanged at the
                         key's token_uri) and presents that as
                         `oauth2accesstoken:<token>`; `"present": "json-key"`
                         sends the key itself as `_json_key:<file>` instead.

WHY JSON IN ONE ENVIRONMENT VARIABLE: the same reasons as XHC_JWT_ISSUERS
(app/jwtconfig.py). The declaration holds no secret -- a key FILE is named,
never inlined -- so one variable is what compose, a container and a Kubernetes
Deployment all set the same way, and JSON needs no new dependency.

    [{"hosts": ["*-docker.pkg.dev", "gcr.io", "*.gcr.io"],
      "provider": "gcp-metadata"}]

HOST PATTERNS are an exact host (`gcr.io`, `localhost:5000`) or a leading
`*.` / `*-` wildcard whose `*` stands for ONE non-empty DNS label: `*.gcr.io`
matches `us.gcr.io` and not `gcr.io` or `a.b.gcr.io`; `*-docker.pkg.dev`
matches `us-docker.pkg.dev` and `europe-west1-docker.pkg.dev`.

PRECEDENCE when several entries match one host: an exact pattern beats every
wildcard, and among wildcards the LONGEST pattern wins. That is unambiguous:
two different wildcard suffixes of one host of the same length would be the
same string, and the same pattern in two entries is refused at startup. List
order never decides anything, so reordering the JSON cannot change behaviour.

Parsing (parse) is pure and runs in config.py, so a malformed declaration
refuses to start the server with a named error. Reading a key file is I/O and
runs in load(), at startup, which refuses an unreadable or unusable file.

NEVER LOGGED: a token, an assertion, or any part of a key. Errors name the
provider, the host pattern and the file path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

log = logging.getLogger("xhc.regcreds")

PROVIDERS = ("static", "gcp-metadata", "gcp-service-account")
PRESENT = ("access-token", "json-key")
CLOUD_PLATFORM = "https://www.googleapis.com/auth/cloud-platform"
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
JWT_BEARER_GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"
# Google's ceiling for an assertion's lifetime is one hour after iat.
ASSERTION_LIFETIME_S = 3600
# The username every Google registry accepts for an OAuth access token.
ACCESS_TOKEN_USER = "oauth2accesstoken"
JSON_KEY_USER = "_json_key"

_KEYS = {"hosts", "provider", "key_file", "scopes", "metadata_url", "present"}
# A host, optionally with a port. No scheme, no path, no userinfo.
_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:[0-9]{1,5})?$")
_LABEL_RE = re.compile(r"^[a-z0-9-]+$")

_HUB_ALIASES = {"docker.io", "index.docker.io", "registry-1.docker.io"}


def now() -> float:
    """Wall-clock seconds. One indirection so tests can advance a fake clock
    for every expiry decision here and in registry.py at once."""
    return time.time()


class RegistryCredentialsError(ValueError):
    """XHC_REGISTRY_CREDENTIALS does not describe a usable configuration."""


class CredentialUnavailable(Exception):
    """A provider could not produce a credential: the metadata server or token
    endpoint did not answer, refused, or answered with something unusable.

    The message names the provider and the cause. It never carries a token."""


@dataclass(frozen=True)
class CredentialEntry:
    hosts: tuple[str, ...]
    provider: str
    key_file: str | None = None
    scopes: tuple[str, ...] = (CLOUD_PLATFORM,)
    metadata_url: str | None = None
    present: str = "access-token"

    def label(self) -> str:
        return f"{self.provider} for {', '.join(self.hosts)}"


@dataclass(frozen=True)
class Credential:
    """`userpass` is the Basic `user:password` text; `expires_at` is when it
    stops being valid (None: it does not expire, as with a static login)."""

    userpass: str
    expires_at: float | None = None

    def __repr__(self) -> str:  # never print the secret
        return f"Credential({self.userpass.split(':', 1)[0]}:..., expires_at={self.expires_at})"


# ---------------------------------------------------------------- parsing


def _canonical_host(host: str) -> str:
    return "docker.io" if host in _HUB_ALIASES else host


def _check_pattern(index: int, pattern: object) -> str:
    where = f"XHC_REGISTRY_CREDENTIALS entry #{index}"
    if not isinstance(pattern, str) or not pattern.strip():
        raise RegistryCredentialsError(f"{where}: every host must be a non-empty string")
    p = pattern.strip().lower()
    if "://" in p or "/" in p:
        raise RegistryCredentialsError(
            f"{where}: host {pattern!r} must be a bare host (optionally :port), "
            "not a URL or a path")
    if p.startswith(("*.", "*-")):
        rest = p[2:]
        if "*" in rest or not rest or not _HOST_RE.match(rest):
            raise RegistryCredentialsError(
                f"{where}: host {pattern!r} is not a valid wildcard; only a leading "
                "'*.' or '*-' followed by a host is accepted")
        return p
    if "*" in p:
        raise RegistryCredentialsError(
            f"{where}: host {pattern!r} has a '*' that is not a leading '*.' or '*-'")
    if not _HOST_RE.match(p):
        raise RegistryCredentialsError(f"{where}: host {pattern!r} is not a valid host")
    return _canonical_host(p)


def parse(raw: str | None) -> tuple[CredentialEntry, ...]:
    """Validate the declaration on its ARGUMENTS alone. Raises a named error."""
    if raw is None or not raw.strip():
        return ()
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise RegistryCredentialsError(
            f"XHC_REGISTRY_CREDENTIALS is not valid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise RegistryCredentialsError(
            "XHC_REGISTRY_CREDENTIALS must be a JSON list of objects")
    entries: list[CredentialEntry] = []
    seen: dict[str, int] = {}
    for i, e in enumerate(data):
        where = f"XHC_REGISTRY_CREDENTIALS entry #{i}"
        if not isinstance(e, dict):
            raise RegistryCredentialsError(f"{where}: must be an object")
        unknown = sorted(set(e) - _KEYS)
        if unknown:
            raise RegistryCredentialsError(
                f"{where}: unknown key(s) {unknown}; accepted: {sorted(_KEYS)}")
        provider = e.get("provider")
        if provider not in PROVIDERS:
            raise RegistryCredentialsError(
                f"{where}: 'provider' must be one of {list(PROVIDERS)}, got {provider!r}")
        hosts = e.get("hosts")
        if not isinstance(hosts, list) or not hosts:
            raise RegistryCredentialsError(f"{where}: 'hosts' must be a non-empty list")
        patterns = tuple(_check_pattern(i, h) for h in hosts)
        for p in patterns:
            if p in seen:
                raise RegistryCredentialsError(
                    f"{where}: host {p!r} is already named by entry #{seen[p]}; "
                    "a host may be named once")
            seen[p] = i

        key_file = e.get("key_file")
        if provider == "gcp-service-account":
            if not isinstance(key_file, str) or not key_file.strip():
                raise RegistryCredentialsError(
                    f"{where}: provider gcp-service-account needs 'key_file', the path "
                    "to a service-account JSON key")
        elif key_file is not None:
            raise RegistryCredentialsError(
                f"{where}: 'key_file' applies only to gcp-service-account")

        present = e.get("present", "access-token")
        if present not in PRESENT:
            raise RegistryCredentialsError(
                f"{where}: 'present' must be one of {list(PRESENT)}, got {present!r}")
        if "present" in e and provider != "gcp-service-account":
            raise RegistryCredentialsError(
                f"{where}: 'present' applies only to gcp-service-account")

        scopes = e.get("scopes", [CLOUD_PLATFORM])
        if (not isinstance(scopes, list) or not scopes
                or not all(isinstance(s, str) and s.strip() for s in scopes)):
            raise RegistryCredentialsError(f"{where}: 'scopes' must be a non-empty list of strings")
        if "scopes" in e and provider != "gcp-service-account":
            raise RegistryCredentialsError(
                f"{where}: 'scopes' applies only to gcp-service-account (the metadata "
                "server's scopes are the node's or the workload's, not ours to ask for)")

        metadata_url = e.get("metadata_url")
        if metadata_url is not None:
            if provider != "gcp-metadata":
                raise RegistryCredentialsError(
                    f"{where}: 'metadata_url' applies only to gcp-metadata")
            if not isinstance(metadata_url, str) or not metadata_url.startswith(
                    ("http://", "https://")):
                raise RegistryCredentialsError(f"{where}: 'metadata_url' must be an http(s) URL")

        entries.append(CredentialEntry(
            hosts=patterns, provider=provider,
            key_file=key_file.strip() if isinstance(key_file, str) else None,
            scopes=tuple(s.strip() for s in scopes), metadata_url=metadata_url,
            present=present))
    return tuple(entries)


def match(entries: tuple[CredentialEntry, ...], host: str) -> CredentialEntry | None:
    """The entry that governs `host`, by the precedence in the module docstring."""
    host = _canonical_host(host.lower())
    best: tuple[int, CredentialEntry] | None = None
    for e in entries:
        for p in e.hosts:
            if p == host:
                return e
            if p.startswith("*") and host.endswith(p[1:]):
                label = host[: len(host) - (len(p) - 1)]
                if (label and _LABEL_RE.match(label)
                        and (best is None or len(p) > best[0])):
                    best = (len(p), e)
    return best[1] if best else None


# ---------------------------------------------------------------- providers


ClientFactory = Callable[[], httpx.AsyncClient]


class GcpMetadataProvider:
    """The metadata server's token, through the tier's own client for it.

    One token client, not two: s3client.GcpMetadataToken is what the GCS tier
    has used against real GKE. This only adapts its result to a registry
    login and its failures to CredentialUnavailable."""

    kind = "gcp-metadata"

    def __init__(self, entry: CredentialEntry, client_factory: ClientFactory):
        from . import s3client

        self._s3client = s3client
        self._token = s3client.GcpMetadataToken(
            url=entry.metadata_url or s3client.GCP_TOKEN_URL,
            client_factory=client_factory, clock=lambda: now())

    async def credential(self) -> Credential:
        try:
            token = await self._token.token()
        except self._s3client.TierAuthError as exc:
            raise CredentialUnavailable(f"metadata server: {exc}") from None
        except httpx.HTTPError as exc:
            raise CredentialUnavailable(
                f"metadata server unreachable: {type(exc).__name__}") from None
        return Credential(f"{ACCESS_TOKEN_USER}:{token}", self._token.expires_at)


class GcpServiceAccountProvider:
    """A service-account key, held in memory, minting access tokens.

    The OAuth 2.0 JWT-bearer flow Google documents for server-to-server use:
    an RS256 assertion carrying iss (the account's email), scope, aud (the
    token endpoint), iat and exp (at most an hour later), POSTed with
    grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer. The access token in
    the answer is what reaches the registry; the key never leaves this process.
    """

    kind = "gcp-service-account"

    def __init__(self, entry: CredentialEntry, client_factory: ClientFactory):
        path = entry.key_file or ""
        where = f"XHC_REGISTRY_CREDENTIALS ({entry.label()})"
        try:
            raw = Path(path).read_text()
        except OSError as exc:
            raise RegistryCredentialsError(
                f"{where}: key_file {path!r} is not readable: {exc.strerror or exc}") from None
        try:
            key = json.loads(raw)
        except ValueError:
            raise RegistryCredentialsError(
                f"{where}: key_file {path!r} is not JSON") from None
        if not isinstance(key, dict) or key.get("type") != "service_account":
            raise RegistryCredentialsError(
                f"{where}: key_file {path!r} is not a service-account key "
                "(its \"type\" is not \"service_account\")")
        for field in ("client_email", "private_key"):
            if not isinstance(key.get(field), str) or not key[field]:
                raise RegistryCredentialsError(
                    f"{where}: key_file {path!r} has no {field!r}")
        token_uri = key.get("token_uri") or GOOGLE_TOKEN_URI
        if not isinstance(token_uri, str) or not token_uri.startswith(("https://", "http://")):
            raise RegistryCredentialsError(f"{where}: key_file {path!r} has an unusable token_uri")
        try:
            from cryptography.hazmat.primitives.serialization import load_pem_private_key

            self._key = load_pem_private_key(key["private_key"].encode(), password=None)
        except (ValueError, TypeError):
            # Deliberately no detail from the exception: it can quote key material.
            raise RegistryCredentialsError(
                f"{where}: key_file {path!r} carries a private_key that does not load") from None
        self.email: str = key["client_email"]
        self.key_id: str | None = key.get("private_key_id") or None
        self.token_uri: str = token_uri
        self.scopes = entry.scopes
        self.present = entry.present
        self._raw = raw if entry.present == "json-key" else None
        self._client_factory = client_factory
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    def __repr__(self) -> str:
        return f"GcpServiceAccountProvider({self.email})"

    def assertion(self, issued_at: float | None = None) -> str:
        import jwt

        iat = int(now() if issued_at is None else issued_at)
        claims = {
            "iss": self.email,
            "scope": " ".join(self.scopes),
            "aud": self.token_uri,
            "iat": iat,
            "exp": iat + ASSERTION_LIFETIME_S,
        }
        headers = {"kid": self.key_id} if self.key_id else None
        return jwt.encode(claims, self._key, algorithm="RS256", headers=headers)

    async def credential(self) -> Credential:
        if self._raw is not None:
            return Credential(f"{JSON_KEY_USER}:{self._raw}", None)
        from .s3client import _TOKEN_SLACK_S

        async with self._lock:
            if self._token and now() < self._expires_at - _TOKEN_SLACK_S:
                return Credential(f"{ACCESS_TOKEN_USER}:{self._token}", self._expires_at)
            try:
                r = await self._client_factory().post(
                    self.token_uri,
                    data={"grant_type": JWT_BEARER_GRANT, "assertion": self.assertion()})
            except httpx.HTTPError as exc:
                raise CredentialUnavailable(
                    f"token endpoint unreachable: {type(exc).__name__}") from None
            if r.status_code != 200:
                raise CredentialUnavailable(
                    f"token endpoint returned {r.status_code} for {self.email}")
            try:
                body = r.json()
                token = body["access_token"]
                ttl = float(body.get("expires_in") or 0)
            except (ValueError, KeyError, TypeError):
                raise CredentialUnavailable(
                    "token endpoint answered 200 without a usable access_token") from None
            if not isinstance(token, str) or not token:
                raise CredentialUnavailable("token endpoint answered 200 with an empty token")
            self._token, self._expires_at = token, now() + ttl
            return Credential(f"{ACCESS_TOKEN_USER}:{token}", self._expires_at)


# ---------------------------------------------------------------- state


@dataclass
class _State:
    entries: tuple[CredentialEntry, ...] | None = None
    providers: dict[int, object] | None = None


_s = _State()


def _entries() -> tuple[CredentialEntry, ...]:
    from .config import settings

    return tuple(getattr(settings, "registry_credentials", ()) or ())


def _build(entries, client_factory: ClientFactory) -> dict[int, object]:
    out: dict[int, object] = {}
    for i, e in enumerate(entries):
        if e.provider == "gcp-metadata":
            out[i] = GcpMetadataProvider(e, client_factory)
        elif e.provider == "gcp-service-account":
            out[i] = GcpServiceAccountProvider(e, client_factory)
    return out


_client_factory: ClientFactory | None = None


def set_client_factory(factory: ClientFactory) -> None:
    """registry.py installs its loop-bound client, so a token fetch shares the
    registry client's lifetime and is rebuilt on a new event loop with it."""
    global _client_factory  # noqa: PLW0603 - module-level wiring
    _client_factory = factory


def load() -> None:
    """Startup: build every provider, reading key files. Raises
    RegistryCredentialsError; never degrades to a narrower configuration."""
    reset()
    entries = _entries()
    if _client_factory is None:
        raise RuntimeError("regcreds.load() before registry installed its client")
    providers = _build(entries, _client_factory)
    _s.entries, _s.providers = entries, providers
    for e in entries:
        log.info("registry credentials: %s", e.label())


def reset() -> None:
    """Forget every provider and its cached token. Tests call this; so does load()."""
    _s.entries = None
    _s.providers = None


def entry_for(host: str) -> CredentialEntry | None:
    """The configured entry for `host`, without building anything."""
    return match(_entries(), host)


def provider_for(host: str):
    """The live provider for `host`, or None when the static path applies.

    Built lazily when load() has not run (an app driven without its lifespan,
    as the tests drive it). A key file that fails to load there is a provider
    failure for this request, not a crash."""
    if _s.providers is None:
        entries = _entries()
        try:
            providers = _build(entries, _client_factory or _default_client)
        except RegistryCredentialsError as exc:
            raise CredentialUnavailable(str(exc)) from None
        _s.entries, _s.providers = entries, providers
    entries = _s.entries or ()
    e = match(entries, host)
    if e is None or e.provider == "static":
        return None
    return _s.providers.get(entries.index(e))


def _default_client() -> httpx.AsyncClient:  # pragma: no cover - registry installs one
    raise RuntimeError("no HTTP client installed for credential providers")
