#!/usr/bin/env python3
"""Benchmark cache scanning at fleet scale.

/_cache/repos and the eviction sweep both call huggingface_hub's
scan_cache_dir(), which stats every blob in the tree. On an 80 TB array that is
the most likely scaling pain point, so measure it rather than guess.

Builds a synthetic cache of sparse files (logical size only -- costs no real
disk) in the standard HF layout, then times a scan.

    python scripts/bench_scan.py --repos 500 --files-per-repo 30 --file-size 5G
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import parse_size


def build(root: Path, repos: int, files_per_repo: int, file_size: int, revisions: int) -> int:
    total_files = 0
    for r in range(repos):
        folder = root / f"models--synthetic--model-{r:05d}"
        blobs = folder / "blobs"
        blobs.mkdir(parents=True, exist_ok=True)
        (folder / "refs").mkdir(parents=True, exist_ok=True)
        for rev in range(revisions):
            commit = f"{r:016x}{rev:024x}"[:40]
            snap = folder / "snapshots" / commit
            snap.mkdir(parents=True, exist_ok=True)
            for f in range(files_per_repo):
                etag = f"{r:08x}{rev:04x}{f:028x}"[:40]
                blob = blobs / etag
                if not blob.exists():
                    # Sparse: allocates no real blocks, but reports file_size
                    # to stat(), which is exactly what scan_cache_dir reads.
                    with open(blob, "wb") as fh:
                        fh.truncate(file_size)
                link = snap / f"model-{f:05d}.safetensors"
                if not link.exists():
                    os.symlink(os.path.relpath(blob, snap), link)
                total_files += 1
            if rev == 0:
                (folder / "refs" / "main").write_text(commit)
    return total_files


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="./bench-cache")
    ap.add_argument("--repos", type=int, default=500)
    ap.add_argument("--files-per-repo", type=int, default=30)
    ap.add_argument("--revisions", type=int, default=1)
    ap.add_argument("--file-size", default="5G")
    ap.add_argument("--keep", action="store_true", help="do not delete the tree afterwards")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    size = parse_size(args.file_size)
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)

    print(
        f"building {args.repos} repos x {args.revisions} rev x {args.files_per_repo} files "
        f"@ {args.file_size} sparse -> {root}"
    )
    t0 = time.time()
    n = build(root, args.repos, args.files_per_repo, size, args.revisions)
    print(f"  built {n:,} files in {time.time() - t0:.1f}s")
    real = sum(f.stat().st_blocks * 512 for f in root.glob("**/blobs/*"))
    print(f"  logical: {n * size / 1e12:.1f} TB   actual disk: {real / 1e6:.1f} MB (sparse)")

    from huggingface_hub import scan_cache_dir

    print("\nscanning (cold):")
    t0 = time.time()
    info = scan_cache_dir(str(root))
    cold = time.time() - t0
    print(f"  {cold:.2f}s  repos={len(info.repos)}  size_on_disk={info.size_on_disk / 1e12:.1f} TB")

    print("scanning (warm, metadata cached by OS):")
    t0 = time.time()
    scan_cache_dir(str(root))
    warm = time.time() - t0
    print(f"  {warm:.2f}s")

    print(f"\nper-file cost: {1e6 * cold / n:.1f} us cold, {1e6 * warm / n:.1f} us warm")
    if cold > 5:
        print("\nNOTE: >5s per scan. The 30s view cache absorbs this for /_cache/repos,")
        print("but the eviction sweep pays it every XHC_EVICT_INTERVAL. Consider")
        print("raising that interval or moving to an incremental index.")

    if not args.keep:
        shutil.rmtree(root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
