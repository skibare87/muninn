"""What a cached snapshot is SUPPOSED to hold, so /_cache/repos can say whether
it holds it.

THE DEFECT THIS EXISTS FOR. A prewarm killed part-way leaves a snapshot holding
whatever finished first -- typically the small files, because they finish
first. Listed next to a pin, 32 MB of a 52 GB repo read exactly like a finished
prewarm. The on-disk layout cannot tell those apart: huggingface_hub writes a
snapshot entry per file as each one lands, and nothing on disk records what the
full set was meant to be.

So the prewarm records it. Before snapshot_download starts, the job fetches the
revision's file listing (with sizes) and stores it here, keyed by commit. The
listing endpoint then judges completeness from local data only -- no upstream
call per repo, ever.

WHAT "EXPECTED" MEANS, and it is deliberately narrow:

  - It is what a prewarm ASKED FOR. A prewarm with allow_patterns legitimately
    fetches a subset; judging it against the whole repo would call every
    filtered prewarm incomplete. Several prewarms of one commit with different
    patterns expect the UNION of what they asked for, and a prewarm with no
    patterns expects the whole repo. The patterns are matched with
    huggingface_hub's own `filter_repo_objects`, the function snapshot_download
    uses, so "asked for" means exactly what the download meant by it.
  - It is UNKNOWN (`complete: null`) for a snapshot no prewarm listed -- one
    assembled file by file from client misses, or one whose listing call failed.
    Unknown is never reported as complete.
  - A file counts as present only if its size matches the listing where the
    listing gave one.

WHERE IT LIVES: beside the blobs, in the cache tree's `.xhc/manifests/`, not in
XHC_STATE_DIR. It describes those blobs, is regenerable from upstream, and is
only meaningful while they exist -- the same reasoning that keeps the viewer
cache in the tree (see statedir). If the blob disk is thrown away, the
manifests go with it, which is correct.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from pathlib import Path

from huggingface_hub.utils import filter_repo_objects

from . import statedir

log = logging.getLogger("xhc.manifests")

_DIR = "manifests"
# Distinct pattern sets remembered per commit. A handful is the realistic case;
# the bound only stops a script that varies patterns from growing a file.
_MAX_REQUESTS = 32


def _folder(repo_type: str, repo_id: str) -> Path:
    from .cachefs import repo_folder_name  # cachefs imports this module

    return statedir.hf_tree_dir() / _DIR / repo_folder_name(repo_id, repo_type)


def _path(repo_type: str, repo_id: str, commit: str) -> Path:
    return _folder(repo_type, repo_id) / f"{commit}.json"


def load(repo_type: str, repo_id: str, commit: str) -> dict | None:
    """The recorded manifest, or None if none was recorded or it is unreadable.

    Unreadable collapses to "unknown" here ON PURPOSE, and it is safe to: the
    only consumer reports completeness, and unknown renders as `null`, never as
    complete. Nothing is deleted or protected on the strength of this file.
    """
    p = _path(repo_type, repo_id, commit)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text())
        if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
            raise ValueError("not a manifest")
        return data
    except (OSError, ValueError) as exc:
        log.warning("manifest %s unreadable, completeness unknown: %s", p, exc)
        return None


def record(
    repo_type: str,
    repo_id: str,
    commit: str,
    files: dict[str, int | None],
    allow_patterns: list[str] | None,
) -> None:
    """Record the listing for `commit` and add `allow_patterns` to what was asked.

    The listing is replaced (it is the same commit, so it should be identical);
    the requests accumulate.
    """
    p = _path(repo_type, repo_id, commit)
    p.parent.mkdir(parents=True, exist_ok=True)
    prev = load(repo_type, repo_id, commit)
    requests: list = list(prev.get("requests", [])) if prev else []
    asked = sorted(allow_patterns) if allow_patterns else None
    if asked not in requests:
        requests.append(asked)
    requests = requests[-_MAX_REQUESTS:]
    body = {
        "version": 1,
        "repo_type": repo_type,
        "repo_id": repo_id,
        "commit": commit,
        "recorded_at": time.time(),
        "files": files,
        "requests": requests,
    }
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(body, sort_keys=True))
    os.replace(tmp, p)


def forget(repo_type: str, repo_id: str, commit: str | None = None) -> None:
    """Drop one commit's manifest, or the repo's whole folder."""
    try:
        if commit is None:
            shutil.rmtree(_folder(repo_type, repo_id), ignore_errors=True)
        else:
            _path(repo_type, repo_id, commit).unlink(missing_ok=True)
    except OSError as exc:
        log.warning("could not remove manifest for %s: %s", repo_id, exc)


def _expected(manifest: dict) -> tuple[dict[str, int | None], str, list | None]:
    files: dict[str, int | None] = manifest["files"]
    requests = manifest.get("requests") or [None]
    if any(r is None for r in requests):
        return dict(files), "repo", None
    names: set[str] = set()
    for pats in requests:
        names.update(filter_repo_objects(items=list(files), allow_patterns=pats))
    return {n: files[n] for n in names}, "allow_patterns", requests


def completeness(
    repo_type: str, repo_id: str, commit: str, present: dict[str, int]
) -> dict:
    """Judge one cached commit against its recorded manifest. Local only.

    `present` maps repo-relative path -> size on disk for what the snapshot
    holds. Returns the fields /_cache/repos exposes per revision.
    """
    m = load(repo_type, repo_id, commit)
    if m is None:
        return {
            "complete": None,
            "files_present": len(present),
            "files_expected": None,
            "bytes_present": sum(present.values()),
            "bytes_expected": None,
            "expected_scope": None,
        }
    expected, scope, patterns = _expected(m)
    have = [
        n for n, size in expected.items()
        if n in present and (size is None or present[n] == size)
    ]
    sizes = list(expected.values())
    out = {
        "complete": len(have) == len(expected),
        "files_present": len(have),
        "files_expected": len(expected),
        "bytes_present": sum(present[n] for n in have),
        "bytes_expected": sum(sizes) if all(s is not None for s in sizes) else None,
        "expected_scope": scope,
    }
    if patterns is not None:
        out["allow_patterns"] = patterns
    return out


def summarise(revisions: list[dict]) -> dict:
    """Repo-level roll-up of per-revision completeness.

    Three-valued AND: any incomplete revision makes the repo incomplete; else
    any unknown one makes it unknown; only all-known-complete is complete. The
    counts are sums, and an expected total is given only when every revision's
    is known -- a partial sum would read as the repo's total.
    """
    states = [r.get("complete") for r in revisions]
    if any(s is False for s in states):
        complete: bool | None = False
    elif not states or any(s is None for s in states):
        complete = None
    else:
        complete = True

    def total(field: str) -> int | None:
        vals = [r.get(field) for r in revisions]
        return sum(vals) if vals and all(v is not None for v in vals) else None

    return {
        "complete": complete,
        "files_present": total("files_present"),
        "files_expected": total("files_expected"),
        "bytes_present": total("bytes_present"),
        "bytes_expected": total("bytes_expected"),
    }
