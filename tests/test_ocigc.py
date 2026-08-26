"""Mark-and-sweep for the OCI cache.

The property that matters and is easy to get wrong: a blob referenced by a
retained manifest must NEVER be swept. Naive LRU gets this wrong and the damage
does not show up until someone pulls the image, long after the eviction.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("HF_HUB_CACHE", "/tmp/xhc-test-cache")
os.environ.setdefault("XHC_DOCKER_DIR", "/tmp/xhc-test-docker")

from app import ocigc, ocistore
from app.cachefs import StateUnavailable
from app.config import settings

UP = "ghcr.io"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "docker_dir", str(tmp_path))
    monkeypatch.setattr(settings, "orphan_policy", "retain")
    ocistore.reset_stats_cache()
    return tmp_path


def _blob(body: bytes) -> str:
    d = ocistore.compute_digest(body)
    p = ocistore.blob_path(UP, d)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(body)
    return d


def _manifest(doc: dict, media: str) -> str:
    body = json.dumps(doc).encode()
    d = ocistore.compute_digest(body)
    ocistore.store_manifest(UP, d, body, media)
    return d


def _image(repo: str, tag: str, layers: list[bytes], cfg: bytes = b"config"):
    cfg_d = _blob(cfg)
    layer_ds = [_blob(b) for b in layers]
    man = _manifest(
        {
            "schemaVersion": 2,
            "config": {"digest": cfg_d},
            "layers": [{"digest": d} for d in layer_ds],
        },
        "application/vnd.oci.image.manifest.v1+json",
    )
    ocistore.write_tag(UP, repo, tag, "fp", man, "application/vnd.oci.image.manifest.v1+json")
    return man, cfg_d, layer_ds


# -- marking ----------------------------------------------------------------


def test_tagged_image_protects_its_config_and_layers(store):
    man, cfg, layers = _image("org/app", "v1", [b"layerA", b"layerB"])
    m = ocigc.mark()
    assert m.protects_manifest(UP, man)
    assert m.protects_blob(UP, cfg)
    for d in layers:
        assert m.protects_blob(UP, d)


def test_multi_arch_index_protects_every_platform_manifest_and_its_layers(store):
    """An index points at per-platform manifests which point at layers. A walk
    that stops at the index would sweep every layer of a multi-arch image."""
    amd_layer, arm_layer = _blob(b"amd64-layer"), _blob(b"arm64-layer")
    amd = _manifest(
        {"config": {"digest": _blob(b"c1")}, "layers": [{"digest": amd_layer}]},
        "application/vnd.oci.image.manifest.v1+json",
    )
    arm = _manifest(
        {"config": {"digest": _blob(b"c2")}, "layers": [{"digest": arm_layer}]},
        "application/vnd.oci.image.manifest.v1+json",
    )
    index = _manifest(
        {"manifests": [{"digest": amd}, {"digest": arm}]},
        "application/vnd.oci.image.index.v1+json",
    )
    ocistore.write_tag(UP, "org/multi", "latest", "fp", index, "index")

    m = ocigc.mark()
    for d in (index, amd, arm):
        assert m.protects_manifest(UP, d)
    assert m.protects_blob(UP, amd_layer)
    assert m.protects_blob(UP, arm_layer)


def test_shared_base_layer_survives_dropping_one_of_two_images(store):
    """Two images sharing a base layer store it once; removing one tag must not
    take the layer the other still needs."""
    shared = b"base-layer"
    _image("org/a", "v1", [shared, b"only-a"])
    _, _, b_layers = _image("org/b", "v1", [shared, b"only-b"])
    shared_d = ocistore.compute_digest(shared)

    ocigc.list_tags()[0]  # sanity: tags exist
    (ocistore.tag_path(UP, "org/a", "v1", "fp")).unlink()
    m = ocigc.mark()
    assert m.protects_blob(UP, shared_d), "shared base layer was not protected"
    for d in b_layers:
        assert m.protects_blob(UP, d)


# -- sweeping ---------------------------------------------------------------


def test_sweep_removes_only_unreferenced_blobs(store):
    _, cfg, layers = _image("org/app", "v1", [b"kept"])
    orphan_blob = _blob(b"referenced-by-nothing")

    res = ocigc.sweep(ocigc.mark())
    assert res["blobs"] == 1
    assert not ocistore.blob_path(UP, orphan_blob).exists()
    assert ocistore.blob_path(UP, cfg).exists()
    for d in layers:
        assert ocistore.blob_path(UP, d).exists()


def test_sweep_never_touches_a_blob_a_retained_manifest_needs(store):
    """The whole point. Proven by pulling the image back after a forced GC."""
    man, cfg, layers = _image("org/app", "v1", [b"L1", b"L2"])
    ocigc.sweep(ocigc.mark())
    held = ocistore.load_manifest(UP, man)
    assert held is not None
    doc = json.loads(held.body)
    for entry in [doc["config"], *doc["layers"]]:
        assert ocistore.blob_path(UP, entry["digest"]).is_file(), entry["digest"]


# -- pins -------------------------------------------------------------------


def test_pin_by_digest_retains_the_whole_closure_with_no_tag(store):
    """A pin that keeps the manifest but loses its layers is worse than no pin:
    it looks intact until someone pulls it."""
    man, cfg, layers = _image("org/app", "v1", [b"L1", b"L2"])
    ocistore.tag_path(UP, "org/app", "v1", "fp").unlink()   # no tag points at it now
    ocigc.save_pins({f"{UP}/org/app@{man}"})

    ocigc.sweep(ocigc.mark())
    assert ocistore.load_manifest(UP, man) is not None
    assert ocistore.blob_path(UP, cfg).is_file()
    for d in layers:
        assert ocistore.blob_path(UP, d).is_file()


def test_without_the_pin_the_same_closure_is_swept(store):
    """Confirms the previous test passes because of the pin, not by accident."""
    man, cfg, layers = _image("org/app", "v1", [b"L1", b"L2"])
    ocistore.tag_path(UP, "org/app", "v1", "fp").unlink()
    ocigc.sweep(ocigc.mark())
    assert ocistore.load_manifest(UP, man) is None
    assert not ocistore.blob_path(UP, cfg).exists()
    for d in layers:
        assert not ocistore.blob_path(UP, d).exists()


# -- fail closed ------------------------------------------------------------


def test_gc_refuses_when_pins_are_unreadable(store):
    """an internal issue's lesson, built in rather than retrofitted: unknown protection
    must stop deletion, not enable it."""
    _image("org/app", "v1", [b"L1"])
    (store / ".xhc").mkdir(parents=True, exist_ok=True)
    (store / ".xhc" / "pins.json").write_text("}{ not json")

    res = ocigc.collect()
    assert res["refused"] is True
    assert res["freed_bytes"] == 0
    assert res["blobs"] == 0
    with pytest.raises(StateUnavailable):
        ocigc.load_pins(strict=True)


def test_absent_pins_file_is_not_an_error(store):
    _image("org/app", "v1", [b"L1"])
    assert ocigc.load_pins(strict=True) == set()
    assert ocigc.collect()["refused"] is False


# -- capacity ---------------------------------------------------------------


def test_capacity_pressure_drops_lru_tags_but_never_pinned_or_orphaned(store, monkeypatch):
    _image("org/keep-pinned", "v1", [b"P" * 4096])
    _image("org/keep-orphan", "v1", [b"O" * 4096])
    _image("org/droppable", "v1", [b"D" * 4096])

    ocigc.save_pins({f"{UP}/org/keep-pinned:v1"})
    ocigc.save_orphans({f"{UP}/org/keep-orphan:v1": {"marked_at": 0}})
    # force pressure: tiny capacity so the high-water mark is already breached
    monkeypatch.setattr(settings, "docker_capacity_bytes", 1024)

    res = ocigc.collect()
    assert res["refused"] is False
    assert res["tags_dropped"] >= 1
    remaining = {t.key for t in ocigc.list_tags()}
    assert f"{UP}/org/keep-pinned:v1" in remaining, "dropped a PINNED tag"
    assert f"{UP}/org/keep-orphan:v1" in remaining, "dropped a RETAINED ORPHAN"
    assert f"{UP}/org/droppable:v1" not in remaining
