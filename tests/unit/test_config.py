"""Config loading: defaults, PSRMCP_CONFIG, bad TOML -> E_CONFIG."""

from __future__ import annotations

import pytest

from psrmcp.config import load_config
from psrmcp.errors import ErrorCode, PsrmcpError


def test_defaults(tmp_path):
    cfg = load_config(None)
    assert cfg.server.long_job_threshold_s == 20
    assert cfg.server.max_concurrent_jobs == 2
    assert cfg.limits.max_threads >= 1
    assert cfg.data.allow_absolute_paths is False


def test_bad_toml_raises_e_config(tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text("[server\nbroken", encoding="utf-8")
    with pytest.raises(PsrmcpError) as ei:
        load_config(bad)
    assert ei.value.code == ErrorCode.CONFIG


def test_wrong_typed_value_rejected(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[data]\nread_roots = "not-a-list"\n', encoding="utf-8")
    with pytest.raises(PsrmcpError) as ei:
        load_config(p)
    assert ei.value.code == ErrorCode.CONFIG
