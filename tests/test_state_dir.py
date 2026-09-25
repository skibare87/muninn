"""XHC_STATE_DIR: durable state on its own volume, blobs on disposable disk.

Pins protect blobs from eviction, so every way this can go wrong is a way for
protection to vanish silently: a path that reads the old location while another
writes the new one, a migration that starts empty while the old file still
exists, or an unreadable file that turns into "nothing is pinned". Each of those
has a test here, and the unset case has one too, because an existing deployment
must not move.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import cachefs, ocigc, policy
from app.cachefs import StateUnavailable
from app.config import Settings, settings


@pytest.fixture
def trees(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    docker = tmp_path / "docker"
    state = tmp_path / "state"
    cache.mkdir()
    docker.mkdir()
    monkeypatch.setattr(settings, "cache_dir", str(cache))
    monkeypatch.setattr(settings, "docker_dir", str(docker))
    monkeypatch.setattr(settings, "orphan_policy", "retain")
    # raising=False so this module can run against code that predates the
    # setting and fail on the ASSERTION, for the reason it exists.
    monkeypatch.setattr(settings, "state_dir", None, raising=False)
    return cache, docker, state


def _use_state_dir(monkeypatch, state: Path) -> None:
    monkeypatch.setattr(settings, "state_dir", str(state), raising=False)


def _statedir():
    from app import statedir

    return statedir


# -- unset: nothing moves ---------------------------------------------------


def test_unset_keeps_state_inside_each_cache_tree(trees):
    cache, docker, _ = trees
    cachefs.save_pins({"models/org/a"})
    ocigc.save_pins({"registry-1.docker.io/library/x:1"})

    assert json.loads((cache / ".xhc" / "pins.json").read_text()) == ["models/org/a"]
    assert json.loads((docker / ".xhc" / "pins.json").read_text()) == [
        "registry-1.docker.io/library/x:1"
    ]


# -- set: state goes to the state dir, per protocol -------------------------


def test_set_writes_pins_under_the_state_dir_and_reads_them_back(trees, monkeypatch):
    cache, docker, state = trees
    _use_state_dir(monkeypatch, state)

    cachefs.save_pins({"models/org/a"})
    ocigc.save_pins({"registry-1.docker.io/library/x:1"})

    assert json.loads((state / "hf" / "pins.json").read_text()) == ["models/org/a"]
    assert json.loads((state / "oci" / "pins.json").read_text()) == [
        "registry-1.docker.io/library/x:1"
    ]
    # Neither tree gained a pins file: the whole point is that the blob disk
    # can be thrown away without taking protection with it.
    assert not (cache / ".xhc" / "pins.json").exists()
    assert not (docker / ".xhc" / "pins.json").exists()

    # The two protocols never share a file, so neither can overwrite the other.
    assert cachefs.load_pins(strict=True) == {"models/org/a"}
    assert ocigc.load_pins(strict=True) == {"registry-1.docker.io/library/x:1"}


def test_orphans_and_policy_follow_pins_to_the_state_dir(trees, monkeypatch):
    """Orphans are protection too (under retain they are the only copy left),
    and a runtime policy edit is meant to survive a restart. Moving pins alone
    would leave both on the disposable disk."""
    cache, docker, state = trees
    _use_state_dir(monkeypatch, state)

    cachefs.save_orphans({"models/org/gone": {"marked_at": 1}})
    ocigc.save_orphans({"ghcr.io/o/r:t": {"marked_at": 1}})
    policy.save({"mode": "allowlist", "allow": ["org/*"], "deny": [], "scope": "ingest"})

    assert (state / "hf" / "orphans.json").is_file()
    assert (state / "oci" / "orphans.json").is_file()
    assert (state / "hf" / "policy.json").is_file()
    assert not (cache / ".xhc").exists()
    assert not (docker / ".xhc").exists()
    assert policy.load()["mode"] == "allowlist"


# -- migration --------------------------------------------------------------


def _legacy(tree: Path, name: str, content: str) -> Path:
    d = tree / ".xhc"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(content)
    return p


def test_startup_copies_existing_in_tree_state_across(trees, monkeypatch, caplog):
    cache, docker, state = trees
    hf_old = _legacy(cache, "pins.json", json.dumps(["models/org/keep"]))
    _legacy(cache, "orphans.json", json.dumps({"models/org/gone": {"marked_at": 1}}))
    oci_old = _legacy(docker, "pins.json", json.dumps(["ghcr.io/o/r:t"]))
    _use_state_dir(monkeypatch, state)

    with caplog.at_level("INFO"):
        _statedir().prepare()

    assert json.loads((state / "hf" / "pins.json").read_text()) == ["models/org/keep"]
    assert (state / "hf" / "orphans.json").is_file()
    assert json.loads((state / "oci" / "pins.json").read_text()) == ["ghcr.io/o/r:t"]
    assert cachefs.load_pins(strict=True) == {"models/org/keep"}
    assert ocigc.load_pins(strict=True) == {"ghcr.io/o/r:t"}
    # Copied, not moved: unsetting the variable must still find the old state.
    assert hf_old.is_file() and oci_old.is_file()
    assert "migrated" in caplog.text and str(hf_old) in caplog.text


def test_a_reader_never_sees_empty_pins_while_the_old_file_exists(trees, monkeypatch):
    """Even a path that runs before startup migration must not read "no pins"
    off an empty state dir while the in-tree file still holds them."""
    cache, _, state = trees
    _legacy(cache, "pins.json", json.dumps(["models/org/keep"]))
    _use_state_dir(monkeypatch, state)

    assert cachefs.load_pins(strict=True) == {"models/org/keep"}
    assert cachefs.protected_keys(strict=True) >= {"models/org/keep"}


def test_migration_never_overwrites_state_already_in_the_state_dir(trees, monkeypatch):
    cache, _, state = trees
    _legacy(cache, "pins.json", json.dumps(["models/org/old"]))
    (state / "hf").mkdir(parents=True)
    (state / "hf" / "pins.json").write_text(json.dumps(["models/org/new"]))
    _use_state_dir(monkeypatch, state)

    _statedir().prepare()
    assert cachefs.load_pins(strict=True) == {"models/org/new"}


def test_a_corrupt_old_file_migrates_as_corrupt_and_still_fails_closed(trees, monkeypatch):
    """Migration copies bytes, it does not parse them. Parsing would force a
    choice about what an unreadable file means, and the only safe answer is the
    one load_pins already gives."""
    cache, _, state = trees
    _legacy(cache, "pins.json", "}{ not json")
    _use_state_dir(monkeypatch, state)

    _statedir().prepare()
    with pytest.raises(StateUnavailable):
        cachefs.load_pins(strict=True)


# -- refuse to start --------------------------------------------------------


def test_an_unusable_state_dir_refuses_to_start(trees, monkeypatch, tmp_path):
    statedir = _statedir()
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("a file where the state directory should be")
    _use_state_dir(monkeypatch, blocker / "state")

    with pytest.raises(statedir.StateDirError, match="XHC_STATE_DIR"):
        statedir.prepare()


def test_an_unusable_state_dir_stops_the_app_booting(trees, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from app.main import app

    statedir = _statedir()
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    _use_state_dir(monkeypatch, blocker / "state")
    monkeypatch.setattr(settings, "docker_enabled", False)

    with pytest.raises(statedir.StateDirError), TestClient(app):
        pass


def test_a_relative_state_dir_is_refused_on_the_argument(monkeypatch):
    monkeypatch.setenv("XHC_STATE_DIR", "state")
    with pytest.raises(ValueError, match="XHC_STATE_DIR"):
        Settings.from_env()


def test_the_setting_is_read_from_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("XHC_STATE_DIR", str(tmp_path))
    assert Settings.from_env().state_dir == str(tmp_path)
    monkeypatch.delenv("XHC_STATE_DIR")
    assert Settings.from_env().state_dir is None


# -- fail closed, with the state dir set ------------------------------------


def test_unreadable_pins_in_the_state_dir_fail_closed(trees, monkeypatch):
    _, _, state = trees
    _use_state_dir(monkeypatch, state)
    (state / "hf").mkdir(parents=True)
    (state / "hf" / "pins.json").write_text("}{")
    (state / "oci").mkdir(parents=True)
    (state / "oci" / "pins.json").write_text("}{")

    with pytest.raises(StateUnavailable):
        cachefs.load_pins(strict=True)
    with pytest.raises(StateUnavailable):
        cachefs.protected_keys(strict=True)
    with pytest.raises(StateUnavailable):
        ocigc.load_pins(strict=True)
    assert ocigc.collect()["refused"] is True


# -- the OCI walk skips state in both modes ---------------------------------


def test_oci_walk_skips_state_inside_the_store_in_both_modes(trees, monkeypatch):
    _, docker, _ = trees
    # A legacy dir left behind by migration, and a state dir placed inside the
    # store, both shaped like an upstream so a naive walk would descend them.
    for d in (docker / ".xhc", docker / "state"):
        (d / "tags" / "x").mkdir(parents=True)
        (d / "tags" / "x" / "t.json").write_text("{}")
        (d / "blobs" / "sha256").mkdir(parents=True)
        (d / "blobs" / "sha256" / ("0" * 64)).write_text("x")

    assert [b for b in ocigc._enumerate("blobs") if b.upstream == ".xhc"] == []

    _use_state_dir(monkeypatch, docker / "state")
    assert ocigc._enumerate("blobs") == []
    statedir = _statedir()
    assert statedir.is_oci_state_entry(docker / ".xhc")
    assert statedir.is_oci_state_entry(docker / "state")
