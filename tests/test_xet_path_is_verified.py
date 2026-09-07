"""The verification covers the Xet transport too, measured against the real Hub.

WHY THIS FILE EXISTS AS A CORRECTION. When byte verification shipped, its README
section, its release tag and its decision record all carried the sentence "the
Xet download path is not covered by this check and has not been measured here."
The second half was true. THE FIRST HALF WAS WRONG, and it was wrong in the
direction that understated the guarantee.

The check runs on the file AFTER hf_hub_download returns, so it hashes whatever
landed regardless of which transport delivered it. Measured against the real Hub:
the download takes the Xet path (xet_get called, http_get not), the blob's
filename is the sha256 of the bytes, and verify_ingested returns VERIFIED.

I asserted a limitation of my own code without running it. A limitation is a
negative claim, and negative claims are the ones I take for free -- so a
one-command check went unrun and the wrong sentence reached three documents.

WHAT REMAINS UNMEASURED, stated precisely so it is not overstated again: whether
hf_xet independently detects a corrupt chunk during reconstruction. That question
is no longer load-bearing for the guarantee, because a corrupt reconstruction
fails the post-ingest hash whatever hf_xet did or did not notice. It is
defence-in-depth, not coverage.

Network test. Skipped when the Hub is unreachable, because a test that silently
passes offline would restore exactly the unverified claim it exists to prevent.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REPO = "hf-internal-testing/tiny-random-gpt2"
FILENAME = "pytorch_model.bin"


def _hub_reachable() -> bool:
    try:
        import httpx

        return httpx.head(
            f"https://huggingface.co/api/models/{REPO}", timeout=8, follow_redirects=True
        ).status_code == 200
    except Exception:  # noqa: BLE001 - any failure means "not available"
        return False


pytestmark = pytest.mark.skipif(
    not _hub_reachable(), reason="huggingface.co unreachable -- see this module's docstring"
)


def test_the_xet_transport_is_taken_and_the_result_is_verified(monkeypatch):
    """One test, because the two halves are only meaningful together.

    Asserting "verify_ingested returns VERIFIED" alone would pass just as well
    on the plain HTTP path, which is already covered elsewhere -- so it would
    prove nothing about Xet. The transport assertion is what makes the second
    assertion mean what it claims.
    """
    from huggingface_hub import file_download, hf_hub_download

    from app import jobs, metrics

    monkeypatch.setenv("HF_XET_CACHE", tempfile.mkdtemp(prefix="xetcache-"))

    took = {"xet": 0, "http": 0}
    real_xet, real_http = file_download.xet_get, file_download.http_get

    def counted_xet(*a, **k):
        took["xet"] += 1
        return real_xet(*a, **k)

    def counted_http(*a, **k):
        took["http"] += 1
        return real_http(*a, **k)

    monkeypatch.setattr(file_download, "xet_get", counted_xet)
    monkeypatch.setattr(file_download, "http_get", counted_http)

    cache = tempfile.mkdtemp(prefix="hfcache-")
    path = Path(hf_hub_download(repo_id=REPO, filename=FILENAME, cache_dir=cache))

    if took["xet"] == 0:
        pytest.skip(
            "the Hub did not serve this file over Xet on this run; the claim under "
            "test is about the Xet path and cannot be checked from a plain download"
        )
    assert took["http"] == 0, "mixed transports would make the result ambiguous"

    blob = path.resolve()
    assert re.match(r"^[0-9a-f]{64}$", blob.name), "expected an LFS sha256 ETag"
    assert hashlib.sha256(blob.read_bytes()).hexdigest() == blob.name

    metrics.reset()
    assert jobs.verify_ingested(path) == "VERIFIED"
    assert metrics.snapshot()["ingest_verify"]["VERIFIED"] == 1


def test_a_corrupted_xet_result_is_refused(monkeypatch):
    """The negative control, and the one that makes the test above mean anything.

    Without it, "VERIFIED on the Xet path" is consistent with a verifier that
    returns VERIFIED for everything. Corrupting the landed file in place is the
    honest stand-in for a bad reconstruction: the verifier cannot tell how the
    bytes got there, which is precisely why it covers both transports.
    """
    from huggingface_hub import hf_hub_download

    from app import jobs, metrics

    monkeypatch.setenv("HF_XET_CACHE", tempfile.mkdtemp(prefix="xetcache-"))
    cache = tempfile.mkdtemp(prefix="hfcache-")
    path = Path(hf_hub_download(repo_id=REPO, filename=FILENAME, cache_dir=cache))

    blob = path.resolve()
    data = bytearray(blob.read_bytes())
    data[0] ^= 0xFF  # one bit-flip; same length, so no length check can see it
    os.chmod(blob, 0o644)
    blob.write_bytes(bytes(data))

    metrics.reset()
    with pytest.raises(jobs.IngestDigestMismatch):
        jobs.verify_ingested(path)
    assert metrics.snapshot()["ingest_verify"]["MISMATCH"] == 1
    assert not blob.exists(), "a mismatched blob must not survive on disk"
