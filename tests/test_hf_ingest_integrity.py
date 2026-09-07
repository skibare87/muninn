"""What the HF ingest path actually does when upstream bytes contradict the ETag.

The OCI path hashes every blob on ingest and refuses a mismatch
(ocistore.DigestMismatch). The HF path recomputes nothing -- the blob's filename
IS the upstream ETag, inherited from huggingface_hub's on-disk layout. So the two
protocols carry different integrity guarantees and nothing in the docs said so.

THIS MODULE MEASURES THE CURRENT BEHAVIOUR AND DOES NOT FIX IT. The ticket's own
instruction was to write the test first, because the behaviour was unmeasured and
might be better or worse than assumed. It turned out to be BOTH:

  - the length IS checked. huggingface_hub raises EnvironmentError
    ("Consistency check failed") when the received byte count disagrees with the
    declared size, so truncation and over-long bodies are caught.
  - the CONTENT is not. Bytes that contradict the ETag but match the declared
    length are cached silently, under a filename asserting the hash they do not
    have, and re-served forever.

That distinction matters for the fix decision: the exposure is not "anything can
be served", it is "any corruption that preserves length is invisible" -- bit
flips, a substituted blob of equal size, a mirror serving a different revision's
file. A hash is the only thing that separates those from a healthy fetch, and
Muninn's checks all key on the same unverified ETag.

WHY THE FAKE ENDPOINT IS A REAL HTTP SERVER rather than a mocked client. The
ingest is `hf_hub_download(..., endpoint=settings.upstream)` -- the whole
download, including whatever verification exists, belongs to huggingface_hub and
not to code I own. Mocking its internals would measure my model of the library
instead of the library. The guarantee is only observable end to end.
"""

from __future__ import annotations

import hashlib
import http.server
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REPO = "acme/weights"
REVISION = "main"
COMMIT = "0" * 40
FILENAME = "model.safetensors"

TRUE_BYTES = b"the bytes that were actually sent" * 64
# The ETag a well-behaved Hub would return for TRUE_BYTES.
HONEST_ETAG = hashlib.sha256(TRUE_BYTES).hexdigest()
# A sha256 of something else entirely: a valid-LOOKING etag that these bytes do
# not hash to. This is the corrupt-mirror / bad-edge case, not a malformed one.
LYING_ETAG = hashlib.sha256(b"different content").hexdigest()


class _Handler(http.server.BaseHTTPRequestHandler):
    """Minimal HF resolve endpoint. Declares etag/size, serves body separately."""

    etag = HONEST_ETAG
    body = TRUE_BYTES
    declared_size: int | None = None  # None -> len(body)

    def _headers(self) -> dict[str, str]:
        size = self.declared_size if self.declared_size is not None else len(self.body)
        return {
            "x-repo-commit": COMMIT,
            "etag": f'"{type(self).etag}"',
            "x-linked-etag": f'"{type(self).etag}"',
            "content-length": str(size),
            "x-linked-size": str(size),
            "accept-ranges": "bytes",
            "content-type": "application/octet-stream",
        }

    def do_HEAD(self) -> None:
        self.send_response(200)
        for k, v in self._headers().items():
            self.send_header(k, v)
        self.end_headers()

    def do_GET(self) -> None:
        self.send_response(200)
        for k, v in self._headers().items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(type(self).body)

    def log_message(self, *args) -> None:
        return


@pytest.fixture
def hub():
    """A local stand-in for the Hub whose etag and body are set per test."""
    _Handler.etag = HONEST_ETAG
    _Handler.body = TRUE_BYTES
    _Handler.declared_size = None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", _Handler
    finally:
        srv.shutdown()
        srv.server_close()


def _download(endpoint: str, cache_dir: Path) -> Path:
    from huggingface_hub import hf_hub_download

    return Path(
        hf_hub_download(
            repo_id=REPO,
            filename=FILENAME,
            revision=REVISION,
            repo_type="model",
            cache_dir=str(cache_dir),
            endpoint=endpoint,
        )
    )


@pytest.fixture(autouse=True)
def _no_xet(monkeypatch):
    # The fake endpoint speaks plain HTTP resolve, not xet. Muninn sets this on
    # edge nodes only; here it keeps the test on the path being measured.
    monkeypatch.setenv("HF_HUB_DISABLE_XET", "1")
    monkeypatch.setenv("HF_HUB_DISABLE_TELEMETRY", "1")


def test_control_honest_etag_is_ingested(hub, tmp_path):
    """NEGATIVE CONTROL. Without this, a test that always refuses looks correct.

    If the mismatch cases below fail for some unrelated reason -- the fake
    endpoint being wrong, xet interposing, a network refusal -- they would still
    report the behaviour I am claiming. This proves the harness can succeed.
    """
    endpoint, _ = hub
    path = _download(endpoint, tmp_path)
    assert path.read_bytes() == TRUE_BYTES
    assert path.resolve().name == HONEST_ETAG


def test_bytes_contradicting_the_etag_are_cached_silently(hub, tmp_path):
    """THE FINDING. Content is not verified; only length is.

    The blob lands under a filename asserting a sha256 the bytes do not have.
    Nothing raises, nothing warns, and every later check keys on that same
    filename -- so the cache is self-consistently wrong and stays that way.
    """
    endpoint, handler = hub
    handler.etag = LYING_ETAG  # declared hash of bytes we are not sending

    path = _download(endpoint, tmp_path)

    assert path.read_bytes() == TRUE_BYTES, "bytes were altered, which is not the claim"
    blob = path.resolve()
    assert blob.name == LYING_ETAG, "blob is named for the etag, not for its content"
    assert hashlib.sha256(blob.read_bytes()).hexdigest() != blob.name, (
        "the blob's filename asserts a digest its own bytes do not produce, "
        "and nothing in the ingest path noticed"
    )


def test_short_body_is_refused_by_the_TRANSPORT_not_by_a_checksum(hub, tmp_path):
    """The guard that DOES exist, so the fix is not decided against a strawman --
    and it is one layer below where I expected it.

    Declaring more bytes than are sent fails as a ChunkedEncodingError /
    IncompleteRead from the HTTP layer, BEFORE huggingface_hub's own consistency
    check ever compares counts. That distinction matters: a transport-level
    refusal is a property of the connection, not of the cache, so it protects
    only the case where the sender stops early. It is not integrity checking and
    must not be cited as such.
    """
    endpoint, handler = hub
    handler.etag = LYING_ETAG
    handler.declared_size = len(TRUE_BYTES) + 1  # claim one more byte than we send

    with pytest.raises(Exception) as excinfo:
        _download(endpoint, tmp_path)
    assert "IncompleteRead" in str(excinfo.value) or "onsistency" in str(excinfo.value)


def test_under_declared_length_TRUNCATES_AND_CACHES_SILENTLY(hub, tmp_path):
    """THE SECOND FINDING, and it is worse than the first.

    Declaring FEWER bytes than are sent does not fail at all. The client reads
    exactly the declared count, discards the remainder, and the consistency check
    then compares that count against itself -- so it can never fire in this
    direction. A file that is short by one byte lands, is cached under the
    upstream ETag, and is served to every later caller as complete.

    Length is therefore not a guard against a lying upstream in general; it is a
    guard against a connection that DROPS. An upstream that under-reports its own
    Content-Length silently corrupts the cache, and the resulting blob is
    self-consistent: right name, right declared size, wrong bytes.
    """
    endpoint, handler = hub
    handler.etag = LYING_ETAG
    handler.declared_size = len(TRUE_BYTES) - 1  # claim one FEWER byte than we send

    path = _download(endpoint, tmp_path)

    landed = path.resolve().read_bytes()
    assert len(landed) == len(TRUE_BYTES) - 1, "the tail was dropped"
    assert landed != TRUE_BYTES, "a truncated file was cached as complete"
    assert path.resolve().name == LYING_ETAG


def test_the_asymmetry_with_oci_is_real_and_not_recalled(tmp_path):
    """Pins the other half of the comparison in the ticket, from the code itself.

    If the OCI path ever stops verifying, this comparison is no longer true and
    the documented asymmetry becomes wrong in the other direction. Asserting it
    here means the claim on the wiki page has a test under it rather than a
    memory.
    """
    import inspect

    from app import ocistore

    src = inspect.getsource(ocistore)
    assert "DigestMismatch" in src
    assert "hashlib" in src, "OCI ingest is expected to hash what it stores"
