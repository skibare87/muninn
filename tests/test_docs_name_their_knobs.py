"""Every setting the code reads is named in the README (an internal issue follow-up).

a colleague was told to enable store-and-forward, could not do it from the
documentation, and was about to reverse-engineer the variable names out of the
running container. the maintainer: "that's a failure in its documentation".

THE SHAPE, in a colleague's words: documentation that describes BEHAVIOUR without
naming the KNOB reads as complete to its author, because the author already
knows the knob. It surfaced twice on the same page, and both times only when
someone tried to act on it -- which is far too late, and by then the reader's
cheapest next move is to guess from the source.

Proofreading does not catch this: the prose is correct, it is just unusable.
A mechanical check does.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _knobs() -> set[str]:
    """Every XHC_ variable config.py actually reads from the environment."""
    return set(re.findall(r'"(XHC_[A-Z0-9_]+)"', (ROOT / "app" / "config.py").read_text()))


def test_every_environment_variable_is_named_in_the_readme():
    readme = (ROOT / "README.md").read_text()
    missing = sorted(k for k in _knobs() if k not in readme)
    assert not missing, (
        "settings the code reads but the README never names, so a reader can "
        f"only find them by reading the source: {missing}"
    )


def test_enumerated_settings_document_their_accepted_values():
    """A name alone is not enough for a setting with a fixed vocabulary.

    `XHC_DOCKER_PUSH_MODE` named without `store-forward` beside it is exactly
    the gap a colleague hit: they knew a mode existed and could not spell it.
    """
    readme = (ROOT / "README.md").read_text()
    for knob, values in {
        "XHC_DOCKER_PUSH_MODE": ["proxy", "store-forward"],
        "XHC_DOCKER_AUTH": ["none", "basic"],
        "XHC_DOCKER_POLICY": ["open", "allowlist"],
        "XHC_ORPHAN_POLICY": ["retain", "evict"],
    }.items():
        assert knob in readme, f"{knob} is not in the README at all"
        for v in values:
            assert v in readme, f"{knob} is named but its value {v!r} is not"
