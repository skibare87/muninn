"""Management API for the OCI cache, mounted under /_cache/docker.

Phase 3. Until this existed the cache could not be operated: garbage collection
ran only on its interval with no way to force it, and a pin could only be set by
hand-editing `$XHC_DOCKER_DIR/.xhc/pins.json` on the host. Both were real rough
edges rather than design choices, and both are the kind of gap that pushes an
operator into editing state files under a live process.

Prewarm is the endpoint that gets used daily: pull an image ahead of a rollout
so the fleet only ever sees hits. It is fire-and-forget -- it returns a job and
callers poll it, so nobody has to hold an HTTP connection open across a 30 GB
pull, and nobody needs a human relaying the call.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from . import metrics, ocicompat, ocigc, ocistore, policy, registry
from .cachefs import StateUnavailable
from .manage import require_manage_token

log = logging.getLogger("xhc.ocimanage")

router = APIRouter(prefix="/_cache/docker", tags=["docker"])

_jobs: dict = {}  # id -> PrewarmJob, defined below
_tasks: set[asyncio.Task] = set()


@dataclass
class PrewarmJob:
    id: str
    image: str
    state: str = "pending"
    error: str | None = None
    blobs_total: int = 0
    blobs_done: int = 0
    bytes_done: int = 0
    done: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def as_dict(self) -> dict:
        return {
            "id": self.id, "image": self.image, "state": self.state, "error": self.error,
            "blobs_total": self.blobs_total, "blobs_done": self.blobs_done,
            "bytes_done": self.bytes_done,
        }


class PrewarmRequest(BaseModel):
    image: str = Field(description="e.g. ghcr.io/org/img:1.2.3 or …@sha256:…")
    pin: bool = Field(default=False, description="pin the image and its whole blob closure")


class PinRequest(BaseModel):
    image: str


class EvictRequest(BaseModel):
    image: str = Field(description="drops the tag; blobs go on the next sweep if unreferenced")


def _split(image: str) -> tuple[str, str]:
    """Split `name:tag` or `name@sha256:…` into (name, reference)."""
    if "@" in image:
        name, _, ref = image.partition("@")
        return name, ref
    head, sep, tail = image.rpartition(":")
    if sep and "/" not in tail:
        return head, tail
    return image, "latest"


async def _run_prewarm(job: PrewarmJob, ref: registry.Ref, reference: str, pin: bool) -> None:
    try:
        job.state = "running"
        accept = ",".join([
            "application/vnd.oci.image.index.v1+json",
            "application/vnd.docker.distribution.manifest.list.v2+json",
            "application/vnd.oci.image.manifest.v1+json",
            "application/vnd.docker.distribution.manifest.v2+json",
        ])
        roots = [reference]
        seen: set[str] = set()
        blobs: list[str] = []
        while roots:
            r = roots.pop()
            if r in seen:
                continue
            seen.add(r)
            resp = await registry.get(ref, f"manifests/{r}", {"accept": accept})
            metrics.record_docker_upstream(ref.upstream, resp.status_code)
            if resp.status_code != 200:
                raise RuntimeError(f"upstream {resp.status_code} for {r}")
            body = resp.content
            media = resp.headers.get("content-type") or "application/vnd.oci.image.manifest.v1+json"
            digest = resp.headers.get("docker-content-digest") or ocistore.compute_digest(body)
            ocistore.store_manifest(ref.upstream, digest, body, media)
            if not ocistore.DIGEST_RE.match(r):
                ocistore.write_tag(ref.upstream, ref.repo, r,
                                   ocistore.accept_fingerprint(accept), digest, media)
            doc = json.loads(body)
            for child in doc.get("manifests") or []:
                if child.get("digest"):
                    roots.append(child["digest"])
            cfg = doc.get("config")
            if isinstance(cfg, dict) and cfg.get("digest"):
                blobs.append(cfg["digest"])
            for layer in doc.get("layers") or []:
                if layer.get("digest"):
                    blobs.append(layer["digest"])

        blobs = list(dict.fromkeys(blobs))
        job.blobs_total = len(blobs)
        for d in blobs:
            if ocistore.blob_path(ref.upstream, d).is_file():
                job.blobs_done += 1
                continue
            bj = await ocicompat._ensure_blob(ref, d)
            await bj.done.wait()
            if bj.state != "done":
                raise RuntimeError(f"blob {d} failed: {bj.error}")
            job.blobs_done += 1
            job.bytes_done += bj.size or 0

        if pin:
            pins = ocigc.load_pins()
            pins.add(f"{ref.upstream}/{ref.repo}"
                     + (f"@{reference}" if ocistore.DIGEST_RE.match(reference) else f":{reference}"))
            ocigc.save_pins(pins)
        job.state = "done"
    except asyncio.CancelledError:
        job.state = "error"
        job.error = "cancelled"
        raise
    except Exception as exc:  # noqa: BLE001 - reported on the job, not swallowed
        job.state = "error"
        job.error = f"{type(exc).__name__}: {exc}"
        log.warning("docker prewarm %s failed: %s", job.id, exc)
    finally:
        job.done.set()


@router.post("/prewarm", dependencies=[Depends(require_manage_token)])
async def prewarm(req: PrewarmRequest) -> dict:
    """Pull an image and its whole closure ahead of a rollout.

    Fire-and-forget: returns a job id. Pass a DIGEST rather than a tag for
    anything you intend to reproduce -- a tag can move mid-pull and assemble a
    tree from two commits.
    """
    name, reference = _split(req.image)
    try:
        ref = registry.resolve(name)
    except registry.ResolveError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    verdict = policy.check_docker(ref.upstream, ref.repo)
    if not verdict.allowed:
        raise HTTPException(status_code=403, detail=f"blocked by policy: {verdict.reason}")
    job = PrewarmJob(id=uuid.uuid4().hex[:12], image=req.image)
    _jobs[job.id] = job
    task = asyncio.create_task(_run_prewarm(job, ref, reference, req.pin))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return {"job": job.as_dict()}


@router.get("/prewarm/{job_id}", dependencies=[Depends(require_manage_token)])
async def prewarm_status(job_id: str) -> dict:
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="no such job")
    return {"job": job.as_dict()}


@router.get("/images", dependencies=[Depends(require_manage_token)])
async def list_images() -> dict:
    """Cached tags with their pin and orphan state."""
    try:
        pins = ocigc.pinned_tag_keys(strict=True)
        orphans = ocigc.load_orphans(strict=True)
    except StateUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    out = []
    for t in ocigc.list_tags():
        out.append({
            "image": t.key, "digest": t.digest, "upstream": t.upstream,
            "pinned": t.key in pins, "orphan": t.key in orphans,
            "last_used": t.last_used,
        })
    stats = ocistore.stats(force=True)
    return {"images": sorted(out, key=lambda x: x["image"]), "stats": stats}


@router.get("/pins", dependencies=[Depends(require_manage_token)])
async def get_pins() -> dict:
    try:
        return {"pins": sorted(ocigc.load_pins(strict=True))}
    except StateUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/pins", dependencies=[Depends(require_manage_token)])
async def add_pin(req: PinRequest) -> dict:
    """Pin an image. The pin covers its whole blob closure -- a pin that kept
    the manifest but let its layers go would look intact until someone pulled."""
    try:
        pins = ocigc.load_pins(strict=True)
    except StateUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    pins.add(req.image)
    ocigc.save_pins(pins)
    return {"pins": sorted(pins)}


@router.delete("/pins", dependencies=[Depends(require_manage_token)])
async def remove_pin(req: PinRequest) -> dict:
    try:
        pins = ocigc.load_pins(strict=True)
    except StateUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    pins.discard(req.image)
    ocigc.save_pins(pins)
    return {"pins": sorted(pins)}


@router.delete("/images", dependencies=[Depends(require_manage_token)])
async def evict_image(req: EvictRequest) -> dict:
    """Drop a tag. Its blobs are removed by the next sweep if nothing else
    references them -- eviction is top-down and never deletes a blob directly."""
    try:
        pins = ocigc.pinned_tag_keys(strict=True)
    except StateUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if req.image in pins:
        raise HTTPException(
            status_code=409,
            detail=f"{req.image} is pinned; DELETE /_cache/docker/pins first",
        )
    dropped = [t.path for t in ocigc.list_tags() if t.key == req.image]
    if not dropped:
        raise HTTPException(status_code=404, detail=f"{req.image} is not cached")
    for p in dropped:
        p.unlink(missing_ok=True)
    return {"dropped": req.image, "tags_removed": len(dropped)}


@router.post("/gc", dependencies=[Depends(require_manage_token)])
async def run_gc(dry_run: bool = False) -> dict:
    """Run mark-and-sweep now rather than waiting for the interval."""
    return await asyncio.to_thread(ocigc.collect, 0, dry_run)
