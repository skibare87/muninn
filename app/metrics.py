"""Prometheus metrics.

Hand-rolled text format rather than a client library: the exposition format is a
few lines of string building, and this service otherwise has four dependencies.

Counters live in the process and reset on restart, which is fine — Prometheus
handles counter resets, and every gauge here is derived from disk or from live
state rather than accumulated.
"""

from __future__ import annotations

import threading
from collections import Counter

_lock = threading.Lock()

# Labelled counters, kept as flat dicts so the exposition loop stays trivial.
_requests: Counter[str] = Counter()  # by result: HIT, MISS-STREAM, SYNTHESIZED, ...
_upstream: Counter[str] = Counter()  # by status class: 2xx, 4xx, 404, 5xx, error
_clients: Counter[str] = Counter()  # by X-Muninn-Client, when sent
_bytes_served = 0
_bytes_ingested = 0

# Bounds the label cardinality: a client that sends a unique header per request
# would otherwise grow this map without limit and blow up the scrape.
MAX_CLIENT_LABELS = 200


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
            "upstream": dict(_upstream),
            "clients": dict(_clients),
            "bytes_served": _bytes_served,
            "bytes_ingested": _bytes_ingested,
        }


def reset() -> None:
    global _bytes_served, _bytes_ingested  # noqa: PLW0603 - test helper
    with _lock:
        _requests.clear()
        _upstream.clear()
        _clients.clear()
        _bytes_served = 0
        _bytes_ingested = 0


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
    emit("muninn_bytes_served_total", "counter", [("", snap["bytes_served"])])
    emit("muninn_bytes_ingested_total", "counter", [("", snap["bytes_ingested"])])

    for name, value in sorted(gauges.items()):
        emit(name, "gauge", [("", value)])

    return "\n".join(out) + "\n"
