"""A by-digest prewarm protects what it fetched.

THE DEFECT: an image prewarmed BY DIGEST is referenced by no tag, so the
docker GC (app/ocigc.py) treated its whole closure as garbage -- during the
pull, where v0.9.28 at least noticed and ended the job in `error`, and after
it, where nothing noticed at all: the job said `done` and the next sweep took
the image the operator had just asked to be warm.

THE DECISION: a by-digest prewarm pins what it asked for by default, and the
pin is written BEFORE the first byte is fetched, so a sweep that runs mid-pull
already sees it. `pin=false` opts out and restores the old behaviour. The pin
is an ordinary entry in GET /_cache/docker/pins, the job names it
(`pinned_as`), and DELETE /_cache/docker/pins removes it like any other.

Tests that fail against v0.9.28:
  test_a_by_digest_prewarm_survives_a_gc_after_it_completes
  test_a_by_digest_prewarm_survives_a_gc_run_during_it
  test_an_unreadable_pins_file_fails_the_job_and_is_not_overwritten
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import ocigc, ocimanage, ocistore, registry, statedir
from tests.test_oci_prewarm_ledger import (
    UP,
    _wait_for,
    state,  # noqa: F401 - fixture
    upstream,  # noqa: F401 - fixture
)


def _by_digest(up) -> str:
    return f"{UP}/org/app@{up.manifest_digest}"


def _submit_default(m: ocimanage.PrewarmManager, image: str):
    """Submit exactly as POST /_cache/docker/prewarm does with no `pin` field."""
    req = ocimanage.PrewarmRequest(image=image)
    name, reference = ocimanage._split(image)
    return m.submit(image, registry.resolve(name), reference, req.pin)


def _submit(m, image, pin):
    name, reference = ocimanage._split(image)
    return m.submit(image, registry.resolve(name), reference, pin)


def _closure_on_disk(up) -> bool:
    if not ocistore.manifest_path(UP, up.manifest_digest).is_file():
        return False
    return all(ocistore.blob_path(UP, d).is_file() for d in up.blobs)


def test_a_by_digest_prewarm_survives_a_gc_after_it_completes(state, upstream):  # noqa: F811
    async def scenario():
        m = ocimanage.PrewarmManager()
        job = _submit_default(m, _by_digest(upstream))
        await job.done.wait()
        return job

    job = asyncio.run(scenario())
    assert job.state == "done", job.error
    res = ocigc.collect()
    assert res["refused"] is False
    assert _closure_on_disk(upstream), f"GC collected a prewarmed image: {res}"


def test_a_by_digest_prewarm_survives_a_gc_run_during_it(state, upstream):  # noqa: F811
    second = upstream.layer_digest(1)
    upstream.gate[second] = asyncio.Event()

    async def scenario():
        m = ocimanage.PrewarmManager()
        job = _submit_default(m, _by_digest(upstream))
        await _wait_for(lambda: second in upstream.reached)
        res = ocigc.collect()  # the real sweep, mid-pull
        upstream.gate[second].set()
        await job.done.wait()
        return job, res

    job, res = asyncio.run(scenario())
    assert job.state == "done", f"{job.error} (gc: {res})"
    assert res["blobs"] == 0 and res["manifests"] == 0, res
    assert _closure_on_disk(upstream)


def test_opting_out_restores_the_old_behaviour(state, upstream):  # noqa: F811
    async def scenario():
        m = ocimanage.PrewarmManager()
        job = _submit(m, _by_digest(upstream), pin=False)
        await job.done.wait()
        return job

    job = asyncio.run(scenario())
    assert job.state == "done"
    assert job.pin is False
    assert ocigc.load_pins(strict=True) == set()
    ocigc.collect()
    assert not ocistore.manifest_path(UP, upstream.manifest_digest).exists()
    assert not any(ocistore.blob_path(UP, d).exists() for d in upstream.blobs)


def test_opting_out_mid_pull_still_ends_in_error_not_done(state, upstream):  # noqa: F811
    second = upstream.layer_digest(1)
    upstream.gate[second] = asyncio.Event()

    async def scenario():
        m = ocimanage.PrewarmManager()
        job = _submit(m, _by_digest(upstream), pin=False)
        await _wait_for(lambda: second in upstream.reached)
        ocigc.collect()
        upstream.gate[second].set()
        await job.done.wait()
        return job

    job = asyncio.run(scenario())
    assert job.state == "error"
    assert "no longer on disk" in job.error


def test_the_implicit_pin_is_visible_and_removable(state, upstream):  # noqa: F811
    async def scenario():
        m = ocimanage.PrewarmManager()
        job = _submit_default(m, _by_digest(upstream))
        await job.done.wait()
        listed = await ocimanage.get_pins()
        return job, listed

    job, listed = asyncio.run(scenario())
    key = f"{UP}/org/app@{upstream.manifest_digest}"
    assert job.pin is True
    assert job.as_dict()["pinned_as"] == key
    assert listed["pins"] == [key]
    # Removal through the existing API, and then the image is ordinary garbage.
    after = asyncio.run(ocimanage.remove_pin(ocimanage.PinRequest(image=key)))
    assert after["pins"] == []
    ocigc.collect()
    assert not ocistore.manifest_path(UP, upstream.manifest_digest).exists()


def test_a_by_tag_prewarm_is_not_pinned_by_default(state, upstream):  # noqa: F811
    """A tag already roots the closure; pinning it by default would make every
    prewarmed tag unevictable under capacity pressure, which nobody asked for."""
    async def scenario():
        m = ocimanage.PrewarmManager()
        job = _submit_default(m, f"{UP}/org/app:v1")
        await job.done.wait()
        return job

    job = asyncio.run(scenario())
    assert job.state == "done"
    assert job.pin is False
    assert ocigc.load_pins(strict=True) == set()


def test_a_failed_prewarm_rolls_back_the_pin_it_added(state, upstream):  # noqa: F811
    asked = "sha256:" + "a" * 64  # upstream serves a different manifest

    async def scenario():
        m = ocimanage.PrewarmManager()
        job = _submit_default(m, f"{UP}/org/app@{asked}")
        await job.done.wait()
        return job

    job = asyncio.run(scenario())
    assert job.state == "error"
    assert ocigc.load_pins(strict=True) == set()


def test_a_failed_prewarm_keeps_a_pin_that_was_already_there(state, upstream):  # noqa: F811
    asked = "sha256:" + "a" * 64
    key = f"{UP}/org/app@{asked}"
    ocigc.save_pins({key})

    async def scenario():
        m = ocimanage.PrewarmManager()
        job = _submit_default(m, key)
        await job.done.wait()
        return job

    job = asyncio.run(scenario())
    assert job.state == "error"
    assert ocigc.load_pins(strict=True) == {key}


def test_an_unreadable_pins_file_fails_the_job_and_is_not_overwritten(state, upstream):  # noqa: F811
    """Reading pins non-strictly and saving would REPLACE every other pin with
    this one -- the fail-open the GC itself refuses to commit."""
    pins = statedir.oci_file("pins.json")
    pins.parent.mkdir(parents=True, exist_ok=True)
    pins.write_text("}{")

    async def scenario():
        m = ocimanage.PrewarmManager()
        job = _submit(m, _by_digest(upstream), pin=True)
        await job.done.wait()
        return job

    job = asyncio.run(scenario())
    assert job.state == "error"
    assert "pins" in (job.error or "")
    assert pins.read_text() == "}{"


@pytest.mark.parametrize("body,expected", [({}, None), ({"pin": False}, False),
                                           ({"pin": True}, True)])
def test_the_request_distinguishes_unset_from_false(body, expected):
    req = ocimanage.PrewarmRequest(image="x/y@sha256:" + "b" * 64, **body)
    assert req.pin is expected


def test_the_pin_is_recorded_in_the_ledger(state, upstream):  # noqa: F811
    async def scenario():
        m = ocimanage.PrewarmManager()
        job = _submit_default(m, _by_digest(upstream))
        await job.done.wait()
        m.flush()
        return job

    job = asyncio.run(scenario())
    rows = json.loads((state / "prewarm.json").read_text())
    text = json.dumps(rows)
    assert job.id in text and f'"pinned_as": "{_by_digest(upstream)}"' in text

