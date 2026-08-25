"""Ingest policy: what this cache is allowed to fetch, and how big.

Any host that can reach the port can otherwise cause an ingest of any repo.
Nothing stops a typo pulling a 500GB dataset onto the array, or a model you
would rather not have on the box.

Policy gates *ingest*, not *serving*, by default. A repo already in the cache
keeps being served even if a later policy change would forbid fetching it, so
tightening policy cannot break a fleet mid-rollout. XHC_POLICY_SCOPE=all extends
enforcement to cache hits for anyone who wants the stricter reading.
"""

from __future__ import annotations

import fnmatch
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from .config import settings

log = logging.getLogger("xhc.policy")

_POLICY_FILE = "policy.json"


@dataclass
class Decision:
    allowed: bool
    reason: str = ""


def _state_path() -> Path:
    d = Path(settings.cache_dir) / ".xhc"
    d.mkdir(parents=True, exist_ok=True)
    return d / _POLICY_FILE


def _split(raw: str | None) -> list[str]:
    return [p.strip() for p in (raw or "").split(",") if p.strip()]


def load() -> dict:
    """Effective policy: the persisted file if present, else the env defaults.

    Same precedence as pins -- env seeds it, a runtime edit wins and survives a
    restart.
    """
    p = _state_path()
    if p.is_file():
        try:
            data = json.loads(p.read_text())
            if isinstance(data, dict):
                return {
                    "mode": data.get("mode", settings.ingest_policy),
                    "allow": list(data.get("allow", [])),
                    "deny": list(data.get("deny", [])),
                    "scope": data.get("scope", settings.policy_scope),
                    "max_file_bytes": data.get("max_file_bytes", settings.max_file_bytes),
                    "source": "file",
                }
        except (OSError, ValueError):
            log.warning("policy file unreadable, falling back to env", exc_info=True)
    return {
        "mode": settings.ingest_policy,
        "allow": _split(settings.allow_repos),
        "deny": _split(settings.deny_repos),
        "scope": settings.policy_scope,
        "max_file_bytes": settings.max_file_bytes,
        "source": "env",
    }


def save(policy: dict) -> dict:
    keep = {
        "mode": policy.get("mode", "open"),
        "allow": list(policy.get("allow", [])),
        "deny": list(policy.get("deny", [])),
        "scope": policy.get("scope", "ingest"),
        "max_file_bytes": policy.get("max_file_bytes"),
    }
    if keep["mode"] not in ("open", "allowlist"):
        raise ValueError("mode must be open|allowlist")
    if keep["scope"] not in ("ingest", "all"):
        raise ValueError("scope must be ingest|all")
    # Preserve a docker policy this caller did not send, so saving the HF half
    # cannot silently erase the registry half.
    existing = _raw_file()
    if "docker" in existing:
        keep["docker"] = existing["docker"]
    if "docker" in policy:
        keep["docker"] = policy["docker"]
    p = _state_path()
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(keep, indent=2, sort_keys=True))
    tmp.replace(p)
    return load()


def _matches(key: str, patterns: list[str]) -> str | None:
    for pat in patterns:
        if fnmatch.fnmatch(key, pat):
            return pat
    return None


def check(repo_type: str, repo_id: str, policy: dict | None = None) -> Decision:
    """Decide whether this repo may be ingested."""
    pol = policy or load()
    key = f"{repo_type}s/{repo_id}"

    denied_by = _matches(key, pol["deny"])
    if denied_by:
        # Deny always wins: an explicit block should not be overridable by a
        # broad allow pattern someone added later.
        return Decision(False, f"denied by pattern {denied_by!r}")

    if pol["mode"] == "allowlist":
        allowed_by = _matches(key, pol["allow"])
        if not allowed_by:
            return Decision(False, "not on the allowlist")

    return Decision(True)


def check_size(size: int | None, policy: dict | None = None) -> Decision:
    """Refuse an ingest that is larger than the configured per-file cap.

    Uses the content-length from the metadata HEAD we already perform on the
    miss path, so it costs nothing extra and refuses before any bytes move.
    """
    pol = policy or load()
    cap = pol.get("max_file_bytes")
    if not cap or not size:
        return Decision(True)
    if size > cap:
        return Decision(False, f"file is {size} bytes, over the {cap} byte limit")
    return Decision(True)


def enforced_on_hits(policy: dict | None = None) -> bool:
    return (policy or load()).get("scope") == "all"


def _raw_file() -> dict:
    p = _state_path()
    if p.is_file():
        try:
            data = json.loads(p.read_text())
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            pass
    return {}


def load_docker() -> dict:
    """Effective Docker/OCI policy.

    Same shape and semantics as the HF side so there is one model to learn, but
    with an extra axis: `registries` gates the upstream HOST, separately from
    the image patterns. That separation is deliberate -- a two-line allowlist of
    hosts blocks the entire class of "someone pulled a random image from a
    random registry through the shared box", which is the exposure that
    path-prefix routing creates.
    """
    d = _raw_file().get("docker")
    if isinstance(d, dict):
        return {
            "mode": d.get("mode", settings.docker_policy),
            "registries": list(d.get("registries", [])),
            "allow": list(d.get("allow", [])),
            "deny": list(d.get("deny", [])),
            "scope": d.get("scope", settings.policy_scope),
            "max_blob_bytes": d.get("max_blob_bytes", settings.docker_max_blob_bytes),
            "source": "file",
        }
    return {
        "mode": settings.docker_policy,
        "registries": _split(settings.allow_registries),
        "allow": _split(settings.allow_images),
        "deny": _split(settings.deny_images),
        "scope": settings.policy_scope,
        "max_blob_bytes": settings.docker_max_blob_bytes,
        "source": "env",
        "deny_registries": _split(settings.deny_registries),
    }


def check_docker(upstream: str, repo: str, policy: dict | None = None) -> Decision:
    """Decide whether this image may be ingested. Deny always wins, as on the
    HF side."""
    pol = policy or load_docker()
    key = f"{upstream}/{repo}"

    denied_host = _matches(upstream, pol.get("deny_registries", []))
    if denied_host:
        return Decision(False, f"registry denied by pattern {denied_host!r}")

    denied_by = _matches(key, pol["deny"])
    if denied_by:
        return Decision(False, f"denied by pattern {denied_by!r}")

    if pol["mode"] == "allowlist":
        # The registry allowlist, when set, is authoritative on the host.
        if pol["registries"] and not _matches(upstream, pol["registries"]):
            return Decision(False, f"registry {upstream!r} is not on the allowlist")
        if pol["allow"] and not _matches(key, pol["allow"]):
            return Decision(False, "not on the allowlist")
        if not pol["registries"] and not pol["allow"]:
            return Decision(False, "not on the allowlist")
    elif pol["registries"] and not _matches(upstream, pol["registries"]):
        # Registries can be restricted even in `open` mode: it is the cheap,
        # high-value guard and should not require flipping the whole policy.
        return Decision(False, f"registry {upstream!r} is not on the allowlist")

    return Decision(True)


def check_blob_size(size: int | None, policy: dict | None = None) -> Decision:
    """Refuse an oversized layer using the upstream Content-Length, before any
    bytes move."""
    pol = policy or load_docker()
    cap = pol.get("max_blob_bytes")
    if not cap or not size:
        return Decision(True)
    if size > cap:
        return Decision(False, f"blob is {size} bytes, over the {cap} byte limit")
    return Decision(True)


def docker_enforced_on_hits(policy: dict | None = None) -> bool:
    return (policy or load_docker()).get("scope") == "all"
