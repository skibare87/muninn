"""Warn at startup when the container's memory limit cannot hold the ingests
it is configured to run.

The figures are MEASUREMENTS, not guarantees, and they belong to hf-xet rather
than to this code: a plain hf_hub_download with no Muninn in the path peaked at
about 2.3 GiB of anonymous memory on one 3.9 GB file (hf-xet 1.6.0), levelled
off around 2-2.5 GiB for single files up to 50 GB, and climbed to about 3 GiB
over a four-file snapshot fetched one file at a time. With HF_HUB_DISABLE_XET=1
the same file peaked at about 50 MiB.

So this warns rather than refuses: a limit that is too small is likely to be
OOM-killed mid-ingest, which the job ledger reports as `interrupted`, but the
number it is compared against is an estimate from one library version.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger("xhc.memcheck")

GIB = 1 << 30
# Per concurrent xet ingest, rounded up from the measurements above.
XET_PER_INGEST = int(2.5 * GIB)
# Each extra file in flight inside one snapshot, measured: 8 workers peaked
# about 1.5 GiB above 1 worker on a four-file snapshot.
XET_PER_EXTRA_FILE = int(0.5 * GIB)
BASELINE = 256 << 20


def cgroup_limit(path: str = "/sys/fs/cgroup/memory.max") -> int | None:
    """The cgroup v2 memory limit in bytes, or None when unlimited or unknown."""
    try:
        raw = Path(path).read_text().strip()
    except OSError:
        return None
    if raw == "max" or not raw.isdigit():
        return None
    return int(raw)


def estimate(ingest_concurrency: int, snapshot_max_workers: int, xet: bool) -> int:
    if not xet:
        return BASELINE + ingest_concurrency * (64 << 20)
    per_job = XET_PER_INGEST + max(0, snapshot_max_workers - 1) * XET_PER_EXTRA_FILE
    return BASELINE + ingest_concurrency * per_job


def check(ingest_concurrency: int, snapshot_max_workers: int, limit: int | None = None) -> str | None:
    """Return the warning text when the limit looks too small, else None."""
    limit = cgroup_limit() if limit is None else limit
    if limit is None:
        return None
    xet = os.environ.get("HF_HUB_DISABLE_XET", "").strip().lower() not in ("1", "true", "yes")
    need = estimate(ingest_concurrency, snapshot_max_workers, xet)
    if limit >= need:
        return None
    return (
        f"memory limit {limit / GIB:.1f} GiB is below the ~{need / GIB:.1f} GiB measured for "
        f"XHC_INGEST_CONCURRENCY={ingest_concurrency} x XHC_SNAPSHOT_MAX_WORKERS="
        f"{snapshot_max_workers} with hf-xet {'on' if xet else 'off'}. Ingests are likely to be "
        "OOM-killed and will show as `interrupted`. Lower XHC_INGEST_CONCURRENCY or raise the "
        "limit; see 'Sizing memory for ingest' in the README."
    )
