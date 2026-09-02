from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Response
from fastapi.responses import JSONResponse

from . import (
    cachefs,
    dockerauth,
    hfcompat,
    manage,
    metrics,
    ocicompat,
    ocigc,
    ocimanage,
    ocistore,
    orphans,
    refs,
)
from . import registry as ociregistry
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
    if settings.docker_enabled:
        Path(settings.docker_dir).mkdir(parents=True, exist_ok=True)

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
    if settings.docker_enabled:
        log.info(
            "docker/OCI pull-through on /v2/* | dir=%s policy=%s tag_ttl=%ss",
            settings.docker_dir,
            settings.docker_policy,
            settings.docker_tag_ttl_s,
        )
        if settings.docker_policy == "open" and not settings.allow_registries:
            # Parity with the HF side is the ruling, but the exposure it implies
            # is different in kind: anyone who can reach this host can pull from
            # ANY registry onto the array. Say so once, at boot.
            log.warning(
                "docker policy is `open` with no registry allowlist: any client "
                "may pull from any upstream registry through this cache. Set "
                "XHC_ALLOW_REGISTRIES to restrict it."
            )

        # Raises rather than degrading to open. An ABSENT config means the
        # operator did not ask for auth; an UNREADABLE credential file when they
        # DID ask is unknown, and resolving unknown to permissive is what
        # disarmed pin protection in an internal issue. Losing a password file must not
        # silently reopen the cache.
        dockerauth.load()

        if settings.docker_push_enabled:
            log.warning(
                "push-through is ENABLED (mode=%s). Any client that can reach "
                "this cache may push to any registry it holds credentials for, "
                "under this cache's identity and with no attribution -- a "
                "docker push cannot identify itself. Restrict who can reach the "
                "port, or set XHC_DOCKER_HTPASSWD to require a credential.",
                settings.docker_push_mode,
            )
            if settings.docker_push_mode == "store-forward":
                log.warning(
                    "push mode is `store-forward`: clients are told 201 BEFORE "
                    "the upstream registry has the content. A push followed by "
                    "a pull elsewhere can race it. Use `proxy` if a 201 must "
                    "mean the registry really has it."
                )
                if not settings.docker_cache_on_push:
                    log.info(
                        "XHC_DOCKER_CACHE_ON_PUSH=0 with store-forward: the "
                        "store is EPHEMERAL -- pushed content is held only "
                        "until the upstream push confirms, then dropped."
                    )
        if settings.docker_auth == "basic":
            log.warning(
                "client auth is `basic`: credentials are sent in clear unless "
                "something terminates TLS in front of this cache. Muninn cannot "
                "see its own front, so this is a warning and not a refusal."
            )
            log.warning(
                "client auth is a GATE, not per-client isolation: everyone who "
                "authenticates sees everything this cache holds. A cached hit "
                "consults no credentials at all."
            )

    evictor = asyncio.create_task(cachefs.eviction_loop())
    docker_gc = asyncio.create_task(ocigc.gc_loop()) if settings.docker_enabled else None
    orphan_sweep = asyncio.create_task(orphans.orphan_loop())
    try:
        yield
    finally:
        for task in (evictor, orphan_sweep, docker_gc):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await hfcompat.close_client()
        await ociregistry.close_client()
        await orphans.close_client()
        await refs.close_client()


app = FastAPI(
    title="muninn",
    description=(
        "Hugging Face edge cache. Ingests from the Hub over the WAN with native Xet "
        "(parallel range GETs), serves the LAN over plain HTTP (no Xet, no chunk "
        "reassembly). Point clients at this host with HF_ENDPOINT."
    ),
    version="0.5.0",
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
        # Bytes fetched by in-flight ingests. Without this a running prewarm and
        # a stalled one look identical on this endpoint (an internal issue).
        # downloaded_bytes is a METHOD. Uncalled it is a truthy bound method, so
        # `or 0` never fires and sum() raises TypeError -- but ONLY when a job is
        # actually running, because the generator is otherwise empty. The endpoint
        # was healthy whenever it had nothing to report and 500'd exactly when a
        # human would look at it. an internal issue.
        "muninn_ingest_bytes_inflight": sum(
            (j.downloaded_bytes() or 0) for j in jobs if j.state == "running"
        ),
        "muninn_orphans": len(orphan_state),
        "muninn_orphan_bytes": sum(o.get("size_on_disk", 0) for o in orphan_state.values()),
        "muninn_ref_lookups_total": refs.stats()["lookups"],
        "muninn_disk_free_bytes": disk["fs_free"],
    }
    if settings.docker_enabled:
        dstats = ocistore.stats()
        gauges["muninn_docker_blobs"] = dstats["blobs"]
        gauges["muninn_docker_manifests"] = dstats["manifests"]
        gauges["muninn_docker_bytes"] = dstats["bytes"]
        if settings.docker_capacity_bytes:
            gauges["muninn_docker_capacity_bytes"] = settings.docker_capacity_bytes
    body = metrics.render(
        gauges,
        {
            "muninn_requests_total": "File requests by cache result.",
            "muninn_cache_bytes": "Bytes currently held in the cache.",
            "muninn_orphan_bytes": "Bytes retained for repos deleted upstream.",
            "muninn_ingest_bytes_inflight": (
                "Bytes fetched so far by ingests still running. Rises while a prewarm "
                "is healthy; flat means stalled."
            ),
        },
    )
    return Response(content=body, media_type="text/plain; version=0.0.4; charset=utf-8")


app.include_router(manage.router)
# Before hfcompat: /v2/* is the Docker surface and the HF catch-all would
# otherwise swallow it.
if settings.docker_enabled:
    app.include_router(ocimanage.router)
    # Every route on ocicompat's router is /v2/*, so gating it here gates
    # exactly the pull surface and nothing else. /healthz and /metrics live on
    # other routers and stay unauthenticated by design; /_cache keeps its own
    # XHC_MANAGE_TOKEN. Two credentials, two surfaces, no crossover.
    app.include_router(
        ocicompat.router,
        dependencies=[Depends(dockerauth.require_pull_auth)],
    )
# Must be last: hfcompat owns the catch-all route.
app.include_router(hfcompat.router)
