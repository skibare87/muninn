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
import secrets
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import Response, StreamingResponse

from .config import settings
from .jobs import Job

log = logging.getLogger("xhc.serving")

CHUNK = int(os.environ.get("XHC_STREAM_CHUNK") or 4 * 1024 * 1024)

# After coalescing, disjoint ranges can total at most the file size, so the
# classic multi-range amplification (CVE-2011-3192, "killapache" -- thousands of
# overlapping ranges each nearly the whole file) is defeated by the coalescing
# below, not by this cap. The cap only bounds per-part bookkeeping.
MAX_RANGES = int(os.environ.get("XHC_MAX_RANGES") or 64)

_RANGE_HEADER_RE = re.compile(r"^bytes\s*=\s*(.+)$", re.IGNORECASE)
_ONE_RANGE_RE = re.compile(r"^(\d*)-(\d*)$")


def coalesce(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge overlapping and adjacent ranges.

    This is the actual defence against multi-range amplification: a request for
    a thousand overlapping copies of the same bytes collapses to one range, so
    the response body can never exceed the file size. RFC 9110 explicitly
    permits a server to coalesce, so clients must cope with fewer parts than
    they asked for.
    """
    if not ranges:
        return []
    ordered = sorted(ranges)
    out = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = out[-1]
        if start <= last_end + 1:  # overlapping or adjacent
            out[-1] = (last_start, max(last_end, end))
        else:
            out.append((start, end))
    return out


def parse_ranges(header: str | None, size: int) -> list[tuple[int, int]] | None:
    """Parse a Range header into inclusive (start, end) pairs.

    Returns None when the header is absent, malformed, or asks for more parts
    than we will serve -- in all of those cases the caller sends the whole file
    with a 200, which RFC 9110 permits. Raises 416 only when the header is
    well-formed but every range falls outside the file.
    """
    if not header:
        return None
    m = _RANGE_HEADER_RE.match(header.strip())
    if not m:
        return None
    specs = [x.strip() for x in m.group(1).split(",") if x.strip()]
    if not specs:
        return None

    parsed: list[tuple[int, int]] = []
    for spec in specs:
        one = _ONE_RANGE_RE.match(spec)
        if one is None:
            return None  # a malformed member invalidates the whole header
        start_s, end_s = one.group(1), one.group(2)
        if start_s == "" and end_s == "":
            return None
        if start_s == "":
            length = int(end_s)
            if length <= 0:
                continue  # zero-length suffix is unsatisfiable, not fatal
            start, end = max(0, size - length), size - 1
        else:
            start = int(start_s)
            end = min(int(end_s), size - 1) if end_s else size - 1
        if start >= size or start > end:
            continue  # skip unsatisfiable members, keep the rest
        parsed.append((start, end))

    if not parsed:
        raise HTTPException(
            status_code=416,
            detail="range not satisfiable",
            headers={"content-range": f"bytes */{size}"},
        )

    merged = coalesce(parsed)
    if len(merged) > MAX_RANGES:
        log.warning("range request had %d parts after coalescing; serving whole file", len(merged))
        return None
    return merged


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """Single-range convenience wrapper, kept for callers that only need one."""
    ranges = parse_ranges(header, size)
    if not ranges or len(ranges) != 1:
        return None
    return ranges[0]


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


MEDIA = "application/octet-stream"


def _part_header(boundary: str, start: int, end: int, size: int) -> bytes:
    return (
        f"--{boundary}\r\n"
        f"Content-Type: {MEDIA}\r\n"
        f"Content-Range: bytes {start}-{end}/{size}\r\n\r\n"
    ).encode()


def _multipart_length(boundary: str, ranges: list[tuple[int, int]], size: int) -> int:
    """Exact body length. Must be exact: a wrong Content-Length makes clients
    hang waiting for bytes that never come, rather than fail loudly."""
    total = 0
    for start, end in ranges:
        total += len(_part_header(boundary, start, end, size))
        total += end - start + 1
        total += 2  # CRLF terminating the part body
    total += len(f"--{boundary}--\r\n".encode())
    return total


def _read_multipart(path: Path, ranges: list[tuple[int, int]], size: int, boundary: str):
    """Sync generator; Starlette runs it in a threadpool."""
    with open(path, "rb") as fh:
        for start, end in ranges:
            yield _part_header(boundary, start, end, size)
            remaining = end - start + 1
            fh.seek(start)
            while remaining > 0:
                data = fh.read(min(CHUNK, remaining))
                if not data:
                    break
                remaining -= len(data)
                yield data
            yield b"\r\n"
    yield f"--{boundary}--\r\n".encode()


def file_response(
    path: Path, size: int, range_header: str | None, headers: dict[str, str]
) -> Response:
    """Serve a fully-cached file, honouring single or multi Range requests."""
    hdrs = dict(headers)
    hdrs["accept-ranges"] = "bytes"
    ranges = parse_ranges(range_header, size)

    if ranges is None:
        hdrs["content-length"] = str(size)
        return StreamingResponse(
            _read_range(path, 0, size - 1), status_code=200, headers=hdrs, media_type=MEDIA
        )

    if len(ranges) == 1:
        start, end = ranges[0]
        hdrs["content-length"] = str(end - start + 1)
        hdrs["content-range"] = f"bytes {start}-{end}/{size}"
        return StreamingResponse(
            _read_range(path, start, end), status_code=206, headers=hdrs, media_type=MEDIA
        )

    # multipart/byteranges -- what parquet readers and other range-batching
    # clients issue when they want several column chunks in one round trip.
    boundary = secrets.token_hex(16)
    hdrs["content-length"] = str(_multipart_length(boundary, ranges, size))
    return StreamingResponse(
        _read_multipart(path, ranges, size, boundary),
        status_code=206,
        headers=hdrs,
        media_type=f"multipart/byteranges; boundary={boundary}",
    )


async def tail_follow(
    job: Job, final_path: Path, size: int, start: int = 0, end: int | None = None
):
    """Stream a file that is still being written by an in-flight ingest.

    `start`/`end` (inclusive) let a resuming client be served a byte range off a
    cold cache: we wait until the ingest has written past `start`, then stream
    from there. That only works because ingest writes sequentially, which is
    exactly what scripts/verify_sequential_writes.py establishes -- with
    out-of-order writes a partial file is not a valid prefix and this would
    serve zeroes the client accepts as real data.

    Measured on huggingface_hub 0.34.4 against a 3.95GB Xet-backed file: writes
    were sequential both with and without HF_XET_RECONSTRUCT_WRITE_SEQUENTIALLY
    (17/17 and 9/9 sampled prefixes valid, growing from 0, no preallocation).
    Not a documented guarantee, so the image sets the flag and the script
    re-checks it -- run it after any hf_xet upgrade.
    """
    last = (size - 1) if end is None else end
    pos = start
    waited = 0.0
    warned = False
    incomplete: Path | None = None

    while pos <= last:
        if job.state == "error":
            log.error("ingest %s failed mid-stream: %s", job.id, job.error)
            # The client already has a 2xx and some bytes; there is no in-band
            # way to signal failure. Truncating is the honest option -- the
            # length will not match and the client treats it as a failed transfer.
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
                want = min(CHUNK, available - pos, last - pos + 1)
                chunk = await asyncio.to_thread(_read_at, src, pos, want)
                if chunk:
                    pos += len(chunk)
                    waited = 0.0
                    yield chunk
                    continue

        if job.state == "done":
            # Ingest finished; drain the rest of the requested span from the
            # committed blob.
            try:
                final_size = final_path.stat().st_size
            except OSError:
                final_size = size
            stop = min(last, final_size - 1)
            while pos <= stop:
                chunk = await asyncio.to_thread(_read_at, final_path, pos, stop - pos + 1)
                if not chunk:
                    break
                pos += len(chunk)
                yield chunk
            return

        await asyncio.sleep(settings.stream_poll_interval_s)
        waited += settings.stream_poll_interval_s
        if pos == start and waited > settings.stream_start_timeout_s and not warned:
            # We never found the .incomplete file. Its naming is a
            # huggingface_hub implementation detail that shifts between
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
        return fh.read(max(0, min(length, CHUNK)))
