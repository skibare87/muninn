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
