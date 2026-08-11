"""Cache layout helpers, scanning, pinning and LRU eviction.

We deliberately reuse the stock huggingface_hub cache layout on disk
(`models--org--name/{blobs,snapshots,refs}`) rather than inventing our own.
Two reasons: the ingest path is just `hf_hub_download`, so atomicity, symlinking
and blob-level dedup across revisions come for free; and the directory stays
readable by any standard HF client, so you can bypass this service entirely
(mount it read-only, point HF_HUB_CACHE at it) if it ever gets in the way.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from huggingface_hub import scan_cache_dir

from .config import settings

log = logging.getLogger("xhc.cachefs")

REPO_ID_SEPARATOR = "--"
_STATE_DIR = ".xhc"
_PINS_FILE = "pins.json"
_ORPHANS_FILE = "orphans.json"

# scan_cache_dir() stats every blob, so its cost tracks FILE COUNT, not bytes.
# Measured: ~36us/file, i.e. 0.55s for 15k files (80TB of large shards) but 12s
# for 200k files (many small dataset shards at the same total size). A fixed TTL
# would let a slow scan eat most of the wall clock under polling, so the view
# cache adapts: hold the result for 10x the time the scan took, clamped.
_SCAN_TTL_MIN_S = 30.0
_SCAN_TTL_MAX_S = 600.0
_SCAN_TTL_FACTOR = 10.0


def repo_folder_name(repo_id: str, repo_type: str) -> str:
    """Mirror of huggingface_hub's on-disk folder naming."""
    parts = [f"{repo_type}s", *repo_id.split("/")]
    return REPO_ID_SEPARATOR.join(parts)


def repo_key(repo_type: str, repo_id: str) -> str:
    return f"{repo_type}s/{repo_id}"


@dataclass
class ResolvedFile:
    path: Path
    commit: str
    size: int
    etag: str | None = None


def resolve_local(
    repo_type: str, repo_id: str, revision: str, filename: str
) -> ResolvedFile | None:
    """Return the local file for a repo/revision/filename, or None if not cached.

    `revision` may be a branch/tag (resolved via refs/) or a commit sha.
    """
    base = Path(settings.cache_dir) / repo_folder_name(repo_id, repo_type)
    if not base.is_dir():
        return None

    commit: str | None = None
    ref_file = base / "refs" / revision
    if ref_file.is_file():
        try:
            commit = ref_file.read_text().strip()
        except OSError:
            commit = None
    if commit is None and (base / "snapshots" / revision).is_dir():
        commit = revision
    if not commit:
        return None

    target = base / "snapshots" / commit / filename
    # `target` is a symlink into blobs/; is_file() follows it, so a dangling
    # link (blob evicted out from under us) correctly reads as a miss.
    if not target.is_file():
        return None
    try:
        size = target.stat().st_size
    except OSError:
        return None

    # In the HF cache layout, snapshots/<commit>/<file> is a symlink into
    # blobs/<etag> -- so the blob's filename *is* the upstream ETag. Recovering
    # it here means a cache hit can answer with the same ETag the Hub would,
    # which huggingface_hub requires (it refuses downloads without one).
    etag: str | None = None
    if target.is_symlink():
        try:
            link = os.readlink(target)
            name = os.path.basename(link)
            if name and not name.endswith(".incomplete"):
                etag = name
        except OSError:
            etag = None

    return ResolvedFile(path=target, commit=commit, size=size, etag=etag)


def blob_incomplete_path(repo_type: str, repo_id: str, etag: str) -> Path:
    """Where hf_hub_download writes bytes before committing them to blobs/."""
    base = Path(settings.cache_dir) / repo_folder_name(repo_id, repo_type)
    return base / "blobs" / f"{etag}.incomplete"


# --------------------------------------------------------------------------
# pins
# --------------------------------------------------------------------------


def _state_dir() -> Path:
    d = Path(settings.cache_dir) / _STATE_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_pins() -> set[str]:
    p = _state_dir() / _PINS_FILE
    if not p.is_file():
        return set()
    try:
        return set(json.loads(p.read_text()))
    except (OSError, ValueError):
        log.warning("pins file unreadable, treating as empty", exc_info=True)
        return set()


def save_pins(pins: set[str]) -> None:
    p = _state_dir() / _PINS_FILE
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(sorted(pins), indent=2))
    tmp.replace(p)


# --------------------------------------------------------------------------
# orphans: repos whose upstream has gone away
#
# Once a repo is deleted (or gated) on the Hub, the copy here is the only copy.
# Evicting it is irreversible in a way that evicting a live repo is not: a live
# repo can always be re-fetched, an orphan cannot. Under the default retain
# policy these are exempt from eviction, which is what makes the cache usable
# as a reproducibility archive rather than just an accelerator.
# --------------------------------------------------------------------------


def load_orphans() -> dict[str, dict]:
    p = _state_dir() / _ORPHANS_FILE
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        log.warning("orphans file unreadable, treating as empty", exc_info=True)
        return {}


def save_orphans(orphans: dict[str, dict]) -> None:
    p = _state_dir() / _ORPHANS_FILE
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(orphans, indent=2, sort_keys=True))
    tmp.replace(p)


def protected_keys() -> set[str]:
    """Repo keys eviction must not touch: explicit pins, plus orphans when the
    policy is to retain them."""
    protected = load_pins()
    if settings.orphan_policy == "retain":
        protected |= set(load_orphans())
    return protected


# --------------------------------------------------------------------------
# scanning
# --------------------------------------------------------------------------


@dataclass
class RepoView:
    repo_id: str
    repo_type: str
    key: str
    size_on_disk: int
    nb_files: int
    last_accessed: float
    pinned: bool
    revisions: list[dict]


@dataclass
class CacheView:
    scanned_at: float
    size_on_disk: int
    repos: list[RepoView]
    warnings: list[str]
    scan_duration_s: float = 0.0
    nb_files: int = 0

    @property
    def ttl_s(self) -> float:
        return min(_SCAN_TTL_MAX_S, max(_SCAN_TTL_MIN_S, self.scan_duration_s * _SCAN_TTL_FACTOR))


_scan_lock = asyncio.Lock()
_scan_cache: CacheView | None = None


def _scan_sync() -> CacheView:
    started = time.time()
    pins = load_pins()
    info = scan_cache_dir(settings.cache_dir)
    repos: list[RepoView] = []
    for r in info.repos:
        key = repo_key(r.repo_type, r.repo_id)
        revs = [
            {
                "commit": rev.commit_hash,
                "refs": sorted(rev.refs),
                "size_on_disk": rev.size_on_disk,
                "nb_files": rev.nb_files,
                "last_modified": rev.last_modified,
            }
            for rev in r.revisions
        ]
        revs.sort(key=lambda x: x["last_modified"], reverse=True)
        repos.append(
            RepoView(
                repo_id=r.repo_id,
                repo_type=r.repo_type,
                key=key,
                size_on_disk=r.size_on_disk,
                nb_files=r.nb_files,
                last_accessed=r.last_accessed,
                pinned=key in pins,
                revisions=revs,
            )
        )
    repos.sort(key=lambda r: r.size_on_disk, reverse=True)
    # scan_cache_dir flags any directory it does not recognise, including our
    # own state dir. Drop that one so real warnings stay visible.
    warnings = [str(w) for w in info.warnings if f"/{_STATE_DIR}" not in str(w)]
    duration = time.time() - started
    view = CacheView(
        scanned_at=time.time(),
        size_on_disk=info.size_on_disk,
        repos=repos,
        warnings=warnings,
        scan_duration_s=round(duration, 3),
        nb_files=sum(r.nb_files for r in repos),
    )
    if duration > 5:
        log.warning(
            "cache scan took %.1fs over %d files; holding view for %.0fs",
            duration,
            view.nb_files,
            view.ttl_s,
        )
    return view


async def get_view(force: bool = False) -> CacheView:
    global _scan_cache  # noqa: PLW0603 - module-level view cache
    async with _scan_lock:
        fresh = (
            _scan_cache is not None
            and not force
            and (time.time() - _scan_cache.scanned_at) < _scan_cache.ttl_s
        )
        if not fresh:
            _scan_cache = await asyncio.to_thread(_scan_sync)
        return _scan_cache


def invalidate_view() -> None:
    global _scan_cache  # noqa: PLW0603 - module-level view cache
    _scan_cache = None


# --------------------------------------------------------------------------
# capacity + eviction
# --------------------------------------------------------------------------


def disk_stats() -> dict:
    usage = shutil.disk_usage(settings.cache_dir)
    capacity = settings.capacity_bytes or usage.total
    return {
        "fs_total": usage.total,
        "fs_used": usage.used,
        "fs_free": usage.free,
        "capacity": capacity,
        "capacity_source": "XHC_CACHE_MAX_SIZE" if settings.capacity_bytes else "filesystem",
    }


def _evict_sync(target_free_bytes: int = 0) -> dict:
    """Delete least-recently-accessed unpinned revisions until under low water.

    Pinning is repo-level and absolute: a pinned repo is never a candidate, even
    if that means we cannot reach the low-water mark. That is the correct
    failure mode for a fleet rollout -- better to run hot on disk than to evict
    the model every node is about to ask for.
    """
    pins = load_pins()
    orphans = load_orphans()
    protected = protected_keys()
    stats = disk_stats()
    capacity = stats["capacity"]
    low = int(capacity * settings.low_water)
    high = int(capacity * settings.high_water)

    info = scan_cache_dir(settings.cache_dir)
    used = info.size_on_disk

    if used <= high and used + target_free_bytes <= capacity:
        return {
            "evicted": [],
            "freed": 0,
            "used_before": used,
            "used_after": used,
            "reason": "under high water",
        }

    goal = min(low, capacity - target_free_bytes)

    # (last_accessed, size, commit, repo_key) per revision, oldest first.
    candidates = []
    protected_bytes = 0
    for r in info.repos:
        if repo_key(r.repo_type, r.repo_id) in protected:
            protected_bytes += r.size_on_disk
            continue
        for rev in r.revisions:
            candidates.append(
                (
                    r.last_accessed,
                    rev.size_on_disk,
                    rev.commit_hash,
                    repo_key(r.repo_type, r.repo_id),
                )
            )
    candidates.sort(key=lambda c: c[0])

    to_delete: list[str] = []
    evicted: list[dict] = []
    projected = used
    for last_accessed, size, commit, key in candidates:
        if projected <= goal:
            break
        to_delete.append(commit)
        evicted.append(
            {"repo": key, "commit": commit, "size": size, "last_accessed": last_accessed}
        )
        projected -= size

    freed = 0
    if to_delete:
        strategy = info.delete_revisions(*to_delete)
        freed = strategy.expected_freed_size
        strategy.execute()
        log.info("evicted %d revisions, freed %d bytes", len(to_delete), freed)

    if used - freed > goal:
        # Protection won over the target. Say so loudly rather than silently
        # running hot: on a reproducibility archive this is expected, but it is
        # also exactly how a disk fills up unnoticed.
        log.warning(
            "eviction could not reach target: %.1fGB still used vs %.1fGB goal; "
            "%.1fGB is protected (%d pinned, %d orphaned)",
            (used - freed) / 1e9,
            goal / 1e9,
            protected_bytes / 1e9,
            len(pins),
            len(orphans),
        )

    return {
        "evicted": evicted,
        "freed": freed,
        "used_before": used,
        "used_after": used - freed,
        "goal": goal,
        "reached_goal": (used - freed) <= goal,
        "protected_bytes": protected_bytes,
        "pinned_skipped": sorted(pins),
        "orphans_skipped": sorted(orphans) if settings.orphan_policy == "retain" else [],
    }


async def evict(target_free_bytes: int = 0) -> dict:
    result = await asyncio.to_thread(_evict_sync, target_free_bytes)
    if result.get("freed"):
        invalidate_view()
    return result


def delete_repo_sync(repo_type: str, repo_id: str) -> dict:
    info = scan_cache_dir(settings.cache_dir)
    commits = [
        rev.commit_hash
        for r in info.repos
        if r.repo_id == repo_id and r.repo_type == repo_type
        for rev in r.revisions
    ]
    if not commits:
        return {"deleted": False, "freed": 0}
    strategy = info.delete_revisions(*commits)
    freed = strategy.expected_freed_size
    strategy.execute()
    # delete_revisions leaves the (now empty) repo folder behind.
    folder = Path(settings.cache_dir) / repo_folder_name(repo_id, repo_type)
    if folder.is_dir() and not any((folder / "snapshots").glob("*")):
        shutil.rmtree(folder, ignore_errors=True)
    return {"deleted": True, "freed": freed, "revisions": commits}


async def eviction_loop() -> None:
    """Background sweep so we never wait for a miss to discover we are full."""
    while True:
        try:
            await asyncio.sleep(settings.evict_interval_s)
            stats = disk_stats()
            # Deliberately NOT force=True. evict() re-scans authoritatively
            # before deleting anything, so forcing here would pay for two full
            # scans every sweep -- 24s at 200k files -- to answer a question a
            # slightly stale view answers fine. Worst case we defer an eviction
            # by one interval.
            view = await get_view()
            if view.size_on_disk > stats["capacity"] * settings.high_water:
                log.info("high-water exceeded (%d bytes used), evicting", view.size_on_disk)
                await evict()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("eviction sweep failed; continuing")
