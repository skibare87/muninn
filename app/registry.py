"""Upstream OCI registry client: name resolution, the bearer-token dance, creds.

Two jobs.

**Resolution.** `docker pull muninn.host/ghcr.io/org/img:tag` reaches us as
`GET /v2/ghcr.io/org/img/manifests/tag`, because Docker treats the first
component of a reference as a registry host only when it contains a `.` or `:`
(or is `localhost`), and dots are legal inside a repository path. So the whole
`ghcr.io/org/img` arrives as an opaque repository name and we split it here.

**Auth.** Registries answer `401` with a `WWW-Authenticate: Bearer` challenge;
the client is expected to fetch a short-lived scoped token from the named realm
and retry. Muninn performs that on the fleet's behalf, so edge nodes stay
anonymous and credentials live in exactly one place -- the same win as HF_TOKEN.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .config import settings

log = logging.getLogger("xhc.registry")

# Docker Hub is the special case, and it is the most used upstream. `docker.io`
# is not the API host, `index.docker.io` is an alias for it, and single-segment
# repositories carry an implicit `library/`. Encoded once, here, so no other
# module has to know.
_HUB_ALIASES = {"docker.io", "index.docker.io", "registry-1.docker.io"}
_HUB_CANONICAL = "docker.io"
_HUB_API = "https://registry-1.docker.io"

_CHALLENGE_RE = re.compile(r'(\w+)="([^"]*)"')
# The scheme is a bare token with no ="value", so _CHALLENGE_RE cannot capture
# it. Matching it separately is the whole point: a Basic challenge carries a
# realm exactly like a Bearer one, and telling them apart is the only job the
# guard in _authed_request has (an internal issue).
_SCHEME_RE = re.compile(r"^\s*([A-Za-z]+)")

# Upstreams that have answered a Basic challenge. Once a registry has ASKED,
# subsequent requests carry credentials up front rather than being challenged
# again -- see _authed_request for why that is a correctness fix and not an
# optimisation.
_basic_upstreams: set[str] = set()

_client: httpx.AsyncClient | None = None
_tokens: dict[tuple[str, str], tuple[str, float]] = {}
_token_locks: dict[tuple[str, str], asyncio.Lock] = {}
_creds_cache: dict | None = None


@dataclass(frozen=True)
class Ref:
    """A resolved reference. `upstream` is the canonical host used for policy,
    storage paths and metrics; `api` is where requests actually go."""

    upstream: str
    api: str
    repo: str

    @property
    def key(self) -> str:
        return f"{self.upstream}/{self.repo}"


class ResolveError(ValueError):
    pass


def resolve(name: str) -> Ref:
    """Split `<maybe-host>/<repo...>` into an upstream and a repository."""
    name = name.strip("/")
    if not name:
        raise ResolveError("empty repository name")
    head, _, rest = name.partition("/")

    if "." in head or ":" in head or head == "localhost":
        host, repo = head, rest
    else:
        # No dot in the first segment, so it is part of the repo name and the
        # default upstream applies: `muninn.host/nginx` -> docker.io/library/nginx.
        host, repo = settings.docker_default_upstream, name

    if host in _HUB_ALIASES:
        upstream, api = _HUB_CANONICAL, _HUB_API
        if repo and "/" not in repo:
            repo = f"library/{repo}"
    else:
        upstream, api = host, f"https://{host}"

    if not repo:
        raise ResolveError(f"no repository in {name!r}")
    return Ref(upstream=upstream, api=api, repo=repo)


def _get_client() -> httpx.AsyncClient:
    global _client  # noqa: PLW0603 - module-level cache
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.request_timeout_s, read=None),
            follow_redirects=True,
        )
    return _client


async def close_client() -> None:
    global _client  # noqa: PLW0603 - module-level cache
    if _client is not None:
        await _client.aclose()
        _client = None


def _load_credentials() -> dict:
    """Read a standard ~/.docker/config.json. Reusing the format `docker login`
    already produces means no bespoke config format, and mounting it as a Docker
    secret keeps credentials out of `docker inspect`."""
    global _creds_cache  # noqa: PLW0603 - module-level cache
    if _creds_cache is not None:
        return _creds_cache
    _creds_cache = {}
    path = settings.registry_auth_file
    if path and Path(path).is_file():
        try:
            data = json.loads(Path(path).read_text())
            for host, entry in (data.get("auths") or {}).items():
                canonical = _HUB_CANONICAL if host in _HUB_ALIASES else host
                # Hosts appear with and without scheme/path in real config files.
                canonical = canonical.split("/")[0].replace("https://", "")
                if entry.get("auth"):
                    _creds_cache[canonical] = base64.b64decode(entry["auth"]).decode()
                elif entry.get("username"):
                    _creds_cache[canonical] = f"{entry['username']}:{entry.get('password','')}"
            log.info("loaded registry credentials for %s", sorted(_creds_cache))
        except (OSError, ValueError, KeyError) as exc:
            log.warning("could not read XHC_REGISTRY_AUTH_FILE %s: %s", path, exc)
    return _creds_cache


def reset_credentials() -> None:
    global _creds_cache  # noqa: PLW0603 - module-level cache
    _creds_cache = None


def has_credentials(upstream: str) -> bool:
    """Whether credentials are configured for this upstream.

    Public because ocicompat needs it to tell "the cache is not logged in to
    that registry" from "the credentials it has were rejected". Those have
    different fixes and different owners, and before an internal issue both rendered as
    404 MANIFEST_UNKNOWN along with "genuinely not there".
    """
    return _basic_for(upstream) is not None


def _basic_for(upstream: str) -> str | None:
    creds = _load_credentials().get(upstream)
    return base64.b64encode(creds.encode()).decode() if creds else None


def _parse_challenge(header: str) -> dict:
    """Parse a WWW-Authenticate header into its params, plus a `_scheme` key.

    `_scheme` is underscore-prefixed so it cannot collide with a real parameter
    name from a registry we have not met.
    """
    parsed = dict(_CHALLENGE_RE.findall(header or ""))
    m = _SCHEME_RE.match(header or "")
    if m:
        parsed["_scheme"] = m.group(1).lower()
    return parsed


async def _fetch_token(ref: Ref, challenge: dict, out: dict | None = None) -> str | None:
    """Run the bearer token dance. `out["token"]` records WHY it failed.

    THIS FUNCTION USED TO RETURN None FOR FIVE DISTINCT STATES -- no realm, a
    non-URL realm, a transport error, a non-200 from the token endpoint, and a
    200 carrying no token -- and the caller could not tell them apart. That is
    the same collapse that made every upstream 401 render as 502, one layer
    down, and it is why the fix for THAT did not reach every registry.

    The distinction that matters to a caller is whether the registry's auth
    service gave us an ANSWER:

        declined     401/403 -- it understood us and refused this scope. For a
                     registry that will not leak existence, an anonymous
                     refusal is indistinguishable from "no such repository".
        unreachable  transport error or 5xx -- we never got an answer. This is
                     what a gateway failure actually is.
        unusable     a realm we cannot or must not use, or a 200 with no token.
    """

    def _mark(state: str) -> None:
        if out is not None:
            out["token"] = state

    realm = challenge.get("realm")
    if not realm:
        _mark("unusable")
        return None
    # The realm is registry-controlled text, not necessarily a URL. One registry
    # sends `Basic realm="Authorization Required"`, and httpx treats that as a
    # RELATIVE url, then raises ValueError from urllib inside its cookie
    # handling -- which `except httpx.HTTPError` does not catch, so it escaped
    # as a 500. Validate before requesting rather than widening the except and
    # calling it handled. an internal issue.
    if urlparse(realm).scheme not in ("http", "https") or not urlparse(realm).netloc:
        log.warning("ignoring non-URL auth realm %r from %s", realm, ref.upstream)
        _mark("unusable")
        return None
    params = {k: v for k, v in challenge.items() if k in ("service", "scope") and v}
    params.setdefault("scope", f"repository:{ref.repo}:pull")
    headers = {}
    basic = _basic_for(ref.upstream)
    if basic:
        headers["authorization"] = f"Basic {basic}"
    try:
        r = await _get_client().get(realm, params=params, headers=headers)
    except (httpx.HTTPError, ValueError) as exc:
        # ValueError as well: httpx raises it out of urllib for a malformed URL
        # rather than as an httpx error. Belt and braces behind the check above.
        log.warning("token fetch failed for %s: %s", ref.upstream, exc)
        _mark("unreachable")
        return None
    if r.status_code != 200:
        log.warning("token endpoint %s returned %s for %s", realm, r.status_code, ref.key)
        # 401/403 is the auth service ANSWERING: it understood the request and
        # refused this scope. Anything else -- 5xx, 429, a redirect loop -- is a
        # failure to get an answer at all, and only that is a gateway problem.
        _mark("declined" if r.status_code in (401, 403) else "unreachable")
        return None
    body = r.json()
    token = body.get("token") or body.get("access_token")
    if not token:
        log.warning("token endpoint %s returned 200 with no token for %s", realm, ref.key)
        _mark("unusable")
        return None
    ttl = float(body.get("expires_in") or 300)
    # Expire a little early rather than discovering staleness mid-pull.
    _tokens[(ref.upstream, params["scope"])] = (token, time.time() + max(ttl - 30, 30))
    return token


def _cached_token(ref: Ref) -> str | None:
    entry = _tokens.get((ref.upstream, f"repository:{ref.repo}:pull"))
    if not entry:
        return None
    token, expires = entry
    return token if time.time() < expires else None


def clear_tokens() -> None:
    _tokens.clear()
    _basic_upstreams.clear()


async def _authed_request(
    method: str,
    url: str,
    ref: Ref,
    headers: dict,
    *,
    stream: bool,
    content=None,
    out: dict | None = None,
):
    """Issue a request, performing the bearer dance once on a 401.

    Returns an httpx.Response. When `stream` is True the caller owns closing it.

    `out`, when given, records `authenticated`: whether we actually presented
    credentials on the request whose response is returned. A 401 means two very
    different things depending on it. If we never authenticated, the cache could
    not talk to the registry. If we DID -- an anonymous bearer token counts,
    because the registry issued it -- then the registry understood us and said
    no, which is an ANSWER rather than a gateway failure. Registries that refuse
    to leak existence answer 401 for a repo that does not exist, so without this
    flag a typo'd image name is indistinguishable from a broken cache.
    """
    if out is not None:
        out["authenticated"] = False
    client = _get_client()
    hdrs = dict(headers)
    token = _cached_token(ref)
    if token:
        hdrs["authorization"] = f"Bearer {token}"
    elif ref.upstream in _basic_upstreams and "authorization" not in hdrs:
        # PREEMPTIVE, AND IT IS A CORRECTNESS FIX RATHER THAN A SAVED ROUND TRIP.
        #
        # Challenge-response means sending the whole body, being told 401, and
        # sending it again. For a small body that is merely wasteful. For a
        # large one it BREAKS: the server answers 401 and closes as soon as it
        # has the headers, without draining the body, so our remaining writes
        # fail and httpx raises ReadError -- fast, and nothing to do with size
        # limits or timeouts.
        #
        # Measured against a server that 401s without draining: a 64 KiB body
        # gets a clean 401 because it fits in socket buffers, a 3 MiB body
        # raises ReadError. That is exactly the reported asymmetry -- a small
        # config blob succeeding while a 3 MiB layer failed 0.6s into a freshly
        # opened session.
        #
        # Credentials still go ONLY to an upstream that has already challenged
        # us for Basic, so configuring an auth file cannot leak them to a
        # registry that never asked. The first request to a registry is still
        # challenge-response; every one after it is not.
        basic = _basic_for(ref.upstream)
        if basic:
            hdrs["authorization"] = f"Basic {basic}"
            if out is not None:
                out["authenticated"] = True

    async def send(h):
        # `content` is a CALLABLE returning a fresh body, not a body. The auth
        # dance re-sends the request after a 401, and a consumed file handle or
        # exhausted iterator would silently send an empty second request --
        # which for a push means an accepted upload containing nothing. A
        # factory makes the retry safe for a 5 GB layer streamed off disk as
        # well as for a small chunk held in memory.
        body = content() if content is not None else None
        req = client.build_request(method, url, headers=h, content=body)
        return await client.send(req, stream=stream)

    _t = time.monotonic()
    r = await send(hdrs)
    if (time.monotonic() - _t) > 1.0:
        log.warning("%s %s took %.1fs before any auth retry", method, url,
                    time.monotonic() - _t)
    if r.status_code != 401:
        return r

    challenge = _parse_challenge(r.headers.get("www-authenticate", ""))
    if stream:
        await r.aclose()
    else:
        await r.aread()

    if challenge.get("_scheme") == "basic":
        # A Basic challenge IS satisfiable when the operator has mounted
        # credentials for this upstream -- that is the entire point of
        # XHC_REGISTRY_AUTH_FILE, and it did not work.
        #
        # _basic_for() existed and was called from exactly one place: inside
        # _fetch_token, to authenticate to a BEARER TOKEN ENDPOINT. No code path
        # ever put Authorization: Basic on a registry request. So against a
        # registry that speaks plain Basic and has no token endpoint, the
        # credentials loaded, logged at startup, sat in memory and were never
        # sent. A stated feature that had never worked on any release.
        #
        # The comment that used to be here said "not a bearer challenge we can
        # satisfy (e.g. Basic)", which encoded the false belief that Basic could
        # not be satisfied at all. It can. an internal issue.
        #
        # Challenge-response rather than preemptive: credentials go only to an
        # upstream that actually asked for them, so configuring an auth file
        # cannot leak them to a registry that never challenges.
        _basic_upstreams.add(ref.upstream)
        basic = _basic_for(ref.upstream)
        if not basic:
            # Challenged for Basic and we hold nothing for this upstream. Hand
            # back the registry's own answer; inventing one would hide the fact
            # that the operator has not configured credentials for it.
            log.info("basic challenge from %s and no credentials for it", ref.upstream)
            return r
        hdrs["authorization"] = f"Basic {basic}"
        if out is not None:
            out["authenticated"] = True
        _t = time.monotonic()
        try:
            return await send(hdrs)
        finally:
            # Isolates the re-send from everything around it. A stall observed
            # BETWEEN a 401 and its authenticated retry is either here or in
            # the caller, and these two timings say which without anyone
            # reading the source.
            if (time.monotonic() - _t) > 1.0:
                log.warning("basic re-send to %s took %.1fs", url,
                            time.monotonic() - _t)

    if challenge.get("_scheme") != "bearer" or "realm" not in challenge:
        # Neither Basic nor a usable Bearer challenge. Hand the client the
        # registry's own answer rather than inventing one.
        #
        # This previously tested `challenge.get("Bearer") is None`, which was
        # always true because the scheme token is never captured as a key=value
        # pair -- so the condition collapsed to "has a realm" and EVERY Basic
        # challenge entered the bearer dance. That is what produced a 500 on one
        # registry and a misleading 404 on another. an internal issue.
        return r

    lock = _token_locks.setdefault((ref.upstream, ref.repo), asyncio.Lock())
    async with lock:
        # Another request may have refreshed while we waited.
        token = _cached_token(ref) or await _fetch_token(ref, challenge, out)
    if not token:
        return r
    hdrs["authorization"] = f"Bearer {token}"
    if out is not None:
        # An ANONYMOUS token counts. The registry issued it, so it understood
        # the request; a 401 after presenting it is the registry's answer about
        # this repository, not a failure to authenticate.
        out["authenticated"] = True
    return await send(hdrs)


async def get(ref: Ref, path: str, headers: dict | None = None, *,
              method: str = "GET", out: dict | None = None):
    """Non-streaming request against `<api>/v2/<repo>/<path>`; body is read."""
    url = f"{ref.api}/v2/{ref.repo}/{path}"
    r = await _authed_request(method, url, ref, headers or {}, stream=False, out=out)
    if not hasattr(r, "_content_read"):
        await r.aread()
    return r


async def open_stream(ref: Ref, path: str, headers: dict | None = None):
    """Streaming request; the caller MUST close the returned response."""
    url = f"{ref.api}/v2/{ref.repo}/{path}"
    return await _authed_request("GET", url, ref, headers or {}, stream=True)


async def request(ref: Ref, method: str, path: str, headers: dict | None = None,
                  content=None):
    """Arbitrary method against `<api>/v2/<repo>/<path>`, with an optional body.

    `content` is a callable returning a fresh body per attempt -- see send().
    Used by the push path, which needs PATCH and PUT with real payloads and
    must survive the bearer/basic dance without sending an empty retry.
    """
    url = f"{ref.api}/v2/{ref.repo}/{path}"
    r = await _authed_request(method, url, ref, headers or {}, stream=False,
                              content=content)
    if not hasattr(r, "_content_read"):
        await r.aread()
    return r


async def request_absolute(ref: Ref, method: str, url: str,
                           headers: dict | None = None, content=None):
    """Same, against an absolute URL.

    An upload session's Location is registry-chosen and may point anywhere --
    a different host, a different path shape, with its own query string. It
    must be used verbatim rather than reconstructed, which is why this exists
    alongside request().
    """
    r = await _authed_request(method, url, ref, headers or {}, stream=False,
                              content=content)
    if not hasattr(r, "_content_read"):
        await r.aread()
    return r
