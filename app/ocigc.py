"""Garbage collection for the OCI cache: mark and sweep, not LRU.

The one genuinely new hard problem in 0.5.0. Hugging Face blobs belong to
exactly one snapshot tree, so LRU is safe there. **Docker blobs are referenced
by manifests, and manifests by tags** -- possibly several, and an index points
at per-platform manifests which point at layers. Evicting a blob that a
retained manifest still needs produces an image that fails at pull time with a
baffling error, long after the eviction that caused it.

So: walk tags -> manifests -> blobs to build a referenced set, and only sweep
what is outside it. Freeing space when everything is referenced means dropping
a TAG first and re-marking -- eviction is top-down, never bottom-up.

**Pinning an image pins its whole closure.** A pin that retains a manifest but
lets its layers go is worse than no pin, because it looks intact until someone
pulls it.

State is read with strict=True throughout. An absent state file legitimately
means "nothing is pinned"; an unreadable one means "we do not know", and those
must never collapse -- that mistake in the HF path (an internal issue) silently disarmed
pin protection inside an unattended loop.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import ocistore
from .cachefs import StateUnavailable
from .config import settings

log = logging.getLogger("xhc.ocigc")

_PINS_FILE = "pins.json"
_ORPHANS_FILE = "orphans.json"
_MAX_INDEX_DEPTH = 8


def _state_dir() -> Path:
    d = ocistore.root() / ".xhc"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read_json(name: str, default, strict: bool):
    p = _state_dir() / name
    if not p.is_file():
        return default
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError) as exc:
        if strict:
            raise StateUnavailable(f"{name} unreadable: {exc}") from exc
        log.warning("%s unreadable, treating as empty", name, exc_info=True)
        return default


def _write_json(name: str, data) -> None:
    p = _state_dir() / name
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    os.replace(tmp, p)


def load_pins(strict: bool = False) -> set[str]:
    """Pinned image references, as `<upstream>/<repo>:<tag>` or `...@sha256:...`."""
    data = _read_json(_PINS_FILE, [], strict)
    if not isinstance(data, list):
        if strict:
            raise StateUnavailable("docker pins file is not a list")
        return set()
    return set(data)


def save_pins(pins: set[str]) -> None:
    _write_json(_PINS_FILE, sorted(pins))


def load_orphans(strict: bool = False) -> dict[str, dict]:
    data = _read_json(_ORPHANS_FILE, {}, strict)
    if not isinstance(data, dict):
        if strict:
            raise StateUnavailable("docker orphans file is not an object")
        return {}
    return data


def save_orphans(orphans: dict[str, dict]) -> None:
    _write_json(_ORPHANS_FILE, orphans)


def mark_orphan(upstream: str, repo: str, tag: str) -> None:
    """Record that a tag no longer resolves upstream.

    Deleted tags are far more common in registry land than deleted HF repos, so
    this fires often. Under `retain` the cached copy is the only one left.
    """
    o = load_orphans()
    o[f"{upstream}/{repo}:{tag}"] = {"marked_at": time.time()}
    save_orphans(o)


# ---------------------------------------------------------------------------
# marking
# ---------------------------------------------------------------------------


@dataclass
class TagRef:
    upstream: str
    repo: str
    tag: str
    accept_fp: str
    digest: str
    path: Path
    last_used: float

    @property
    def key(self) -> str:
        return f"{self.upstream}/{self.repo}:{self.tag}"


def list_tags() -> list[TagRef]:
    """Every tag->digest mapping currently on disk."""
    out: list[TagRef] = []
    r = ocistore.root()
    if not r.is_dir():
        return out
    for up in sorted(p for p in r.iterdir() if p.is_dir() and p.name != ".xhc"):
        tags_root = up / "tags"
        if not tags_root.is_dir():
            continue
        for dirpath, _dirs, files in os.walk(tags_root):
            for fn in files:
                if not fn.endswith(".json"):
                    continue
                full = Path(dirpath) / fn
                try:
                    entry = json.loads(full.read_text())
                    st = full.stat()
                except (OSError, ValueError):
                    continue
                digest = entry.get("digest")
                if not digest:
                    continue
                name, _, accept_fp = fn[:-5].rpartition("@")
                repo = str(Path(dirpath).relative_to(tags_root))
                out.append(
                    TagRef(
                        upstream=up.name,
                        repo=repo,
                        tag=name or fn[:-5],
                        accept_fp=accept_fp,
                        digest=digest,
                        path=full,
                        last_used=max(st.st_atime, st.st_mtime),
                    )
                )
    return out


def _walk(upstream: str, digest: str, manifests: set[str], blobs: set[str], depth: int = 0) -> None:
    """Follow a manifest to everything it references, recursing through indexes."""
    if digest in manifests or depth > _MAX_INDEX_DEPTH:
        return
    held = ocistore.load_manifest(upstream, digest)
    if held is None:
        # Referenced but not cached. Nothing to protect, and not an error: a
        # partially-pulled image is normal.
        return
    manifests.add(digest)
    try:
        doc = json.loads(held.body)
    except ValueError:
        log.warning("manifest %s/%s is not JSON; treating as a leaf", upstream, digest)
        return
    if not isinstance(doc, dict):
        return
    # index / manifest list -> per-platform manifests
    for child in doc.get("manifests") or []:
        if isinstance(child, dict) and child.get("digest"):
            _walk(upstream, child["digest"], manifests, blobs, depth + 1)
    # image manifest -> config + layers
    cfg = doc.get("config")
    if isinstance(cfg, dict) and cfg.get("digest"):
        blobs.add(cfg["digest"])
    for layer in doc.get("layers") or []:
        if isinstance(layer, dict) and layer.get("digest"):
            blobs.add(layer["digest"])


@dataclass
class Marking:
    manifests: dict[str, set[str]] = field(default_factory=dict)
    blobs: dict[str, set[str]] = field(default_factory=dict)

    def protects_blob(self, upstream: str, digest: str) -> bool:
        return digest in self.blobs.get(upstream, set())

    def protects_manifest(self, upstream: str, digest: str) -> bool:
        return digest in self.manifests.get(upstream, set())


def mark(roots: list[TagRef] | None = None, strict: bool = True) -> Marking:
    """Build the referenced set from tag roots plus pinned digests.

    A pin is a root in its own right, which is what makes a pin retain the whole
    closure rather than just the manifest.
    """
    tags = list_tags() if roots is None else roots
    pins = load_pins(strict=strict)
    m = Marking()
    for t in tags:
        m.manifests.setdefault(t.upstream, set())
        m.blobs.setdefault(t.upstream, set())
        _walk(t.upstream, t.digest, m.manifests[t.upstream], m.blobs[t.upstream])
    # Pins given by digest are roots even if no tag points at them any more.
    for pin in pins:
        ref, sep, digest = pin.partition("@")
        if not sep or not ocistore.DIGEST_RE.match(digest):
            continue
        upstream = ref.split("/", 1)[0]
        m.manifests.setdefault(upstream, set())
        m.blobs.setdefault(upstream, set())
        _walk(upstream, digest, m.manifests[upstream], m.blobs[upstream])
    return m


def pinned_tag_keys(strict: bool = True) -> set[str]:
    """Pins expressed as `<upstream>/<repo>:<tag>`."""
    return {p for p in load_pins(strict=strict) if "@" not in p}


# ---------------------------------------------------------------------------
# sweeping
# ---------------------------------------------------------------------------


@dataclass
class OnDisk:
    upstream: str
    digest: str
    path: Path
    size: int
    last_used: float


def _enumerate(kind: str) -> list[OnDisk]:
    """Every blob (or manifest) on disk, with size and last-use."""
    out: list[OnDisk] = []
    r = ocistore.root()
    if not r.is_dir():
        return out
    for up in sorted(p for p in r.iterdir() if p.is_dir() and p.name != ".xhc"):
        base = up / kind / "sha256"
        if not base.is_dir():
            continue
        for dirpath, _dirs, files in os.walk(base):
            for fn in files:
                if fn.endswith(".meta") or fn.startswith(".") or ".incomplete" in fn:
                    continue
                full = Path(dirpath) / fn
                try:
                    st = full.stat()
                except OSError:
                    continue
                out.append(
                    OnDisk(
                        upstream=up.name,
                        digest=f"sha256:{fn}",
                        path=full,
                        size=st.st_size,
                        last_used=max(st.st_atime, st.st_mtime),
                    )
                )
    return out


def _unlink(path: Path) -> int:
    try:
        n = path.stat().st_size
    except OSError:
        return 0
    try:
        path.unlink()
    except OSError:
        return 0
    meta = path.with_suffix(".meta")
    if meta.exists():
        try:
            meta.unlink()
        except OSError:
            pass
    return n


def sweep(marking: Marking, dry_run: bool = False) -> dict:
    """Delete blobs and manifests outside the referenced set.

    Always safe: by construction nothing here is reachable from a tag or a pin.
    """
    freed = 0
    removed_blobs = removed_manifests = 0
    for item in _enumerate("blobs"):
        if marking.protects_blob(item.upstream, item.digest):
            continue
        freed += item.size if dry_run else _unlink(item.path)
        removed_blobs += 1
    for item in _enumerate("manifests"):
        if marking.protects_manifest(item.upstream, item.digest):
            continue
        freed += item.size if dry_run else _unlink(item.path)
        removed_manifests += 1
    return {"blobs": removed_blobs, "manifests": removed_manifests, "freed_bytes": freed}


def collect(target_free_bytes: int = 0, dry_run: bool = False) -> dict:
    """Full GC. Sweep what is unreferenced; if still over the high-water mark,
    drop least-recently-used tags and sweep again.

    Tags are dropped, never blobs directly -- eviction is top-down, because a
    blob is only safe to remove once nothing points at it. Pinned tags and
    retained orphans are never candidates, even if that means the target is not
    reached: running hot on disk is recoverable, and for an orphan the copy here
    is the only one left.
    """
    try:
        pins = pinned_tag_keys(strict=True)
        orphans = load_orphans(strict=True)
    except StateUnavailable as exc:
        # Same rule as the HF path (an internal issue): if we cannot tell what is
        # protected, we must not delete anything.
        log.error("REFUSING TO GC: %s -- cannot tell what is protected", exc)
        return {
            "refused": True,
            "reason": str(exc),
            "freed_bytes": 0,
            "blobs": 0,
            "manifests": 0,
            "tags_dropped": 0,
            "reached_goal": False,
        }

    protected_tags = set(pins)
    if settings.orphan_policy == "retain":
        protected_tags |= set(orphans)

    result = sweep(mark(strict=True), dry_run=dry_run)
    result.update({"refused": False, "tags_dropped": 0, "protected_tags": len(protected_tags)})

    cap = settings.docker_capacity_bytes
    if not cap:
        result["reached_goal"] = True
        return result

    high = int(cap * settings.high_water)
    low = int(cap * settings.low_water)
    used = ocistore.stats(force=True)["bytes"]
    if used <= high and used + target_free_bytes <= cap:
        result["reached_goal"] = True
        return result

    goal = min(low, cap - target_free_bytes)
    candidates = sorted(
        (t for t in list_tags() if t.key not in protected_tags),
        key=lambda t: t.last_used,
    )
    for tag in candidates:
        if used <= goal:
            break
        if not dry_run:
            try:
                tag.path.unlink()
            except OSError:
                continue
        result["tags_dropped"] += 1
        again = sweep(mark(strict=True), dry_run=dry_run)
        for k in ("blobs", "manifests", "freed_bytes"):
            result[k] += again[k]
        used = ocistore.stats(force=True)["bytes"] if not dry_run else used - again["freed_bytes"]

    result["reached_goal"] = used <= goal
    if not result["reached_goal"]:
        log.warning(
            "docker GC could not reach %d bytes: %d protected tags are unevictable",
            goal,
            len(protected_tags),
        )
    return result


async def gc_loop() -> None:
    """Periodic mark-and-sweep, on the same interval as the HF evictor.

    Deliberately quiet: it logs only when it removes something or refuses, so a
    healthy cache does not fill the log with zeros -- and a refusal is therefore
    visible rather than buried.
    """
    import asyncio

    while True:
        try:
            await asyncio.sleep(settings.evict_interval_s)
            if not settings.docker_enabled:
                continue
            res = await asyncio.to_thread(collect)
            if res.get("refused"):
                log.error("docker GC refused: %s", res.get("reason"))
            elif res["freed_bytes"] or res["tags_dropped"]:
                log.info(
                    "docker GC: freed %d bytes (%d blobs, %d manifests, %d tags dropped)",
                    res["freed_bytes"],
                    res["blobs"],
                    res["manifests"],
                    res["tags_dropped"],
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("docker GC loop error")
