"""HF_ENDPOINT-compatible surface.

Design note: we do NOT reimplement the Hub API. Metadata requests (/api/...,
refs, repo info) are proxied straight upstream -- they are small, latency-bound,
and any divergence from the real API breaks clients in subtle ways. Only
`/…/resolve/…` file bytes are intercepted and served from cache. That keeps the
compatibility surface tiny while still capturing 100% of the bytes.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from urllib.parse import quote, unquote

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from huggingface_hub import get_hf_file_metadata, hf_hub_url

from . import cachefs, serving
from .config import settings
from .jobs import manager

log = logging.getLogger("xhc.hfcompat")

router = APIRouter()

_REPO_TYPE_PREFIX = {"datasets": "dataset", "spaces": "space"}
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}

_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    global _client  # noqa: PLW0603 - module-level singleton client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.request_timeout_s, read=None),
            follow_redirects=False,
        )
    return _client


async def close_client() -> None:
    global _client  # noqa: PLW0603 - module-level singleton client
    if _client is not None:
        await _client.aclose()
        _client = None


def parse_resolve(full_path: str) -> tuple[str, str, str, str] | None:
    """'datasets/org/name/resolve/main/a/b.bin' -> (dataset, org/name, main, a/b.bin)."""
    if "/resolve/" not in full_path:
        return None
    head, rest = full_path.split("/resolve/", 1)
    repo_type = "model"
    for prefix, rtype in _REPO_TYPE_PREFIX.items():
        if head.startswith(prefix + "/"):
            repo_type = rtype
            head = head[len(prefix) + 1 :]
            break
    if "/" not in rest:
        return None
    revision, filename = rest.split("/", 1)
    revision = unquote(revision)
    if not head or not filename:
        return None
    return repo_type, head, revision, filename


def is_xet_token_path(full_path: str) -> bool:
    return "/xet-read-token/" in full_path or "/xet-write-token/" in full_path


def upstream_resolve_url(repo_type: str, repo_id: str, revision: str, filename: str) -> str:
    return hf_hub_url(
        repo_id=repo_id,
        filename=filename,
        repo_type=repo_type,
        revision=revision,
        endpoint=settings.upstream,
    )


async def fetch_metadata(repo_type: str, repo_id: str, revision: str, filename: str):
    """HEAD upstream for etag/commit/size. Cheap, and always authoritative."""
    url = upstream_resolve_url(repo_type, repo_id, revision, filename)
    return await asyncio.to_thread(get_hf_file_metadata, url, token=settings.hf_token)


def _cache_headers(commit: str, etag: str | None, extra: dict | None = None) -> dict[str, str]:
    hdrs: dict[str, str] = {"x-repo-commit": commit}
    if etag:
        hdrs["etag"] = etag if etag.startswith(('"', "W/")) else f'"{etag}"'
    if extra:
        hdrs.update(extra)
    return hdrs


@router.api_route(
    "/{full_path:path}",
    methods=["GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
)
async def catch_all(full_path: str, request: Request) -> Response:
    # 1. Stop clients from negotiating Xet through us. If they got a real
    #    casUrl they would pull bytes straight from HF and the cache would
    #    never see them -- a silent, and very expensive, bypass.
    if settings.block_client_xet and is_xet_token_path(full_path):
        log.debug("blocking client xet negotiation: %s", full_path)
        return JSONResponse(
            {"error": "Xet is disabled on this cache endpoint; use the LFS resolve path."},
            status_code=404,
        )

    parsed = parse_resolve(full_path)
    if parsed is None or request.method not in ("GET", "HEAD"):
        return await proxy_upstream(full_path, request)

    repo_type, repo_id, revision, filename = parsed
    return await serve_file(repo_type, repo_id, revision, filename, request)


async def _hit_response(
    local: cachefs.ResolvedFile,
    repo_type: str,
    repo_id: str,
    revision: str,
    filename: str,
    request: Request,
    range_header: str | None,
) -> Response:
    etag = local.etag
    if etag is None:
        # No symlink to read the etag from (copy-mode cache, or an odd
        # filesystem). huggingface_hub refuses to download without an ETag, so
        # pay for one upstream HEAD rather than serving an unusable response.
        try:
            meta = await fetch_metadata(repo_type, repo_id, revision, filename)
            etag = (meta.etag or "").strip('"') or None
        except Exception as exc:  # noqa: BLE001 - degrade to no-etag, do not fail the hit
            log.warning("hit for %s/%s but no etag available: %s", repo_id, filename, exc)

    headers = _cache_headers(local.commit, etag, {"x-xhc-cache": "HIT"})
    if request.method == "HEAD":
        headers["content-length"] = str(local.size)
        headers["accept-ranges"] = "bytes"
        return Response(status_code=200, headers=headers)
    return serving.file_response(local.path, local.size, range_header, headers)


async def serve_file(
    repo_type: str, repo_id: str, revision: str, filename: str, request: Request
) -> Response:
    range_header = request.headers.get("range")

    # --- fast path: already cached -----------------------------------------
    local = cachefs.resolve_local(repo_type, repo_id, revision, filename)
    if local is not None:
        return await _hit_response(
            local, repo_type, repo_id, revision, filename, request, range_header
        )

    # --- miss: ask upstream what this actually is --------------------------
    try:
        meta = await fetch_metadata(repo_type, repo_id, revision, filename)
    except Exception as exc:
        log.warning("upstream metadata failed for %s/%s: %s", repo_id, filename, exc)
        raise HTTPException(status_code=502, detail=f"upstream metadata failed: {exc}") from exc

    commit = meta.commit_hash or revision
    etag = (meta.etag or "").strip('"')
    size = meta.size or 0

    # The requested revision may have been a branch; re-check by commit sha in
    # case we already hold these exact bytes under a different ref.
    if meta.commit_hash:
        local = cachefs.resolve_local(repo_type, repo_id, meta.commit_hash, filename)
        if local is not None:
            headers = _cache_headers(local.commit, local.etag or etag, {"x-xhc-cache": "HIT"})
            if request.method == "HEAD":
                headers["content-length"] = str(local.size)
                headers["accept-ranges"] = "bytes"
                return Response(status_code=200, headers=headers)
            return serving.file_response(local.path, local.size, range_header, headers)

    if request.method == "HEAD":
        # Answer metadata without triggering an ingest. Clients HEAD constantly
        # (every hf_hub_download starts with one); ingesting here would prefetch
        # things nobody asked to download.
        headers = _cache_headers(
            commit,
            etag,
            {
                "content-length": str(size),
                "accept-ranges": "bytes",
                "x-xhc-cache": "MISS",
            },
        )
        return Response(status_code=200, headers=headers)

    # --- kick off (or join) the single-flight ingest ------------------------
    incomplete = str(cachefs.blob_incomplete_path(repo_type, repo_id, etag)) if etag else None
    job = await manager.ensure_file(
        repo_type,
        repo_id,
        revision,
        filename,
        expected_size=size or None,
        incomplete_path=incomplete,
    )

    headers = _cache_headers(
        commit,
        etag,
        {
            "x-xhc-cache": "MISS",
            "x-xhc-job": job.id,
            "x-xhc-miss-policy": settings.miss_policy,
        },
    )

    if settings.miss_policy == "redirect":
        # Client pulls from HF with its own hf_xet at full fan-out speed -- no
        # slower than going direct -- while we ingest in the background for
        # everyone who asks next.
        url = upstream_resolve_url(repo_type, repo_id, revision, filename)
        return RedirectResponse(url, status_code=302, headers=headers)

    if settings.miss_policy == "wait":
        await job.done.wait()
        if job.state == "error":
            raise HTTPException(status_code=502, detail=f"ingest failed: {job.error}")
        local = cachefs.resolve_local(repo_type, repo_id, revision, filename)
        if local is None:
            raise HTTPException(status_code=500, detail="ingest reported success but file missing")
        headers["x-xhc-cache"] = "MISS-WAIT"
        return serving.file_response(local.path, local.size, range_header, headers)

    # stream: tail-follow the partial file as the ingest writes it.
    final_path = (
        Path(settings.cache_dir)
        / cachefs.repo_folder_name(repo_id, repo_type)
        / "snapshots"
        / commit
        / filename
    )
    if size:
        headers["content-length"] = str(size)
    headers["x-xhc-cache"] = "MISS-STREAM"
    return StreamingResponse(
        serving.tail_follow(job, final_path, size),
        status_code=200,
        headers=headers,
        media_type="application/octet-stream",
    )


async def proxy_upstream(full_path: str, request: Request) -> Response:
    """Transparent pass-through for everything that is not file bytes."""
    url = f"{settings.upstream}/{quote(full_path)}"
    if request.url.query:
        url = f"{url}?{request.url.query}"

    headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}
    # Use the cache's identity unless the client brought its own. This is how
    # you keep Hub tokens off the edge nodes entirely.
    if "authorization" not in {k.lower() for k in headers} and settings.hf_token:
        headers["authorization"] = f"Bearer {settings.hf_token}"
    # httpx injects its own accept-encoding, which would make us request a
    # gzipped body the client never asked for and then forward it verbatim.
    # Pin it to whatever the client actually sent.
    headers.setdefault("accept-encoding", "identity")

    body = await request.body()
    client = get_client()
    req = client.build_request(request.method, url, headers=headers, content=body or None)
    try:
        resp = await client.send(req, stream=True)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"upstream error: {exc}") from exc

    out_headers = {k: v for k, v in resp.headers.items() if k.lower() not in _HOP_BY_HOP}

    async def body_iter():
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
        finally:
            await resp.aclose()

    return StreamingResponse(
        body_iter(),
        status_code=resp.status_code,
        headers=out_headers,
        media_type=resp.headers.get("content-type"),
    )
