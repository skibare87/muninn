"""What a Hugging Face ETag says about the bytes it names. THE ONE COPY.

The Hub gives an LFS (or Xet-backed) file its sha256 as the ETag, and every
other file its git blob id: sha1(b"blob <size>\\0" + content). huggingface_hub
uses the same two shapes to decide whether an ETag is a content hash
(file_download.REGEX_SHA256). Measured against the Hub on real repos before
anything here relied on it.

Both the ingest verifier (jobs.verify_ingested) and the object-store tier
(reads, write-back, restore) build their hash object here, so the rule cannot
drift between them. Each of them feeds it in its own single pass.
"""

from __future__ import annotations

import hashlib
import re

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")


def etag_kind(etag: str | None) -> str | None:
    """"sha256", "gitsha1", or None for an ETag of neither shape."""
    if not etag:
        return None
    if SHA256_RE.match(etag):
        return "sha256"
    if GIT_SHA1_RE.match(etag):
        return "gitsha1"
    return None


def hasher_for(etag: str, size: int | None):
    """A fresh hash object whose hexdigest equals `etag` exactly when the bytes
    fed to it are the content that ETag names. None for an ETag of neither shape.

    A git blob id covers the content THROUGH a header naming its length, so
    `size` is required for one, and it is the size the caller expects: bytes of
    any other length cannot produce the id, whatever they are. hashlib is looked
    up at call time, so a test counting hash passes sees these too.
    """
    kind = etag_kind(etag)
    if kind == "sha256":
        return hashlib.sha256()
    if kind == "gitsha1":
        if size is None:
            raise ValueError("a git blob id cannot be checked without the content's size")
        h = hashlib.sha1(usedforsecurity=False)
        h.update(b"blob %d\0" % size)
        return h
    return None
