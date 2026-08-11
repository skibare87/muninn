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


# ---------------------------------------------------------------------------
# Range handling. Multi-range matters for dataset workloads: parquet readers
# batch column-chunk reads into one request.
# ---------------------------------------------------------------------------


def test_coalesce_merges_overlapping_and_adjacent():
    from app.serving import coalesce

    assert coalesce([(0, 100), (50, 200)]) == [(0, 200)]  # overlapping
    assert coalesce([(0, 99), (100, 199)]) == [(0, 199)]  # adjacent
    assert coalesce([(0, 99), (200, 299)]) == [(0, 99), (200, 299)]  # disjoint
    assert coalesce([(200, 299), (0, 99)]) == [(0, 99), (200, 299)]  # unordered
    assert coalesce([]) == []


def test_coalesce_defeats_range_amplification():
    # CVE-2011-3192: many overlapping ranges, each nearly the whole file. After
    # coalescing the body can never exceed the file size.
    from app.serving import coalesce

    merged = coalesce([(0, 999_999)] * 500)
    assert merged == [(0, 999_999)]
    assert sum(e - s + 1 for s, e in merged) <= 1_000_000


@pytest.mark.parametrize(
    "header,expected",
    [
        ("bytes=0-99,200-299", [(0, 99), (200, 299)]),
        ("bytes=0-500,400-800", [(0, 800)]),  # coalesced
        ("bytes=0-99,100-199", [(0, 199)]),  # adjacent -> one part
        ("bytes=-100", [(900, 999)]),  # suffix
        ("bytes=500-", [(500, 999)]),  # open-ended
        ("bytes=0-99,5000-6000", [(0, 99)]),  # drop unsatisfiable member
        ("bytes = 0-99 , 200-299", [(0, 99), (200, 299)]),  # whitespace
        ("bytes=abc", None),  # malformed -> ignore header
        ("bytes=", None),
        (None, None),
    ],
)
def test_parse_ranges(header, expected):
    from app.serving import parse_ranges

    assert parse_ranges(header, 1000) == expected


def test_parse_ranges_416_only_when_all_unsatisfiable():
    from fastapi import HTTPException

    from app.serving import parse_ranges

    with pytest.raises(HTTPException) as exc:
        parse_ranges("bytes=2000-3000", 1000)
    assert exc.value.status_code == 416
    assert exc.value.headers["content-range"] == "bytes */1000"


def test_too_many_ranges_falls_back_to_whole_file(monkeypatch):
    from app import serving

    monkeypatch.setattr(serving, "MAX_RANGES", 4)
    disjoint = ",".join(f"{i * 10}-{i * 10 + 1}" for i in range(20))
    assert serving.parse_ranges(f"bytes={disjoint}", 1000) is None


def test_multipart_content_length_is_exact(tmp_path):
    # A wrong Content-Length makes clients hang waiting for bytes that never
    # arrive, rather than fail loudly -- so assert the arithmetic against the
    # bytes the generator actually produces.
    from app.serving import _multipart_length, _read_multipart

    data = bytes(range(256)) * 8
    f = tmp_path / "blob.bin"
    f.write_bytes(data)
    ranges = [(0, 99), (500, 599), (1000, 1099)]
    boundary = "testboundary"

    declared = _multipart_length(boundary, ranges, len(data))
    actual = sum(len(c) for c in _read_multipart(f, ranges, len(data), boundary))
    assert declared == actual


def test_multipart_body_parses_and_round_trips(tmp_path):
    import email

    from app.serving import _read_multipart

    data = bytes(range(256)) * 8
    f = tmp_path / "blob.bin"
    f.write_bytes(data)
    ranges = [(0, 49), (100, 149)]
    boundary = "b0undary"

    body = b"".join(_read_multipart(f, ranges, len(data), boundary))
    msg = email.message_from_bytes(
        f"Content-Type: multipart/byteranges; boundary={boundary}\r\n"
        "MIME-Version: 1.0\r\n\r\n".encode()
        + body
    )
    parts = [p for p in msg.walk() if p.get_payload(decode=True) is not None]
    assert len(parts) == 2
    for part, (start, end) in zip(parts, ranges, strict=True):
        assert part.get("Content-Range") == f"bytes {start}-{end}/{len(data)}"
        assert part.get_payload(decode=True) == data[start : end + 1]


def test_parse_range_single_wrapper_rejects_multi():
    from app.serving import parse_range

    assert parse_range("bytes=0-99", 1000) == (0, 99)
    assert parse_range("bytes=0-99,200-299", 1000) is None  # caller must use parse_ranges


# ---------------------------------------------------------------------------
# Config wiring.
#
# This has now bitten twice: a dataclass field is added, from_env() is not
# updated, and the setting silently ignores its environment variable. Both
# times it was only caught by inspecting the *effective* value at runtime.
# This table asserts the wiring directly.
# ---------------------------------------------------------------------------

ENV_TO_SETTING = [
    ("XHC_MISS_POLICY", "wait", "miss_policy", "wait"),
    ("XHC_UPSTREAM", "https://example.invalid", "upstream", "https://example.invalid"),
    ("XHC_CACHE_MAX_SIZE", "5T", "capacity_bytes", 5 * 1024**4),
    ("XHC_HIGH_WATER", "0.8", "high_water", 0.8),
    ("XHC_LOW_WATER", "0.6", "low_water", 0.6),
    ("XHC_EVICT_INTERVAL", "123", "evict_interval_s", 123),
    ("XHC_BLOCK_CLIENT_XET", "0", "block_client_xet", False),
    ("XHC_INGEST_CONCURRENCY", "9", "ingest_concurrency", 9),
    ("XHC_NEGATIVE_TTL", "11", "negative_ttl_s", 11.0),
    ("XHC_ORPHAN_POLICY", "evict", "orphan_policy", "evict"),
    ("XHC_ORPHAN_CHECK_INTERVAL", "77", "orphan_check_interval_s", 77.0),
    ("XHC_SYNTHESIZE_REPO_INFO", "0", "synthesize_repo_info", False),
    ("XHC_REF_TTL", "42", "ref_ttl_s", 42.0),
    ("XHC_INGEST_POLICY", "allowlist", "ingest_policy", "allowlist"),
    ("XHC_ALLOW_REPOS", "models/a/*", "allow_repos", "models/a/*"),
    ("XHC_DENY_REPOS", "datasets/*", "deny_repos", "datasets/*"),
    ("XHC_POLICY_SCOPE", "all", "policy_scope", "all"),
    ("XHC_MAX_FILE_BYTES", "2G", "max_file_bytes", 2 * 1024**3),
    ("XHC_VIEWER_ENDPOINTS", "parquet", "viewer_endpoints", "parquet"),
    ("XHC_VIEWER_CACHE_TTL", "60", "viewer_cache_ttl_s", 60.0),
    ("XHC_DATASETS_SERVER", "https://ds.invalid", "datasets_server", "https://ds.invalid"),
    ("XHC_DATASETS_SERVER_ENDPOINTS", "splits", "datasets_server_endpoints", "splits"),
    ("XHC_STREAM_POLL_INTERVAL", "0.5", "stream_poll_interval_s", 0.5),
    ("XHC_STREAM_START_TIMEOUT", "60", "stream_start_timeout_s", 60.0),
    ("XHC_PORT", "9999", "port", 9999),
    ("XHC_REQUEST_TIMEOUT", "13", "request_timeout_s", 13.0),
    ("XHC_MANAGE_TOKEN", "sekrit", "manage_token", "sekrit"),
]


@pytest.mark.parametrize("env,value,field,expected", ENV_TO_SETTING)
def test_env_var_reaches_settings(env, value, field, expected, monkeypatch):
    from app.config import Settings

    monkeypatch.setenv(env, value)
    assert getattr(Settings.from_env(), field) == expected


def test_every_setting_has_an_env_var_or_is_deliberately_internal():
    """Catch a field added to Settings without a corresponding env var.

    Anything genuinely internal goes in the exempt set with a reason, so the
    omission is a decision rather than an oversight.
    """
    import dataclasses

    from app.config import Settings

    exempt = {
        "hf_token",  # HF_TOKEN / HUGGING_FACE_HUB_TOKEN, handled separately
        "cache_dir",  # HF_HUB_CACHE, the huggingface_hub name
        "host",  # XHC_HOST, string passthrough
        "xet_env",  # reporting only, mirrors the HF_XET_* process env
    }
    covered = {field for _, _, field, _ in ENV_TO_SETTING} | exempt
    declared = {f.name for f in dataclasses.fields(Settings)}
    missing = declared - covered
    assert not missing, f"Settings fields with no env-var test: {sorted(missing)}"


# ---------------------------------------------------------------------------
# Ref revalidation
# ---------------------------------------------------------------------------


def test_immutable_revisions_are_never_revalidated():
    from app import refs

    assert refs.is_immutable("a" * 40)
    assert refs.is_immutable("71034c5d8bde858ff824298bdedc65515b97d2b9"[:40].ljust(40, "0"))
    assert not refs.is_immutable("main")
    assert not refs.is_immutable("v1.0")
    assert not refs.is_immutable("refs/pr/1")
    assert not refs.is_immutable("A" * 40)  # uppercase is not a git sha


def test_sha_pinned_requests_make_no_upstream_call():
    import asyncio

    from app import refs

    refs.clear()
    sha = "b" * 40
    assert asyncio.run(refs.upstream_commit("model", "org/repo", sha)) == sha
    assert refs.stats()["lookups"] == 0


def test_ref_ttl_zero_disables_revalidation():
    import asyncio

    from app import refs
    from app.config import settings

    refs.clear()
    original = settings.ref_ttl_s
    settings.ref_ttl_s = 0
    try:
        stale = asyncio.run(refs.is_stale("model", "org/repo", "main", "c" * 40))
        assert stale is False
        assert refs.stats()["lookups"] == 0
    finally:
        settings.ref_ttl_s = original
        refs.clear()


def test_unknown_upstream_never_reports_stale():
    # Fail-open: if we cannot find out, we keep serving what we have. Required
    # for orphans, where upstream 404s by definition.
    import asyncio

    from app import refs
    from app.config import settings

    refs.clear()
    original = settings.ref_ttl_s
    settings.ref_ttl_s = 300

    class Failing:
        async def get(self, url, headers=None):
            raise httpx_mod.ConnectError("down")

    import httpx as httpx_mod

    failing = Failing()
    saved = refs._get_client
    refs._get_client = lambda: failing
    try:
        assert asyncio.run(refs.is_stale("model", "org/repo", "main", "d" * 40)) is False
        assert refs.stats()["failed"] == 1
    finally:
        refs._get_client = saved
        settings.ref_ttl_s = original
        refs.clear()


# ---------------------------------------------------------------------------
# Ingest policy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mode,allow,deny,repo_type,repo_id,expected",
    [
        ("open", [], [], "model", "org/name", True),
        ("open", [], ["models/org/*"], "model", "org/name", False),
        ("open", [], ["datasets/*"], "model", "org/name", True),
        ("open", [], ["datasets/*"], "dataset", "big/set", False),
        ("allowlist", ["models/ok/*"], [], "model", "ok/name", True),
        ("allowlist", ["models/ok/*"], [], "model", "other/name", False),
        # deny beats allow, always
        ("allowlist", ["models/*"], ["models/org/secret"], "model", "org/secret", False),
        ("open", ["models/*"], ["models/*"], "model", "any/thing", False),
    ],
)
def test_policy_matching(mode, allow, deny, repo_type, repo_id, expected, tmp_path):
    from app import policy
    from app.config import settings

    original = settings.cache_dir
    settings.cache_dir = str(tmp_path)
    try:
        pol = {
            "mode": mode,
            "allow": allow,
            "deny": deny,
            "scope": "ingest",
            "max_file_bytes": None,
        }
        assert policy.check(repo_type, repo_id, pol).allowed is expected
    finally:
        settings.cache_dir = original


def test_policy_size_guard():
    from app import policy

    pol = {"mode": "open", "allow": [], "deny": [], "scope": "ingest", "max_file_bytes": 1000}
    assert policy.check_size(999, pol).allowed
    assert policy.check_size(1000, pol).allowed
    assert not policy.check_size(1001, pol).allowed
    # No cap, or unknown size -> allowed
    assert policy.check_size(10**12, {**pol, "max_file_bytes": None}).allowed
    assert policy.check_size(None, pol).allowed


def test_policy_file_overrides_env_and_round_trips(tmp_path):
    from app import policy
    from app.config import settings

    original = settings.cache_dir
    settings.cache_dir = str(tmp_path)
    try:
        assert policy.load()["source"] == "env"
        saved = policy.save(
            {
                "mode": "allowlist",
                "allow": ["models/a/*"],
                "deny": [],
                "scope": "all",
                "max_file_bytes": 42,
            }
        )
        assert saved["source"] == "file"
        assert saved["mode"] == "allowlist"
        assert policy.enforced_on_hits() is True
        assert policy.load()["max_file_bytes"] == 42
    finally:
        settings.cache_dir = original


def test_policy_rejects_invalid_values(tmp_path):
    from app import policy
    from app.config import settings

    original = settings.cache_dir
    settings.cache_dir = str(tmp_path)
    try:
        with pytest.raises(ValueError):
            policy.save({"mode": "nonsense"})
        with pytest.raises(ValueError):
            policy.save({"mode": "open", "scope": "nonsense"})
    finally:
        settings.cache_dir = original


# ---------------------------------------------------------------------------
# Tree synthesis
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,expected",
    [
        ("api/models/org/name/tree/main", ("model", "org/name", "main", "")),
        ("api/models/org/name/tree/main/sub/dir", ("model", "org/name", "main", "sub/dir")),
        ("api/datasets/org/ds/tree/v1", ("dataset", "org/ds", "v1", "")),
        ("api/models/gpt2/tree/main", ("model", "gpt2", "main", "")),
    ],
)
def test_parse_tree_path(path, expected):
    from app.hfcompat import parse_tree_path

    assert parse_tree_path(path) == expected


@pytest.mark.parametrize(
    "path", ["api/models/org/name", "api/models/org/name/tree/", "org/name/resolve/main/f"]
)
def test_parse_tree_path_rejects_others(path):
    from app.hfcompat import parse_tree_path

    assert parse_tree_path(path) is None


def test_synthesize_tree_matches_snapshot(tmp_path):
    from app.config import settings
    from app.hfcompat import synthesize_tree

    original = settings.cache_dir
    settings.cache_dir = str(tmp_path)
    try:
        commit = "e" * 40
        base = tmp_path / "models--org--name"
        (base / "refs").mkdir(parents=True)
        (base / "refs" / "main").write_text(commit)
        snap = base / "snapshots" / commit
        (snap / "nested").mkdir(parents=True)
        (snap / "config.json").write_text("{}")
        (snap / "nested" / "w.bin").write_text("xx")

        flat = synthesize_tree("model", "org/name", "main", "", recursive=True, expand=False)
        assert {e["path"] for e in flat} == {"config.json", "nested/w.bin"}
        assert all(e["type"] == "file" for e in flat)

        shallow = synthesize_tree("model", "org/name", "main", "", recursive=False, expand=False)
        kinds = {e["path"]: e["type"] for e in shallow}
        assert kinds == {"config.json": "file", "nested": "directory"}

        expanded = synthesize_tree("model", "org/name", "main", "", recursive=True, expand=True)
        assert expanded[0]["lastCommit"] is None  # honest about what we cannot know

        assert (
            synthesize_tree("model", "org/none", "main", "", recursive=True, expand=False) is None
        )
    finally:
        settings.cache_dir = original


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def test_metrics_counters_and_exposition():
    from app import metrics

    metrics.reset()
    metrics.record_request("HIT", "gpu-01")
    metrics.record_request("HIT", "gpu-01")
    metrics.record_request("MISS-STREAM", "gpu-02")
    metrics.record_upstream(200)
    metrics.record_upstream(404)
    metrics.record_upstream(None)
    metrics.record_served(1500)
    metrics.record_ingested(900)

    out = metrics.render({"muninn_cache_bytes": 42})
    assert 'muninn_requests_total{result="HIT"} 2' in out
    assert 'muninn_requests_total{result="MISS-STREAM"} 1' in out
    assert 'muninn_client_requests_total{client="gpu-01"} 2' in out
    assert 'muninn_upstream_requests_total{status="404"} 1' in out
    assert 'muninn_upstream_requests_total{status="error"} 1' in out
    assert "muninn_bytes_served_total 1500" in out
    assert "muninn_bytes_ingested_total 900" in out
    assert "muninn_cache_bytes 42" in out
    assert "# TYPE muninn_cache_bytes gauge" in out
    metrics.reset()


def test_metrics_client_label_cardinality_is_bounded():
    # A client sending a unique header per request would otherwise grow the
    # label set without limit and blow up the scrape.
    from app import metrics

    metrics.reset()
    for i in range(metrics.MAX_CLIENT_LABELS + 50):
        metrics.record_request("HIT", f"client-{i}")
    snap = metrics.snapshot()
    assert len(snap["clients"]) <= metrics.MAX_CLIENT_LABELS + 1
    assert snap["clients"]["__other__"] == 50
    metrics.reset()


def test_metrics_escapes_label_values():
    from app import metrics

    metrics.reset()
    metrics.record_request("HIT", 'we"ird\nvalue')
    out = metrics.render({})
    assert "\n" not in out.split('client="')[1].split('"}')[0]
    assert '\\"' in out
    metrics.reset()


# ---------------------------------------------------------------------------
# Dataset metadata cache
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,expected",
    [
        ("api/datasets/org/ds/parquet", ("org/ds", "parquet", "parquet")),
        ("api/datasets/org/ds/croissant", ("org/ds", "croissant", "croissant")),
        (
            "api/datasets/org/ds/parquet/default/train",
            ("org/ds", "parquet", "parquet/default/train"),
        ),
        ("api/datasets/imdb/parquet", ("imdb", "parquet", "parquet")),
    ],
)
def test_viewer_parse_path(path, expected):
    from app import viewer

    assert viewer.parse_path(path) == expected


@pytest.mark.parametrize(
    "path",
    [
        "api/datasets/org/ds/rows",  # query-dependent, never cached
        "api/datasets/org/ds",  # repo info, handled elsewhere
        "api/models/org/name/parquet",  # models are not datasets
        "api/datasets/org/ds/tree/main",
    ],
)
def test_viewer_parse_path_rejects_others(path):
    from app import viewer

    assert viewer.parse_path(path) is None


def test_viewer_store_load_and_ttl(tmp_path):
    from app import viewer
    from app.config import settings

    original_dir, original_ttl = settings.cache_dir, settings.viewer_cache_ttl_s
    settings.cache_dir = str(tmp_path)
    try:
        assert viewer.load("org/ds", "parquet") is None
        viewer.store("org/ds", "parquet", b'{"a":1}', "application/json")
        entry = viewer.load("org/ds", "parquet")
        assert entry["body"] == '{"a":1}'

        settings.viewer_cache_ttl_s = 3600
        assert viewer.is_fresh(entry) is True
        settings.viewer_cache_ttl_s = 0  # 0 disables freshness...
        assert viewer.is_fresh(entry) is False
        # ...but the entry survives, so it can still answer a deleted upstream.
        assert viewer.load("org/ds", "parquet") is not None

        assert viewer.stats()["entries"] == 1
        assert viewer.clear() == 1
        assert viewer.load("org/ds", "parquet") is None
    finally:
        settings.cache_dir, settings.viewer_cache_ttl_s = original_dir, original_ttl


def test_viewer_keys_do_not_collide_across_subpaths(tmp_path):
    from app import viewer
    from app.config import settings

    original = settings.cache_dir
    settings.cache_dir = str(tmp_path)
    try:
        viewer.store("org/ds", "parquet", b"root", None)
        viewer.store("org/ds", "parquet/default/train", b"split", None)
        assert viewer.load("org/ds", "parquet")["body"] == "root"
        assert viewer.load("org/ds", "parquet/default/train")["body"] == "split"
        assert viewer.stats()["entries"] == 2
    finally:
        settings.cache_dir = original


# ---------------------------------------------------------------------------
# datasets-server proxy (opt-in, at our own prefix)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,expected",
    [
        ("datasets-server/splits", ("splits", "splits")),
        ("datasets-server/first-rows", ("first-rows", "first-rows")),
        ("datasets-server/rows", ("rows", "rows")),
        ("datasets-server/some/deep/path", ("some", "some/deep/path")),
    ],
)
def test_parse_datasets_server_path(path, expected):
    from app import viewer

    assert viewer.parse_datasets_server_path(path) == expected


@pytest.mark.parametrize(
    "path", ["api/datasets/org/ds/parquet", "datasets-server/", "org/repo/resolve/main/f", ""]
)
def test_parse_datasets_server_path_rejects_others(path):
    from app import viewer

    assert viewer.parse_datasets_server_path(path) is None


def test_rows_is_never_cacheable():
    # Query-dependent and unbounded: caching it badly means serving wrong rows.
    from app import viewer

    assert viewer.ds_cacheable("splits")
    assert viewer.ds_cacheable("first-rows")
    assert not viewer.ds_cacheable("rows")


def test_ds_cache_key_is_query_order_independent():
    # dataset/config/split arrive as query params here, so the key must include
    # them -- and must not treat a reordering as a different request.
    from app import viewer

    a = viewer.ds_cache_key("splits", "dataset=org%2Fds&config=default")
    b = viewer.ds_cache_key("splits", "config=default&dataset=org%2Fds")
    assert a == b
    assert viewer.ds_cache_key("splits", "dataset=other") != a


def test_ds_store_and_load_roundtrip(tmp_path):
    from app import viewer
    from app.config import settings

    original = settings.cache_dir
    settings.cache_dir = str(tmp_path)
    try:
        k1 = viewer.ds_cache_key("splits", "dataset=a/b")
        k2 = viewer.ds_cache_key("splits", "dataset=c/d")
        assert viewer.ds_load(k1) is None
        viewer.ds_store(k1, b'{"splits":[]}', "application/json")
        viewer.ds_store(k2, b'{"splits":[1]}', "application/json")
        assert viewer.ds_load(k1)["body"] == '{"splits":[]}'
        assert viewer.ds_load(k2)["body"] == '{"splits":[1]}'
        assert viewer.stats()["entries"] == 2
    finally:
        settings.cache_dir = original
