"""/_cache/repos says whether a cached snapshot is complete.

THE DEFECT: after a prewarm was killed part-way, the repo appeared in
/_cache/repos as pinned, holding only its small files -- 32 MB of a 52 GB repo --
and nothing distinguished that from a finished prewarm. A pinned repo reads as
"this is ready for the fleet".

What the cache knows about a snapshot's expected contents comes from ONE place:
the listing the prewarm fetched before downloading, recorded locally. The
listing endpoint never calls upstream. Where no listing was recorded (a snapshot
assembled file by file from client misses), completeness is `null` -- unknown,
never guessed as complete.

The fake Hub is a real HTTP server, for the same reason as in
test_hf_ingest_integrity: the download belongs to huggingface_hub, and a mock
would measure my model of it rather than the library.
"""

from __future__ import annotations

import asyncio
import hashlib
import http.server
import json
import sys
import threading
from pathlib import Path
from typing import ClassVar
from urllib.parse import unquote, urlparse

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import cachefs, jobs, manage
from app.config import settings

REPO = "acme/model"
COMMIT = "1" * 40
FILES = {
    "config.json": b'{"a": 1}',
    "tokenizer.json": b'{"vocab": []}',
    "model-00001.safetensors": b"W" * 4096,
    "model-00002.safetensors": b"X" * 8192,
}


def _etag(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


class _Hub(http.server.BaseHTTPRequestHandler):
    withheld: ClassVar[set[str]] = set()
    log: ClassVar[list[tuple[str, str, str | None]]] = []

    def _file(self) -> str | None:
        path = unquote(urlparse(self.path).path)
        prefix = f"/{REPO}/resolve/"
        if not path.startswith(prefix):
            return None
        _rev, _, name = path[len(prefix):].partition("/")
        return name

    def _info(self) -> None:
        body = json.dumps({
            "id": REPO,
            "sha": COMMIT,
            "siblings": [{"rfilename": n, "size": len(b)} for n, b in FILES.items()],
        }).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve(self, with_body: bool) -> None:
        path = urlparse(self.path).path
        if path.startswith(f"/api/models/{REPO}/revision/"):
            type(self).log.append((self.command, "INFO", None))
            return self._info()
        name = self._file()
        type(self).log.append((self.command, name or path, self.headers.get("range")))
        if name not in FILES or name in type(self).withheld:
            self.send_response(404)
            self.send_header("x-error-code", "EntryNotFound")
            self.send_header("content-length", "0")
            self.end_headers()
            return
        body = FILES[name]
        start = 0
        rng = self.headers.get("range")
        if rng and with_body and rng.startswith("bytes="):
            start = int(rng[6:].split("-")[0])
        self.send_response(206 if start else 200)
        for k, v in {
            "x-repo-commit": COMMIT,
            "etag": f'"{_etag(body)}"',
            "x-linked-etag": f'"{_etag(body)}"',
            "x-linked-size": str(len(body)),
            "content-length": str(len(body) - start),
            "accept-ranges": "bytes",
            "content-type": "application/octet-stream",
        }.items():
            self.send_header(k, v)
        self.end_headers()
        if with_body:
            self.wfile.write(body[start:])

    def do_HEAD(self) -> None:
        self._serve(False)

    def do_GET(self) -> None:
        self._serve(True)

    def log_message(self, *args) -> None:
        return


@pytest.fixture
def hub(tmp_path, monkeypatch):
    _Hub.withheld = set()
    _Hub.log = []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Hub)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(settings, "cache_dir", str(cache))
    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "state"), raising=False)
    monkeypatch.setattr(settings, "upstream", f"http://127.0.0.1:{srv.server_address[1]}")
    monkeypatch.setattr(settings, "hf_token", None)
    monkeypatch.setattr(settings, "hf_verify_ingest", True)
    cachefs.invalidate_view()
    try:
        yield _Hub
    finally:
        cachefs.invalidate_view()
        srv.shutdown()
        srv.server_close()


def _prewarm(allow_patterns=None) -> jobs.Job:
    async def go():
        m = jobs.JobManager()
        job = await m.ensure_snapshot("model", REPO, "main", allow_patterns)
        await job.done.wait()
        await asyncio.sleep(0)
        return job

    return asyncio.run(go())


def _listing() -> dict:
    cachefs.invalidate_view()
    body = asyncio.run(manage.list_repos(refresh=True))
    (repo,) = [r for r in body["repos"] if r["repo_id"] == REPO]
    return repo


def test_a_complete_prewarm_reports_complete(hub):
    job = _prewarm()
    assert job.state == "done", job.error
    r = _listing()
    assert r["complete"] is True
    assert r["files_present"] == r["files_expected"] == 4
    assert r["bytes_expected"] == sum(len(b) for b in FILES.values())
    assert r["bytes_present"] == r["bytes_expected"]
    (rev,) = r["revisions"]
    assert rev["complete"] is True
    assert rev["expected_scope"] == "repo"


def test_a_partial_prewarm_reports_incomplete_with_counts(hub):
    hub.withheld = {"model-00002.safetensors"}
    job = _prewarm()
    assert job.state == "error"
    r = _listing()
    assert r["complete"] is False
    assert r["files_expected"] == 4
    assert r["files_present"] == 3
    assert r["bytes_expected"] == sum(len(b) for b in FILES.values())
    assert r["bytes_present"] == r["bytes_expected"] - len(FILES["model-00002.safetensors"])


def test_listing_makes_no_upstream_call(hub):
    _prewarm()
    before = len(hub.log)
    _listing()
    _listing()
    assert len(hub.log) == before


def test_allow_patterns_prewarm_is_complete_relative_to_what_was_asked(hub):
    job = _prewarm(["*.json"])
    assert job.state == "done", job.error
    r = _listing()
    assert r["complete"] is True
    assert r["files_expected"] == r["files_present"] == 2
    (rev,) = r["revisions"]
    assert rev["expected_scope"] == "allow_patterns"
    assert rev["allow_patterns"] == [["*.json"]]


def test_a_snapshot_without_a_recorded_listing_is_unknown(hub):
    # Assembled by hand, as a client miss would: no prewarm, no listing.
    repo = Path(settings.cache_dir) / "models--acme--model"
    (repo / "blobs").mkdir(parents=True)
    (repo / "snapshots" / COMMIT).mkdir(parents=True)
    body = FILES["config.json"]
    blob = repo / "blobs" / _etag(body)
    blob.write_bytes(body)
    (repo / "snapshots" / COMMIT / "config.json").symlink_to(blob)

    r = _listing()
    assert r["complete"] is None
    assert r["files_expected"] is None
    assert r["bytes_expected"] is None
    assert r["files_present"] == 1


def test_resubmitting_an_interrupted_prewarm_resumes_it(hub):
    """The documented resume is: submit the same prewarm again.

    Evidence that it is safe and cheap rather than a re-download: files already
    in the snapshot get NO request at all (huggingface_hub returns the pointer
    before any network call when the revision is a commit, and snapshot_download
    always passes the commit), and a half-written blob resumes with a Range
    request from where it stopped.
    """
    hub.withheld = {"model-00002.safetensors"}
    assert _prewarm().state == "error"

    # And leave a half-downloaded blob behind, as an OOM kill would.
    body = FILES["model-00002.safetensors"]
    blobs = Path(settings.cache_dir) / "models--acme--model" / "blobs"
    (blobs / f"{_etag(body)}.incomplete").write_bytes(body[:1000])

    hub.withheld = set()
    hub.log = []
    job = _prewarm()
    assert job.state == "done", job.error

    fetched = {name for (_m, name, _r) in hub.log if name != "INFO"}
    assert fetched == {"model-00002.safetensors"}, hub.log
    ranges = [r for (m, name, r) in hub.log if m == "GET" and name == "model-00002.safetensors"]
    assert ranges == ["bytes=1000-"]

    got = (Path(settings.cache_dir) / "models--acme--model" / "snapshots" / COMMIT
           / "model-00002.safetensors").read_bytes()
    assert got == body
    assert _listing()["complete"] is True
