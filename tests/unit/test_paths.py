"""PathResolver: whitelist, traversal, control chars, leading dash, artifact lookup."""

from __future__ import annotations

import pytest

from pathlib import Path

from psrmcp.errors import ErrorCode, PsrmcpError
from psrmcp.paths import PathResolver


def _resolver(tmp_path, **kw):
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    cfg = load_cfg(tmp_path, data, **kw)
    return PathResolver(cfg, artifact_lookup=lambda aid: None), data


def load_cfg(tmp_path, data, *, absolute=False):
    import tempfile

    p = Path(tempfile.mkstemp(suffix=".toml", dir=tmp_path)[1])
    p.write_text(
        f'[server]\nworkspace = "{tmp_path / "ws"}"\n'
        f'[data]\nread_roots = ["{data}"]\nallow_absolute_paths = {str(absolute).lower()}\n'
    )
    from psrmcp.config import load_config

    return load_config(p)


def test_relative_within_root_ok(tmp_path):
    r, data = _resolver(tmp_path)
    f = data / "obs.ar"
    f.write_bytes(b"x")
    res = r.resolve("obs.ar", root="data", what="test")
    assert res.resolved == f.resolve()
    assert res.artifact_id is None


def test_traversal_denied(tmp_path):
    r, _ = _resolver(tmp_path)
    with pytest.raises(PsrmcpError) as ei:
        r.resolve("../escape.ar", root="data", what="test")
    assert ei.value.code in (ErrorCode.PATH_DENIED, ErrorCode.INVALID_INPUT)


def test_leading_dash_denied(tmp_path):
    r, _ = _resolver(tmp_path)
    with pytest.raises(PsrmcpError):
        r.resolve("-rf", root="data", what="test")


def test_control_chars_denied(tmp_path):
    r, _ = _resolver(tmp_path)
    with pytest.raises(PsrmcpError):
        r.resolve("a\x00b.ar", root="data", what="test")


def test_absolute_denied_by_default(tmp_path):
    r, data = _resolver(tmp_path)
    outside = tmp_path / "outside.ar"
    outside.write_bytes(b"x")
    with pytest.raises(PsrmcpError) as ei:
        r.resolve(str(outside), root="data", what="test")
    assert ei.value.code == ErrorCode.PATH_DENIED


def test_missing_file_not_found(tmp_path):
    r, _ = _resolver(tmp_path)
    with pytest.raises(PsrmcpError) as ei:
        r.resolve("ghost.ar", root="data", what="test")
    assert ei.value.code == ErrorCode.NOT_FOUND


def test_artifact_lookup_used(tmp_path):
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    cfg = load_cfg(tmp_path, data)
    target = tmp_path / "ws" / "art.ar"
    target.parent.mkdir(exist_ok=True)
    target.write_bytes(b"y")

    def lookup(aid):
        from psrmcp.paths import ResolvedInput

        if aid == "art_deadbeef":
            return ResolvedInput(raw=aid, artifact_id=aid, resolved=target, root=None, size_bytes=1)
        return None

    r = PathResolver(cfg, artifact_lookup=lookup)
    res = r.resolve("art_deadbeef", root=None, what="test")
    assert res.resolved == target

    with pytest.raises(PsrmcpError) as ei:
        r.resolve("art_unknown01", root=None, what="test")
    assert ei.value.code == ErrorCode.NOT_FOUND


def test_artifact_lookup_bare_path_wrapped(tmp_path):
    # store.lookup_id 返回裸 Path;resolver 必须包成 ResolvedInput,
    # 否则所有走 art_ id 的工具都会在 .resolved/.artifact_id 上崩。
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    cfg = load_cfg(tmp_path, data)
    target = tmp_path / "ws" / "art.ar"
    target.parent.mkdir(exist_ok=True)
    target.write_bytes(b"0123456789")

    r = PathResolver(cfg, artifact_lookup=lambda aid: target if aid == "art_cafe1234" else None)
    res = r.resolve("art_cafe1234", root=None, what="test")
    assert isinstance(res.resolved, Path)
    assert res.resolved == target
    assert res.artifact_id == "art_cafe1234"
    assert res.size_bytes == 10
