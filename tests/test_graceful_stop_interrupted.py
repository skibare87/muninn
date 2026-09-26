"""A prewarm still in flight at a GRACEFUL stop comes back `interrupted`.

WHY THIS RUNS THE REAL SERVER. The claim is about what happens between SIGTERM
and process exit, and that sequence belongs to uvicorn and asyncio.run, not to
code in this repo: uvicorn runs the lifespan's shutdown half, returns from
serve(), and asyncio.run then cancels every task still alive. A job's cancel
handler runs in THAT last step -- after the lifespan's final ledger flush -- so
a test that drives the lifespan by hand measures a sequence production never
executes.

TWO WAYS THE SAME SIGTERM ENDS, and only one of them is what ships. After
serve() returns, uvicorn restores the SIGTERM handler it found and RE-RAISES
the signal. For an ordinary process that handler is the default action, so the
process dies right there -- before asyncio.run cancels anything -- and the
ledger is left saying `running`, which the next boot reads as interrupted. But
the shipped image runs uvicorn as PID 1 (exec-form CMD, no init), and the
kernel drops a default-action signal sent to a namespace's init: measured, a
`signal.raise_signal(SIGTERM)` in that image as PID 1 prints and exits 0. So in
a container the re-raise does nothing, asyncio.run goes on to cancel the job,
and its cancel handler used to overwrite the ledger with `error: cancelled`.
The `pid1` case reproduces that here by starting uvicorn with SIGTERM ignored,
which is the disposition it then restores and re-raises into. The `plain` case
passed before the fix; `pid1` failed with state `error`.

What it pins: a job that was `running` when SIGTERM arrived is recorded
`interrupted`, with its progress, both in the ledger file the stopped process
left and in what the next boot serves. Before the fix it was recorded
`error: cancelled`, which reads as a failure somebody should investigate rather
than as work a restart cut short.

Bounded everywhere, so it cannot hang the suite: the fake Hub trickles but stops
sending once the signal is in, every wait has a deadline, and the server is
killed if it outlives STOP_TIMEOUT_S.
"""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
REPO = "acme/slow"
COMMIT = "1" * 40
FILENAME = "model.safetensors"
# huggingface_hub writes in 10 MiB chunks (constants.DOWNLOAD_CHUNK_SIZE), so
# nothing lands on disk until that much has arrived. FAST_PREFIX is sent at
# once, so progress is visible early; the rest trickles, so the job is still
# running when the signal arrives.
SIZE = 24 * 1024 * 1024
FAST_PREFIX = 11 * 1024 * 1024
CHUNK = 64 * 1024
# How long a whole graceful stop may take. Each of the app's shutdown steps is
# bounded at 10 s (app/shutdown.py) and an idle stop takes well under a second,
# so this is reached only if the stop hangs.
STOP_TIMEOUT_S = 30.0
TOKEN = "t0ken-for-tests"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _SlowHub(http.server.BaseHTTPRequestHandler):
    """repo_info at once; the one LFS file trickled until `stop` is set."""

    stop = threading.Event()
    body = b"W" * SIZE
    sha = hashlib.sha256(body).hexdigest()

    def _send(self, with_body: bool) -> None:
        path = unquote(urlparse(self.path).path)
        if path.startswith(f"/api/models/{REPO}/revision/"):
            info = json.dumps({"id": REPO, "sha": COMMIT, "siblings": [{
                "rfilename": FILENAME, "size": SIZE,
                "lfs": {"sha256": self.sha, "size": SIZE, "pointerSize": 134},
            }]}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(info)))
            self.end_headers()
            self.wfile.write(info)
            return
        if not path.startswith(f"/{REPO}/resolve/") or not path.endswith(FILENAME):
            self.send_response(404)
            self.send_header("content-length", "0")
            self.end_headers()
            return
        self.send_response(200)
        for k, v in {
            "x-repo-commit": COMMIT, "etag": f'"{self.sha}"',
            "x-linked-etag": f'"{self.sha}"', "x-linked-size": str(SIZE),
            "content-length": str(SIZE), "accept-ranges": "bytes",
            "content-type": "application/octet-stream",
        }.items():
            self.send_header(k, v)
        self.end_headers()
        if not with_body:
            return
        sent = FAST_PREFIX
        try:
            self.wfile.write(self.body[:FAST_PREFIX])
            while sent < SIZE and not type(self).stop.is_set():
                self.wfile.write(self.body[sent:sent + CHUNK])
                self.wfile.flush()
                sent += CHUNK
                time.sleep(0.1)
            # Stopped part-way: drop the connection so the client's read ends
            # now rather than at its own timeout.
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            return

    def do_HEAD(self):
        self._send(False)

    def do_GET(self):
        self._send(True)

    def log_message(self, *a):
        return


@pytest.fixture
def slow_hub():
    _SlowHub.stop = threading.Event()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _SlowHub)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", _SlowHub.stop
    finally:
        _SlowHub.stop.set()
        srv.shutdown()
        srv.server_close()


# Container PID 1: a SIGTERM whose action is the default is dropped. uvicorn
# restores whatever handler it found at start and re-raises into it, so
# starting it with SIGTERM ignored gives the re-raise the same no-op.
_AS_PID1 = (
    "import signal, sys; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    "import uvicorn; sys.argv[0] = 'uvicorn'; uvicorn.main()"
)


def _start(tmp_path: Path, upstream: str, port: int, log, mode: str = "plain") -> subprocess.Popen:
    # Selected, not filtered: only what the server needs from this environment,
    # plus the settings under test.
    env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "TMPDIR") if k in os.environ}
    env.update({
        "HF_HUB_CACHE": str(tmp_path / "cache"),
        "HF_HOME": str(tmp_path / "hfhome"),
        "HF_XET_CACHE": str(tmp_path / "xet"),
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "XHC_STATE_DIR": str(tmp_path / "state"),
        "XHC_UPSTREAM": upstream,
        "XHC_MANAGE_TOKEN": TOKEN,
        "XHC_DOCKER_ENABLED": "0",
        "XHC_LOG_LEVEL": "INFO",
    })
    return subprocess.Popen(
        [sys.executable, *(["-c", _AS_PID1] if mode == "pid1" else ["-m", "uvicorn"]),
         "app.main:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "info"],
        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
    )


def _wait_up(base: str, proc: subprocess.Popen, deadline_s: float = 30.0) -> None:
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        if proc.poll() is not None:
            raise AssertionError(f"server exited during startup: {proc.returncode}")
        try:
            if httpx.get(f"{base}/healthz", timeout=1.0).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise AssertionError("server did not come up")


def _stop(proc: subprocess.Popen) -> int:
    proc.send_signal(signal.SIGTERM)
    try:
        return proc.wait(timeout=STOP_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)
        raise AssertionError(f"graceful stop did not finish within {STOP_TIMEOUT_S}s") from None


@pytest.mark.parametrize("mode", ["plain", "pid1"])
def test_graceful_stop_records_running_prewarm_as_interrupted(tmp_path, slow_hub, mode):
    upstream, hub_stop = slow_hub
    (tmp_path / "cache").mkdir()
    headers = {"authorization": f"Bearer {TOKEN}"}
    log_path = tmp_path / "server.log"
    with open(log_path, "wb") as log:
        port = _free_port()
        base = f"http://127.0.0.1:{port}"
        proc = _start(tmp_path, upstream, port, log, mode)
        try:
            _wait_up(base, proc)
            r = httpx.post(f"{base}/_cache/prewarm", headers=headers,
                           json={"repo_id": REPO}, timeout=10)
            assert r.status_code == 200, r.text
            job_id = r.json()["job"]["id"]

            # Running AND reporting progress, so "with its progress" has
            # something to be checked against. A snapshot's progress is sampled
            # every 5 s (jobs._SNAPSHOT_SAMPLE_S), so this takes that long.
            end = time.monotonic() + 30
            job: dict = {}
            while time.monotonic() < end:
                job = httpx.get(f"{base}/_cache/jobs/{job_id}", headers=headers,
                                timeout=5).json()
                if job["state"] != "running" or job.get("downloaded_bytes"):
                    break
                time.sleep(0.2)
            assert job["state"] == "running" and job["downloaded_bytes"], job
        except BaseException:
            proc.kill()
            proc.wait(timeout=10)
            raise
        # The download runs in a worker thread, and asyncio.run waits for the
        # default executor once the loop is done. End the transfer shortly
        # after the signal, as a real Hub connection would be torn down.
        threading.Timer(0.5, hub_stop.set).start()
        _stop(proc)

    server_log = log_path.read_text(errors="replace")
    ledger = json.loads((tmp_path / "state" / "hf" / "jobs.json").read_text())
    rec = next(j for j in ledger["jobs"] if j["id"] == job_id)
    assert rec["state"] == "interrupted", (rec, server_log[-4000:])
    assert rec["error"] is None, rec
    assert rec["interrupted_at"] is not None, rec
    assert rec["finished_at"] is None, rec
    assert rec["recorded_bytes"], rec  # its progress, not nothing

    # And the next boot serves the same answer.
    with open(tmp_path / "server2.log", "wb") as log:
        port = _free_port()
        base = f"http://127.0.0.1:{port}"
        proc = _start(tmp_path, upstream, port, log)
        try:
            _wait_up(base, proc)
            job = httpx.get(f"{base}/_cache/jobs/{job_id}", headers=headers,
                            timeout=5).json()
        finally:
            _stop(proc)
    assert job["state"] == "interrupted", job
    assert job["error"] is None, job
    assert job["downloaded_bytes"] == rec["recorded_bytes"], job
