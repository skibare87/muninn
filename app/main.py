from __future__ import annotations

import asyncio
import hmac
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from fastapi.responses import JSONResponse

from . import (
    authzmanage,
    cachefs,
    config,
    console,
    dockerauth,
    hfcompat,
    httpclients,
    jwtauth,
    manage,
    managegate,
    memcheck,
    metrics,
    ocicompat,
    ocigc,
    ocimanage,
    ocipush,
    ocistore,
    orphans,
    pushlimits,
    refs,
    shutdown,
    statedir,
    tier,
    webauth,
)
from .config import settings
from .jobs import ACTIVE_STATES, manager

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
    # Before anything reads pins: validates XHC_STATE_DIR (raising, never
    # falling back) and copies in-tree state across on first use.
    statedir.prepare()
    # After prepare(), so the ledger is read from wherever state lives. Never
    # raises for an unreadable ledger: job history is not protection (see
    # JobManager.load_ledger for why this is the opposite of pins).
    manager.load_ledger()
    if settings.docker_enabled:
        # The OCI prewarm table, on the same machinery and the same terms: a
        # prewarm that was running comes back `interrupted`, and an unreadable
        # ledger is set aside rather than stopping the boot.
        ocimanage.manager.load_ledger()
    warning = memcheck.check(settings.ingest_concurrency, settings.snapshot_max_workers)
    if warning:
        log.warning(warning)

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
    if not managegate.enabled():
        log.warning(
            "management API is disabled: XHC_MANAGE_TOKEN is unset, so every /_cache "
            "route answers 404. Set XHC_MANAGE_TOKEN to enable it."
        )

    log.info(
        "muninn up | cache=%s capacity=%s miss_policy=%s",
        settings.cache_dir,
        settings.capacity_bytes or "filesystem",
        settings.miss_policy,
    )
    if settings.docker_enabled:
        # Print the RESOLVED regime in words, not the raw number. `0` means
        # NEVER revalidate and reads like "no delay"; an operator seeing
        # `tag_ttl=0s` in a boot log has no way to tell which of the three
        # behaviours they actually have without reading the comparison.
        ttl = settings.docker_tag_ttl_s
        if ttl == config.ALWAYS_REVALIDATE:
            ttl_desc = "always-revalidate"
        elif ttl <= 0:
            ttl_desc = "NEVER-revalidate (0: mutable tags are frozen once cached)"
        else:
            ttl_desc = f"{ttl:g}s"
        log.info(
            "docker/OCI pull-through on /v2/* | dir=%s policy=%s tag_ttl=%s",
            settings.docker_dir,
            settings.docker_policy,
            ttl_desc,
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
            # Before the warnings, so a reader sees WHAT WAS LOADED first and
            # the caveats second.
            pushlimits.describe()
            # Forwards owed from a previous run. Re-enqueued rather than only
            # reported: store-forward promised the client eventual delivery,
            # and a restart is not a reason to withdraw that.
            await ocipush.resume()
            log.warning(
                "push-through is ENABLED (mode=%s). Any client that can reach "
                "this cache may push to any registry it holds credentials for, "
                "under this cache's identity and with no attribution -- a "
                "docker push cannot identify itself. Restrict who can reach the "
                "port, or set BOTH XHC_DOCKER_AUTH=basic AND "
                "XHC_DOCKER_HTPASSWD -- the file alone is ignored.",
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

    jwt_warm = None
    if settings.jwt_issuers:
        # Raises on a file key set that is missing or empty, or a CA or
        # fetch-token file that cannot be read: refused at boot, not discovered
        # as every workload being refused.
        jwtauth.load()
        # Remote key sets are fetched in the background, never blocking boot. A
        # fetch that fails here is retried on first use.
        jwt_warm = asyncio.create_task(jwtauth.warm())
        surfaces = ["/v2"] if settings.docker_enabled else []
        if settings.hf_auth == "key":
            surfaces.append("the Hugging Face surface")
        log.info("workload JWTs are accepted on: %s. Never on /_cache, which takes "
                 "only XHC_MANAGE_TOKEN.", ", ".join(surfaces) or "NO SURFACE")
        if settings.hf_auth != "key":
            log.warning("XHC_JWT_ISSUERS is set but XHC_HF_AUTH is not `key`: the "
                        "Hugging Face surface is unauthenticated and ignores tokens.")

    # The object-store tier: probes in the background and fails open, so a
    # bucket that is down at boot delays nothing. A no-op when XHC_TIER2 is unset.
    await tier.start()

    evictor = asyncio.create_task(cachefs.eviction_loop())
    docker_gc = asyncio.create_task(ocigc.gc_loop()) if settings.docker_enabled else None
    orphan_sweep = asyncio.create_task(orphans.orphan_loop())
    try:
        yield
    finally:
        # Every step is time-bounded and names itself if it overruns: a
        # cancelled task is not guaranteed to finish (see app/shutdown.py), and
        # a shutdown that waits on one forever is a hang with no culprit.
        #
        # HF ingest jobs FIRST, and marked before they are cancelled: stop()
        # records every in-flight job as `interrupted` and writes the ledger
        # synchronously, so that record exists even if a later step overruns
        # or the orchestrator's kill arrives. Only then are the jobs cancelled,
        # and their cancel handler leaves the mark alone. The old order -- a
        # flush here, the cancel left to asyncio.run's teardown -- let that
        # handler overwrite the flush with `error: cancelled` whenever the
        # process outlived the lifespan, which as a container's PID 1 it does.
        await shutdown.bounded(
            manager.stop(), "HF ingest shutdown", timeout=2 * shutdown.STEP_TIMEOUT_S
        )
        await shutdown.cancel_and_wait(
            (evictor, orphan_sweep, docker_gc, jwt_warm), "the background loops"
        )
        # OCI prewarms are cancelled (each loop re-raises a cancel that httpx
        # swallowed) and recorded as `interrupted` with their progress. The step
        # is bounded; one that overruns is abandoned, still recorded as running,
        # and reported interrupted by the next boot.
        if settings.docker_enabled:
            await shutdown.bounded(
                ocimanage.manager.stop(), "OCI prewarm shutdown",
                timeout=2 * shutdown.STEP_TIMEOUT_S,
            )
        # tier.stop() bounds its own two waits; its outer bound is longer than
        # both together, so it is only reached if stop() itself is at fault.
        await shutdown.bounded(tier.stop(), "tier.stop()", timeout=3 * shutdown.STEP_TIMEOUT_S)
        # Every long-lived outbound client (Hub, registries, refs, orphans,
        # OIDC), on the loop that built it. See app/httpclients.py.
        await shutdown.bounded(httpclients.close_all(), "httpclients.close_all()")


app = FastAPI(
    title="muninn",
    description=(
        "Hugging Face edge cache. Ingests from the Hub over the WAN with native Xet "
        "(parallel range GETs), serves the LAN over plain HTTP (no Xet, no chunk "
        "reassembly). Point clients at this host with HF_ENDPOINT."
    ),
    version="0.5.0",
    lifespan=lifespan,
    # FastAPI mounts /docs, /redoc and /openapi.json with no authentication of
    # any kind. On a private cache that is a convenience; on a public one it is
    # a published description of the management API for anyone who asks. None
    # of the three is needed to operate the cache, so they can be switched off
    # entirely rather than gated -- an endpoint that does not exist cannot be
    # misconfigured later.
    docs_url="/docs" if settings.docs_enabled else None,
    redoc_url="/redoc" if settings.docs_enabled else None,
    openapi_url="/openapi.json" if settings.docs_enabled else None,
)


@app.get("/healthz", include_in_schema=False)
async def healthz() -> JSONResponse:
    try:
        disk = cachefs.disk_stats()
    except OSError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)
    return JSONResponse({"ok": True, "free_bytes": disk["fs_free"]})


@app.get("/metrics", include_in_schema=False)
async def prometheus_metrics(
    authorization: str | None = Header(default=None),
) -> Response:
    """Prometheus exposition.

    Unauthenticated by default, and that default has a bounded reason: it
    carries no repo names or file paths, only counts, so it is safe to scrape
    from a LAN. THE BOUND IS "FROM A LAN" -- the `registry` label names which
    upstreams are in use and cache_bytes is a capacity signal, so on a public
    hostname this is an unauthenticated read of how much a shared service holds
    and who it talks to. Set XHC_METRICS_AUTH=token there.

    The reason it is not simply gated for everyone: this endpoint is an existing
    monitoring contract, and a scrape that starts returning 401 stops somebody's
    alerting without anything going red.
    """
    if settings.metrics_auth == "token":
        # compare_digest so the comparison does not leak the token's prefix
        expected = f"Bearer {settings.manage_token}"
        if authorization is None or not hmac.compare_digest(authorization, expected):
            raise HTTPException(status_code=401, detail="metrics require a management token")
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
        "muninn_ingest_jobs_active": sum(1 for j in jobs if j.state in ACTIVE_STATES),
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
    gauges.update(tier.gauges())
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
            "muninn_tier_bytes": (
                "Bytes under the tier's content prefix at the last reconcile, from "
                "LIST. It grows without bound: Muninn never deletes from the tier."
            ),
            "muninn_tier_bytes_read_total": "Body bytes read from the tier. HEADs are not counted.",
            "muninn_tier_bytes_written_total": (
                "Body bytes written to the tier. HEADs are not counted."
            ),
        },
    )
    return Response(content=body, media_type="text/plain; version=0.0.4; charset=utf-8")


# Every /_cache router is gated by managegate.ManageRoute, its route class: no
# XHC_MANAGE_TOKEN -> 404 naming the setting, wrong token -> 401. A new /_cache
# router must use it; tests/test_manage_gate.py enumerates app.routes to check.
app.include_router(manage.router)
# Headless provisioning. MOUNTED ALWAYS, and additionally refuses with 404
# unless there is a store to provision.
#
# Always mounted, which is the opposite of how the console is treated, because
# in THIS app an unmounted path is not a 404: it falls to the Hugging Face
# catch-all below and is proxied to the Hub, request body and all. A POST of a
# principal to a deployment that had not enabled this would have been sent to
# a third party. A route that exists and refuses answers locally.
app.include_router(authzmanage.router)
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
# Before hfcompat for the same reason as /v2: /_auth/* would otherwise be
# swallowed by the HF catch-all, and a login route that resolves to a Hub proxy
# is a login route that silently does not exist.
#
# Mounted ONLY when login is configured. An unconfigured deployment gets no
# /_auth surface at all rather than routes that 404 with an explanation --
# there is nothing to explain to an anonymous caller, and a disabled-but-present
# endpoint is still a place to aim traffic.
if webauth.enabled():
    app.include_router(webauth.router)
    # Same mount condition, deliberately: the key-management surface exists only
    # where there is a login to put in front of it. Mounting it without one
    # would leave every handler depending on require_login to 401 -- correct,
    # and one refactor away from not being.
    app.include_router(console.router)
# Must be last: hfcompat owns the catch-all route.
app.include_router(hfcompat.router)
