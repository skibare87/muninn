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

from . import cachefs
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


@router.post("/evict", dependencies=[Depends(require_manage_token)])
async def run_evict(req: EvictRequest) -> dict:
    return await cachefs.evict(req.target_free_bytes)


@router.delete("/repos", dependencies=[Depends(require_manage_token)])
async def delete_repo(req: RepoRef) -> dict:
    if cachefs.repo_key(req.repo_type, req.repo_id) in cachefs.load_pins():
        raise HTTPException(status_code=409, detail="repo is pinned; unpin before deleting")
    result = await asyncio.to_thread(cachefs.delete_repo_sync, req.repo_type, req.repo_id)
    if not result["deleted"]:
        raise HTTPException(status_code=404, detail="repo not in cache")
    cachefs.invalidate_view()
    return result
