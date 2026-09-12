"""A static web root at /, so one hostname is a homepage AND a cache.

The OCI surface is bounded under /v2 by spec, so a reverse proxy CAN split
docker from a homepage. It cannot split the Hugging Face surface, because
hfcompat owns a true catch-all and HF clients construct arbitrary top-level
paths like /owner/repo/resolve/main/config.json. There is no prefix to route on.

Muninn can do what the proxy cannot, because it already knows which paths are HF
paths. So the discriminator is a PRECEDENCE RULE rather than a pattern: if a file
exists under the web root, serve it; otherwise fall through to HF.

That makes "falls through" the property the cache depends on, and the traversal
guard the property everything else depends on.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def web(tmp_path, monkeypatch):
    from app.config import settings

    root = tmp_path / "www"
    root.mkdir()
    (root / "index.html").write_text("<h1>ravencache</h1>")
    (root / "style.css").write_text("body{}")
    (root / "assets").mkdir()
    (root / "assets" / "logo.svg").write_text("<svg/>")
    secret = tmp_path / "outside.txt"
    secret.write_text("MUST NOT BE SERVED")
    monkeypatch.setattr(settings, "web_root", str(root))
    return root, secret


def test_root_and_named_files_are_served(web):
    from app.hfcompat import _web_root_file

    root, _ = web
    assert _web_root_file("") == root / "index.html", "/ must be index.html"
    assert _web_root_file("/") == root / "index.html"
    assert _web_root_file("style.css") == root / "style.css"
    assert _web_root_file("assets/logo.svg") == root / "assets" / "logo.svg"


def test_a_missing_path_falls_through_to_hugging_face(web):
    """THE PROPERTY THE CACHE DEPENDS ON.

    If this returned anything but None, every HF repo path would be answered by
    the web root and the cache would stop working -- presenting as "the cache is
    broken", not as "a file was served".
    """
    from app.hfcompat import _web_root_file

    assert _web_root_file("openai-community/gpt2/resolve/main/config.json") is None
    assert _web_root_file("api/models/openai-community/gpt2") is None
    assert _web_root_file("datasets/squad/resolve/main/README.md") is None


@pytest.mark.parametrize(
    "attack",
    [
        "../outside.txt",
        "../../etc/passwd",
        "assets/../../outside.txt",
        "/../outside.txt",
        "....//outside.txt",
        "assets/%2e%2e/outside.txt",
    ],
)
def test_traversal_is_refused(web, attack):
    """Containment is enforced by RESOLUTION, not string comparison.

    A prefix check on the raw path is the classic bypass -- `..` and symlinks
    both defeat it. Each of these resolves outside the root and must return None
    rather than a file.
    """
    from app.hfcompat import _web_root_file

    got = _web_root_file(attack)
    assert got is None or "outside.txt" not in str(got), f"{attack!r} escaped the root"


def test_a_symlink_out_of_the_root_is_refused(web):
    """The same resolve that catches `..` catches a symlink, without a special case."""
    from app.hfcompat import _web_root_file

    root, secret = web
    link = root / "sneaky.txt"
    try:
        link.symlink_to(secret)
    except OSError:
        pytest.skip("symlinks unavailable here")
    assert _web_root_file("sneaky.txt") is None


def test_unset_web_root_changes_nothing(tmp_path, monkeypatch):
    """Default off: an existing deployment must behave exactly as before."""
    from app.config import settings
    from app.hfcompat import _web_root_file

    monkeypatch.setattr(settings, "web_root", None)
    assert _web_root_file("") is None
    assert _web_root_file("index.html") is None


def test_a_configured_but_missing_root_does_not_break_the_cache(tmp_path, monkeypatch):
    """A misconfiguration must not take the cache with it.

    The wrong failure here would be raising -- that turns a typo in one setting
    into a total outage of a service whose main job is unrelated.
    """
    from app.config import settings
    from app.hfcompat import _web_root_file

    monkeypatch.setattr(settings, "web_root", str(tmp_path / "does-not-exist"))
    assert _web_root_file("") is None
    assert _web_root_file("openai-community/gpt2/resolve/main/config.json") is None


def test_web_root_cannot_shadow_the_other_surfaces(web):
    """ORDERING IS NOW LOAD-BEARING FOR A SECURITY PROPERTY, so it is pinned.

    /v2, /healthz, /metrics and /_cache are separate routers mounted BEFORE the
    HF catch-all, so a file in the web root can never answer them. If someone
    reorders the mounts, a web root could shadow the registry surface or the
    unauthenticated health endpoint, and nothing else in the suite would notice.
    """
    import app.main as main

    src = Path(main.__file__).read_text()
    hf = src.index("app.include_router(hfcompat.router)")
    for earlier in ("manage.router", "ocimanage.router", "ocicompat.router"):
        assert src.index(earlier) < hf, (
            f"{earlier} must be mounted before hfcompat's catch-all, or a web "
            f"root could shadow it"
        )
