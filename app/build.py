"""What source is this process running?

an internal issue. A container cannot know its own image digest: the digest is computed
over the image that contains the answer, so baking it in is circular. The
runtime knows (`docker inspect --format '{{.Image}}'`), the process does not.

So report something the process CAN know and that cannot drift from what is
running: a fingerprint over the source actually loaded. It answers "what source
is this", which is the question two agents spent several exchanges guessing at.

It deliberately does NOT claim provenance. A fingerprint tells you which source
is running; mapping that back to a published build is a separate step nobody has
established, and reporting it as if it did would be the adjacent measurement
this ticket exists to avoid.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

_cached: str | None = None


def source_fingerprint() -> str:
    """SHA-256 over the contents of every .py in this package, path-ordered.

    Computed from the files on disk rather than from a build-time constant, so
    it cannot claim to be something the process is not running.
    """
    global _cached  # noqa: PLW0603 - module-level cache
    if _cached is not None:
        return _cached
    root = Path(__file__).resolve().parent
    h = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        h.update(str(path.relative_to(root)).encode())
        try:
            h.update(path.read_bytes())
        except OSError:
            h.update(b"<unreadable>")
    _cached = h.hexdigest()
    return _cached


def info() -> dict:
    """Provenance the process can honestly report."""
    return {
        "source_fingerprint": source_fingerprint(),
        # Set by the image build if it knows; absent rather than guessed.
        "image_ref": os.environ.get("XHC_IMAGE_REF") or None,
        "note": (
            "source_fingerprint identifies the SOURCE running, not the image. "
            "A container cannot know its own image digest; ask the runtime "
            "(docker inspect --format '{{.Image}}')."
        ),
    }
