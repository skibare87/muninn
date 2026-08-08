"""Configuration, sourced entirely from environment variables.

Xet tuning vars (HF_XET_*) are deliberately *not* re-exported from here: hf_xet
reads them from the process environment when its runtime initialises, so they
must be set by the container env (see docker-compose.yml). We only read them
back for reporting on /_cache/status.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([KMGTP]?)i?B?\s*$", re.IGNORECASE)
_MULT = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4, "P": 1024**5}

# Xet knobs we surface on /_cache/status so a misconfigured ingest is obvious.
XET_ENV_KEYS = (
    "HF_HUB_DISABLE_XET",
    "HF_XET_HIGH_PERFORMANCE",
    "HF_XET_NUM_CONCURRENT_RANGE_GETS",
    "HF_XET_RECONSTRUCT_WRITE_SEQUENTIALLY",
    "HF_XET_CHUNK_CACHE_SIZE_BYTES",
    "HF_XET_CACHE",
)


def parse_size(value: str | None, default: int | None = None) -> int | None:
    """Parse '70T', '500GB', '1024' into bytes. Units are binary (1T = 2**40)."""
    if value is None or value.strip() == "":
        return default
    m = _SIZE_RE.match(value)
    if not m:
        raise ValueError(f"cannot parse size {value!r} (expected e.g. '70T', '500G', '1024')")
    return int(float(m.group(1)) * _MULT[m.group(2).upper()])


def _env_bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _env_float(key: str, default: float) -> float:
    raw = os.environ.get(key)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


@dataclass
class Settings:
    # --- upstream / identity -------------------------------------------------
    upstream: str = "https://huggingface.co"
    hf_token: str | None = None

    # --- storage -------------------------------------------------------------
    cache_dir: str = "/cache"
    capacity_bytes: int | None = None
    high_water: float = 0.90
    low_water: float = 0.75
    evict_interval_s: int = 900

    # --- behaviour -----------------------------------------------------------
    # What to do when a client asks for a file we do not have yet.
    #   stream   - tail-follow the partial file as the ingest writes it.
    #              Default: N concurrent cold clients share ONE WAN fetch and
    #              all receive bytes at ingest speed. Requires sequential
    #              writes -- run scripts/verify_sequential_writes.py before
    #              trusting it, and again after any hf_xet upgrade.
    #   redirect - 302 the client upstream and ingest in the background. Note
    #              this coalesces the *ingest* but not the clients: N cold
    #              clients each pull the file from the WAN themselves, and they
    #              need Hub tokens to do it.
    #   wait     - block until ingest completes, then serve. Always correct,
    #              but the client pays ingest latency then transfer latency.
    miss_policy: str = "stream"
    # Intercept /api/.../xet-{read,write}-token/... so clients cannot fetch a
    # casUrl and pull bytes straight from HF, bypassing this cache.
    block_client_xet: bool = True
    ingest_concurrency: int = 4
    stream_poll_interval_s: float = 0.25
    stream_start_timeout_s: float = 120.0

    # --- server --------------------------------------------------------------
    host: str = "0.0.0.0"
    port: int = 8080
    manage_token: str | None = None
    request_timeout_s: float = 60.0

    xet_env: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> Settings:
        # NB: default comes from the dataclass field, not a literal here, so the
        # two cannot drift apart.
        miss_policy = (os.environ.get("XHC_MISS_POLICY") or cls.miss_policy).strip().lower()
        if miss_policy not in ("redirect", "stream", "wait"):
            raise ValueError(
                f"XHC_MISS_POLICY must be one of redirect|stream|wait, got {miss_policy!r}"
            )

        high = _env_float("XHC_HIGH_WATER", 0.90)
        low = _env_float("XHC_LOW_WATER", 0.75)
        if not 0 < low < high <= 1:
            raise ValueError(f"require 0 < XHC_LOW_WATER ({low}) < XHC_HIGH_WATER ({high}) <= 1")

        token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN") or None

        return cls(
            upstream=os.environ.get("XHC_UPSTREAM", "https://huggingface.co").rstrip("/"),
            hf_token=token,
            cache_dir=os.environ.get("HF_HUB_CACHE", "/cache"),
            capacity_bytes=parse_size(os.environ.get("XHC_CACHE_MAX_SIZE"), None),
            high_water=high,
            low_water=low,
            evict_interval_s=_env_int("XHC_EVICT_INTERVAL", 900),
            miss_policy=miss_policy,
            block_client_xet=_env_bool("XHC_BLOCK_CLIENT_XET", True),
            ingest_concurrency=_env_int("XHC_INGEST_CONCURRENCY", 4),
            stream_poll_interval_s=_env_float("XHC_STREAM_POLL_INTERVAL", 0.25),
            stream_start_timeout_s=_env_float("XHC_STREAM_START_TIMEOUT", 120.0),
            host=os.environ.get("XHC_HOST", "0.0.0.0"),
            port=_env_int("XHC_PORT", 8080),
            manage_token=os.environ.get("XHC_MANAGE_TOKEN") or None,
            request_timeout_s=_env_float("XHC_REQUEST_TIMEOUT", 60.0),
            xet_env={k: os.environ[k] for k in XET_ENV_KEYS if k in os.environ},
        )


settings = Settings.from_env()
