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


# --------------------------------------------------------------------------
# What MUNINN does, now that the library's behaviour above is known.
#
# The cases above characterise huggingface_hub and stay true whatever this
# service does. These drive the real ingest entry point instead, so they fail
# if the verification is removed, disabled by accident, or moved somewhere it
# no longer runs.
# --------------------------------------------------------------------------


@pytest.fixture
def ingest(hub, tmp_path, monkeypatch):
    """The real _download_file, pointed at the fake Hub."""
    from app import jobs, metrics
    from app.config import settings

    endpoint, handler = hub
    monkeypatch.setattr(settings, "upstream", endpoint)
    monkeypatch.setattr(settings, "cache_dir", str(tmp_path))
    monkeypatch.setattr(settings, "hf_token", None)
    monkeypatch.setattr(settings, "hf_verify_ingest", True)
    metrics.reset()

    mgr = jobs.JobManager()
    job = jobs.Job(
        id="test",
        kind="file",
        repo_type="model",
        repo_id=REPO,
        revision=REVISION,
        filename=FILENAME,
    )
    return mgr, job, handler, metrics


def test_muninn_refuses_bytes_that_contradict_the_etag(ingest, tmp_path):
    """The fix. A mismatch is refused and the bytes do not survive on disk.

    The OCI path has always done this (ocistore.DigestMismatch). This is the
    same refusal on the other protocol, which is the asymmetry that was the
    whole ticket.
    """
    from app import jobs

    mgr, job, handler, metrics = ingest
    handler.etag = LYING_ETAG

    with pytest.raises(jobs.IngestDigestMismatch) as excinfo:
        mgr._download_file(job)

    assert LYING_ETAG in str(excinfo.value), "the refusal names the digest it expected"
    blob = tmp_path / f"models--{REPO.replace('/', '--')}" / "blobs" / LYING_ETAG
    assert not blob.exists(), (
        "a blob whose NAME asserts a digest its bytes do not have must not survive: "
        "every later check in this service keys on that name"
    )
    assert metrics.snapshot()["ingest_verify"]["MISMATCH"] == 1


def test_muninn_accepts_an_honest_file_and_records_it_verified(ingest):
    """NEGATIVE CONTROL for the refusal. Without this, a check that refuses
    everything would look identical to a correct one."""
    mgr, job, handler, metrics = ingest
    path = mgr._download_file(job)
    assert path.read_bytes() == TRUE_BYTES
    snap = metrics.snapshot()["ingest_verify"]
    assert snap["VERIFIED"] == 1
    assert snap["MISMATCH"] == 0


def test_a_non_sha256_etag_is_UNVERIFIABLE_and_not_counted_as_verified(ingest):
    """An ETag that is a git object id rather than a content hash cannot be
    checked. That is normal and it must not render the same as a pass -- an
    unverifiable file passed off as verified is the fail-open this project
    keeps confessing.
    """
    mgr, job, handler, metrics = ingest
    handler.etag = "a" * 40  # a git object id, not a sha256
    handler.body = TRUE_BYTES

    path = mgr._download_file(job)  # accepted: nothing to check against

    assert path.read_bytes() == TRUE_BYTES
    snap = metrics.snapshot()["ingest_verify"]
    assert snap["UNVERIFIABLE"] == 1
    assert snap["VERIFIED"] == 0, "unverifiable must never be counted as verified"


def test_truncation_by_under_declared_length_is_now_caught_too(ingest, tmp_path):
    """Finding 2 from the measurement above, closed by the same mechanism.

    An under-declared Content-Length silently truncates the file. Length checks
    cannot see it, because the client compares the declared count against
    itself. The hash can, because truncated bytes do not hash to the ETag.
    """
    from app import jobs

    mgr, job, handler, metrics = ingest
    handler.etag = HONEST_ETAG  # honest about the hash
    handler.declared_size = len(TRUE_BYTES) - 1  # lying about the length

    with pytest.raises(jobs.IngestDigestMismatch):
        mgr._download_file(job)
    assert metrics.snapshot()["ingest_verify"]["MISMATCH"] == 1


def test_the_knob_is_what_makes_the_difference(ingest):
    """Turning verification off restores the old behaviour exactly.

    This is what proves the refusal above comes from this check and not from
    something incidental in the harness.
    """
    from app.config import settings

    mgr, job, handler, metrics = ingest
    handler.etag = LYING_ETAG
    settings.hf_verify_ingest = False

    path = mgr._download_file(job)  # no refusal

    assert path.resolve().name == LYING_ETAG
    assert metrics.snapshot()["ingest_verify"]["MISMATCH"] == 0


# --------------------------------------------------------------------------
# Snapshot ingest. A prewarm pulls many files and snapshot_download offers no
# per-file hook, so verification runs over the landed tree.
# --------------------------------------------------------------------------


def _plant(root, name, content, etag):
    """Build the HF on-disk shape: snapshots/<commit>/<name> -> blobs/<etag>."""
    repo = root / f"models--{REPO.replace('/', '--')}"
    blobs = repo / "blobs"
    snap = repo / "snapshots" / COMMIT
    blobs.mkdir(parents=True, exist_ok=True)
    snap.mkdir(parents=True, exist_ok=True)
    blob = blobs / etag
    blob.write_bytes(content)
    link = snap / name
    link.symlink_to(blob)
    return snap, link, blob


def test_verify_tree_hashes_each_blob_once_and_reports_all_three_outcomes(tmp_path):
    """One honest file, one unverifiable, one corrupt, plus a second reference
    to the honest blob to prove inode dedup.

    Hashing a blob once per snapshot ENTRY rather than once per blob would be
    the same work repeated on a repo whose files share content -- and the HF
    layout makes that the normal case, not an edge one.
    """
    from app import jobs, metrics

    root = tmp_path
    snap, _, _ = _plant(root, "good.bin", TRUE_BYTES, HONEST_ETAG)
    _plant(root, "gitfile.txt", b"small config", "b" * 40)  # git oid: unverifiable
    _plant(root, "bad.bin", b"x" * len(TRUE_BYTES), LYING_ETAG)  # wrong content
    (snap / "alias.bin").symlink_to(snap / "good.bin")  # second ref, same inode

    metrics.reset()
    verified, unverifiable, mismatches = jobs.verify_tree(snap, since=0)

    assert verified == 1, "the duplicate reference must not be hashed twice"
    assert unverifiable == 1
    assert len(mismatches) == 1
    assert "bad" in mismatches[0] or LYING_ETAG in mismatches[0]
    snapshot = metrics.snapshot()["ingest_verify"]
    assert snapshot["VERIFIED"] == 1
    assert snapshot["UNVERIFIABLE"] == 1
    assert snapshot["MISMATCH"] == 1


def test_verify_tree_skips_blobs_that_were_not_fetched_this_run(tmp_path):
    """A repeat prewarm must not re-hash the half it already had.

    This is what keeps the check an INGEST check rather than a scrub. Losing it
    would make every prewarm pay for the whole repo, which is the cost objection
    that was wrongly assumed about the single-file path.
    """
    import os
    import time

    from app import jobs, metrics

    snap, link, blob = _plant(tmp_path, "old.bin", TRUE_BYTES, HONEST_ETAG)
    old = time.time() - 3600
    os.utime(blob, (old, old))

    metrics.reset()
    verified, unverifiable, mismatches = jobs.verify_tree(snap, since=time.time() - 60)

    assert (verified, unverifiable, mismatches) == (0, 0, [])
    assert metrics.snapshot()["ingest_verify"]["VERIFIED"] == 0
