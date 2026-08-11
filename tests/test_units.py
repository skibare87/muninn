"""Offline unit tests -- no network, no Hugging Face access required.

These cover the pure functions where a silent regression would be hard to spot:
size/range/path parsing and cache-layout naming. The behaviour that actually
matters (ingest, coalescing, streaming, integrity) needs the live Hub and is
documented under "Verified behaviour" in the README; it is not run in CI.

    pytest -q          # or: python tests/test_units.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("HF_HUB_CACHE", "/tmp/xhc-test-cache")

from app.cachefs import repo_folder_name, repo_key
from app.config import parse_size
from app.hfcompat import is_xet_token_path, parse_resolve
from app.serving import parse_range


@pytest.mark.parametrize(
    "text,expected",
    [
        ("70T", 70 * 1024**4),
        ("500GB", 500 * 1024**3),
        ("1024", 1024),
        ("10G", 10 * 1024**3),
        ("1.5T", int(1.5 * 1024**4)),
    ],
)
def test_parse_size(text, expected):
    assert parse_size(text) == expected


def test_parse_size_rejects_garbage():
    with pytest.raises(ValueError):
        parse_size("banana")


def test_parse_size_default():
    assert parse_size(None, 99) == 99
    assert parse_size("", 99) == 99


@pytest.mark.parametrize(
    "path,expected",
    [
        (
            "org/repo/resolve/main/model.safetensors",
            ("model", "org/repo", "main", "model.safetensors"),
        ),
        ("datasets/org/ds/resolve/main/d/x.parquet", ("dataset", "org/ds", "main", "d/x.parquet")),
        ("spaces/org/sp/resolve/main/app.py", ("space", "org/sp", "main", "app.py")),
        # revisions arrive URL-encoded
        ("org/repo/resolve/refs%2Fpr%2F1/a/b.bin", ("model", "org/repo", "refs/pr/1", "a/b.bin")),
    ],
)
def test_parse_resolve(path, expected):
    assert parse_resolve(path) == expected


@pytest.mark.parametrize("path", ["api/models/org/repo", "org/repo", "", "org/repo/resolve/main"])
def test_parse_resolve_rejects_non_resolve(path):
    assert parse_resolve(path) is None


def test_xet_token_paths_are_recognised():
    # If this regresses, clients silently negotiate Xet and pull bytes straight
    # from HF, bypassing the cache entirely.
    assert is_xet_token_path("api/models/org/repo/xet-read-token/main")
    assert is_xet_token_path("api/datasets/org/ds/xet-write-token/main")
    assert not is_xet_token_path("api/models/org/repo")
    assert not is_xet_token_path("org/repo/resolve/main/f.bin")


@pytest.mark.parametrize(
    "header,size,expected",
    [
        ("bytes=0-99", 1000, (0, 99)),
        ("bytes=500-", 1000, (500, 999)),
        ("bytes=-100", 1000, (900, 999)),  # suffix range
        ("bytes=0-99999", 1000, (0, 999)),  # clamped to EOF
        (None, 1000, None),
        ("bytes=0-10,20-30", 1000, None),  # multi-range unsupported -> whole file
        ("garbage", 1000, None),
    ],
)
def test_parse_range(header, size, expected):
    assert parse_range(header, size) == expected


def test_parse_range_unsatisfiable():
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        parse_range("bytes=2000-3000", 1000)
    assert exc.value.status_code == 416


def test_repo_folder_name_matches_hf_layout():
    # Must match huggingface_hub exactly or every cache lookup misses.
    assert repo_folder_name("meta-llama/Llama-3-8B", "model") == "models--meta-llama--Llama-3-8B"
    assert repo_folder_name("squad", "dataset") == "datasets--squad"
    assert repo_key("model", "org/repo") == "models/org/repo"


def test_miss_policy_validation():
    from app.config import Settings

    os.environ["XHC_MISS_POLICY"] = "bogus"
    try:
        with pytest.raises(ValueError):
            Settings.from_env()
    finally:
        del os.environ["XHC_MISS_POLICY"]


def test_miss_policy_default_is_stream():
    from app.config import Settings

    os.environ.pop("XHC_MISS_POLICY", None)
    assert Settings.from_env().miss_policy == "stream"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


# ---------------------------------------------------------------------------
# Upstream error translation.
#
# Regression guard for a real production defect: a missing optional file was
# reported as 502. huggingface_hub treats 5xx as "the hub is broken", so it
# retried with backoff and ultimately raised LocalEntryNotFoundError -- a
# connectivity error -- for a file that simply does not exist. Clients probe
# for optional files on every model load, so this taxed every load.
# ---------------------------------------------------------------------------


def _resp(status, headers=None):
    import requests

    r = requests.Response()
    r.status_code = status
    r.headers.update(headers or {})
    return r


@pytest.mark.parametrize(
    "exc_name,status,error_code,expect_status,expect_code",
    [
        ("EntryNotFoundError", 404, "EntryNotFound", 404, "EntryNotFound"),
        ("RepositoryNotFoundError", 404, "RepoNotFound", 404, "RepoNotFound"),
        ("RevisionNotFoundError", 404, "RevisionNotFound", 404, "RevisionNotFound"),
        ("GatedRepoError", 403, "GatedRepo", 403, "GatedRepo"),
        # A genuine hub fault must stay retryable, not be flattened to 404.
        ("HfHubHTTPError", 500, None, 500, None),
        ("HfHubHTTPError", 503, None, 503, None),
    ],
)
def test_upstream_failure_passes_through_status_and_error_code(
    exc_name, status, error_code, expect_status, expect_code
):
    from huggingface_hub import errors as E

    from app.hfcompat import upstream_failure

    exc = getattr(E, exc_name)(
        "boom", response=_resp(status, {"X-Error-Code": error_code} if error_code else {})
    )
    http_exc = upstream_failure(exc, "org/repo", "f.json")
    assert http_exc.status_code == expect_status
    assert (http_exc.headers or {}).get("X-Error-Code") == expect_code


def test_upstream_failure_without_response_uses_exception_class():
    from huggingface_hub import errors as E

    from app.hfcompat import upstream_failure

    http_exc = upstream_failure(E.EntryNotFoundError("no response"), "org/repo", "f.json")
    assert http_exc.status_code == 404
    assert http_exc.headers["X-Error-Code"] == "EntryNotFound"


def test_transport_failure_is_still_502():
    # No upstream response behind it -> genuinely a bad gateway.
    from app.hfcompat import upstream_failure

    assert upstream_failure(OSError("connection refused"), "o/r", "f").status_code == 502
    assert upstream_failure(TimeoutError("timed out"), "o/r", "f").status_code == 502


def test_gated_is_not_reported_as_missing():
    # 403 must not collapse into 404: the caller needs to know to fix a token,
    # not conclude the file does not exist.
    from huggingface_hub import errors as E

    from app.hfcompat import upstream_failure

    exc = E.GatedRepoError("gated", response=_resp(403, {"X-Error-Code": "GatedRepo"}))
    assert upstream_failure(exc, "o/r", "f").status_code == 403


def test_negative_cache_roundtrip():
    from fastapi import HTTPException

    from app import hfcompat

    hfcompat.negative_cache_clear()
    key = ("model", "org/repo", "main", "processor_config.json")
    assert hfcompat._negative_cache_get(*key) is None
    hfcompat._negative_cache_put(*key, HTTPException(status_code=404))
    assert hfcompat._negative_cache_get(*key).status_code == 404
    # A different file must not be shadowed by the cached miss.
    assert hfcompat._negative_cache_get("model", "org/repo", "main", "config.json") is None
    hfcompat.negative_cache_clear()


def test_negative_cache_expires():
    import time as _time

    from fastapi import HTTPException

    from app import hfcompat
    from app.config import settings

    hfcompat.negative_cache_clear()
    original = settings.negative_ttl_s
    settings.negative_ttl_s = 0.05
    try:
        key = ("model", "org/repo", "main", "gone.json")
        hfcompat._negative_cache_put(*key, HTTPException(status_code=404))
        assert hfcompat._negative_cache_get(*key) is not None
        _time.sleep(0.1)
        assert hfcompat._negative_cache_get(*key) is None
    finally:
        settings.negative_ttl_s = original
        hfcompat.negative_cache_clear()


def test_negative_cache_disabled_by_zero_ttl():
    from fastapi import HTTPException

    from app import hfcompat
    from app.config import settings

    hfcompat.negative_cache_clear()
    original = settings.negative_ttl_s
    settings.negative_ttl_s = 0
    try:
        key = ("model", "org/repo", "main", "x.json")
        hfcompat._negative_cache_put(*key, HTTPException(status_code=404))
        assert hfcompat._negative_cache_get(*key) is None
    finally:
        settings.negative_ttl_s = original


# ---------------------------------------------------------------------------
# Orphan retention. A live repo can be re-fetched; an orphan cannot, so
# evicting one is irreversible.
# ---------------------------------------------------------------------------


def test_orphan_policy_validation():
    from app.config import Settings

    os.environ["XHC_ORPHAN_POLICY"] = "nonsense"
    try:
        with pytest.raises(ValueError):
            Settings.from_env()
    finally:
        del os.environ["XHC_ORPHAN_POLICY"]


def test_orphan_policy_defaults_to_retain():
    from app.config import Settings

    os.environ.pop("XHC_ORPHAN_POLICY", None)
    assert Settings.from_env().orphan_policy == "retain"


def test_protected_keys_includes_orphans_only_when_retaining(tmp_path):
    from app import cachefs
    from app.config import settings

    original_dir, original_policy = settings.cache_dir, settings.orphan_policy
    settings.cache_dir = str(tmp_path)
    try:
        cachefs.save_pins({"models/org/pinned"})
        cachefs.save_orphans({"models/org/gone": {"reason": "deleted", "since": 0}})

        settings.orphan_policy = "retain"
        assert cachefs.protected_keys() == {"models/org/pinned", "models/org/gone"}

        settings.orphan_policy = "evict"
        assert cachefs.protected_keys() == {"models/org/pinned"}
    finally:
        settings.cache_dir, settings.orphan_policy = original_dir, original_policy


def test_orphan_state_survives_unreadable_file(tmp_path):
    from app import cachefs
    from app.config import settings

    original = settings.cache_dir
    settings.cache_dir = str(tmp_path)
    try:
        (tmp_path / ".xhc").mkdir(parents=True, exist_ok=True)
        (tmp_path / ".xhc" / "orphans.json").write_text("{ not json")
        assert cachefs.load_orphans() == {}  # degrade, don't crash
    finally:
        settings.cache_dir = original


@pytest.mark.parametrize(
    "status,expected_state",
    [
        (200, "alive"),
        (404, "orphaned"),  # deleted
        (401, "orphaned"),  # access revoked -- also unrecoverable
        (403, "orphaned"),  # gated
        # Anything ambiguous must NOT change state: a transient fault must
        # never make an irreplaceable archive evictable.
        (429, "unknown"),
        (500, "unknown"),
        (503, "unknown"),
    ],
)
def test_orphan_classification(status, expected_state):
    import asyncio

    import httpx

    from app import orphans

    class FakeClient:
        async def get(self, url, headers=None):
            return httpx.Response(status, request=httpx.Request("GET", url))

    original = orphans._get_client
    fake = FakeClient()
    orphans._get_client = lambda: fake
    try:
        state, _ = asyncio.run(orphans._classify("model", "org/repo"))
        assert state == expected_state
    finally:
        orphans._get_client = original


def test_orphan_classification_treats_network_error_as_unknown():
    import asyncio

    import httpx

    from app import orphans

    class FailingClient:
        async def get(self, url, headers=None):
            raise httpx.ConnectError("no route to host")

    original = orphans._get_client
    failing = FailingClient()
    orphans._get_client = lambda: failing
    try:
        state, reason = asyncio.run(orphans._classify("model", "org/repo"))
        assert state == "unknown"
        assert "unreachable" in reason
    finally:
        orphans._get_client = original


# ---------------------------------------------------------------------------
# Repo-info synthesis: keeps an orphaned repo enumerable, so snapshot_download
# still works after the repo is deleted from the Hub.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,expected",
    [
        ("api/models/org/name", ("model", "org/name", None)),
        ("api/models/org/name/revision/main", ("model", "org/name", "main")),
        ("api/models/gpt2", ("model", "gpt2", None)),  # canonical, no org
        ("api/datasets/org/ds/revision/v1.1", ("dataset", "org/ds", "v1.1")),
        ("api/spaces/org/sp", ("space", "org/sp", None)),
        ("api/models/org/name/revision/refs%2Fpr%2F1", ("model", "org/name", "refs/pr/1")),
    ],
)
def test_parse_repo_info_path(path, expected):
    from app.hfcompat import parse_repo_info_path

    assert parse_repo_info_path(path) == expected


@pytest.mark.parametrize(
    "path",
    [
        "api/models/org/name/tree/main",  # sub-resource, not a repo-info call
        "api/models/org/name/paths-info/main",
        "api/whoami-v2",
        "org/name/resolve/main/f.bin",
        "api/unknown/org/name",
        "api/models/",
    ],
)
def test_parse_repo_info_path_rejects_other_endpoints(path):
    # We synthesize a file listing and nothing else; anything broader must fall
    # through to the real Hub rather than be answered from guesswork.
    from app.hfcompat import parse_repo_info_path

    assert parse_repo_info_path(path) is None


def test_synthesize_returns_none_when_not_cached(tmp_path):
    from app.config import settings
    from app.hfcompat import synthesize_repo_info

    original = settings.cache_dir
    settings.cache_dir = str(tmp_path)
    try:
        # Nothing cached -> must not fabricate a listing.
        assert synthesize_repo_info("model", "org/never-seen", "main") is None
    finally:
        settings.cache_dir = original


def test_synthesize_builds_listing_from_snapshot(tmp_path):
    from app.config import settings
    from app.hfcompat import synthesize_repo_info

    original = settings.cache_dir
    settings.cache_dir = str(tmp_path)
    try:
        commit = "a" * 40
        base = tmp_path / "models--org--name"
        snap = base / "snapshots" / commit / "nested"
        snap.mkdir(parents=True)
        (base / "refs").mkdir(parents=True)
        (base / "refs" / "main").write_text(commit)
        (base / "snapshots" / commit / "config.json").write_text("{}")
        (snap / "weights.safetensors").write_text("x")

        body = synthesize_repo_info("model", "org/name", "main")
        assert body is not None
        assert body["sha"] == commit
        assert {s["rfilename"] for s in body["siblings"]} == {
            "config.json",
            "nested/weights.safetensors",
        }
        # Tagged so an archived answer is distinguishable from a live one.
        assert body["xhcSynthesized"] is True
    finally:
        settings.cache_dir = original


def test_synthesized_tag_does_not_break_huggingface_hub_parsing():
    # The tag rides in the JSON body, so it must survive ModelInfo/DatasetInfo
    # construction or it would break every client instead of informing them.
    from huggingface_hub.hf_api import DatasetInfo, ModelInfo

    payload = {
        "id": "org/name",
        "sha": "b" * 40,
        "siblings": [{"rfilename": "config.json"}],
        "private": False,
        "xhcSynthesized": True,
    }
    assert ModelInfo(**payload).sha == "b" * 40
    assert DatasetInfo(**payload).sha == "b" * 40
