"""A gap in a counter timeline is not a zero, and they render identically.

An operator tried to answer "has the fleet been hitting the upstream-auth path?"
over seven days and could not. `up{job="muninn"}` had 77 continuous hourly
points and the disk-derived gauge had 77; `requests{result="HIT"}` had TWO, and
`requests{result="UPSTREAM_AUTH"}` had one.

Scraping had been continuous the whole time. The SERIES had not. A
collections.Counter materialises a key on first increment, so between a restart
and the first request of a given kind, that series is absent from the exposition
-- and absent renders exactly like zero to anyone reading a graph, while
breaking `increase()` in a way zero would not.

Prometheus handles a counter RESET. It cannot handle a series that is not there.
These tests pin the distinction.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import metrics


def test_every_declared_series_exists_before_anything_happens():
    """The whole point: a fresh process must already expose them at zero."""
    body = metrics.render({})
    for result, kind in metrics._DOCKER_SERIES:
        needle = f'result="{result}",kind="{kind}"'
        assert needle in body, f"{needle} absent from a fresh exposition"


def test_seeding_never_overwrites_a_live_value():
    """_seed() is called at import and must stay idempotent -- re-running it
    after real traffic must not zero the counts it finds."""
    metrics.record_docker("HIT", "manifest")
    before = metrics._docker["HIT|manifest"]
    assert before > 0
    metrics._seed()
    assert metrics._docker["HIT|manifest"] == before


def test_declared_series_cover_every_call_site():
    """A new result that nobody adds to _DOCKER_SERIES silently reintroduces
    the gap for that label. Read the call sites rather than trusting the list.
    """
    import re

    declared = {f"{r}|{k}" for r, k in metrics._DOCKER_SERIES}
    app_dir = Path(__file__).resolve().parent.parent / "app"
    literal = re.compile(r'record_docker\(\s*"([A-Z_]+)"\s*,\s*"([a-z]+)"\s*\)')
    found = set()
    for src in app_dir.glob("*.py"):
        for result, kind in literal.findall(src.read_text()):
            found.add(f"{result}|{kind}")
    missing = found - declared
    assert not missing, (
        f"call sites emit {sorted(missing)}, which _DOCKER_SERIES does not seed, "
        "so those series will be absent until the first occurrence after a restart"
    )
