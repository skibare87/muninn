"""The LAN leg: plain, boring, fast file serving.

No chunk reassembly, no content-addressing, no protocol cleverness. A cache hit
is an open() and a loop. That is deliberate -- on the re-pull path the client is
on your fabric, and every cycle spent reconstructing chunks in userspace is a
cycle not spent filling the NIC.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import Response, StreamingResponse

from .config import settings
from .jobs import Job

log = logging.getLogger("xhc.serving")

CHUNK = int(os.environ.get("XHC_STREAM_CHUNK") or 4 * 1024 * 1024)
_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """Parse a single-range 'bytes=' header into inclusive (start, end).

    Multi-range requests are not supported; we return None so the caller sends
    the whole file, which is a legal (if unhelpful) response. HF clients only
    use ranges to resume, which is always a single suffix range.
    """
    if not header:
        return None
    m = _RANGE_RE.match(header.strip())
    if not m:
        return None
    start_s, end_s = m.group(1), m.group(2)
    if start_s == "" and end_s == "":
        return None
    if start_s == "":
        length = int(end_s)
        if length <= 0:
            return None
        start = max(0, size - length)
        end = size - 1
    else:
        start = int(start_s)
        end = int(end_s) if end_s else size - 1
        end = min(end, size - 1)
    if start > end or start >= size:
        raise HTTPException(
            status_code=416,
            detail="range not satisfiable",
            headers={"content-range": f"bytes */{size}"},
        )
    return start, end


def _read_range(path: Path, start: int, end: int):
    """Sync generator; Starlette runs it in a threadpool."""
    remaining = end - start + 1
    with open(path, "rb") as fh:
        fh.seek(start)
        while remaining > 0:
            data = fh.read(min(CHUNK, remaining))
            if not data:
                break
            remaining -= len(data)
            yield data


def file_response(
    path: Path, size: int, range_header: str | None, headers: dict[str, str]
) -> Response:
    """Serve a fully-cached file, honouring a single Range if present."""
    hdrs = dict(headers)
    hdrs["accept-ranges"] = "bytes"
    rng = parse_range(range_header, size)
    if rng is None:
        hdrs["content-length"] = str(size)
        return StreamingResponse(
            _read_range(path, 0, size - 1),
            status_code=200,
            headers=hdrs,
            media_type="application/octet-stream",
        )
    start, end = rng
    hdrs["content-length"] = str(end - start + 1)
    hdrs["content-range"] = f"bytes {start}-{end}/{size}"
    return StreamingResponse(
        _read_range(path, start, end),
        status_code=206,
        headers=hdrs,
        media_type="application/octet-stream",
    )


async def tail_follow(job: Job, final_path: Path, size: int):
    """Stream a file that is still being written by an in-flight ingest.

    IMPORTANT: this is only correct if the ingest writes the output file as a
    strict prefix. hf_xet reconstructs a file from terms that it MAY write at
    parallel file offsets; if it does, a partial file is not a valid prefix and
    this would serve zeroes that the client accepts as real data.

    Measured on huggingface_hub 0.34.4 against a 3.95GB Xet-backed file: writes
    were sequential both with and without HF_XET_RECONSTRUCT_WRITE_SEQUENTIALLY
    (17/17 and 9/9 sampled prefixes valid, file growing from 0 with no
    preallocation). That is not a documented guarantee, so the image sets the
    flag anyway and scripts/verify_sequential_writes.py re-checks it -- run that
    after any hf_xet upgrade. See README "Verifying sequential writes".
    """
    pos = 0
    waited = 0.0
    warned = False
    incomplete: Path | None = None

    while True:
        if job.state == "error":
            log.error("ingest %s failed mid-stream: %s", job.id, job.error)
            # The client has already received a 200 and some bytes; there is no
            # way to signal an error in-band. Truncating the body is the only
            # honest option -- content-length will not match and the client will
            # treat it as a failed transfer.
            return

        if incomplete is None and job.incomplete_path:
            cand = Path(job.incomplete_path)
            if cand.exists():
                incomplete = cand

        src = incomplete if (incomplete and incomplete.exists()) else None
        if src is None and job.state == "done":
            src = final_path

        if src is not None:
            try:
                available = src.stat().st_size
            except OSError:
                available = 0
            if available > pos:
                chunk = await asyncio.to_thread(_read_at, src, pos, available - pos)
                if chunk:
                    pos += len(chunk)
                    waited = 0.0
                    yield chunk
                    continue

        if job.state == "done":
            # Ingest finished; drain whatever is left of the committed blob.
            try:
                final_size = final_path.stat().st_size
            except OSError:
                final_size = size
            while pos < final_size:
                chunk = await asyncio.to_thread(
                    _read_at, final_path, pos, min(CHUNK, final_size - pos)
                )
                if not chunk:
                    break
                pos += len(chunk)
                yield chunk
            return

        await asyncio.sleep(settings.stream_poll_interval_s)
        waited += settings.stream_poll_interval_s
        if pos == 0 and waited > settings.stream_start_timeout_s and not warned:
            # We never found the .incomplete file. Its naming is a
            # huggingface_hub implementation detail and shifts between
            # versions, so degrade to `wait` semantics rather than failing:
            # the client blocks, but it gets correct bytes.
            warned = True
            log.warning(
                "ingest %s: no partial file after %.0fs (looked for %s); "
                "falling back to wait-for-completion",
                job.id,
                waited,
                job.incomplete_path,
            )


def _read_at(path: Path, offset: int, length: int) -> bytes:
    with open(path, "rb") as fh:
        fh.seek(offset)
        return fh.read(min(length, CHUNK))
