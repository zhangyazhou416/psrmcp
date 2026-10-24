"""CLI: --init-config writes a loadable template; server module wiring builds."""

from __future__ import annotations

import subprocess
import sys


def test_init_config_writes_loadable_template(tmp_path):
    target = tmp_path / "sub" / "config.toml"
    rc = subprocess.run(
        [sys.executable, "-m", "psrmcp.server", "--init-config", str(target)],
        capture_output=True, text=True, timeout=60,
    )
    assert rc.returncode == 0, rc.stderr
    assert target.exists() and "[backends]" in target.read_text(encoding="utf-8")

    from psrmcp.config import load_config

    cfg = load_config(target)  # template must load without E_CONFIG
    assert cfg.server.workspace is not None

    # Refuse to overwrite an existing file.
    rc2 = subprocess.run(
        [sys.executable, "-m", "psrmcp.server", "--init-config", str(target)],
        capture_output=True, text=True, timeout=60,
    )
    assert rc2.returncode == 1


def test_build_registry_yields_nineteen(tmp_path):
    from pathlib import Path

    from psrmcp._context import build_context
    from psrmcp.config import load_config
    from psrmcp.server import build_registry, create_server

    cfg = load_config(None)
    cfg.server.workspace = tmp_path / "ws"
    ctx = build_context(cfg)
    reg = build_registry(ctx)
    assert len(reg.names()) == 19

    server = create_server(cfg)
    assert server.pulsar_registry is not None
    assert server.pulsar_ctx is ctx or server.pulsar_ctx.cfg.server.workspace == cfg.server.workspace
