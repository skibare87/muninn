"""Keep mutable refs honest without paying for it on every cache hit.

A cache hit is a pure disk read, which is the whole point of the LAN leg. But it
means a moved `main` upstream would otherwise never be noticed: we would serve
the old commit forever and, because HEAD is answered the same way, tell the
client that old commit *is* `main`.

The fix revalidates the **ref**, not the file. Per-file revalidation would put an
upstream HEAD on every hit and destroy the hot path; a ref->commit map with a TTL
costs one small API call per repo-and-ref actually requested per window, and
nothing at all for sha-pinned requests.

Fail-open is deliberate: if upstream cannot answer, we serve what we have. That
is required for orphan retention, where upstream 404s forever by definition.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from urllib.parse import quote

import httpx

from .config import settings

log = logging.getLogger("xhc.refs")

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# (repo_type, repo_id, revision) -> (commit or None, monotonic checked_at)
_cache: dict[tuple[str, str, str], tuple[str | None, float]] = {}
_locks: dict[tuple[str, str, str], asyncio.Lock] = {}
_client: httpx.AsyncClient | None = None

# Observability for the tests and /_cache/status: how many upstream lookups we
# actually made. Single-flight is only provable by counting.
_stats = {"lookups": 0, "hits": 0, "changed": 0, "failed": 0}


def stats() -> dict:
    return dict(_stats)


def _get_client() -> httpx.AsyncClient:
    global _client  # noqa: PLW0603 - module-level singleton client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(15.0), follow_redirects=True)
    return _client


async def close_client() -> None:
    global _client  # noqa: PLW0603 - module-level singleton client
    if _client is not None:
        await _client.aclose()
        _client = None


def is_immutable(revision: str) -> bool:
    """A 40-hex commit sha can never move, so it never needs revalidating."""
    return bool(_SHA_RE.match(revision))


async def _fetch_commit(repo_type: str, repo_id: str, revision: str) -> str | None:
    url = f"{settings.upstream}/api/{repo_type}s/{repo_id}/revision/{quote(revision, safe='')}"
    headers = {"Authorization": f"Bearer {settings.hf_token}"} if settings.hf_token else {}
    try:
        r = await _get_client().get(url, headers=headers)
    except httpx.HTTPError as exc:
        log.debug("ref lookup unreachable for %s/%s@%s: %s", repo_type, repo_id, revision, exc)
        return None
    if r.status_code != 200:
        # 404 means deleted (an orphan) -- expected, and must not invalidate
        # anything. Anything else is equally uninformative about the ref.
        log.debug("ref lookup got %s for %s/%s@%s", r.status_code, repo_type, repo_id, revision)
        return None
    try:
        return r.json().get("sha")
    except ValueError:
        return None


async def upstream_commit(repo_type: str, repo_id: str, revision: str) -> str | None:
    """Commit that `revision` currently points at upstream, or None if unknown.

    None means "we could not find out" -- never "it is gone". Callers must treat
    it as a reason to keep serving what they have.
    """
    if is_immutable(revision):
        return revision
    if settings.ref_ttl_s <= 0:
        return None

    key = (repo_type, repo_id, revision)
    entry = _cache.get(key)
    now = time.monotonic()
    if entry is not None and (now - entry[1]) < settings.ref_ttl_s:
        _stats["hits"] += 1
        return entry[0]

    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        # Re-check: while we waited, another request may have refreshed it.
        # This is what makes forty nodes rotating together cost one lookup.
        entry = _cache.get(key)
        now = time.monotonic()
        if entry is not None and (now - entry[1]) < settings.ref_ttl_s:
            _stats["hits"] += 1
            return entry[0]

        _stats["lookups"] += 1
        commit = await _fetch_commit(repo_type, repo_id, revision)
        if commit is None:
            _stats["failed"] += 1
        # Cache the failure too, so an unreachable upstream is asked once per
        # TTL rather than on every single request.
        _cache[key] = (commit, time.monotonic())
        return commit


async def is_stale(repo_type: str, repo_id: str, revision: str, local_commit: str) -> bool:
    """True only when we positively know upstream has moved on.

    Unknown, unreachable, deleted, immutable -> False. The bias is always toward
    serving the bytes we already hold.
    """
    if settings.ref_ttl_s <= 0 or is_immutable(revision):
        return False
    current = await upstream_commit(repo_type, repo_id, revision)
    if current is None or current == local_commit:
        return False
    _stats["changed"] += 1
    log.info(
        "%s/%s@%s moved upstream: %s -> %s; refetching",
        repo_type,
        repo_id,
        revision,
        local_commit[:12],
        current[:12],
    )
    return True


def invalidate(repo_type: str, repo_id: str, revision: str | None = None) -> None:
    for key in [k for k in _cache if k[0] == repo_type and k[1] == repo_id]:
        if revision is None or key[2] == revision:
            _cache.pop(key, None)


def clear() -> None:
    _cache.clear()
    _locks.clear()
    for k in _stats:
        _stats[k] = 0
