"""Temp files from `ocistore._atomic_write` are never mistaken for manifests,
a live one is never removed, and a stale one is reclaimed.

THE DEFECT: `_atomic_write` writes `<name>.part<pid>` beside its target and
renames it into place. The GC's manifest walk excluded names by denylist
(`.meta`, dot-files, `.incomplete`), so a manifest temp read as an unreferenced
manifest with the digest `sha256:<hex>.part<pid>` and was swept. That reclaimed
leftovers by accident -- and, in the window between write and rename, deleted a
LIVE temp, so the writer's rename failed and the manifest write with it.

THE RULE: the GC walk counts a file as a blob or manifest only if its name is
exactly 64 lowercase hex characters. Temp files are reclaimed by the same
sweep, and on the same three guards, as stale `.incomplete` downloads: no
in-process owner, no lock held, idle past XHC_DOCKER_PARTIAL_MAX_AGE.

Fails against v0.9.29: test_a_manifest_temp_is_never_counted_or_swept_as_a_manifest
(the temp is swept as a manifest), test_gc_during_a_live_write_does_not_break_it
(the rename fails), and every test reading `partials["writes"]`.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import ocigc, ocistore
from app.config import settings

UP = "ghcr.io"
DAY = 24 * 3600
BODY = b'{"schemaVersion":2,"layers":[]}'


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "docker_dir", str(tmp_path / "docker"))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"), raising=False)
    monkeypatch.setattr(settings, "docker_capacity_bytes", 0, raising=False)
    ocistore.reset_stats_cache()
    return tmp_path


def _temp_beside(final: Path, suffix: str = ".part4242", age_s: float = DAY,
                 body: bytes = BODY) -> Path:
    final.parent.mkdir(parents=True, exist_ok=True)
    p = final.with_name(final.name + suffix)
    p.write_bytes(body)
    t = time.time() - age_s
    os.utime(p, (t, t))
    return p


def test_a_manifest_temp_is_never_counted_or_swept_as_a_manifest(store):
    """FAILS ON v0.9.29: collect() reports manifests == 1 and the temp is gone.

    Fresh, so the stale-temp rule keeps it: the only way it can disappear is by
    being treated as a manifest."""
    digest = ocistore.compute_digest(BODY)
    tmp = _temp_beside(ocistore.manifest_path(UP, digest), age_s=0)
    res = ocigc.collect()
    assert tmp.exists(), "a temp file was swept as an unreferenced manifest"
    assert res["manifests"] == 0
    assert all(item.path != tmp for item in ocigc._enumerate("manifests"))
    assert res["partials"]["writes"]["kept_young"] == 1


def test_only_exact_hex_names_are_enumerated(store):
    digest = ocistore.compute_digest(BODY)
    final = ocistore.manifest_path(UP, digest)
    final.parent.mkdir(parents=True, exist_ok=True)
    final.write_bytes(BODY)
    hexpart = final.name
    for odd in (hexpart + ".part1", hexpart + ".meta.part1",
                hexpart + ".part1.deadbeef", hexpart.upper(), hexpart[:-1],
                hexpart + "0", "." + hexpart, hexpart + ".meta"):
        (final.parent / odd).write_bytes(b"x")
    found = [i.path.name for i in ocigc._enumerate("manifests")]
    assert found == [hexpart]
    assert ocistore.stats(force=True)["manifests"] == 1


def test_gc_during_a_live_write_does_not_break_it(store, monkeypatch):
    """FAILS ON v0.9.29: the GC deletes the writer's temp and os.replace raises.

    The writer is held between writing its temp and renaming it; GC runs in
    that window with the age guard disabled, so only the owner and lock guards
    stand between it and the file."""
    monkeypatch.setattr(settings, "docker_partial_max_age_s", 0.0)
    digest = ocistore.compute_digest(BODY)
    target = ocistore.manifest_path(UP, digest)
    at_rename = threading.Event()
    release = threading.Event()
    real_replace = os.replace
    seen_temps: list[Path] = []

    def held_replace(src, dst, *a, **kw):
        if Path(dst) == target:
            seen_temps.append(Path(src))
            at_rename.set()
            assert release.wait(10), "test never released the writer"
        return real_replace(src, dst, *a, **kw)

    monkeypatch.setattr(os, "replace", held_replace)
    errors: list[BaseException] = []

    def writer():
        try:
            ocistore.store_manifest(UP, digest, BODY, "application/vnd.oci.image.manifest.v1+json")
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assert below
            errors.append(exc)

    t = threading.Thread(target=writer)
    t.start()
    try:
        assert at_rename.wait(10), "writer never reached its rename"
        res = ocigc.collect()
        assert seen_temps and seen_temps[0].exists(), "GC removed a live temp"
    finally:
        release.set()
        t.join(10)
    assert not errors, errors
    assert target.read_bytes() == BODY
    assert res["manifests"] == 0
    assert res["partials"]["writes"]["removed"] == 0
    assert res["partials"]["writes"]["kept_owned"] == 1


def test_the_writer_holds_the_lock_so_another_process_sees_it(store, monkeypatch):
    """The in-process owner table cannot reach a second process; the lock can.
    Blind the owner check and the lock alone must keep a live temp."""
    monkeypatch.setattr(settings, "docker_partial_max_age_s", 0.0)
    monkeypatch.setattr(ocistore, "owns_write", lambda _p: False)
    digest = ocistore.compute_digest(BODY)
    target = ocistore.manifest_path(UP, digest)
    at_rename = threading.Event()
    release = threading.Event()
    real_replace = os.replace
    locked: list[bool] = []

    def held_replace(src, dst, *a, **kw):
        if Path(dst) == target:
            locked.append(ocistore.partial_is_locked(Path(src)))
            at_rename.set()
            release.wait(10)
        return real_replace(src, dst, *a, **kw)

    monkeypatch.setattr(os, "replace", held_replace)
    t = threading.Thread(target=ocistore.store_manifest,
                         args=(UP, digest, BODY, "application/json"))
    t.start()
    try:
        assert at_rename.wait(10)
        res = ocigc.sweep_partials()
    finally:
        release.set()
        t.join(10)
    assert locked == [True], "a live temp was not locked by its writer"
    assert res["writes"]["kept_locked"] == 1 and res["writes"]["removed"] == 0
    assert target.read_bytes() == BODY


@pytest.mark.parametrize("suffix", [".part4242", ".part4242.0a1b2c3d"])
def test_a_stale_manifest_temp_from_a_dead_writer_is_reclaimed(store, suffix):
    digest = ocistore.compute_digest(BODY)
    tmp = _temp_beside(ocistore.manifest_path(UP, digest), suffix=suffix)
    meta_tmp = _temp_beside(ocistore.manifest_meta_path(UP, digest), suffix=suffix)
    res = ocigc.collect()
    assert not tmp.exists() and not meta_tmp.exists()
    w = res["partials"]["writes"]
    assert w["scanned"] == 2 and w["removed"] == 2
    assert w["freed_bytes"] == 2 * len(BODY)
    assert res["manifests"] == 0, "reclaimed as a temp, not swept as a manifest"
    # The aggregate counts them too, so nothing is hidden from the top line.
    assert res["partials"]["removed"] == 2


def test_a_stale_tag_temp_is_reclaimed_and_the_tag_is_not(store):
    ocistore.write_tag(UP, "library/alpine", "3", "default",
                       ocistore.compute_digest(BODY), "application/json")
    tag = ocistore.tag_path(UP, "library/alpine", "3", "default")
    tmp = _temp_beside(tag)
    res = ocigc.sweep_partials()
    assert not tmp.exists() and tag.exists()
    assert res["writes"]["removed"] == 1


def test_a_fresh_temp_is_kept(store):
    digest = ocistore.compute_digest(BODY)
    tmp = _temp_beside(ocistore.manifest_path(UP, digest), age_s=60)
    res = ocigc.sweep_partials()
    assert tmp.exists()
    assert res["writes"]["kept_young"] == 1 and res["writes"]["removed"] == 0


def test_a_file_that_is_neither_final_nor_temp_is_left_alone(store):
    """Positive match both ways: an unrecognised name is not ours to delete."""
    digest = ocistore.compute_digest(BODY)
    odd = _temp_beside(ocistore.manifest_path(UP, digest), suffix=".bak")
    res = ocigc.collect()
    assert odd.exists()
    assert res["manifests"] == 0 and res["partials"]["writes"]["scanned"] == 0


def test_a_failed_write_leaves_no_temp(store, monkeypatch):
    target = ocistore.manifest_path(UP, ocistore.compute_digest(BODY))

    def boom(*_a, **_k):
        raise OSError("disk went away")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        ocistore._atomic_write(target, BODY)
    assert list(target.parent.iterdir()) == []
    assert not ocistore._writing
