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
import errno
import fcntl
import json
import logging
import os
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from huggingface_hub import scan_cache_dir

from . import manifests, shutdown, statedir
from .config import settings

log = logging.getLogger("xhc.cachefs")

REPO_ID_SEPARATOR = "--"
_STATE_DIR = statedir.TREE_DIR_NAME
_PINS_FILE = "pins.json"
_ORPHANS_FILE = "orphans.json"

# Cache-root entries huggingface_hub creates and then warns it does not
# recognise. Exact basenames only -- see the filter in get_view().
_BENIGN_CACHE_ENTRIES = frozenset({"CACHEDIR.TAG", "version.txt", ".locks"})

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


def resolve_commit(repo_type: str, repo_id: str, revision: str) -> str | None:
    """Map a branch/tag/sha to a commit we actually hold, or None."""
    base = Path(settings.cache_dir) / repo_folder_name(repo_id, repo_type)
    if not base.is_dir():
        return None
    ref_file = base / "refs" / revision
    if ref_file.is_file():
        try:
            commit = ref_file.read_text().strip()
            if commit and (base / "snapshots" / commit).is_dir():
                return commit
        except OSError:
            pass
    if (base / "snapshots" / revision).is_dir():
        return revision
    return None


def snapshot_files(repo_type: str, repo_id: str, commit: str) -> list[str]:
    """Repo-relative paths held for a commit.

    Only files that actually resolve are listed: a snapshot entry is a symlink
    into blobs/, and a dangling one means the blob was evicted. Advertising a
    file we cannot then serve would be worse than omitting it.
    """
    root = Path(settings.cache_dir) / repo_folder_name(repo_id, repo_type) / "snapshots" / commit
    if not root.is_dir():
        return []
    out = []
    for p in root.rglob("*"):
        try:
            if p.is_file():
                out.append(p.relative_to(root).as_posix())
        except OSError:
            continue
    return sorted(out)


def repo_is_cached(repo_type: str, repo_id: str) -> bool:
    base = Path(settings.cache_dir) / repo_folder_name(repo_id, repo_type)
    return (base / "snapshots").is_dir() and any((base / "snapshots").iterdir())


def forget_orphan(key: str) -> bool:
    """Drop an orphan mark. Called after a delete so state cannot go stale."""
    orphans = load_orphans()
    existed = orphans.pop(key, None) is not None
    if existed:
        save_orphans(orphans)
    return existed


def blob_incomplete_path(repo_type: str, repo_id: str, etag: str) -> Path:
    """Where hf_hub_download writes bytes before committing them to blobs/."""
    base = Path(settings.cache_dir) / repo_folder_name(repo_id, repo_type)
    return base / "blobs" / f"{etag}.incomplete"


def hub_lock_path(folder: str, etag: str) -> Path:
    """huggingface_hub's own per-blob lock: `.locks/<repo folder>/<etag>.lock`.

    The hub holds it (a filelock flock) for the whole life of the blob's
    `.incomplete` file, and the tier fill takes the same lock for its
    `.tier.incomplete`. The kernel drops it when the holder dies.
    """
    return Path(settings.cache_dir) / ".locks" / folder / f"{etag}.lock"


# --------------------------------------------------------------------------
# stale partial downloads
# --------------------------------------------------------------------------
#
# huggingface_hub downloads into `blobs/<etag>.incomplete` and renames it on
# completion; the tier fill writes `blobs/<etag>.tier.incomplete`. A process
# killed mid-file leaves the partial behind (measured: SIGKILL leaves the
# partial, a 0-byte `.locks/<repo>/<etag>.lock`, `refs/<rev>` and an empty
# `snapshots/<commit>/`, on the plain-HTTP and the xet path alike).
# scan_cache_dir() counts only blobs a snapshot links to, so without this those
# bytes use the disk and are invisible to eviction.

PARTIAL_SUFFIX = ".incomplete"
_REPO_PREFIXES = ("models--", "datasets--", "spaces--")

# The last sweep's result, for /_cache/status. None until the first sweep.
_last_partial_sweep: dict | None = None


def _partial_etag(name: str) -> str:
    # `<etag>.incomplete` or `<etag>.tier.incomplete`; an etag has no dot.
    return name.split(".", 1)[0]


def iter_partials():
    """Yield (repo folder name, etag, path) for every partial in the HF cache.

    One directory listing per repo's `blobs/`, never a recursive walk: that is
    the only place either writer puts one. Repo folders only, by prefix, so the
    state dir and anything else sharing the root are never looked into.
    """
    root = Path(settings.cache_dir)
    try:
        with os.scandir(root) as it:
            repos = sorted(e.name for e in it
                           if e.name.startswith(_REPO_PREFIXES) and e.is_dir(follow_symlinks=False))
    except OSError:
        return
    for folder in repos:
        try:
            with os.scandir(root / folder / "blobs") as it:
                names = [e.name for e in it
                         if e.name.endswith(PARTIAL_SUFFIX) and e.is_file(follow_symlinks=False)]
        except OSError:
            continue
        for name in sorted(names):
            yield folder, _partial_etag(name), root / folder / "blobs" / name


def partial_bytes() -> int:
    """Bytes held by every partial in the HF cache right now, owned or not."""
    total = 0
    for _folder, _etag, path in iter_partials():
        try:
            total += path.stat().st_size
        except OSError:
            continue
    return total


def owns_partial(folder: str, etag: str) -> bool:
    """Whether a download in THIS process owns that blob's partial.

    Two writers: a JobManager job (hf_hub_download or snapshot_download writes
    `.incomplete`) and the tier fill (`.tier.incomplete`, or `.incomplete`
    under stream read mode). Read from the sweep's worker thread.
    """
    from . import jobs, tier  # both import this module

    return jobs.manager.owns_partial(folder, etag) or tier.owns_partial(folder, etag)


def sweep_hf_partials(max_age_s: float | None = None, dry_run: bool = False,
                      stop=None) -> dict:
    """Remove HF-cache partials that no download owns any more.

    A partial is removed only when ALL THREE guards agree it is dead:

    1. no download in THIS process owns it (owns_partial);
    2. no process holds huggingface_hub's own lock for it. Every writer holds
       `.locks/<repo>/<etag>.lock` for the life of the partial and the kernel
       drops it when the writer dies, which is what reaches a second process
       sharing this cache. The sweep takes that lock itself, non-blocking, and
       deletes only while holding it, so no download can start on the file in
       between;
    3. it has not been written for XHC_HF_PARTIAL_MAX_AGE -- the backstop for a
       filesystem where the lock is not honoured.

    `stop` is polled between files so a shutdown can end the sweep promptly.
    Returns what it saw as well as what it did: `scanned` and the `kept_*`
    counts make a zero `removed` distinguishable from "found nothing", and
    `kept_bytes` is what partials still hold on disk after the sweep.
    """
    age = settings.hf_partial_max_age_s if max_age_s is None else max_age_s
    res = {"scanned": 0, "removed": 0, "freed_bytes": 0, "kept_owned": 0,
           "kept_locked": 0, "kept_young": 0, "kept_bytes": 0, "max_age_s": age,
           "dry_run": dry_run, "stopped": False}
    for folder, etag, path in iter_partials():
        if stop is not None and stop():
            res["stopped"] = True
            break
        res["scanned"] += 1
        _consider_partial(folder, etag, path, age, dry_run, res)
    if res["removed"]:
        log.info("HF partial sweep: %s %d stale partial download(s), %d bytes "
                 "(kept %d owned, %d locked, %d younger than %.0fs; %d bytes still in partials)",
                 "would remove" if dry_run else "removed", res["removed"], res["freed_bytes"],
                 res["kept_owned"], res["kept_locked"], res["kept_young"], age,
                 res["kept_bytes"])
    return res


def _keep(res: dict, why: str, size: int) -> None:
    res[why] += 1
    res["kept_bytes"] += size


def _consider_partial(folder: str, etag: str, path: Path, age: float,
                      dry_run: bool, res: dict) -> None:
    try:
        st = path.stat()
    except OSError:
        return  # renamed into place or removed since the listing
    if owns_partial(folder, etag):
        _keep(res, "kept_owned", st.st_size)
        return
    if time.time() - st.st_mtime < age:
        _keep(res, "kept_young", st.st_size)
        return
    lock = hub_lock_path(folder, etag)
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o644)
    except OSError:
        # Cannot take part in the lock protocol (read-only, permissions): a
        # holder could exist that this cannot see, so keep it.
        _keep(res, "kept_locked", st.st_size)
        return
    try:
        _remove_if_unlocked(fd, lock, folder, etag, path, age, dry_run, res)
    finally:
        os.close(fd)  # releases the flock, if taken


def _remove_if_unlocked(fd: int, lock: Path, folder: str, etag: str, path: Path,
                        age: float, dry_run: bool, res: dict) -> None:
    try:
        size = path.stat().st_size
    except OSError:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES):
            _keep(res, "kept_locked", size)
            return
        # flock unsupported on this filesystem: the age guard is the guard.
    else:
        # A lock on an inode that was unlinked, or that is no longer the file at
        # that path, excludes nobody (filelock drops such a lock for the same
        # reason), so it proves nothing about the real holder.
        try:
            fst, cur = os.fstat(fd), os.stat(lock)
        except OSError:
            _keep(res, "kept_locked", size)
            return
        if fst.st_nlink == 0 or (fst.st_ino, fst.st_dev) != (cur.st_ino, cur.st_dev):
            _keep(res, "kept_locked", size)
            return
    # Under the lock no download can start on this blob; re-check the rest.
    if owns_partial(folder, etag):
        _keep(res, "kept_owned", size)
        return
    try:
        st = path.stat()
    except OSError:
        return  # renamed into place since the listing
    idle = time.time() - st.st_mtime
    if idle < age:
        _keep(res, "kept_young", st.st_size)
        return
    if not dry_run:
        try:
            path.unlink()
        except OSError:
            _keep(res, "kept_locked", st.st_size)
            return
    res["removed"] += 1
    res["freed_bytes"] += st.st_size
    log.info("HF partial sweep: %s stale partial %s/blobs/%s (%d bytes, idle %.0fs, no owner)",
             "would remove" if dry_run else "removed", folder, path.name, st.st_size, idle)


def last_partial_sweep() -> dict | None:
    """The most recent sweep's result with its trigger and time, or None."""
    return _last_partial_sweep


def _record_sweep(res: dict, trigger: str) -> dict:
    global _last_partial_sweep  # noqa: PLW0603 - module-level status
    _last_partial_sweep = {**res, "trigger": trigger, "at": time.time()}
    return res


async def sweep_partials_async(trigger: str) -> dict:
    """Run the sweep in a worker thread; a cancel also stops the thread.

    Cancelling the await does not stop a thread, so the thread is told to stop
    as well -- otherwise a shutdown would leave it walking the cache.
    """
    stop = threading.Event()
    try:
        res = await asyncio.to_thread(sweep_hf_partials, stop=stop.is_set)
    except asyncio.CancelledError:
        stop.set()
        raise
    return _record_sweep(res, trigger)


# --------------------------------------------------------------------------
# pins
# --------------------------------------------------------------------------


def _state_file(name: str) -> Path:
    # Resolved through statedir so every reader and writer agrees on the
    # location, including when XHC_STATE_DIR moves it off the cache tree.
    return statedir.hf_file(name)


class StateUnavailable(RuntimeError):
    """A protection state file exists but could not be read.

    The distinction that matters: an ABSENT file legitimately means "nothing is
    protected"; an UNREADABLE one means "we do not know what is protected", and
    those must not collapse to the same answer. Any caller that is about to
    delete something has to treat this as a refusal, because the alternative is
    a corrupt pins file silently making every pinned model evictable.
    """


def load_pins(strict: bool = False) -> set[str]:
    """Pinned repo keys.

    `strict=True` raises StateUnavailable rather than returning an empty set
    when the file exists but cannot be parsed. Every destructive caller must
    pass it.
    """
    p = _state_file(_PINS_FILE)
    if not p.is_file():
        return set()
    try:
        return set(json.loads(p.read_text()))
    except (OSError, ValueError) as exc:
        if strict:
            raise StateUnavailable(f"pins file unreadable: {exc}") from exc
        log.warning("pins file unreadable, treating as empty", exc_info=True)
        return set()


def save_pins(pins: set[str]) -> None:
    p = _state_file(_PINS_FILE)
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


def load_orphans(strict: bool = False) -> dict[str, dict]:
    """Repos marked as deleted upstream. See load_pins for `strict`."""
    p = _state_file(_ORPHANS_FILE)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text())
        if not isinstance(data, dict):
            if strict:
                raise StateUnavailable("orphans file is not an object")
            log.warning("orphans file is not an object, treating as empty")
            return {}
        return data
    except (OSError, ValueError) as exc:
        if strict:
            raise StateUnavailable(f"orphans file unreadable: {exc}") from exc
        log.warning("orphans file unreadable, treating as empty", exc_info=True)
        return {}


def save_orphans(orphans: dict[str, dict]) -> None:
    p = _state_file(_ORPHANS_FILE)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(orphans, indent=2, sort_keys=True))
    tmp.replace(p)


def protected_keys(strict: bool = False) -> set[str]:
    """Repo keys eviction must not touch: explicit pins, plus orphans when the
    policy is to retain them.

    Pass `strict=True` from anything that deletes. An unreadable state file then
    raises instead of quietly reporting that nothing is protected.
    """
    protected = load_pins(strict=strict)
    if settings.orphan_policy == "retain":
        protected |= set(load_orphans(strict=strict))
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
    # complete / files_present / files_expected / bytes_present / bytes_expected,
    # rolled up from the revisions. See manifests.py for what "expected" means.
    completeness: dict | None = None


@dataclass
class CacheView:
    scanned_at: float
    size_on_disk: int
    repos: list[RepoView]
    warnings: list[str]
    scan_duration_s: float = 0.0
    nb_files: int = 0
    # Bytes in `blobs/*.incomplete` partials, live or stale. NOT in
    # size_on_disk, which is scan_cache_dir's figure and counts only blobs a
    # snapshot links to; kept separate so muninn_cache_bytes keeps its meaning.
    partial_bytes: int = 0

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
        revs = []
        for rev in r.revisions:
            # From the scan already in hand: completeness is judged on local
            # data only, and must never cost an upstream call per repo.
            present = {
                Path(f.file_path).relative_to(rev.snapshot_path).as_posix(): f.size_on_disk
                for f in rev.files
            }
            revs.append({
                "commit": rev.commit_hash,
                "refs": sorted(rev.refs),
                "size_on_disk": rev.size_on_disk,
                "nb_files": rev.nb_files,
                "last_modified": rev.last_modified,
                **manifests.completeness(r.repo_type, r.repo_id, rev.commit_hash, present),
            })
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
                completeness=manifests.summarise(revs),
            )
        )
    repos.sort(key=lambda r: r.size_on_disk, reverse=True)
    # scan_cache_dir flags any entry it does not recognise, including our own
    # state dir and huggingface_hub's OWN cache metadata files, which it creates
    # itself and then warns about on every scan. Drop the known-benign ones so
    # REAL warnings stay visible -- an operator who learns that this list is
    # always noisy stops reading it, which is the same as not having it.
    #
    # Matched by exact basename, never by pattern: a broad filter would hide a
    # genuinely unrecognised entry, and the whole point of this list is the
    # entries nobody anticipated.
    warnings = [
        str(w)
        for w in info.warnings
        if f"/{_STATE_DIR}" not in str(w)
        and not any(str(w).rstrip().endswith(f"/{n}") for n in _BENIGN_CACHE_ENTRIES)
    ]
    in_partials = partial_bytes()
    duration = time.time() - started
    view = CacheView(
        scanned_at=time.time(),
        size_on_disk=info.size_on_disk,
        repos=repos,
        warnings=warnings,
        scan_duration_s=round(duration, 3),
        nb_files=sum(r.nb_files for r in repos),
        partial_bytes=in_partials,
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
    # strict: if we cannot read what is protected, we must not delete anything.
    # Running hot on disk is recoverable; evicting a pinned model or a retained
    # orphan is not -- for an orphan the copy here is the only one left.
    #
    # "RECOVERABLE" MEANS NEAR CAPACITY, NOT OUT OF SPACE, and the two are not
    # on a spectrum. Measured: an OSError during ingest sets job.state="error"
    # and serving.tail_follow() returns mid-stream, so the client gets a
    # truncated body on an already-sent 2xx and sees a digest mismatch. There
    # is NO fallback to upstream -- a node using this cache has had its image
    # reference rewritten, so the cache IS its registry
    # (`docker pull <closed-port>/library/alpine` -> connection refused).
    # Do not read this comment as "a full disk is fine".
    try:
        pins = load_pins(strict=True)
        orphans = load_orphans(strict=True)
        protected = protected_keys(strict=True)
    except StateUnavailable as exc:
        log.error("REFUSING TO EVICT: %s -- cannot tell what is protected", exc)
        return {
            "evicted": 0,
            "freed_bytes": 0,
            "reached_goal": False,
            "refused": True,
            "reason": str(exc),
        }
    stats = disk_stats()
    capacity = stats["capacity"]
    low = int(capacity * settings.low_water)
    high = int(capacity * settings.high_water)

    # Stale partials first: they are garbage, and reclaiming them may make
    # evicting real data unnecessary. What survives the sweep -- partials a
    # download owns or that are too young to judge -- is COUNTED as used: those
    # bytes are on disk inside this cache's budget, and an owned one is about
    # to become a blob. scan_cache_dir cannot see them at all.
    partials = _record_sweep(sweep_hf_partials(), "evict")
    info = scan_cache_dir(settings.cache_dir)
    blob_bytes = info.size_on_disk
    used = blob_bytes + partials["kept_bytes"]

    if used <= high and used + target_free_bytes <= capacity:
        # UNDER BUDGET IS NOT THE SAME AS HAVING ROOM, and on a shared
        # filesystem they come apart completely. Eviction compares this cache's
        # OWN size against its OWN budget; it never consults free space. So if
        # anything else on the volume fills it -- another dataset in the same
        # ZFS pool, another tenant, a runaway log -- this cache can sit far
        # under budget, decline to evict, and watch every write fail with
        # ENOSPC while reporting "under high water".
        #
        # That silence is the defect. Freeing our own data may not even fix it
        # when the pressure is elsewhere, so ACTING on it is a real decision
        # rather than an obvious one and is deliberately not taken here. Saying
        # so is not: a cache declining to act while the disk fills should be
        # loud about which of the two limits it is actually looking at.
        #
        # muninn_disk_free_bytes carries the true figure for alerting.
        if stats["fs_total"] and stats["fs_free"] < stats["fs_total"] * 0.05:
            log.warning(
                "DISK IS %.1f%% FULL BUT THIS CACHE IS UNDER ITS OWN BUDGET "
                "(%.1f GB used of %.1f GB), so eviction is declining to act. "
                "The space is being consumed by something outside this cache, "
                "and evicting our own data may not recover it. Free space is "
                "%.1f GB.",
                100.0 * (1 - stats["fs_free"] / stats["fs_total"]),
                used / 1e9, capacity / 1e9, stats["fs_free"] / 1e9,
            )
        return {
            "evicted": [],
            "freed": 0,
            "used_before": used,
            "used_after": used,
            "blob_bytes": blob_bytes,
            "partial_bytes": partials["kept_bytes"],
            "partials": partials,
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
        # used_* = blob_bytes + partial_bytes. Stale partials the sweep removed
        # are in partials.freed_bytes, not in `freed` (they were never blobs).
        "blob_bytes": blob_bytes,
        "partial_bytes": partials["kept_bytes"],
        "partials": partials,
        "goal": goal,
        "reached_goal": (used - freed) <= goal,
        "protected_bytes": protected_bytes,
        "pinned_skipped": sorted(pins),
        "orphans_skipped": sorted(orphans) if settings.orphan_policy == "retain" else [],
    }


async def evict(target_free_bytes: int = 0) -> dict:
    result = await asyncio.to_thread(_evict_sync, target_free_bytes)
    if result.get("freed") or (result.get("partials") or {}).get("removed"):
        invalidate_view()
    return result


def delete_revision_sync(repo_type: str, repo_id: str, commit: str) -> dict:
    """Delete a single revision, leaving the repo's other revisions intact."""
    info = scan_cache_dir(settings.cache_dir)
    match = [
        rev.commit_hash
        for r in info.repos
        if r.repo_id == repo_id and r.repo_type == repo_type
        for rev in r.revisions
        if rev.commit_hash == commit or commit in rev.refs
    ]
    if not match:
        return {"deleted": False, "freed": 0}
    strategy = info.delete_revisions(*match)
    freed = strategy.expected_freed_size
    strategy.execute()
    for c in match:
        manifests.forget(repo_type, repo_id, c)
    key = repo_key(repo_type, repo_id)
    # Only release the orphan mark if nothing of the repo survives.
    if not repo_is_cached(repo_type, repo_id):
        forget_orphan(key)
    return {"deleted": True, "freed": freed, "revisions": match}


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
    # The data is gone, so the orphan mark must go too -- otherwise it keeps
    # claiming to retain bytes that no longer exist and inflates retained_bytes.
    forget_orphan(repo_key(repo_type, repo_id))
    manifests.forget(repo_type, repo_id)
    return {"deleted": True, "freed": freed, "revisions": commits}


async def _sweep_partials_logged(trigger: str) -> dict | None:
    try:
        res = await sweep_partials_async(trigger)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("%s sweep of stale HF partial downloads failed", trigger)
        return None
    if res["removed"]:
        invalidate_view()
    return res


async def eviction_loop() -> None:
    """Background sweep so we never wait for a miss to discover we are full.

    Each interval also reclaims stale partial downloads, and so does startup
    (once, before the first interval): a process killed mid-download left
    partials that nothing else will ever look at.
    """
    await _sweep_partials_logged("startup")
    while True:
        shutdown.reraise_if_cancelled()
        try:
            await asyncio.sleep(settings.evict_interval_s)
            swept = await _sweep_partials_logged("interval")
            stats = disk_stats()
            # Deliberately NOT force=True. evict() re-scans authoritatively
            # before deleting anything, so forcing here would pay for two full
            # scans every sweep -- 24s at 200k files -- to answer a question a
            # slightly stale view answers fine. Worst case we defer an eviction
            # by one interval.
            view = await get_view()
            # Partials still on disk after the sweep count against the budget:
            # scan_cache_dir cannot see them, and they use the disk all the same.
            in_partials = swept["kept_bytes"] if swept is not None else view.partial_bytes
            used = view.size_on_disk + in_partials
            if used > stats["capacity"] * settings.high_water:
                log.info("high-water exceeded (%d bytes used, %d of them in partial downloads), "
                         "evicting", used, in_partials)
                await evict()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("eviction sweep failed; continuing")
