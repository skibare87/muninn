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
    # Seconds to remember that a file 404s upstream. 0 disables. Short by
    # design -- see the negative cache note in hfcompat.
    negative_ttl_s: float = 60.0
    # What to do with cached repos whose upstream has disappeared.
    #   retain - never evict them. The copy here is the only copy, so eviction
    #            is irreversible. Makes the cache a reproducibility archive.
    #   evict  - treat them as ordinary LRU candidates.
    orphan_policy: str = "retain"
    orphan_check_interval_s: float = 21600.0  # 6h; 0 disables detection
    # Rebuild repo-info listings from the cached snapshot when upstream 404s,
    # so a repo deleted from the Hub stays enumerable (snapshot_download).
    synthesize_repo_info: bool = True
    # Seconds a ref->commit mapping is trusted before revalidating upstream.
    # 0 disables revalidation entirely: mutable refs then serve whatever was
    # first cached, forever, which is what a pure archive wants.
    ref_ttl_s: float = 300.0
    # Ingest policy. `open` allows anything not explicitly denied; `allowlist`
    # allows nothing that is not explicitly allowed. Deny always wins.
    ingest_policy: str = "open"
    allow_repos: str = ""
    deny_repos: str = ""
    policy_scope: str = "ingest"  # ingest | all
    max_file_bytes: int | None = None
    # Small, stable dataset metadata endpoints worth holding. NOT /rows: it is
    # query-dependent and unbounded. Note /splits lives on
    # datasets-server.huggingface.co and never reaches us at all.
    viewer_endpoints: str = "parquet,croissant"
    viewer_cache_ttl_s: float = 3600.0
    # Opt-in proxy for datasets-server.huggingface.co, exposed at
    # /datasets-server/*. Empty string disables the route entirely.
    datasets_server: str = "https://datasets-server.huggingface.co"
    # /rows is deliberately absent: query-dependent and unbounded.
    datasets_server_endpoints: str = "splits,first-rows,info,size,is-valid,parquet"
    stream_poll_interval_s: float = 0.25
    stream_start_timeout_s: float = 120.0

    # --- docker / OCI pull-through (0.5.0) -----------------------------------
    # A second protocol on the same array, addressed by path prefix:
    #   docker pull muninn.host/ghcr.io/org/img:tag
    # Storage is a SEPARATE root from the HF cache so scan_cache_dir never sees
    # it and image churn can never evict models.
    docker_enabled: bool = True
    docker_dir: str = "/docker"
    docker_capacity_bytes: int | None = None
    # Used when the first path segment has no dot, so `muninn.host/nginx`
    # behaves the way everyone expects.
    docker_default_upstream: str = "docker.io"
    # Seconds a tag->digest mapping is trusted. Parity with XHC_REF_TTL: a tag
    # is a mutable ref pointing at an immutable digest, exactly the main->commit
    # problem. 0 disables revalidation, making the cache a pure archive.
    docker_tag_ttl_s: float = 300.0
    # Client-facing auth. `none` matches the HF side's LAN posture. `basic`
    # implies TLS -- Docker refuses basic auth over plaintext except on
    # localhost. Phase 3.
    docker_auth: str = "none"
    docker_htpasswd: str | None = None
    # Upstream credentials as a standard ~/.docker/config.json, so `docker
    # login` output can be mounted directly and nothing bespoke is invented.
    registry_auth_file: str | None = None

    # --- push-through (an internal issue) ---------------------------------------------
    # OFF by default: enabling it means a client's `docker push` causes Muninn
    # to WRITE to a real registry using credentials the operator mounted. With
    # client auth off, anyone who can reach Muninn can push to anything Muninn
    # is configured for, under Muninn's identity and with no attribution -- a
    # docker push cannot identify itself. the maintainer ruled that acceptable and NOT
    # gated: the network is the trust boundary for pull, and making one verb an
    # exception would be the odd thing. It warns at boot; it does not refuse.
    docker_push_enabled: bool = False
    # proxy         forward as the client uploads. A 201 means upstream has it.
    # store-forward accept to disk, answer 201, push after. Faster and
    #               retryable, and it TELLS THE CLIENT THE PUSH SUCCEEDED
    #               BEFORE IT HAS. The mode that can lie is the one you ask for.
    docker_push_mode: str = "proxy"
    # Keep the pushed image in the cache. A push is nearly always followed by
    # pulls from other nodes and the bytes have already crossed the wire.
    docker_cache_on_push: bool = True
    # Per-registry blob chunking, in regctl's format, so anyone who has already
    # hit this has the values in the shape they already wrote them. Muninn reads
    # only blobChunk/blobMax and ignores any credentials in the file.
    docker_push_limits: str | None = None
    # Global fallback for hosts with no entry, so a single Cloudflare-fronted
    # registry does not require writing and mounting a file for one integer.
    # 0 means no chunking: a monolithic PUT, which is what a docker client does.
    docker_blob_chunk: int = 0
    # Registry-host and image policy. Defaults to `open`, at parity with
    # XHC_INGEST_POLICY on the HF side -- the maintainer's ruling was parity.
    # NOTE the exposure that parity implies: path-prefix routing means anyone
    # who can reach this host can pull from ANY registry onto the array. Two
    # env vars close that (XHC_DOCKER_POLICY=allowlist plus XHC_ALLOW_REGISTRIES)
    # and it is the single highest-value hardening step for a shared box.
    docker_policy: str = "open"
    allow_registries: str = ""
    deny_registries: str = ""
    allow_images: str = ""
    deny_images: str = ""
    docker_max_blob_bytes: int | None = None

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

        orphan_policy = (os.environ.get("XHC_ORPHAN_POLICY") or cls.orphan_policy).strip().lower()
        if orphan_policy not in ("retain", "evict"):
            raise ValueError(f"XHC_ORPHAN_POLICY must be retain|evict, got {orphan_policy!r}")

        ingest_policy = (os.environ.get("XHC_INGEST_POLICY") or cls.ingest_policy).strip().lower()
        if ingest_policy not in ("open", "allowlist"):
            raise ValueError(f"XHC_INGEST_POLICY must be open|allowlist, got {ingest_policy!r}")

        policy_scope = (os.environ.get("XHC_POLICY_SCOPE") or cls.policy_scope).strip().lower()
        if policy_scope not in ("ingest", "all"):
            raise ValueError(f"XHC_POLICY_SCOPE must be ingest|all, got {policy_scope!r}")

        high = _env_float("XHC_HIGH_WATER", 0.90)
        low = _env_float("XHC_LOW_WATER", 0.75)
        if not 0 < low < high <= 1:
            raise ValueError(f"require 0 < XHC_LOW_WATER ({low}) < XHC_HIGH_WATER ({high}) <= 1")

        docker_policy = (os.environ.get("XHC_DOCKER_POLICY") or cls.docker_policy).strip().lower()
        if docker_policy not in ("open", "allowlist"):
            raise ValueError(f"XHC_DOCKER_POLICY must be open|allowlist, got {docker_policy!r}")

        docker_auth = (os.environ.get("XHC_DOCKER_AUTH") or cls.docker_auth).strip().lower()
        push_mode = (
            os.environ.get("XHC_DOCKER_PUSH_MODE") or cls.docker_push_mode
        ).strip().lower()
        if push_mode not in ("proxy", "store-forward"):
            raise ValueError(
                f"XHC_DOCKER_PUSH_MODE must be proxy|store-forward, got {push_mode!r}"
            )
        if docker_auth not in ("none", "basic"):
            raise ValueError(f"XHC_DOCKER_AUTH must be none|basic, got {docker_auth!r}")

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
            negative_ttl_s=_env_float("XHC_NEGATIVE_TTL", cls.negative_ttl_s),
            orphan_policy=orphan_policy,
            orphan_check_interval_s=_env_float(
                "XHC_ORPHAN_CHECK_INTERVAL", cls.orphan_check_interval_s
            ),
            synthesize_repo_info=_env_bool("XHC_SYNTHESIZE_REPO_INFO", cls.synthesize_repo_info),
            ref_ttl_s=_env_float("XHC_REF_TTL", cls.ref_ttl_s),
            ingest_policy=ingest_policy,
            allow_repos=os.environ.get("XHC_ALLOW_REPOS", cls.allow_repos),
            deny_repos=os.environ.get("XHC_DENY_REPOS", cls.deny_repos),
            policy_scope=policy_scope,
            max_file_bytes=parse_size(os.environ.get("XHC_MAX_FILE_BYTES"), None),
            viewer_endpoints=os.environ.get("XHC_VIEWER_ENDPOINTS", cls.viewer_endpoints),
            viewer_cache_ttl_s=_env_float("XHC_VIEWER_CACHE_TTL", cls.viewer_cache_ttl_s),
            datasets_server=os.environ.get("XHC_DATASETS_SERVER", cls.datasets_server).rstrip("/"),
            datasets_server_endpoints=os.environ.get(
                "XHC_DATASETS_SERVER_ENDPOINTS", cls.datasets_server_endpoints
            ),
            stream_poll_interval_s=_env_float("XHC_STREAM_POLL_INTERVAL", 0.25),
            stream_start_timeout_s=_env_float("XHC_STREAM_START_TIMEOUT", 120.0),
            docker_enabled=_env_bool("XHC_DOCKER_ENABLED", cls.docker_enabled),
            docker_dir=os.environ.get("XHC_DOCKER_DIR", cls.docker_dir),
            docker_capacity_bytes=parse_size(os.environ.get("XHC_DOCKER_MAX_SIZE"), None),
            docker_default_upstream=os.environ.get(
                "XHC_DOCKER_DEFAULT_UPSTREAM", cls.docker_default_upstream
            ),
            docker_tag_ttl_s=_env_float("XHC_DOCKER_TAG_TTL", cls.docker_tag_ttl_s),
            docker_auth=docker_auth,
            docker_htpasswd=os.environ.get("XHC_DOCKER_HTPASSWD") or None,
            registry_auth_file=os.environ.get("XHC_REGISTRY_AUTH_FILE") or None,
            docker_push_enabled=_env_bool("XHC_DOCKER_PUSH", cls.docker_push_enabled),
            docker_push_mode=push_mode,
            docker_cache_on_push=_env_bool(
                "XHC_DOCKER_CACHE_ON_PUSH", cls.docker_cache_on_push
            ),
            docker_push_limits=os.environ.get("XHC_DOCKER_PUSH_LIMITS") or None,
            docker_blob_chunk=parse_size(os.environ.get("XHC_DOCKER_BLOB_CHUNK"), cls.docker_blob_chunk),
            docker_policy=docker_policy,
            allow_registries=os.environ.get("XHC_ALLOW_REGISTRIES", cls.allow_registries),
            deny_registries=os.environ.get("XHC_DENY_REGISTRIES", cls.deny_registries),
            allow_images=os.environ.get("XHC_ALLOW_IMAGES", cls.allow_images),
            deny_images=os.environ.get("XHC_DENY_IMAGES", cls.deny_images),
            docker_max_blob_bytes=parse_size(os.environ.get("XHC_DOCKER_MAX_BLOB_BYTES"), None),
            host=os.environ.get("XHC_HOST", "0.0.0.0"),
            port=_env_int("XHC_PORT", 8080),
            manage_token=os.environ.get("XHC_MANAGE_TOKEN") or None,
            request_timeout_s=_env_float("XHC_REQUEST_TIMEOUT", 60.0),
            xet_env={k: os.environ[k] for k in XET_ENV_KEYS if k in os.environ},
        )


settings = Settings.from_env()
