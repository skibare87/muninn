"""Deleting a tag frees its layers, and only its layers.

the maintainer's requirement, 2026-09-01: *"delete by tag is fine alone as long as it
removes all layers not referenced by another tag same for listing by tag"*.

That is a guarantee about the interaction between two things -- dropping a tag
root, and mark-and-sweep collecting what became unreachable -- so it cannot be
tested from either side alone. A test of `evict_image` proves a file was
unlinked. A test of `sweep` proves unreferenced blobs go. NEITHER proves that a
layer shared with a surviving tag SURVIVES, which is the half that would lose
data if it broke.

The shared layer is the whole point. Container images share bases constantly,
so an eviction that over-collects would delete the base of every other image on
the node the first time anyone removed a tag.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import ocigc, ocistore
from app.config import settings

UP = "ghcr.io"
MEDIA = "application/vnd.oci.image.manifest.v1+json"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "docker_dir", str(tmp_path))
    ocistore.reset_stats_cache()
    return tmp_path


def _blob(data: bytes) -> str:
    digest = ocistore.compute_digest(data)
    p = ocistore.blob_path(UP, digest)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return digest


def _image(tag: str, config: bytes, layers: list[bytes]) -> dict:
    cfg = _blob(config)
    ls = [_blob(x) for x in layers]
    body = json.dumps({
        "schemaVersion": 2,
        "config": {"digest": cfg, "size": len(config),
                   "mediaType": "application/vnd.oci.image.config.v1+json"},
        "layers": [{"digest": d, "size": 1,
                    "mediaType": "application/vnd.oci.image.layer.v1.tar"}
                   for d in ls],
    }).encode()
    digest = ocistore.compute_digest(body)
    ocistore.store_manifest(UP, digest, body, MEDIA)
    ocistore.write_tag(UP, "org/app", tag, ocistore.accept_fingerprint(MEDIA),
                       digest, MEDIA)
    return {"config": cfg, "layers": ls, "manifest": digest}


def _drop(tag: str) -> None:
    """What DELETE /_cache/docker/images does: unlink the tag root."""
    for t in ocigc.list_tags():
        if t.key.endswith(f":{tag}"):
            t.path.unlink()


def test_dropping_a_tag_frees_its_exclusive_layers_and_spares_shared_ones(store):
    shared = b"SHARED-BASE-LAYER"
    a = _image("a", b"CONFIG-A", [shared, b"ONLY-IN-A"])
    b = _image("b", b"CONFIG-B", [shared, b"ONLY-IN-B"])
    assert a["layers"][0] == b["layers"][0], "fixture must actually share a layer"

    _drop("a")
    ocigc.sweep(ocigc.mark(strict=False))

    assert not ocistore.blob_path(UP, a["layers"][1]).exists(), \
        "a layer exclusive to the deleted tag survived"
    assert not ocistore.blob_path(UP, a["config"]).exists(), \
        "the deleted tag's config survived"
    assert not ocistore.manifest_path(UP, a["manifest"]).exists(), \
        "the deleted tag's manifest survived"

    assert ocistore.blob_path(UP, a["layers"][0]).exists(), \
        "a layer still referenced by a surviving tag was deleted -- this would " \
        "break every image sharing that base"
    assert ocistore.blob_path(UP, b["layers"][1]).exists()
    assert ocistore.blob_path(UP, b["config"]).exists()
    assert ocistore.manifest_path(UP, b["manifest"]).exists()


def test_dropping_the_last_tag_frees_everything(store):
    img = _image("only", b"CONFIG", [b"L1", b"L2"])
    _drop("only")
    result = ocigc.sweep(ocigc.mark(strict=False))
    for d in [img["config"], *img["layers"]]:
        assert not ocistore.blob_path(UP, d).exists()
    assert result["freed_bytes"] > 0


def test_a_pin_keeps_a_dropped_tags_layers(store, monkeypatch):
    """A pin is a root in its own right.

    Someone who pinned an image expects it to survive a tag being removed --
    that is what pinning means, and collecting it anyway would be the
    irreversible half of the mistake.
    """
    img = _image("pinned", b"CONFIG-P", [b"P1"])
    monkeypatch.setattr(ocigc, "load_pins",
                        lambda strict=True: {f"{UP}/org/app@{img['manifest']}"})
    _drop("pinned")
    ocigc.sweep(ocigc.mark(strict=False))
    assert ocistore.blob_path(UP, img["layers"][0]).exists(), \
        "a pinned image's layer was collected after its tag was dropped"


def test_listing_reports_tags_that_exist_and_not_ones_that_do_not(store):
    """The listing half of the same requirement."""
    _image("keep", b"C1", [b"L1"])
    _image("go", b"C2", [b"L2"])
    keys = {t.key for t in ocigc.list_tags()}
    assert any(k.endswith(":keep") for k in keys)
    assert any(k.endswith(":go") for k in keys)

    _drop("go")
    keys = {t.key for t in ocigc.list_tags()}
    assert any(k.endswith(":keep") for k in keys)
    assert not any(k.endswith(":go") for k in keys), \
        "a deleted tag was still listed"


def test_under_budget_but_disk_nearly_full_is_loud(monkeypatch, caplog, tmp_path):
    """Under budget is not the same as having room.

    Eviction compares this cache's own size against its own budget and never
    consults free space, so on a shared filesystem -- ZFS datasets in one pool,
    another tenant, a runaway log -- the cache can sit far under budget and
    decline to evict while every write fails with ENOSPC.

    Acting on that is a real decision (freeing our own data may not recover
    space consumed elsewhere) and is deliberately not taken. Being SILENT about
    it is not a decision, it is the defect: "under high water" is a true
    statement about the wrong limit.
    """
    import logging

    from app import cachefs

    monkeypatch.setattr(cachefs, "load_pins", lambda strict=False: set())
    monkeypatch.setattr(cachefs, "load_orphans", lambda strict=False: {})
    monkeypatch.setattr(cachefs, "protected_keys", lambda strict=False: set())
    monkeypatch.setattr(cachefs, "disk_stats", lambda: {
        "fs_total": 1000, "fs_used": 990, "fs_free": 10,   # 99% full
        "capacity": 1000, "capacity_source": "XHC_CACHE_MAX_SIZE",
    })
    monkeypatch.setattr(cachefs, "scan_cache_dir",
                        lambda d: type("I", (), {"size_on_disk": 1})())

    with caplog.at_level(logging.WARNING):
        out = cachefs._evict_sync()

    assert out["reason"] == "under high water"
    assert any("UNDER ITS OWN BUDGET" in r.message for r in caplog.records), (
        "declining to evict while the disk is 99% full must say which limit "
        "it is looking at, or the log reads as healthy"
    )


def test_plenty_of_free_space_stays_quiet(monkeypatch, caplog):
    """The negative control: the warning must not fire on a healthy disk, or
    it becomes noise and gets filtered, which is the same as not having it."""
    import logging

    from app import cachefs

    monkeypatch.setattr(cachefs, "load_pins", lambda strict=False: set())
    monkeypatch.setattr(cachefs, "load_orphans", lambda strict=False: {})
    monkeypatch.setattr(cachefs, "protected_keys", lambda strict=False: set())
    monkeypatch.setattr(cachefs, "disk_stats", lambda: {
        "fs_total": 1000, "fs_used": 100, "fs_free": 900,
        "capacity": 1000, "capacity_source": "XHC_CACHE_MAX_SIZE",
    })
    monkeypatch.setattr(cachefs, "scan_cache_dir",
                        lambda d: type("I", (), {"size_on_disk": 1})())

    with caplog.at_level(logging.WARNING):
        cachefs._evict_sync()
    assert not any("UNDER ITS OWN BUDGET" in r.message for r in caplog.records)
