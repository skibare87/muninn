"""Process-wide test setup that MUST run before huggingface_hub is imported.

WHY THIS FILE EXISTS. Several huggingface_hub settings are read ONCE, at module
import, into `constants`:

    HF_HUB_DISABLE_XET = _is_true(os.environ.get("HF_HUB_DISABLE_XET"))
    HF_XET_CACHE       = os.getenv("HF_XET_CACHE", default_xet_cache_path)

So `monkeypatch.setenv(...)` inside a test is INERT if the library is already
imported, and PERMANENT for the whole process if it happens to run before the
first import. Both failure modes bit this suite in one session:

  - a fixture setting HF_HUB_DISABLE_XET appeared to work, because the library
    was imported lazily inside a test. It disabled xet for every later test in
    the process, including the module whose entire subject is the xet path --
    which then skipped, reporting that the HUB had not served xet. A test
    blaming a third party for local contamination.

  - a fixture setting HF_XET_CACHE to a temp directory did nothing at all, so
    tests that believed they were isolated were writing to the shared chunk
    cache in a HOME that many sessions share.

pytest imports conftest before test modules, which is the only place the
assignment is reliable. THE GENERAL RULE, and it is not specific to this
library: a setting read at import time cannot be monkeypatched, and a fixture
that pretends otherwise fails in whichever direction the import order happens to
produce -- silently, and differently depending on which tests you run.
"""

from __future__ import annotations

import os
import tempfile

# A chunk cache this suite owns, so a test run never writes into the shared one.
os.environ.setdefault("HF_XET_CACHE", tempfile.mkdtemp(prefix="muninn-tests-xet-"))
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

# Deliberately NOT setting HF_HUB_DISABLE_XET. Tests that need the plain path
# get it by pointing at an endpoint that serves no xet metadata, which exercises
# the real decision instead of forcing it.
