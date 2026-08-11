"""Cache the small, stable dataset metadata endpoints.

Scope correction from the spec: `/api/datasets/{id}/splits` does not exist on
huggingface.co -- splits, rows and first-rows live on
`datasets-server.huggingface.co`, a different host that clients contact directly
and which therefore never passes through Muninn. Nothing here can help with
those without proxying that host as well, which is a separate feature.

What does traverse us, and is worth holding:

  /api/datasets/{id}/parquet    -- the auto-converted parquet file listing
  /api/datasets/{id}/croissant  -- the ML-metadata description

Both are small, change only when the dataset changes, and are exactly what an
orphaned dataset loses when upstream 404s. `/rows` is deliberately never cached
even if it were reachable: it is query-dependent and unbounded, and caching it
badly means serving wrong rows.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from .config import settings

log = logging.getLogger("xhc.viewer")

_DIR = "viewer"


def cacheable_endpoints() -> set[str]:
    raw = settings.viewer_endpoints or ""
    return {e.strip() for e in raw.split(",") if e.strip()}


def parse_path(full_path: str) -> tuple[str, str, str] | None:
    """Match `api/datasets/{repo}/{endpoint}[/...]` for cacheable endpoints.

    Returns (repo_id, endpoint, cache_key_suffix). Sub-paths are kept in the key
    so `/parquet/default/train` is cached separately from `/parquet`.
    """
    prefix = "api/datasets/"
    if not full_path.startswith(prefix):
        return None
    tail = full_path[len(prefix) :]
    parts = tail.split("/")
    # {org}/{name}/{endpoint}[...] or {name}/{endpoint}[...]
    for split_at in (2, 1):
        if len(parts) > split_at:
            repo_id = "/".join(parts[:split_at])
            endpoint = parts[split_at]
            if endpoint in cacheable_endpoints():
                return repo_id, endpoint, "/".join(parts[split_at:])
    return None


def _path_for(repo_id: str, key_suffix: str) -> Path:
    safe = f"{repo_id}/{key_suffix}".replace("/", "__")
    d = Path(settings.cache_dir) / ".xhc" / _DIR
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{safe}.json"


def load(repo_id: str, key_suffix: str) -> dict | None:
    p = _path_for(repo_id, key_suffix)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        log.warning("viewer cache entry unreadable: %s", p, exc_info=True)
        return None


def store(repo_id: str, key_suffix: str, body: bytes, content_type: str | None) -> None:
    p = _path_for(repo_id, key_suffix)
    tmp = p.with_suffix(".json.tmp")
    try:
        payload = {
            "fetched_at": time.time(),
            "content_type": content_type or "application/json",
            "body": body.decode("utf-8", errors="replace"),
        }
        tmp.write_text(json.dumps(payload))
        tmp.replace(p)
    except OSError:
        log.warning("could not persist viewer cache entry %s", p, exc_info=True)


def is_fresh(entry: dict) -> bool:
    ttl = settings.viewer_cache_ttl_s
    if ttl <= 0:
        return False
    return (time.time() - entry.get("fetched_at", 0)) < ttl


def stats() -> dict:
    d = Path(settings.cache_dir) / ".xhc" / _DIR
    if not d.is_dir():
        return {"entries": 0, "bytes": 0}
    files = list(d.glob("*.json"))
    return {"entries": len(files), "bytes": sum(f.stat().st_size for f in files)}


def clear() -> int:
    d = Path(settings.cache_dir) / ".xhc" / _DIR
    if not d.is_dir():
        return 0
    n = 0
    for f in d.glob("*.json"):
        f.unlink(missing_ok=True)
        n += 1
    return n
