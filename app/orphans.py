"""Detect cached repos whose upstream has disappeared, so they can be retained.

A live repo can always be re-fetched, so evicting it costs only time. An orphan
-- deleted, or gated behind access you no longer have -- exists only here.
Evicting one is irreversible, and for a cache used as a reproducibility archive
that is the difference between "slow" and "the experiment can never be rerun".

This module only *classifies*. The retention decision lives in
cachefs.protected_keys(), driven by XHC_ORPHAN_POLICY.

Bias: marking a repo orphaned is fail-safe (it only prevents deletion), so an
ambiguous answer leaves prior state alone rather than clearing it. Only an
unambiguous 200 un-marks a repo. A transient 5xx or a timeout must never cause
an archive to become evictable.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from . import cachefs
from .config import settings

log = logging.getLogger("xhc.orphans")

# Concurrency against the Hub API. These are small JSON requests, but a cache
# with thousands of repos should not open thousands of sockets or trip rate
# limits.
_CONCURRENCY = 8

_client: httpx.AsyncClient | None = None
_lock = asyncio.Lock()
_last_check: dict = {"at": None, "checked": 0, "orphaned": 0, "errors": 0}


def _get_client() -> httpx.AsyncClient:
    global _client  # noqa: PLW0603 - module-level singleton client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(30.0), follow_redirects=True)
    return _client


async def close_client() -> None:
    global _client  # noqa: PLW0603 - module-level singleton client
    if _client is not None:
        await _client.aclose()
        _client = None


def last_check() -> dict:
    return dict(_last_check)


async def _classify(repo_type: str, repo_id: str) -> tuple[str, str | None]:
    """Return (state, reason) where state is alive | orphaned | unknown."""
    url = f"{settings.upstream}/api/{repo_type}s/{repo_id}"
    headers = {"Authorization": f"Bearer {settings.hf_token}"} if settings.hf_token else {}
    try:
        r = await _get_client().get(url, headers=headers)
    except httpx.HTTPError as exc:
        # Network trouble says nothing about whether the repo exists.
        return "unknown", f"unreachable: {exc}"

    if r.status_code == 200:
        return "alive", None
    if r.status_code == 404:
        return "orphaned", "deleted"
    if r.status_code in (401, 403):
        # Access revoked or repo gated. We cannot re-fetch it either way, so for
        # retention purposes it is just as unrecoverable as a deletion.
        return "orphaned", "gated_or_unauthorized"
    if r.status_code == 429:
        return "unknown", "rate limited"
    return "unknown", f"http {r.status_code}"


async def check_all(force_rescan: bool = False) -> dict:
    """Check every cached repo against upstream and update orphan state."""
    async with _lock:
        view = await cachefs.get_view(force=force_rescan)
        orphans = cachefs.load_orphans()
        sem = asyncio.Semaphore(_CONCURRENCY)
        now = time.time()
        newly, recovered, errors = [], [], 0

        async def one(repo):
            nonlocal errors
            async with sem:
                state, reason = await _classify(repo.repo_type, repo.repo_id)
            if state == "alive":
                if repo.key in orphans:
                    recovered.append(repo.key)
                    orphans.pop(repo.key, None)
            elif state == "orphaned":
                if repo.key not in orphans:
                    newly.append({"key": repo.key, "reason": reason, "size": repo.size_on_disk})
                    orphans[repo.key] = {
                        "reason": reason,
                        "since": now,
                        "last_checked": now,
                        "size_on_disk": repo.size_on_disk,
                    }
                else:
                    orphans[repo.key]["last_checked"] = now
                    orphans[repo.key]["reason"] = reason
                    orphans[repo.key]["size_on_disk"] = repo.size_on_disk
            else:
                # Unknown: leave whatever we already believed untouched.
                errors += 1
                log.debug("orphan check inconclusive for %s: %s", repo.key, reason)

        await asyncio.gather(*(one(r) for r in view.repos))
        cachefs.save_orphans(orphans)

        for n in newly:
            log.warning(
                "upstream gone for %s (%s); %.1fGB retained under XHC_ORPHAN_POLICY=%s",
                n["key"],
                n["reason"],
                n["size"] / 1e9,
                settings.orphan_policy,
            )
        for k in recovered:
            log.info("upstream returned for %s; no longer treated as an orphan", k)

        _last_check.update(at=now, checked=len(view.repos), orphaned=len(orphans), errors=errors)
        return {
            "checked": len(view.repos),
            "orphaned_total": len(orphans),
            "newly_orphaned": newly,
            "recovered": recovered,
            "inconclusive": errors,
            "policy": settings.orphan_policy,
            "retained_bytes": sum(o.get("size_on_disk", 0) for o in orphans.values()),
        }


async def orphan_loop() -> None:
    if settings.orphan_check_interval_s <= 0:
        log.info("orphan detection disabled (XHC_ORPHAN_CHECK_INTERVAL=0)")
        return
    # Don't stampede the Hub API on boot, and let the first cache scan settle.
    await asyncio.sleep(min(120, settings.orphan_check_interval_s))
    while True:
        try:
            result = await check_all(force_rescan=True)
            log.info(
                "orphan sweep: %d repos checked, %d orphaned, %d inconclusive",
                result["checked"],
                result["orphaned_total"],
                result["inconclusive"],
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("orphan sweep failed; continuing")
        await asyncio.sleep(settings.orphan_check_interval_s)
