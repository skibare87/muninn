"""Prometheus metrics.

Hand-rolled text format rather than a client library: the exposition format is a
few lines of string building, and this service otherwise has four dependencies.

Counters live in the process and reset on restart. Prometheus handles a counter
RESET; what it cannot handle is a series that does not exist, and those are not
the same thing.

A `collections.Counter` materialises a key on first increment, so before a given
result has occurred even once since startup, that series is ABSENT from the
exposition rather than present at zero. Over a window containing a restart the
timeline is then full of holes that look exactly like zero, and `increase()`
cannot tell "this never happened" from "the process restarted and nothing has
triggered this label yet". That makes the counters unable to answer the
frequency questions counters exist for -- measured by an operator who tried to
ask "has the fleet been hitting this?" over 7 days and could not, while
`up{job="muninn"}` and the disk-derived gauges were continuous throughout.

So every label combination that CAN occur is seeded to 0 at import. A zero then
means zero, a gap means the process was down, and `increase()` works across a
restart. Gauges are unaffected: they are derived from disk or live state rather
than accumulated.
"""

from __future__ import annotations

import threading
from collections import Counter

_lock = threading.Lock()

# Labelled counters, kept as flat dicts so the exposition loop stays trivial.
_requests: Counter[str] = Counter()  # by result: HIT, MISS-STREAM, SYNTHESIZED, ...
_upstream: Counter[str] = Counter()  # by status class: 2xx, 4xx, 404, 5xx, error
_ingest_verify: Counter[str] = Counter()  # VERIFIED | UNVERIFIABLE | MISMATCH
_clients: Counter[str] = Counter()  # by X-Muninn-Client, when sent
_bytes_served = 0
_bytes_ingested = 0
# Writes toward the Hub (XHC_HF_WRITES=on), by outcome. See _HF_WRITE_SERIES.
_hf_writes: Counter[str] = Counter()

# Docker/OCI counters, kept separate from the HF ones so a registry problem is
# not averaged away into model traffic and vice versa.
_docker: Counter[str] = Counter()  # "result|kind", e.g. "HIT|blob"
_docker_upstream: Counter[str] = Counter()  # "registry|statusclass"
_docker_bytes_served = 0
_docker_bytes_ingested = 0

# The object-store second tier. Counted apart from both protocols' own
# counters, so "served from the tier" never inflates "served" or "ingested".
_tier_requests: Counter[str] = Counter()  # "proto|kind|result"
_tier_verify: Counter[str] = Counter()  # verified | mismatch
_tier_upload: Counter[str] = Counter()  # ok | failed | skipped_* | verify_mismatch | dropped_*
_tier_index_writes: Counter[str] = Counter()  # signed | unsigned | failed | skipped_exists
# Phase 2. One per restore ATTEMPT (a request or prewarm the Hub could not
# answer), by outcome; and one per index OBJECT read, by what its
# authentication said. A ref walk that skips a bad signature and restores from
# an older observation is one "ok" restore and one "bad_signature" read, so the
# second counter is where a tampered entry shows up.
_tier_restore: Counter[str] = Counter()
_tier_index_reads: Counter[str] = Counter()
# BODY bytes only. A HEAD transfers none and is never counted here -- the
# served counter once booked HEADs as bytes, and every figure built on it
# was inflated.
_tier_bytes_read = 0
_tier_bytes_written = 0

_TIER_REQUEST_SERIES: tuple[str, ...] = tuple(
    f"{proto}|{kind}|{result}"
    for proto, kind in (("hf", "blob"), ("oci", "blob"), ("oci", "manifest"))
    for result in ("hit", "miss", "error", "refused")
)
_TIER_VERIFY_SERIES: tuple[str, ...] = ("verified", "mismatch")
_TIER_UPLOAD_SERIES: tuple[str, ...] = (
    "ok", "failed", "skipped_exists", "skipped_evicted", "verify_mismatch",
    "dropped_queue_full",
)
_TIER_INDEX_SERIES: tuple[str, ...] = ("signed", "unsigned", "failed", "skipped_exists")
_TIER_RESTORE_SERIES: tuple[str, ...] = (
    "ok", "ok_unsigned", "unsigned_refused", "bad_signature", "missing",
    "content_missing", "content_mismatch", "no_key", "policy_refused", "error",
)
_TIER_INDEX_READ_SERIES: tuple[str, ...] = (
    "signed_ok", "unsigned_accepted", "unsigned_refused", "bad_signature", "malformed",
)

# Bounds the label cardinality: a client that sends a unique header per request
# would otherwise grow this map without limit and blow up the scrape.
MAX_CLIENT_LABELS = 200

# Every (result, kind) this process can emit, seeded to 0 so the series exists
# from startup rather than from first occurrence. NOT the cartesian product:
# only combinations a code path can actually produce, so the exposition does not
# advertise states that cannot happen. Adding a new record_docker() result means
# adding it here -- there is a test that fails if a call site uses a pair this
# list does not carry.
_DOCKER_SERIES: tuple[tuple[str, str], ...] = (
    ("HIT", "manifest"), ("HIT", "blob"),
    ("MISS", "manifest"), ("MISS", "blob"),
    ("RETAINED", "manifest"),
    ("DENIED", "manifest"), ("DENIED", "blob"),
    ("UPSTREAM_AUTH", "manifest"), ("UPSTREAM_AUTH", "blob"),
    ("PROXIED", "tags"), ("PROXIED", "referrers"),
    ("PUSH", "manifest"), ("PUSH", "blob"),
    ("BYPASS", "blob"),
)

# Ingest verification outcomes. VERIFIED and UNVERIFIABLE are both normal; the
# distinction is the entire point, because an unverifiable file must never be
# indistinguishable from a checked one. MISMATCH is the one that should be zero,
# and it is seeded so that a zero means zero rather than "nothing reported yet".
_INGEST_VERIFY_SERIES: tuple[str, ...] = ("VERIFIED", "UNVERIFIABLE", "MISMATCH")

# Every write that reaches app/hfwrites.py ends in exactly one of these:
#   forwarded             the Hub answered 1xx-3xx
#   upstream_rejected     forwarded, and the Hub answered 4xx/5xx
#   upstream_unreachable  forwarded, and no answer came back
#   denied                refused locally: a grant was missing
#   too_large             refused locally: body over XHC_HF_WRITE_MAX_BODY
#   invalid               refused locally: a body that could not be inspected
# `denied` is the one worth an alert when it is not expected; `forwarded` is the
# one that says the cache's own account is changing things on the Hub.
_HF_WRITE_SERIES: tuple[str, ...] = (
    "forwarded", "upstream_rejected", "upstream_unreachable",
    "denied", "too_large", "invalid",
)

# Results carrying no kind dimension are seeded the same way.
_REQUEST_SERIES: tuple[str, ...] = (
    "DSSERVER-HIT", "DSSERVER-MISS", "DSSERVER-SYNTHESIZED",
    "VIEWER-HIT", "VIEWER-SYNTHESIZED",
)


def _seed() -> None:
    """Make every possible series exist at zero. Idempotent, and it must never
    overwrite a live value -- `setdefault` semantics, not assignment."""
    for result, kind in _DOCKER_SERIES:
        _docker.setdefault(f"{result}|{kind}", 0)
    for result in _REQUEST_SERIES:
        _requests.setdefault(result, 0)
    for result in _INGEST_VERIFY_SERIES:
        _ingest_verify.setdefault(result, 0)
    for k in _TIER_REQUEST_SERIES:
        _tier_requests.setdefault(k, 0)
    for k in _TIER_VERIFY_SERIES:
        _tier_verify.setdefault(k, 0)
    for k in _TIER_UPLOAD_SERIES:
        _tier_upload.setdefault(k, 0)
    for k in _TIER_INDEX_SERIES:
        _tier_index_writes.setdefault(k, 0)
    for k in _HF_WRITE_SERIES:
        _hf_writes.setdefault(k, 0)
    for k in _TIER_RESTORE_SERIES:
        _tier_restore.setdefault(k, 0)
    for k in _TIER_INDEX_READ_SERIES:
        _tier_index_reads.setdefault(k, 0)


_seed()


def record_hf_write(result: str) -> None:
    """One outcome from _HF_WRITE_SERIES. An undeclared result is a bug, and is
    refused here rather than creating a series nobody seeded."""
    if result not in _HF_WRITE_SERIES:
        raise ValueError(f"undeclared hf write result {result!r}")
    with _lock:
        _hf_writes[result] += 1


def record_request(result: str, client: str | None = None) -> None:
    with _lock:
        _requests[result] += 1
        if client:
            if len(_clients) >= MAX_CLIENT_LABELS and client not in _clients:
                _clients["__other__"] += 1
            else:
                _clients[client] += 1


def record_upstream(status: int | None) -> None:
    with _lock:
        if status is None:
            _upstream["error"] += 1
        elif status == 404:
            _upstream["404"] += 1
        else:
            _upstream[f"{status // 100}xx"] += 1


def record_ingest_verify(result: str) -> None:
    """One of VERIFIED / UNVERIFIABLE / MISMATCH, per file ingested."""
    with _lock:
        _ingest_verify[result] += 1


def record_docker(result: str, kind: str) -> None:
    with _lock:
        _docker[f"{result}|{kind}"] += 1


def record_docker_upstream(registry: str, status: int | None) -> None:
    if status is None:
        cls = "error"
    elif status in (401, 404, 429):
        cls = str(status)
    else:
        cls = f"{status // 100}xx"
    with _lock:
        _docker_upstream[f"{registry}|{cls}"] += 1


def record_docker_bytes(served: int = 0, ingested: int = 0) -> None:
    global _docker_bytes_served, _docker_bytes_ingested  # noqa: PLW0603
    with _lock:
        _docker_bytes_served += served
        _docker_bytes_ingested += ingested


def record_tier_request(proto: str, kind: str, result: str) -> None:
    with _lock:
        _tier_requests[f"{proto}|{kind}|{result}"] += 1


def record_tier_verify(result: str) -> None:
    with _lock:
        _tier_verify[result] += 1


def record_tier_upload(result: str) -> None:
    with _lock:
        _tier_upload[result] += 1


def record_tier_index_write(result: str) -> None:
    with _lock:
        _tier_index_writes[result] += 1


def record_tier_restore(result: str) -> None:
    with _lock:
        _tier_restore[result] += 1


def record_tier_index_read(result: str) -> None:
    with _lock:
        _tier_index_reads[result] += 1


def record_tier_bytes(read: int = 0, written: int = 0) -> None:
    global _tier_bytes_read, _tier_bytes_written  # noqa: PLW0603
    with _lock:
        _tier_bytes_read += read
        _tier_bytes_written += written


def record_served(n: int) -> None:
    global _bytes_served  # noqa: PLW0603 - module-level counter
    with _lock:
        _bytes_served += n


def record_ingested(n: int) -> None:
    global _bytes_ingested  # noqa: PLW0603 - module-level counter
    with _lock:
        _bytes_ingested += n


def snapshot() -> dict:
    with _lock:
        return {
            "requests": dict(_requests),
            "ingest_verify": dict(_ingest_verify),
            "upstream": dict(_upstream),
            "clients": dict(_clients),
            "bytes_served": _bytes_served,
            "bytes_ingested": _bytes_ingested,
            "docker": dict(_docker),
            "docker_upstream": dict(_docker_upstream),
            "docker_bytes_served": _docker_bytes_served,
            "docker_bytes_ingested": _docker_bytes_ingested,
            "tier_requests": dict(_tier_requests),
            "tier_verify": dict(_tier_verify),
            "tier_upload": dict(_tier_upload),
            "tier_index_writes": dict(_tier_index_writes),
            "tier_restore": dict(_tier_restore),
            "tier_index_reads": dict(_tier_index_reads),
            "tier_bytes_read": _tier_bytes_read,
            "tier_bytes_written": _tier_bytes_written,
            "hf_writes": dict(_hf_writes),
        }


def reset() -> None:
    global _bytes_served, _bytes_ingested  # noqa: PLW0603 - test helper
    global _docker_bytes_served, _docker_bytes_ingested  # noqa: PLW0603 - test helper
    global _tier_bytes_read, _tier_bytes_written  # noqa: PLW0603 - test helper
    with _lock:
        _tier_requests.clear()
        _tier_verify.clear()
        _tier_upload.clear()
        _tier_index_writes.clear()
        _hf_writes.clear()
        _tier_restore.clear()
        _tier_index_reads.clear()
        _tier_bytes_read = 0
        _tier_bytes_written = 0
        _requests.clear()
        _ingest_verify.clear()
        _upstream.clear()
        _clients.clear()
        _docker.clear()
        _docker_upstream.clear()
        _bytes_served = 0
        _bytes_ingested = 0
        _docker_bytes_served = 0
        _docker_bytes_ingested = 0
        # Re-seed INSIDE the lock. _seed() does not take it, and a reader
        # between the clear and the seed would see every series missing.
        #
        # Without this a reset leaves each series ABSENT rather than zero,
        # which destroys the distinction _seed exists to preserve: a zero means
        # zero, a missing series means the process was down. Found by a test
        # asserting a MISMATCH count of 0 after a reset and getting KeyError.
        _seed()


def _escape(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def render(gauges: dict[str, float], help_text: dict[str, str] | None = None) -> str:
    """Emit the exposition format. `gauges` is name -> value for point-in-time state."""
    snap = snapshot()
    help_text = help_text or {}
    out: list[str] = []

    def emit(name: str, kind: str, samples: list[tuple[str, float]]) -> None:
        if name in help_text:
            out.append(f"# HELP {name} {help_text[name]}")
        out.append(f"# TYPE {name} {kind}")
        out.extend(f"{name}{labels} {value}" for labels, value in samples)

    emit(
        "muninn_requests_total",
        "counter",
        [(f'{{result="{_escape(k)}"}}', v) for k, v in sorted(snap["requests"].items())],
    )
    emit(
        "muninn_ingest_verify_total",
        "counter",
        [(f'{{result="{_escape(k)}"}}', v) for k, v in sorted(snap["ingest_verify"].items())],
    )
    emit(
        "muninn_upstream_requests_total",
        "counter",
        [(f'{{status="{_escape(k)}"}}', v) for k, v in sorted(snap["upstream"].items())],
    )
    if snap["clients"]:
        emit(
            "muninn_client_requests_total",
            "counter",
            [(f'{{client="{_escape(k)}"}}', v) for k, v in sorted(snap["clients"].items())],
        )
    if snap["docker"]:
        emit(
            "muninn_docker_requests_total",
            "counter",
            [
                (f'{{result="{_escape(k.split("|")[0])}",kind="{_escape(k.split("|")[1])}"}}', v)
                for k, v in sorted(snap["docker"].items())
            ],
        )
    if snap["docker_upstream"]:
        emit(
            "muninn_docker_upstream_requests_total",
            "counter",
            [
                (
                    f'{{registry="{_escape(k.split("|")[0])}",'
                    f'status="{_escape(k.split("|")[1])}"}}',
                    v,
                )
                for k, v in sorted(snap["docker_upstream"].items())
            ],
        )
    emit("muninn_docker_bytes_served_total", "counter", [("", snap["docker_bytes_served"])])
    emit(
        "muninn_docker_bytes_ingested_total", "counter", [("", snap["docker_bytes_ingested"])]
    )
    emit(
        "muninn_tier_requests_total",
        "counter",
        [
            (
                f'{{proto="{_escape(k.split("|")[0])}",kind="{_escape(k.split("|")[1])}",'
                f'result="{_escape(k.split("|")[2])}"}}',
                v,
            )
            for k, v in sorted(snap["tier_requests"].items())
        ],
    )
    for name, key in (
        ("muninn_tier_verify_total", "tier_verify"),
        ("muninn_tier_upload_total", "tier_upload"),
        ("muninn_tier_index_writes_total", "tier_index_writes"),
        ("muninn_hf_writes_total", "hf_writes"),
        ("muninn_tier_restore_total", "tier_restore"),
        ("muninn_tier_index_reads_total", "tier_index_reads"),
    ):
        emit(name, "counter",
             [(f'{{result="{_escape(k)}"}}', v) for k, v in sorted(snap[key].items())])
    emit("muninn_tier_bytes_read_total", "counter", [("", snap["tier_bytes_read"])])
    emit("muninn_tier_bytes_written_total", "counter", [("", snap["tier_bytes_written"])])
    emit("muninn_bytes_served_total", "counter", [("", snap["bytes_served"])])
    emit("muninn_bytes_ingested_total", "counter", [("", snap["bytes_ingested"])])

    for name, value in sorted(gauges.items()):
        emit(name, "gauge", [("", value)])

    return "\n".join(out) + "\n"
