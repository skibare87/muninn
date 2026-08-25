"""On-disk layout for the OCI cache, and the digest checking that guards it.

A separate root from the HF cache so `scan_cache_dir` never sees it, the two
layouts cannot confuse each other, and image churn can never evict models.

    $XHC_DOCKER_DIR/
      <upstream>/
        blobs/sha256/<ab>/<digest>              content-addressed, immutable
        manifests/sha256/<ab>/<digest>          VERBATIM bytes, never re-encoded
        manifests/sha256/<ab>/<digest>.meta     media type + size
        tags/<repo>/<tag>@<accept>.json         {digest, media_type, checked_at}

Two properties are load-bearing.

**Blobs are content-addressed, so correctness is checkable rather than assumed.**
This is strictly better than the HF side, where an ETag is taken on trust. We
verify on ingest and never commit unverified bytes.

**Manifests are stored and served as the exact bytes received.** The digest is
computed over those bytes, so any re-serialization -- key order, whitespace,
a trailing newline -- changes the digest and breaks every pull-by-digest and
every signature check. This is the classic way these proxies get subtly broken,
and the reason nothing here ever calls json.dumps() on a manifest.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from .config import settings

DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SAFE = re.compile(r"[^A-Za-z0-9._-]")

_stats_cache: tuple[float, dict] | None = None
_STATS_TTL = 60.0


class DigestMismatch(Exception):
    """Raised when received bytes do not hash to the digest that named them."""


def root() -> Path:
    return Path(settings.docker_dir)


def _safe(part: str) -> str:
    """Flatten a path component so a crafted repo name cannot escape the root."""
    return _SAFE.sub("_", part)


def upstream_root(upstream: str) -> Path:
    return root() / _safe(upstream)


def _sharded(base: Path, digest: str) -> Path:
    # Two-character shard: v0.1.0 benchmarking showed scan cost tracks FILE
    # COUNT, and a busy registry cache reaches six figures of blobs. A flat
    # directory would make every sweep painful.
    hexpart = digest.split(":", 1)[1]
    return base / "sha256" / hexpart[:2] / hexpart


def blob_path(upstream: str, digest: str) -> Path:
    return _sharded(upstream_root(upstream) / "blobs", digest)


def manifest_path(upstream: str, digest: str) -> Path:
    return _sharded(upstream_root(upstream) / "manifests", digest)


def manifest_meta_path(upstream: str, digest: str) -> Path:
    return manifest_path(upstream, digest).with_suffix(".meta")


def accept_fingerprint(accept: str | None) -> str:
    """Normalise an Accept header into a short stable token.

    The same tag legitimately returns different manifests depending on what the
    client accepts -- an OCI index, a Docker manifest list, or a single-platform
    manifest. So the tag->digest mapping is keyed on the accept set as well as
    the tag. Manifests themselves are stored by digest, where no ambiguity
    exists.
    """
    types = sorted(
        t.split(";")[0].strip().lower() for t in (accept or "").split(",") if t.strip()
    )
    if not types:
        return "default"
    return hashlib.sha256(",".join(types).encode()).hexdigest()[:12]


def tag_path(upstream: str, repo: str, tag: str, accept_fp: str) -> Path:
    d = upstream_root(upstream) / "tags" / Path(*[_safe(p) for p in repo.split("/")])
    return d / f"{_safe(tag)}@{accept_fp}.json"


# -- digest verification ----------------------------------------------------


def compute_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def verify(data: bytes, digest: str) -> None:
    got = compute_digest(data)
    if got != digest:
        raise DigestMismatch(f"expected {digest}, computed {got}")


# -- atomic writes ----------------------------------------------------------


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".part{os.getpid()}")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def store_manifest(upstream: str, digest: str, body: bytes, media_type: str) -> None:
    """Persist manifest bytes verbatim after checking they match their digest."""
    verify(body, digest)
    _atomic_write(manifest_path(upstream, digest), body)
    _atomic_write(
        manifest_meta_path(upstream, digest),
        json.dumps(
            {"media_type": media_type, "size": len(body), "fetched_at": time.time()}
        ).encode(),
    )


@dataclass
class StoredManifest:
    body: bytes
    media_type: str
    digest: str


def load_manifest(upstream: str, digest: str) -> StoredManifest | None:
    p = manifest_path(upstream, digest)
    if not p.is_file():
        return None
    meta_p = manifest_meta_path(upstream, digest)
    media = "application/vnd.oci.image.manifest.v1+json"
    if meta_p.is_file():
        try:
            media = json.loads(meta_p.read_text()).get("media_type") or media
        except ValueError:
            pass
    return StoredManifest(body=p.read_bytes(), media_type=media, digest=digest)


# -- tag -> digest ----------------------------------------------------------


def read_tag(upstream: str, repo: str, tag: str, accept_fp: str) -> dict | None:
    p = tag_path(upstream, repo, tag, accept_fp)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text())
    except ValueError:
        return None


def write_tag(
    upstream: str, repo: str, tag: str, accept_fp: str, digest: str, media_type: str
) -> None:
    _atomic_write(
        tag_path(upstream, repo, tag, accept_fp),
        json.dumps(
            {
                "digest": digest,
                "media_type": media_type,
                "checked_at": time.time(),
            }
        ).encode(),
    )


def touch_tag(upstream: str, repo: str, tag: str, accept_fp: str) -> None:
    """Record that a tag was revalidated and found unchanged, so a fail-open
    revalidation does not re-hit upstream on every single request."""
    entry = read_tag(upstream, repo, tag, accept_fp)
    if entry:
        entry["checked_at"] = time.time()
        _atomic_write(tag_path(upstream, repo, tag, accept_fp), json.dumps(entry).encode())


def tag_is_fresh(entry: dict) -> bool:
    ttl = settings.docker_tag_ttl_s
    if ttl <= 0:
        # 0 disables revalidation entirely: mutable tags then serve whatever was
        # first cached, forever, which is what a pure archive wants.
        return True
    return (time.time() - float(entry.get("checked_at") or 0)) < ttl


# -- accounting -------------------------------------------------------------


def stats(force: bool = False) -> dict:
    """Blob/manifest counts and bytes. Cached briefly: this walks the tree and
    /metrics can be scraped often."""
    global _stats_cache  # noqa: PLW0603 - module-level cache
    now = time.time()
    if not force and _stats_cache and now - _stats_cache[0] < _STATS_TTL:
        return _stats_cache[1]
    blobs = manifests = 0
    blob_bytes = 0
    r = root()
    if r.is_dir():
        for up in r.iterdir():
            if not up.is_dir():
                continue
            for dirpath, _dirnames, filenames in os.walk(up / "blobs"):
                for fn in filenames:
                    if fn.startswith("."):
                        continue
                    try:
                        blob_bytes += os.stat(os.path.join(dirpath, fn)).st_size
                        blobs += 1
                    except OSError:
                        pass
            for _dirpath, _dirnames, filenames in os.walk(up / "manifests"):
                manifests += sum(1 for fn in filenames if not fn.endswith(".meta"))
    out = {"blobs": blobs, "manifests": manifests, "bytes": blob_bytes}
    _stats_cache = (now, out)
    return out


def reset_stats_cache() -> None:
    global _stats_cache  # noqa: PLW0603 - module-level cache
    _stats_cache = None
