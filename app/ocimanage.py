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

Prewarm jobs live in a durable, bounded ledger (`prewarm.json` in the OCI state
dir), the same machinery as the HF job table (app/ledger.py). Before it, the
table was a dict in memory: unbounded, and gone on every restart, so a poller
holding a job id across a redeploy got "no such job" and could not tell a
finished pull from a dead one.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import (
    ledger,
    metrics,
    ocicompat,
    ocigc,
    ocipush,
    ocistore,
    policy,
    registry,
    shutdown,
    statedir,
)
from .cachefs import StateUnavailable
from .config import settings
from .managegate import ManageRoute
from .shutdown import reraise_if_cancelled

log = logging.getLogger("xhc.ocimanage")

# Gated as a whole by ManageRoute (managegate.py), like every /_cache router.
router = APIRouter(prefix="/_cache/docker", tags=["docker"], route_class=ManageRoute)

# THE STATE MACHINE, the same one the HF jobs use:
#
#   pending -> running -> verifying -> done
#                     \------+-----> error
#   pending | running | verifying  --(restart or shutdown)-->  interrupted
#
# WHAT `done` MEANS. Every blob this job fetched was hashed as it landed and
# renamed into place only on a match (ocicompat._write_blob), and every manifest
# was checked against its digest before it was stored -- against the digest the
# job ASKED for when it asked by digest, not only the one the upstream's header
# named (see _fetch_closure). `verifying` then confirms the whole closure is on
# disk at its content address, and only then is the job `done`. That last step
# is not decoration: a closure fetched by digest and not pinned is referenced by
# no tag, so a GC sweep that runs mid-prewarm collects the early layers, and
# without the check the job would report `done` over an image that is no longer
# there. Blobs already present are NOT re-hashed (they only ever arrive by a
# verified rename), and the job says how many there were.
LEDGER_FILE = "prewarm.json"
_HISTORY_LIMIT = 50  # finished prewarm jobs kept, as for HF snapshot prewarms
_RETENTION_S = ledger.RETENTION_S

_ACCEPT = ",".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])


@dataclass
class PrewarmJob:
    id: str
    image: str
    pin: bool = False
    kind: str = "prewarm"
    state: str = "pending"
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    manifests_done: int = 0
    blobs_total: int = 0
    blobs_done: int = 0
    # Of blobs_done, how many were already on disk and not fetched this run.
    # A resumed prewarm shows the earlier run's work here rather than re-doing it.
    blobs_present: int = 0
    bytes_done: int = 0
    # The interrupted job this one picked up from, if it was re-submitted.
    resumes: str | None = None
    # When the ledger last wrote this job: the last moment its state and
    # progress are known to have been true.
    updated_at: float | None = None
    interrupted_at: float | None = None
    # Set on a job loaded from the ledger by a later process.
    restored: bool = False
    done: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    @property
    def key(self) -> str:
        # The pin flag is part of the identity: joining an unpinned prewarm
        # from a request that asked for a pin would drop the pin silently.
        return f"{self.image}|pin={int(self.pin)}"

    def as_dict(self) -> dict:
        d = {
            "id": self.id, "image": self.image, "pin": self.pin, "state": self.state,
            "error": self.error, "created_at": self.created_at,
            "started_at": self.started_at, "finished_at": self.finished_at,
            "updated_at": self.updated_at, "interrupted_at": self.interrupted_at,
            "manifests_done": self.manifests_done,
            "blobs_total": self.blobs_total, "blobs_done": self.blobs_done,
            "blobs_present": self.blobs_present, "bytes_done": self.bytes_done,
            "resumes": self.resumes,
        }
        if self.state == "interrupted":
            d["note"] = (
                "the process running this prewarm stopped before it finished; "
                "the counts are its last recorded progress. Nothing resumes it "
                "automatically: POST the same prewarm again, which skips every "
                "blob already cached."
            )
        return d

    _RECORD_FIELDS = (
        "id", "image", "pin", "kind", "state", "error", "created_at", "started_at",
        "finished_at", "manifests_done", "blobs_total", "blobs_done",
        "blobs_present", "bytes_done", "resumes", "interrupted_at",
    )

    def to_record(self, now: float) -> dict:
        rec = {k: getattr(self, k) for k in self._RECORD_FIELDS}
        rec["updated_at"] = self.updated_at if self.restored else now
        return rec

    @classmethod
    def from_record(cls, rec: dict) -> PrewarmJob:
        if not isinstance(rec, dict):
            raise ValueError("job record is not an object")
        kwargs = {k: rec[k] for k in cls._RECORD_FIELDS if k in rec}
        for required in ("id", "image", "state"):
            if not isinstance(kwargs.get(required), str):
                raise ValueError(f"prewarm record missing {required}")
        kwargs["kind"] = "prewarm"
        job = cls(**kwargs)
        job.restored = True
        job.updated_at = rec.get("updated_at")
        job.done.set()
        return job


class PrewarmManager(ledger.LedgeredJobs):
    """OCI prewarm jobs over the shared durable, bounded ledger."""

    LEDGER_FILE = LEDGER_FILE
    LEDGER_LABEL = "OCI prewarm ledger"
    log = log

    def _ledger_path(self) -> Path:
        # Not in statedir.OCI_FILES, for the reason jobs.json is not in
        # HF_FILES: a failed eager migration stops the boot, which is right for
        # pins and wrong for history. load_ledger catches the lazy one.
        return statedir.oci_file(LEDGER_FILE)

    def _job_from_record(self, rec: dict) -> PrewarmJob:
        return PrewarmJob.from_record(rec)

    def _kind_limits(self) -> dict[str, int]:
        return {"prewarm": _HISTORY_LIMIT}

    def _retention_s(self) -> float:
        return _RETENTION_S

    def submit(self, image: str, ref: registry.Ref, reference: str, pin: bool) -> PrewarmJob:
        """Start a prewarm, or join the one already running for the same image.

        No await between the lookup and the insert, so two requests racing
        cannot both start one.
        """
        job = PrewarmJob(id=uuid.uuid4().hex[:12], image=image, pin=pin)
        existing = self._active.get(job.key)
        if existing is not None:
            return existing
        # Re-submitting after an interruption IS the resume. Blobs are
        # content-addressed and only ever land by a verified rename, so the
        # new job skips everything the old one finished; this just says which
        # job it carries on from.
        for old in reversed(self._history):
            if old.key == job.key and old.state == "interrupted":
                job.resumes = old.id
                break
        self._active[job.key] = job
        self._by_id[job.id] = job
        self._track(asyncio.create_task(self._run(job, ref, reference)))
        self._persist(transition=True)
        self._ensure_progress_loop()
        return job

    async def _run(self, job: PrewarmJob, ref: registry.Ref, reference: str) -> None:
        stopped = False
        try:
            job.state = "running"
            job.started_at = time.time()
            self._persist(transition=True)
            manifests, blobs = await _fetch_closure(job, ref, reference)
            job.blobs_total = len(blobs)
            self._persist(transition=True)
            for d in blobs:
                # A cancel swallowed inside httpx/anyio (app/shutdown.py) must
                # not let this loop open the next layer's upstream request.
                reraise_if_cancelled()
                if ocistore.blob_path(ref.upstream, d).is_file():
                    job.blobs_done += 1
                    job.blobs_present += 1
                    continue
                bj = await ocicompat._ensure_blob(ref, d)
                await bj.done.wait()
                if bj.state != "done":
                    raise RuntimeError(f"blob {d} failed: {bj.error}")
                job.blobs_done += 1
                job.bytes_done += bj.size or 0

            if job.pin:
                pins = ocigc.load_pins()
                pins.add(f"{ref.upstream}/{ref.repo}"
                         + (f"@{reference}" if ocistore.DIGEST_RE.match(reference)
                            else f":{reference}"))
                ocigc.save_pins(pins)

            job.state = "verifying"
            self._persist(transition=True)
            missing = _missing_from_disk(ref.upstream, manifests, blobs)
            if missing:
                raise RuntimeError(
                    f"{len(missing)} object(s) of the closure are no longer on disk "
                    f"(collected mid-prewarm? pin it, or prewarm by tag): "
                    + ", ".join(missing[:5])
                )
            # done and finished_at in one step: no await between them.
            job.finished_at = time.time()
            job.state = "done"
        except asyncio.CancelledError:
            # Only shutdown cancels a prewarm. The work is not failed, it is
            # cut off: the same state a kill would leave after the next boot.
            job.state = "interrupted"
            job.interrupted_at = time.time()
            stopped = True
            raise
        except Exception as exc:  # noqa: BLE001 - reported on the job, not swallowed
            job.state = "error"
            job.error = f"{type(exc).__name__}: {exc}"
            log.warning("docker prewarm %s failed: %s", job.id, exc)
        finally:
            if job.finished_at is None and not stopped:
                job.finished_at = time.time()
            job.done.set()
            if self._active.get(job.key) is job:
                del self._active[job.key]
            self._history.append(job)
            self._prune(time.time())
            self._persist(transition=True, urgent=True)

    async def stop(self) -> None:
        """Shutdown: cancel running prewarms (bounded), then record them.

        Cancelled prewarms are written as `interrupted` with their progress.
        One that does not finish within the bound is abandoned by
        cancel_and_wait with a warning; the final flush still records it as
        running, which the next boot reports as interrupted.
        """
        await shutdown.cancel_and_wait(list(self._tasks), "OCI prewarm jobs")
        self.flush()


manager = PrewarmManager()


async def _fetch_closure(job: PrewarmJob, ref: registry.Ref,
                         reference: str) -> tuple[list[str], list[str]]:
    """Walk manifests from `reference`, storing each; return (manifests, blobs)."""
    roots = [reference]
    seen: set[str] = set()
    manifests: list[str] = []
    blobs: list[str] = []
    while roots:
        reraise_if_cancelled()
        r = roots.pop()
        if r in seen:
            continue
        seen.add(r)
        resp = await registry.get(ref, f"manifests/{r}", {"accept": _ACCEPT})
        metrics.record_docker_upstream(ref.upstream, resp.status_code)
        if resp.status_code != 200:
            raise RuntimeError(f"upstream {resp.status_code} for {r}")
        body = resp.content
        media = resp.headers.get("content-type") or "application/vnd.oci.image.manifest.v1+json"
        header = resp.headers.get("docker-content-digest")
        if ocistore.DIGEST_RE.match(r):
            # Asked for by digest: THAT is the expected value. The header comes
            # from the same response as the body, so checking one against the
            # other only proves the response agrees with itself -- an upstream
            # answering with a different manifest and its honest digest would
            # pass, and the manifest actually asked for would never be stored.
            if header and header != r:
                raise ocistore.DigestMismatch(
                    f"asked for {r}, upstream answered with {header}")
            digest = r
        else:
            # A tag has no content address; the header (or the bytes) is all
            # there is.
            digest = header or ocistore.compute_digest(body)
        ocistore.store_manifest(ref.upstream, digest, body, media)
        manifests.append(digest)
        job.manifests_done += 1
        if not ocistore.DIGEST_RE.match(r):
            ocistore.write_tag(ref.upstream, ref.repo, r,
                               ocistore.accept_fingerprint(_ACCEPT), digest, media)
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
    return manifests, list(dict.fromkeys(blobs))


def _missing_from_disk(upstream: str, manifests: list[str], blobs: list[str]) -> list[str]:
    out = [d for d in manifests if not ocistore.manifest_path(upstream, d).is_file()]
    out += [d for d in blobs if not ocistore.blob_path(upstream, d).is_file()]
    return out


class PrewarmRequest(BaseModel):
    image: str = Field(description="e.g. ghcr.io/org/img:1.2.3 or …@sha256:…")
    pin: bool = Field(default=False, description="pin the image and its whole blob closure")


class PinRequest(BaseModel):
    image: str


class AbandonRequest(BaseModel):
    upstream: str = Field(description="registry host, e.g. ghcr.io")
    digest: str = Field(description="sha256:... of the blob whose forward is abandoned")


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


@router.post("/prewarm")
async def prewarm(req: PrewarmRequest) -> dict:
    """Pull an image and its whole closure ahead of a rollout.

    Fire-and-forget: returns a job id. Pass a DIGEST rather than a tag for
    anything you intend to reproduce -- a tag can move mid-pull and assemble a
    tree from two commits. Re-submitting while the same prewarm runs returns
    that job; re-submitting after it was interrupted resumes it, skipping
    every blob already cached.
    """
    name, reference = _split(req.image)
    try:
        ref = registry.resolve(name)
    except registry.ResolveError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    verdict = policy.check_docker(ref.upstream, ref.repo)
    if not verdict.allowed:
        raise HTTPException(status_code=403, detail=f"blocked by policy: {verdict.reason}")
    job = manager.submit(req.image, ref, reference, req.pin)
    return {"job": job.as_dict()}


@router.get("/prewarm")
async def prewarm_list() -> dict:
    """Every prewarm job the ledger holds, running first, then newest first."""
    return {"jobs": [j.as_dict() for j in manager.list()],
            "ledger": manager.ledger_status()}


@router.get("/prewarm/{job_id}")
async def prewarm_status(job_id: str) -> dict:
    job = manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="no such job")
    return {"job": job.as_dict()}


@router.get("/images")
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


@router.get("/pins")
async def get_pins() -> dict:
    try:
        return {"pins": sorted(ocigc.load_pins(strict=True))}
    except StateUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/pins")
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


@router.delete("/pins")
async def remove_pin(req: PinRequest) -> dict:
    try:
        pins = ocigc.load_pins(strict=True)
    except StateUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    pins.discard(req.image)
    ocigc.save_pins(pins)
    return {"pins": sorted(pins)}


@router.delete("/images")
async def evict_image(req: EvictRequest, sweep: bool = False) -> dict:
    """Drop a tag, freeing every layer no other tag still references.

    Eviction is TOP-DOWN and never deletes a blob directly: dropping the tag
    removes the root, and mark-and-sweep then collects whatever became
    unreachable. That is what makes shared layers safe -- a layer another tag
    still uses is still reachable, so it is never collected.

    By default the blobs go on the NEXT scheduled sweep, up to
    XHC_EVICT_INTERVAL away. That is fine for reclaiming space and confusing
    for a human: delete a tag, look at the disk, see nothing freed, conclude it
    failed. So the response says when it will happen, and `?sweep=1` does it
    now and reports the bytes.

    `?sweep=1` walks every blob in the store, so it costs more than the delete
    itself on a large cache. It is opt-in for that reason rather than the
    default.
    """
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

    out = {"dropped": req.image, "tags_removed": len(dropped)}
    if sweep:
        try:
            result = ocigc.sweep(ocigc.mark(strict=True))
        except StateUnavailable as exc:
            # The tag is already gone; say so rather than implying the whole
            # call failed, and let the scheduled sweep finish the job.
            out["swept"] = False
            out["sweep_error"] = str(exc)
            return out
        out["swept"] = True
        out["blobs_removed"] = result["blobs"]
        out["manifests_removed"] = result["manifests"]
        out["freed_bytes"] = result["freed_bytes"]
    else:
        out["swept"] = False
        out["note"] = (
            "layers not referenced by another tag are freed by the next sweep, "
            f"within {settings.evict_interval_s}s. Pass ?sweep=1 to reclaim now "
            "and see the bytes."
        )
    return out


@router.get("/pending")
async def list_pending() -> dict:
    """Pushes accepted but not yet confirmed upstream (store-forward only).

    Documented as "the pending view" before it existed as a route: pending()
    was reachable in-process and by nothing over HTTP, so the documentation
    described a surface a reader could not reach. Anyone following it got the
    HF catch-all and a page of huggingface.co HTML -- a wrong path here returns
    200 with someone else's content rather than a 404.
    """
    return {"pending": ocipush.pending()}


@router.delete("/pending")
async def abandon_pending(req: AbandonRequest) -> dict:
    """Give up on a forward that will not succeed and release its pin.

    A deletion decision rather than housekeeping: the blob is pinned precisely
    because it may be the only copy, and the client that pushed it was told
    201. Refuses while the forward is still running.
    """
    try:
        return ocipush.abandon(req.upstream, req.digest)
    except ocipush.PushError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from exc


@router.post("/gc")
async def run_gc(dry_run: bool = False) -> dict:
    """Run mark-and-sweep now rather than waiting for the interval."""
    return await asyncio.to_thread(ocigc.collect, 0, dry_run)
