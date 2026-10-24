"""Shared fixtures: tmp workspace, FakeRunner, AppContext with no real backends."""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from psrmcp._context import build_context
from psrmcp.config import load_config
from psrmcp.execution.runner import RunResult
from psrmcp.tools.registry import ToolRegistry
from psrmcp.server import build_registry


@pytest.fixture(autouse=True)
def _no_real_backends(monkeypatch):
    """Unit tests are hermetic: strip pulsar-stack dirs from PATH so backend
    probes and tool runners can never reach a real installation (the dev
    shell often has ~/Pulsar/bin exported). Contract tests opt back in
    explicitly by construction."""
    clean = os.pathsep.join(
        p for p in os.environ.get("PATH", "").split(os.pathsep)
        if p and not re.search(r"pulsar", p, re.IGNORECASE)
    )
    monkeypatch.setenv("PATH", clean)


@dataclass
class FakeRunner:
    """Records argv; returns canned results per binary basename."""

    responses: dict = field(default_factory=dict)  # "binary" -> (exit, stdout, stderr)
    calls: list = field(default_factory=list)

    async def run(self, argv, *, cwd=None, env=None, timeout_s=None, cancel=None, stdin_bytes=None):

        self.calls.append(list(argv))
        key = Path(argv[0]).name
        exit_code, out, err = self.responses.get(key, (0, "", ""))
        if isinstance(out, Exception):
            raise out
        return RunResult(
            argv=list(argv), exit_code=exit_code,
            stdout=out.encode() if isinstance(out, str) else out,
            stderr=err.encode() if isinstance(err, str) else err,
            duration_s=0.01, timed_out=False,
        )

    def stderr_tail(self, n: int = 800) -> str:  # pragma: no cover - interface shim
        return ""


@pytest.fixture
def tmp_cfg(tmp_path):
    ws = tmp_path / "ws"
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    ws.mkdir(exist_ok=True)
    toml = tmp_path / "cfg.toml"
    toml.write_text(
        f'[server]\nworkspace = "{ws}"\njob_timeout_s = 60\n'
        f'[data]\nread_roots = ["{data}"]\nallow_absolute_paths = false\n',
        encoding="utf-8",
    )
    return load_config(toml)


@pytest.fixture
def ctx(tmp_cfg):
    c = build_context(tmp_cfg)
    c.backends._cache = {}  # force fresh probes (none available in test PATH)
    return c


@pytest.fixture
def registry(ctx) -> ToolRegistry:
    return build_registry(ctx)


@pytest.fixture
def run_in_loop():
    """Run one coroutine on a fresh loop (for sync-style unit tests)."""

    def _run(coro):
        return asyncio.run(coro)

    return _run
