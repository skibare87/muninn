"""Where durable state lives. The ONE place that answers that question.

Durable state is the small set of files whose loss changes behaviour rather
than costing a re-fetch: pins, orphan marks and the runtime policy. By default
it sits in a `.xhc/` directory inside each cache tree, which is where it has
always been:

    <HF cache>/.xhc/        pins.json  orphans.json  policy.json
    <docker dir>/.xhc/      pins.json  orphans.json

`XHC_STATE_DIR` moves it to its own volume, one subdirectory per protocol so
the two pin files can never be the same file:

    $XHC_STATE_DIR/hf/      replaces <HF cache>/.xhc/
    $XHC_STATE_DIR/oci/     replaces <docker dir>/.xhc/

That lets an operator put blobs on disposable local disk and keep protection on
something that survives the disk. Every reader and writer of these files goes
through `hf_file()` / `oci_file()`, so no code path can read one location while
another writes the other.

**Migration is per file and copies bytes, it never parses.** When the state dir
is set, a file is absent there and the in-tree file exists, the in-tree file is
copied across. It happens at startup (`prepare()`, which logs it) and again,
lazily, the first time any path resolves that file -- so nothing that runs
before startup can read an empty state dir as "nothing is pinned" while the old
file still holds pins. Copying bytes rather than parsing keeps the distinction
the rest of the code depends on: an ABSENT file means nothing is protected, an
UNREADABLE one means unknown and fails closed. A corrupt old file arrives
corrupt, and load_pins refuses it exactly as it would have in place.

The old file is left where it was, so unsetting the variable returns to the
state as it stood at migration time. It is not kept in sync after that.

The HF viewer/datasets-server response cache is NOT durable state: it is
regenerable from upstream and can be large, so it stays with the blobs in the
cache tree (`hf_tree_dir()`), on purpose. So do the prewarm manifests
(`.xhc/manifests/`, see manifests.py): they describe those blobs and are only
meaningful while the blobs exist.

Pending store-forward pushes live here too, under `oci/pending/`, but they are
owned by ocipush.py rather than this module: they MOVE on migration instead of
being copied (a stale second copy of an obligation would re-send a manifest),
and they carry the blob bytes they need, because an obligation to forward bytes
that went with the disk is still a lost push.

The job ledgers -- `jobs.json` for HF ingest, `prewarm.json` for OCI prewarms
-- do live here, because their whole purpose is to outlive the process -- but
they are history, not protection, and an unreadable one never stops the service
(see ledger.LedgeredJobs.load_ledger).
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from .config import settings

log = logging.getLogger("xhc.statedir")

# The in-tree directory name. Also what the OCI walk must skip, in both modes:
# after a migration the old directory is still there.
TREE_DIR_NAME = ".xhc"

# Durable files per protocol. Migration copies exactly these; anything else in
# the old directory (the viewer cache, temp files) stays put.
#
# jobs.json (the ingest job ledger) also lives in the HF state dir but is
# deliberately NOT listed: a failed eager migration here stops the boot, which
# is right for protection and wrong for job history. hf_file() still carries it
# across lazily, and the ledger's loader treats a failure as non-fatal. The same
# holds for prewarm.json (the OCI prewarm ledger) and OCI_FILES.
HF_FILES = ("pins.json", "orphans.json", "policy.json")
OCI_FILES = ("pins.json", "orphans.json")


class StateDirError(RuntimeError):
    """XHC_STATE_DIR is set and cannot be used. Raised at startup, so the
    service refuses to boot instead of falling back to the cache tree -- a
    fallback would put protection back on the disk the operator just told us
    is disposable, and nothing would say so."""


def _configured() -> Path | None:
    raw = getattr(settings, "state_dir", None)
    return Path(raw) if raw else None


def hf_tree_dir() -> Path:
    """The in-tree `.xhc` directory of the HF cache, whatever the mode."""
    return Path(settings.cache_dir) / TREE_DIR_NAME


def oci_tree_dir() -> Path:
    """The in-tree `.xhc` directory of the docker store, whatever the mode."""
    return Path(settings.docker_dir) / TREE_DIR_NAME


def hf_dir() -> Path:
    base = _configured()
    d = base / "hf" if base else hf_tree_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def oci_dir() -> Path:
    base = _configured()
    d = base / "oci" if base else oci_tree_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _migrate_one(old: Path, new: Path) -> bool:
    """Copy `old` to `new` if `new` is absent and `old` exists. Atomic on the
    destination: a crash mid-copy leaves a temp file, never a truncated
    pins.json that would then read as corrupt or, worse, as short."""
    if new.exists() or not old.is_file():
        return False
    tmp = new.with_name(new.name + ".migrating")
    try:
        shutil.copyfile(old, tmp)
        os.replace(tmp, new)
    except OSError as exc:
        # Neither "start empty" nor "keep using the old file" is safe here: the
        # first drops protection, the second splits readers across two places.
        raise StateDirError(
            f"XHC_STATE_DIR: could not migrate {old} to {new}: {exc}. Refusing to "
            "continue with empty state while the old file exists."
        ) from exc
    log.warning("migrated %s -> %s (the old file is left in place and is no "
                "longer read while XHC_STATE_DIR is set)", old, new)
    return True


def hf_file(name: str) -> Path:
    d = hf_dir()
    if _configured():
        _migrate_one(hf_tree_dir() / name, d / name)
    return d / name


def oci_file(name: str) -> Path:
    d = oci_dir()
    if _configured():
        _migrate_one(oci_tree_dir() / name, d / name)
    return d / name


def is_oci_state_entry(path: Path) -> bool:
    """True for a top-level entry of the docker store that holds state rather
    than an upstream: the in-tree `.xhc` (present in default mode, and left
    behind after a migration), or the state dir itself if an operator placed it
    inside the store."""
    if path.name == TREE_DIR_NAME:
        return True
    base = _configured()
    if base is None:
        return False
    try:
        p, b = path.resolve(), base.resolve()
    except OSError:
        return False
    return p == b or b.is_relative_to(p) or p.is_relative_to(b)


def prepare() -> None:
    """Startup: validate the state dir and migrate. No-op when unset.

    Raises StateDirError if the directory cannot be created or written.
    """
    base = _configured()
    if base is None:
        return
    targets = [("hf", hf_tree_dir(), HF_FILES)]
    if settings.docker_enabled or oci_tree_dir().is_dir():
        targets.append(("oci", oci_tree_dir(), OCI_FILES))
    for sub, old_dir, names in targets:
        d = base / sub
        probe = d / ".write-probe"
        try:
            d.mkdir(parents=True, exist_ok=True)
            probe.write_text("ok")
            probe.unlink()
        except OSError as exc:
            raise StateDirError(
                f"XHC_STATE_DIR={base} is set but {d} cannot be created or "
                f"written: {exc}. Fix the mount or unset XHC_STATE_DIR; not "
                "falling back to the cache tree."
            ) from exc
        for name in names:
            _migrate_one(old_dir / name, d / name)
    log.info("durable state in %s (hf/, oci/)", base)
