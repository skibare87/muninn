"""Prewarm one repo at a pinned commit and wait until Muninn says it is done.

POSTs /_cache/prewarm, then polls /_cache/jobs/<id> until the job reaches a
final state. Exit status is the verdict, so the Job's own status is too:

    done         0   the files landed AND passed verification
    error        1   ingest or verification failed
    interrupted  1   the Muninn process restarted mid-job; a Job retry
                     re-submits the same prewarm, which resumes it
    (timeout)    1

`done` is the only success. Gate a rollout on this Job completing, never on
files merely appearing on disk: a job sits at `verifying` while its bytes are
already there.

Standard library only, so it runs from the Muninn image with no second image.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

URL = os.environ.get("MUNINN_URL", "http://muninn:8080").rstrip("/")
TOKEN = os.environ.get("XHC_MANAGE_TOKEN", "")
REPO = os.environ["PREWARM_REPO"]
REPO_TYPE = os.environ.get("PREWARM_REPO_TYPE", "model")
REVISION = os.environ["PREWARM_REVISION"]
PATTERNS = [p.strip() for p in os.environ.get("PREWARM_ALLOW_PATTERNS", "").split(",") if p.strip()]
PIN = os.environ.get("PREWARM_PIN", "1").strip().lower() in ("1", "true", "yes")
POLL_S = float(os.environ.get("PREWARM_POLL_SECONDS", "15"))
TIMEOUT_S = float(os.environ.get("PREWARM_TIMEOUT_SECONDS", "21600"))
READY_TIMEOUT_S = float(os.environ.get("PREWARM_READY_TIMEOUT_SECONDS", "900"))

FINAL = {"done", "error", "interrupted"}


def log(msg: str) -> None:
    print(f"prewarm: {msg}", flush=True)


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(URL + path, data=data, method=method)
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {}


def wait_healthy() -> None:
    """Wait for /healthz to say ok -- the BODY, not just a 200."""
    deadline = time.monotonic() + READY_TIMEOUT_S
    while True:
        try:
            status, body = call("GET", "/healthz")
            if status == 200 and body.get("ok") is True:
                return
        except (urllib.error.URLError, OSError, ValueError):
            pass
        if time.monotonic() > deadline:
            raise SystemExit(f"prewarm: {URL}/healthz not ok after {READY_TIMEOUT_S:.0f}s")
        time.sleep(5)


def main() -> int:
    # Fails on the arguments, before any I/O. A branch name here would make the
    # Job's success mean "whatever main was when it ran", which is not a
    # reproducible rollout gate.
    if not re.fullmatch(r"[0-9a-f]{40}", REVISION):
        raise SystemExit(f"prewarm: PREWARM_REVISION must be a 40-hex commit, got {REVISION!r}")
    if not TOKEN:
        raise SystemExit("prewarm: XHC_MANAGE_TOKEN is empty; /_cache/prewarm would be refused")

    wait_healthy()
    body: dict = {"repo_id": REPO, "repo_type": REPO_TYPE, "revision": REVISION, "pin": PIN}
    if PATTERNS:
        body["allow_patterns"] = PATTERNS
    status, resp = call("POST", "/_cache/prewarm", body)
    if status != 200:
        raise SystemExit(f"prewarm: POST /_cache/prewarm -> HTTP {status}: {resp}")
    job_id = resp["job"]["id"]
    log(f"job {job_id}: {REPO_TYPE} {REPO}@{REVISION} patterns={PATTERNS or 'all'} pin={PIN}")

    deadline = time.monotonic() + TIMEOUT_S
    last = None
    while True:
        status, job = call("GET", f"/_cache/jobs/{job_id}")
        if status == 404:
            # The process restarted AND its ledger lost the job. Unknown, so not
            # success; a Job retry re-submits, which skips what is cached.
            log(f"job {job_id}: no longer known to Muninn ({job.get('detail', '')})")
            return 1
        if status != 200:
            raise SystemExit(f"prewarm: GET /_cache/jobs/{job_id} -> HTTP {status}: {job}")
        state = job.get("state")
        if state != last:
            log(f"job {job_id}: {state}")
            last = state
        if state in FINAL:
            if job.get("verify") is not None:
                log(f"job {job_id}: verify {json.dumps(job['verify'])}")
            if state == "done":
                return 0
            log(f"job {job_id}: {state}: {job.get('error') or job.get('note') or ''}")
            return 1
        if time.monotonic() > deadline:
            log(f"job {job_id}: still {state} after {TIMEOUT_S:.0f}s; giving up")
            return 1
        time.sleep(POLL_S)


if __name__ == "__main__":
    sys.exit(main())
