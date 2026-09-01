#!/usr/bin/env python3
"""Render CHANGELOG.md from the annotated git tags.

THE TAG IS THE SOURCE, THIS FILE IS A RENDERING. An annotated tag is created
atomically with the release and cannot drift from it; a hand-written changelog is
a second copy of the same fact and drifts on the schedule everything else here
has. In this repo alone: the README pinned an image two releases stale while
telling readers to pin a version, the Confluence release history sat four
releases behind, and GitHub Releases stopped five back.

So: write the reasoning into `git tag -a` at release time, then run this. If the
two ever disagree, the tag wins and this file is regenerated -- never patched.

    scripts/gen_changelog.py > CHANGELOG.md
    scripts/gen_changelog.py --check    # non-zero if CHANGELOG.md is stale
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

HEADER = """# Changelog

**Generated from the annotated git tags — do not edit by hand.**

The tag is the source of truth: it is written at release time and cannot drift
from the commit it names. Regenerate with `scripts/gen_changelog.py > CHANGELOG.md`.

Images are published to `ghcr.io/skibare87/muninn`. Only the full `X.Y.Z` tag is
immutable; `X.Y`, `latest` and `edge` all move.
"""


def tags() -> list[str]:
    out = subprocess.run(
        ["git", "tag", "-l", "v*", "--sort=-version:refname"],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    return out


def annotation(tag: str) -> tuple[str, str]:
    """(date, body). Lightweight tags have no body and are reported as such."""
    date = subprocess.run(
        ["git", "log", "-1", "--format=%ad", "--date=short", tag],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    body = subprocess.run(
        ["git", "tag", "-l", "--format=%(contents)", tag],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    # Drop a leading repetition of the version -- `git tag -a v0.6.0 -m "v0.6.0\n\n..."`
    # is a natural way to write one and renders as a duplicated heading here.
    # Matches with or without the leading v.
    body = re.sub(rf"^v?{re.escape(tag.lstrip('v'))}\s*\n+", "", body)
    body = body.split("-----BEGIN PGP SIGNATURE-----")[0].strip()
    return date, body


def render() -> str:
    parts = [HEADER]
    for tag in tags():
        date, body = annotation(tag)
        parts.append(f"\n## {tag} — {date}\n")
        if body:
            parts.append(body + "\n")
        else:
            # An unannotated tag is a gap in the record, and saying so is more
            # useful than rendering an empty section that looks intentional.
            parts.append("_No annotation on this tag; see `git log` for the range._\n")
    return "\n".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="exit non-zero if CHANGELOG.md differs from the tags")
    a = ap.parse_args()
    text = render()
    if a.check:
        path = Path(__file__).resolve().parent.parent / "CHANGELOG.md"
        current = path.read_text() if path.exists() else ""
        if current != text:
            print("CHANGELOG.md is stale -- regenerate it:", file=sys.stderr)
            print("  scripts/gen_changelog.py > CHANGELOG.md", file=sys.stderr)
            return 1
        print("CHANGELOG.md matches the tags")
        return 0
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
