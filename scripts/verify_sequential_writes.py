#!/usr/bin/env python3
"""Verify that hf_xet writes reconstructed files as a strict prefix.

XHC_MISS_POLICY=stream tail-follows the partial file that an in-flight ingest is
writing. That is only safe if the bytes land front-to-back. If hf_xet
reconstructs terms at parallel file offsets, a partial file is NOT a valid
prefix -- streaming it would serve holes (zeroes) that the client accepts as
real data.

HOW THIS TESTS IT
-----------------
We do exactly what the streaming code path does: watch the .incomplete file
grow, and read each byte ONCE, in order, at the moment it first becomes
available -- hashing as we go. That byte stream is what a streaming client
would have received. At the end we hash the finished file the same way and
compare at each sample point.

This is strictly stronger than re-hashing prefixes: if a region is a hole when
we read past it and is filled in later, we captured the hole, exactly as a real
client would have. It is also O(filesize) rather than O(samples x filesize), so
we can sample aggressively enough to catch short-lived holes.

Usage:
    python scripts/verify_sequential_writes.py \\
        --repo-id Qwen/Qwen2.5-7B-Instruct \\
        --filename model-00001-of-00004.safetensors

Use a genuinely large file (multi-GB). A file that completes in one shot proves
nothing -- the script reports INCONCLUSIVE if it could not take enough samples.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import threading
import time
from pathlib import Path

READ_BLOCK = 1 << 22  # 4 MiB
MIN_SAMPLES = 5


def find_incomplete(cache_dir: Path) -> Path | None:
    try:
        candidates = [p for p in cache_dir.glob("**/blobs/*.incomplete") if p.is_file()]
    except OSError:
        return None
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo-id", required=True)
    ap.add_argument("--filename", required=True)
    ap.add_argument("--revision", default="main")
    ap.add_argument("--repo-type", default="model", choices=["model", "dataset", "space"])
    ap.add_argument("--cache-dir", default=os.environ.get("HF_HUB_CACHE", "./verify-cache"))
    ap.add_argument("--samples", type=int, default=200)
    ap.add_argument("--interval", type=float, default=0.2)
    args = ap.parse_args()

    seq = os.environ.get("HF_XET_RECONSTRUCT_WRITE_SEQUENTIALLY", "")
    print(f"HF_XET_RECONSTRUCT_WRITE_SEQUENTIALLY={seq!r}")
    if os.environ.get("HF_HUB_DISABLE_XET", "").lower() in ("1", "true", "yes"):
        print("ERROR: HF_HUB_DISABLE_XET is set; this test only means anything with Xet ON.")
        return 2

    from huggingface_hub import hf_hub_download

    cache_dir = Path(args.cache_dir).resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)
    result: dict = {}

    def download() -> None:
        try:
            result["path"] = hf_hub_download(
                repo_id=args.repo_id,
                filename=args.filename,
                revision=args.revision,
                repo_type=args.repo_type,
                cache_dir=str(cache_dir),
            )
        except Exception as exc:  # noqa: BLE001 - surfaced below
            result["error"] = exc

    t = threading.Thread(target=download, daemon=True)
    t.start()

    # --- read the partial file exactly as a streaming client would ---------
    live = hashlib.sha256()
    pos = 0
    samples: list[tuple[int, str]] = []
    first_seen_size: int | None = None
    inc_path: Path | None = None

    print("following partial file (reading each byte once, in order)...")
    while t.is_alive() and len(samples) < args.samples:
        time.sleep(args.interval)
        if inc_path is None or not inc_path.exists():
            inc_path = find_incomplete(cache_dir)
            if inc_path is None:
                continue
        try:
            size = inc_path.stat().st_size
        except OSError:
            continue
        if first_seen_size is None:
            first_seen_size = size
        if size <= pos:
            continue
        try:
            with open(inc_path, "rb") as fh:
                fh.seek(pos)
                while pos < size:
                    block = fh.read(min(READ_BLOCK, size - pos))
                    if not block:
                        break
                    live.update(block)
                    pos += len(block)
        except OSError:
            continue
        samples.append((pos, live.hexdigest()))

    t.join()
    if "error" in result:
        print(f"download failed: {result['error']}")
        return 2

    final = Path(result["path"])
    final_size = final.stat().st_size
    print(f"download complete: {final_size:,} bytes")
    if first_seen_size is not None:
        print(
            f"partial file first seen at {first_seen_size:,} bytes "
            f"({100 * first_seen_size / final_size:.1f}% of final)"
        )
        if first_seen_size >= final_size:
            print(
                "  NOTE: the partial file appeared at full size immediately -- that is "
                "preallocation, and a strong hint that writes are NOT sequential."
            )
    print(f"captured {len(samples)} samples, followed {pos:,} bytes live")

    if len(samples) < MIN_SAMPLES:
        print(
            f"\nINCONCLUSIVE: only {len(samples)} sample(s) (need >={MIN_SAMPLES}). "
            "The download finished too fast to observe. Retry with a larger file "
            "or a smaller --interval."
        )
        return 3

    # --- replay the finished file the same way and compare ----------------
    print("\ncomparing the live byte stream against the finished file:")
    ref = hashlib.sha256()
    refpos = 0
    failures = 0
    with open(final, "rb") as fh:
        for i, (size, digest) in enumerate(samples, 1):
            if size > final_size:
                print(f"  sample {i} ({size:,}): OVER-READ past final size {final_size:,}")
                failures += 1
                continue
            while refpos < size:
                block = fh.read(min(READ_BLOCK, size - refpos))
                if not block:
                    break
                ref.update(block)
                refpos += len(block)
            match = ref.hexdigest() == digest
            if not match:
                failures += 1
            # Only print the first divergence and a few checkpoints; 200 lines
            # of MATCH is noise.
            if not match or i == 1 or i == len(samples) or i % 25 == 0:
                print(f"  sample {i:>3} at {size:>13,} bytes: {'MATCH' if match else 'MISMATCH'}")

    print()
    if failures == 0:
        print(f"PASS: all {len(samples)} samples were valid prefixes.")
        print("      XHC_MISS_POLICY=stream is safe with this configuration.")
        return 0
    print(f"FAIL: {failures}/{len(samples)} samples diverged -- writes are NOT sequential.")
    print("      Do NOT use XHC_MISS_POLICY=stream; use 'redirect' or 'wait'.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
