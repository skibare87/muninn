from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse

from . import cachefs, hfcompat, manage, metrics, orphans, refs
from .config import settings
from .jobs import manager

logging.basicConfig(
    level=os.environ.get("XHC_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
log = logging.getLogger("xhc")


@asynccontextmanager
async def lifespan(app: FastAPI):
    Path(settings.cache_dir).mkdir(parents=True, exist_ok=True)

    if os.environ.get("HF_HUB_DISABLE_XET", "").strip().lower() in ("1", "true", "yes"):
        # This is the exact misconfiguration the whole design exists to avoid.
        log.warning(
            "HF_HUB_DISABLE_XET is set INSIDE the cache container. Ingest will use the "
            "single-stream LFS bridge and will be slow. Unset it here; set it on the "
            "edge nodes instead."
        )
    if settings.miss_policy == "stream" and not os.environ.get(
        "HF_XET_RECONSTRUCT_WRITE_SEQUENTIALLY"
    ):
        log.warning(
            "XHC_MISS_POLICY=stream without HF_XET_RECONSTRUCT_WRITE_SEQUENTIALLY=1. "
            "Partial files may not be valid prefixes and streamed responses can be "
            "corrupt. See README 'Verifying sequential writes'."
        )
    if not settings.hf_token:
        log.warning("no HF_TOKEN set; gated repos and higher rate limits unavailable")

    log.info(
        "muninn up | cache=%s capacity=%s miss_policy=%s",
        settings.cache_dir,
        settings.capacity_bytes or "filesystem",
        settings.miss_policy,
    )

    evictor = asyncio.create_task(cachefs.eviction_loop())
    orphan_sweep = asyncio.create_task(orphans.orphan_loop())
    try:
        yield
    finally:
        for task in (evictor, orphan_sweep):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await hfcompat.close_client()
        await orphans.close_client()
        await refs.close_client()


app = FastAPI(
    title="muninn",
    description=(
        "Hugging Face edge cache. Ingests from the Hub over the WAN with native Xet "
        "(parallel range GETs), serves the LAN over plain HTTP (no Xet, no chunk "
        "reassembly). Point clients at this host with HF_ENDPOINT."
    ),
    version="0.3.0",
    lifespan=lifespan,
)


@app.get("/healthz", include_in_schema=False)
async def healthz() -> JSONResponse:
    try:
        disk = cachefs.disk_stats()
    except OSError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)
    return JSONResponse({"ok": True, "free_bytes": disk["fs_free"]})


@app.get("/metrics", include_in_schema=False)
async def prometheus_metrics() -> Response:
    """Prometheus exposition. Deliberately unauthenticated: it carries no repo
    names or file paths, only counts, so it is safe to scrape from a LAN."""
    view = await cachefs.get_view()
    disk = cachefs.disk_stats()
    jobs = manager.list()
    orphan_state = cachefs.load_orphans()
    gauges = {
        "muninn_cache_bytes": view.size_on_disk,
        "muninn_cache_capacity_bytes": disk["capacity"],
        "muninn_cache_files": view.nb_files,
        "muninn_cache_repos": len(view.repos),
        "muninn_scan_duration_seconds": view.scan_duration_s,
        "muninn_ingest_jobs_active": sum(1 for j in jobs if j.state in ("pending", "running")),
        "muninn_orphans": len(orphan_state),
        "muninn_orphan_bytes": sum(o.get("size_on_disk", 0) for o in orphan_state.values()),
        "muninn_ref_lookups_total": refs.stats()["lookups"],
        "muninn_disk_free_bytes": disk["fs_free"],
    }
    body = metrics.render(
        gauges,
        {
            "muninn_requests_total": "File requests by cache result.",
            "muninn_cache_bytes": "Bytes currently held in the cache.",
            "muninn_orphan_bytes": "Bytes retained for repos deleted upstream.",
        },
    )
    return Response(content=body, media_type="text/plain; version=0.0.4; charset=utf-8")


app.include_router(manage.router)
# Must be last: hfcompat owns the catch-all route.
app.include_router(hfcompat.router)
