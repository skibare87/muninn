"""Management API, mounted under /_cache.

Everything here is about the centralised-model-management half of the job:
declare what the fleet needs, pin it so eviction cannot touch it, and watch the
WAN ingest go. Prewarm is the primary path in practice -- if you know your model
set in advance, edge nodes should only ever see cache hits.
"""

from __future__ import annotations

import asyncio
import platform
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from . import cachefs, orphans
from .config import XET_ENV_KEYS, settings
from .jobs import manager

router = APIRouter(prefix="/_cache", tags=["manage"])

RepoType = Literal["model", "dataset", "space"]


async def require_manage_token(authorization: str | None = Header(default=None)) -> None:
    if not settings.manage_token:
        return
    expected = f"Bearer {settings.manage_token}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="invalid or missing management token")


class PrewarmRequest(BaseModel):
    repo_id: str
    revision: str = "main"
    repo_type: RepoType = "model"
    allow_patterns: list[str] | None = Field(
        default=None,
        description="glob patterns, e.g. ['*.safetensors','*.json'] to skip .bin duplicates",
    )
    pin: bool = Field(default=False, description="pin the repo as part of prewarming")


class RepoRef(BaseModel):
    repo_id: str
    repo_type: RepoType = "model"


class EvictRequest(BaseModel):
    target_free_bytes: int = 0


class DeleteRequest(BaseModel):
    repo_id: str
    repo_type: RepoType = "model"
    revision: str | None = Field(
        default=None,
        description="delete only this revision (commit sha or ref); omit to delete the whole repo",
    )


@router.get("/status", dependencies=[Depends(require_manage_token)])
async def status() -> dict:
    view = await cachefs.get_view()
    disk = cachefs.disk_stats()
    jobs = manager.list()
    active = [j for j in jobs if j.state in ("pending", "running")]
    return {
        "cache_dir": settings.cache_dir,
        "upstream": settings.upstream,
        "hf_token_present": bool(settings.hf_token),
        "miss_policy": settings.miss_policy,
        "block_client_xet": settings.block_client_xet,
        "ingest_concurrency": settings.ingest_concurrency,
        "disk": disk,
        "cache": {
            "size_on_disk": view.size_on_disk,
            "repo_count": len(view.repos),
            "pct_of_capacity": round(100 * view.size_on_disk / disk["capacity"], 2)
            if disk["capacity"]
            else None,
            "high_water": settings.high_water,
            "low_water": settings.low_water,
            "nb_files": view.nb_files,
            "scanned_at": view.scanned_at,
            # Scan cost tracks file count, not bytes. If this creeps up, that is
            # the thing to watch -- see README "Scaling".
            "scan_duration_s": view.scan_duration_s,
            "scan_ttl_s": view.ttl_s,
            "warnings": view.warnings[:20],
        },
        "jobs": {"active": len(active), "total_tracked": len(jobs)},
        "orphans": {
            "policy": settings.orphan_policy,
            "count": len(_orphans := cachefs.load_orphans()),
            "retained_bytes": sum(v.get("size_on_disk", 0) for v in _orphans.values()),
            "last_check": orphans.last_check(),
        },
        # Surfaced because a silently-unset Xet var is the single most likely
        # cause of "why is my WAN ingest doing 3 MB/s".
        "xet_env": {k: settings.xet_env.get(k) for k in XET_ENV_KEYS},
        "python": platform.python_version(),
    }


@router.get("/repos", dependencies=[Depends(require_manage_token)])
async def list_repos(refresh: bool = False) -> dict:
    view = await cachefs.get_view(force=refresh)
    return {
        "scanned_at": view.scanned_at,
        "size_on_disk": view.size_on_disk,
        "repos": [
            {
                "repo_id": r.repo_id,
                "repo_type": r.repo_type,
                "key": r.key,
                "size_on_disk": r.size_on_disk,
                "nb_files": r.nb_files,
                "last_accessed": r.last_accessed,
                "pinned": r.pinned,
                "revisions": r.revisions,
            }
            for r in view.repos
        ],
    }


@router.post("/prewarm", dependencies=[Depends(require_manage_token)])
async def prewarm(req: PrewarmRequest) -> dict:
    if req.pin:
        pins = cachefs.load_pins()
        pins.add(cachefs.repo_key(req.repo_type, req.repo_id))
        cachefs.save_pins(pins)
    job = await manager.ensure_snapshot(
        req.repo_type, req.repo_id, req.revision, req.allow_patterns
    )
    return {"job": job.to_dict(), "pinned": req.pin}


@router.get("/jobs", dependencies=[Depends(require_manage_token)])
async def list_jobs() -> dict:
    return {"jobs": [j.to_dict() for j in manager.list()]}


@router.get("/jobs/{job_id}", dependencies=[Depends(require_manage_token)])
async def get_job(job_id: str) -> dict:
    job = manager.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="no such job")
    return job.to_dict()


@router.get("/pins", dependencies=[Depends(require_manage_token)])
async def list_pins() -> dict:
    return {"pins": sorted(cachefs.load_pins())}


@router.post("/pins", dependencies=[Depends(require_manage_token)])
async def add_pin(req: RepoRef) -> dict:
    pins = cachefs.load_pins()
    pins.add(cachefs.repo_key(req.repo_type, req.repo_id))
    cachefs.save_pins(pins)
    return {"pins": sorted(pins)}


@router.delete("/pins", dependencies=[Depends(require_manage_token)])
async def remove_pin(req: RepoRef) -> dict:
    pins = cachefs.load_pins()
    pins.discard(cachefs.repo_key(req.repo_type, req.repo_id))
    cachefs.save_pins(pins)
    return {"pins": sorted(pins)}


@router.get("/orphans", dependencies=[Depends(require_manage_token)])
async def list_orphans() -> dict:
    """Cached repos whose upstream has gone away.

    Under XHC_ORPHAN_POLICY=retain these are exempt from eviction: the copy
    here is the only copy, so evicting one cannot be undone.
    """
    o = cachefs.load_orphans()
    return {
        "policy": settings.orphan_policy,
        "check_interval_s": settings.orphan_check_interval_s,
        "last_check": orphans.last_check(),
        "count": len(o),
        "retained_bytes": sum(v.get("size_on_disk", 0) for v in o.values()),
        "orphans": [
            {
                "key": k,
                "reason": v.get("reason"),
                "since": v.get("since"),
                "last_checked": v.get("last_checked"),
                "size_on_disk": v.get("size_on_disk"),
            }
            for k, v in sorted(o.items())
        ],
    }


@router.post("/orphans/check", dependencies=[Depends(require_manage_token)])
async def check_orphans() -> dict:
    """Run an upstream liveness sweep now instead of waiting for the timer."""
    return await orphans.check_all(force_rescan=True)


@router.delete("/orphans", dependencies=[Depends(require_manage_token)])
async def forget_orphan(req: RepoRef) -> dict:
    """Drop a repo's orphan mark, making it evictable again.

    Use when you have deliberately decided an archived copy is no longer worth
    keeping -- the next sweep will re-mark it if upstream is still gone.
    """
    o = cachefs.load_orphans()
    key = cachefs.repo_key(req.repo_type, req.repo_id)
    existed = o.pop(key, None) is not None
    cachefs.save_orphans(o)
    return {"forgotten": existed, "key": key, "remaining": len(o)}


@router.post("/evict", dependencies=[Depends(require_manage_token)])
async def run_evict(req: EvictRequest) -> dict:
    return await cachefs.evict(req.target_free_bytes)


@router.delete("/repos", dependencies=[Depends(require_manage_token)])
async def delete_repo(req: DeleteRequest) -> dict:
    """Forcibly drop a cached repo, or one revision of it.

    This is the escape hatch for retained orphans: retention makes them
    unevictable by the LRU sweep, so releasing the space has to be a deliberate
    act. Deleting also clears any orphan mark, so retained_bytes cannot go on
    claiming space that is no longer held.

    Pins remain absolute -- there is deliberately no force flag. Unpinning first
    is the acceptance step that stops a pinned model being destroyed by one
    mistyped call.
    """
    key = cachefs.repo_key(req.repo_type, req.repo_id)
    if key in cachefs.load_pins():
        raise HTTPException(
            status_code=409,
            detail=f"{key} is pinned; DELETE /_cache/pins first (pins are absolute by design)",
        )

    if req.revision:
        result = await asyncio.to_thread(
            cachefs.delete_revision_sync, req.repo_type, req.repo_id, req.revision
        )
        if not result["deleted"]:
            raise HTTPException(
                status_code=404, detail=f"revision {req.revision} of {key} not in cache"
            )
    else:
        result = await asyncio.to_thread(cachefs.delete_repo_sync, req.repo_type, req.repo_id)
        if not result["deleted"]:
            raise HTTPException(status_code=404, detail=f"{key} not in cache")

    cachefs.invalidate_view()
    result["key"] = key
    result["orphan_mark_cleared"] = not cachefs.repo_is_cached(req.repo_type, req.repo_id)
    return result
