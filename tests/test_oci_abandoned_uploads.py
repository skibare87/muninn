"""Push leftovers are reclaimed: pending-area temps in both layouts, the staging
files of abandoned uploads, and the abandoned sessions themselves.

THE DEFECTS, as of v0.9.30:

1. `ocipush._sweep_strays` returned at once unless XHC_STATE_DIR was set, so a
   `.writing` temp left in `<docker dir>/_pending` (the DEFAULT layout) by a
   process killed between write and rename was never reclaimed.
2. `<docker dir>/_uploads/<upstream>/<uuid>` is outside every tree the GC
   walks. An upload the client never finished -- or one alive when the process
   died -- left its staging file there forever.
3. Upload sessions live in memory with no expiry: an abandoned one was held for
   the life of the process.

THE RULE for (2) and (3) is the docker GC's partial-sweep rule, on the same age
(XHC_DOCKER_PARTIAL_MAX_AGE): a staging file is removed only if no live session
in this process owns it, no process holds its lock, and it has been idle that
long; a session expires once idle that long with nothing running inside it.

Run against v0.9.30, 16 of these 17 fail. Five fail on BEHAVIOUR, which is the
evidence the defects were real: both pending-temp tests (the temp is still
there), both abandoned-upload tests (the staging file is still there), and the
removed-staging test, where v0.9.30 answered 201 and LANDED A 500-BYTE FILE
UNDER THE DIGEST OF A 1000-BYTE LAYER. The other eleven fail only because the
API they use (`uploads` counts, `last_active`) did not exist, which proves
nothing by itself. The one that passes is the control: a normal chunked push
still lands.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import sys
import time
import uuid as uuidlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import ocigc, ocipush, ocistore, registry
from app.config import settings

UP = "ghcr.io"
REPO = f"/v2/{UP}/org/img"
DAY = 24 * 3600
REF = registry.Ref(upstream=UP, api=f"https://{UP}", repo="org/img")


@pytest.fixture
def docker(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "cache_dir", str(tmp_path / "cache"))
    monkeypatch.setattr(settings, "docker_dir", str(tmp_path / "docker"))
    monkeypatch.setattr(settings, "state_dir", None, raising=False)
    monkeypatch.setattr(settings, "docker_push_enabled", True)
    monkeypatch.setattr(settings, "docker_push_mode", "proxy")
    monkeypatch.setattr(settings, "docker_cache_on_push", True)
    monkeypatch.setattr(settings, "docker_partial_max_age_s", 6 * 3600.0)
    ocipush._sessions.clear()
    ocipush._pinned.clear()
    ocipush._pending.clear()
    ocipush._held["bytes"] = None
    ocistore.reset_stats_cache()
    yield Path(settings.docker_dir)
    ocipush._sessions.clear()


@pytest.fixture
def client(docker, monkeypatch):
    async def no_upstream(ref, path, digest):
        return None

    monkeypatch.setattr(ocipush, "push_blob", no_upstream)
    from app.main import app

    return TestClient(app)


def _age(p: Path, seconds: float = DAY) -> None:
    t = time.time() - seconds
    os.utime(p, (t, t))


def _staging(docker: Path, name: str | None = None, body: bytes = b"half-a-layer",
             age_s: float = DAY) -> Path:
    d = docker / "_uploads" / UP
    d.mkdir(parents=True, exist_ok=True)
    p = d / (name or str(uuidlib.uuid4()))
    p.write_bytes(body)
    _age(p, age_s)
    return p


def _idle(up: ocipush.Upload, seconds: float = DAY) -> None:
    up.last_active -= seconds
    _age(up.path, seconds)


# --- 1. pending-area temps, both layouts ----------------------------------------

def _obligation(d: Path) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    marker = d / "ghcr.io_org_sha256_abc.json"
    marker.write_text(json.dumps({"kind": "blob", "upstream": UP, "api": REF.api,
                                  "repo": REF.repo, "digest": "sha256:abc"}))
    return marker


def test_a_stale_pending_temp_is_reclaimed_in_the_default_layout(docker):
    """XHC_STATE_DIR unset: the layout most deployments run, and the one the
    sweep skipped entirely."""
    pend = docker / "_pending"
    marker = _obligation(pend)
    temp = pend / f"{marker.name}.0123456789ab.writing"
    temp.write_bytes(b'{"kind": "bl')        # killed between write and rename
    ocipush._sweep_strays()
    assert not temp.exists(), "a dead .writing temp in _pending was left behind"
    assert marker.exists(), "an obligation was swept as a stray"


def test_the_legacy_pending_dir_is_swept_when_the_state_dir_is_set(docker, tmp_path,
                                                                   monkeypatch):
    """Migration moves the markers out of <docker dir>/_pending, but not a temp
    left beside them; that one was stranded in a directory nothing swept."""
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"))
    legacy = docker / "_pending"
    legacy.mkdir(parents=True)
    temp = legacy / "ghcr.io_org_sha256_abc.json.0123456789ab.writing"
    temp.write_bytes(b"{")
    ocipush.recover()
    assert not temp.exists()


# --- 2. abandoned uploads' staging files ---------------------------------------

def test_a_stale_abandoned_upload_is_reclaimed_and_reported(docker):
    """No session owns it (the client left, or the process that had it died),
    nobody holds its lock, and it is past the age: removed, and counted under
    `uploads` as well as in the partial totals."""
    p = _staging(docker)
    res = ocigc.sweep_partials()
    assert not p.exists(), "abandoned upload staging was never reclaimed"
    assert res["uploads"]["removed"] == 1
    assert res["uploads"]["freed_bytes"] == len(b"half-a-layer")
    assert res["removed"] == 1 and res["writes"]["removed"] == 0


def test_the_full_gc_reports_it(docker):
    p = _staging(docker)
    res = ocigc.collect()
    assert not p.exists()
    assert res["partials"]["uploads"]["removed"] == 1


def test_a_young_orphaned_upload_is_kept(docker):
    """The age is the backstop for a session in ANOTHER process, which this
    one's table cannot see: never removed before it."""
    p = _staging(docker, age_s=60)
    res = ocigc.sweep_partials()
    assert p.exists()
    assert res["uploads"]["kept_young"] == 1


def test_a_locked_upload_is_kept(docker):
    """A chunk being written by another process holds the lock."""
    p = _staging(docker)
    with open(p, "r+b") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        res = ocigc.sweep_partials()
    assert p.exists()
    assert res["uploads"]["kept_locked"] == 1


def test_a_dry_run_removes_nothing(docker):
    p = _staging(docker)
    res = ocigc.sweep_partials(dry_run=True)
    assert p.exists()
    assert res["uploads"]["removed"] == 1


def test_a_file_that_is_not_a_session_is_left_alone(docker):
    """Only uuid-named files are staging; anything else is not ours to delete."""
    p = _staging(docker, name="operator-notes.txt")
    res = ocigc.sweep_partials()
    assert p.exists()
    assert res["uploads"]["scanned"] == 0


# --- 3. a live session is never swept ------------------------------------------

def test_a_live_sessions_staging_is_kept_however_old(docker):
    """Its file is idle past the age -- a client between chunks for a very long
    time -- but the session is still in the table, so the file is kept."""
    up = ocipush.begin(REF)
    up.path.write_bytes(b"x" * 10)
    up.offset = 10
    _age(up.path)
    res = ocigc.sweep_partials()
    assert up.path.exists(), "a live session's staging file was swept"
    assert res["uploads"]["kept_owned"] == 1


def test_a_session_in_the_middle_of_finalising_is_not_expired(docker):
    """A proxy-mode PUT can spend a long time pushing upstream without writing
    here. Idle by the clock, but not dead: `active` keeps it."""
    up = ocipush.begin(REF)
    _idle(up)
    up.active = 1
    assert ocipush.expire_sessions() == 0
    assert up.uuid in ocipush._sessions and up.path.exists()
    res = ocigc.sweep_partials()
    assert up.path.exists() and res["uploads"]["kept_owned"] == 1


# --- 4. sessions expire -------------------------------------------------------

def test_an_idle_session_expires_and_releases_its_staging(docker):
    up = ocipush.begin(REF)
    up.path.write_bytes(b"abandoned")
    fresh = ocipush.begin(REF)
    _idle(up)
    assert ocipush.expire_sessions() == 1
    assert up.uuid not in ocipush._sessions, "an abandoned session is held forever"
    assert not up.path.exists()
    assert fresh.uuid in ocipush._sessions and fresh.path.exists()


def test_an_expired_session_is_refused_on_its_next_chunk_not_500(client, docker):
    """The OCI answer for a session the registry no longer has is
    BLOB_UPLOAD_UNKNOWN, 404, which a docker client handles by starting over."""
    start = client.post(f"{REPO}/blobs/uploads/")
    assert start.status_code == 202
    uuid = start.headers["docker-upload-uuid"]
    location = start.headers["location"]
    assert client.patch(location, content=b"first").status_code == 202
    _idle(ocipush._sessions[uuid])

    r = client.patch(location, content=b"second")
    assert r.status_code == 404, r.text
    assert r.json()["errors"][0]["code"] == "BLOB_UPLOAD_UNKNOWN"
    assert uuid not in ocipush._sessions
    assert not any((docker / "_uploads" / UP).iterdir()), "expiry left its staging file"


def test_a_session_reaped_by_the_gc_loop_is_refused_the_same_way(client, docker):
    location = client.post(f"{REPO}/blobs/uploads/").headers["location"]
    uuid = location.rsplit("/", 1)[1]
    _idle(ocipush._sessions[uuid])
    ocipush.expire_sessions()
    r = client.put(f"{location}?digest=sha256:{'0' * 64}", content=b"x")
    assert r.status_code == 404
    assert r.json()["errors"][0]["code"] == "BLOB_UPLOAD_UNKNOWN"


def test_activity_keeps_a_session_alive(client, docker):
    """Expiry is by IDLE time, not age: a session that keeps sending chunks is
    never expired however long the whole upload takes."""
    location = client.post(f"{REPO}/blobs/uploads/").headers["location"]
    uuid = location.rsplit("/", 1)[1]
    ocipush._sessions[uuid].last_active -= settings.docker_partial_max_age_s - 60
    assert client.patch(location, content=b"a").status_code == 202
    assert ocipush.expire_sessions() == 0
    assert client.patch(location, content=b"b").status_code == 202


# --- 5. a session never lands bytes it did not write ---------------------------

def test_staging_removed_under_a_live_session_is_refused_not_landed(client, docker):
    """If the staging file vanishes (another process's sweep, an operator), the
    old `open("ab")` recreated it empty and the upload finished with a correct
    digest over bytes that were never all on disk -- landed under a digest they
    do not hash to. Now the session is dropped with BLOB_UPLOAD_UNKNOWN."""
    layer = b"0123456789" * 100
    digest = "sha256:" + hashlib.sha256(layer).hexdigest()
    location = client.post(f"{REPO}/blobs/uploads/").headers["location"]
    uuid = location.rsplit("/", 1)[1]
    assert client.patch(location, content=layer[:500]).status_code == 202
    ocipush._sessions[uuid].path.unlink()

    r = client.put(f"{location}?digest={digest}", content=layer[500:])
    assert r.status_code == 404, r.text
    assert r.json()["errors"][0]["code"] == "BLOB_UPLOAD_UNKNOWN"
    assert not ocistore.blob_path(UP, digest).exists(), "a truncated blob was landed"
    assert uuid not in ocipush._sessions


def test_a_normal_push_still_lands(client, docker):
    layer = b"layer-bytes" * 50
    digest = "sha256:" + hashlib.sha256(layer).hexdigest()
    location = client.post(f"{REPO}/blobs/uploads/").headers["location"]
    assert client.patch(location, content=layer[:100]).status_code == 202
    r = client.put(f"{location}?digest={digest}", content=layer[100:])
    assert r.status_code == 201, r.text
    assert ocistore.blob_path(UP, digest).read_bytes() == layer
    assert not ocipush._sessions


def test_gc_loop_expires_sessions(docker, monkeypatch):
    """The loop is what reaches a session no client ever touches again."""
    monkeypatch.setattr(settings, "docker_enabled", True)
    monkeypatch.setattr(settings, "evict_interval_s", 0)
    up = ocipush.begin(REF)
    _idle(up)

    async def one_pass():
        task = asyncio.create_task(ocigc.gc_loop())
        for _ in range(50):
            await asyncio.sleep(0.01)
            if up.uuid not in ocipush._sessions:
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(one_pass())
    assert up.uuid not in ocipush._sessions
    assert not up.path.exists()
