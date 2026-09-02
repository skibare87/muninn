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
