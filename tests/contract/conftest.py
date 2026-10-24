"""Contract tests opt back into the real stack PATH (root conftest strips it)."""

from __future__ import annotations

import os

import pytest

_ORIG_PATH = os.environ.get("PATH", "")


@pytest.fixture(autouse=True)
def _real_stack_path(_no_real_backends, monkeypatch):
    # Depends on the root autouse fixture so the strip runs first, then we
    # restore the developer's real PATH (~/Pulsar/bin etc).
    monkeypatch.setenv("PATH", _ORIG_PATH)
