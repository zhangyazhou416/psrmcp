"""ArtifactStore: atomic commit, sha256, manifest, quota, cache."""

from __future__ import annotations

import json

import pytest

from psrmcp.errors import ErrorCode, PsrmcpError
from psrmcp.execution.workspace import ArtifactStore, Workspace
from psrmcp.models.common import ArtifactKind, Provenance


@pytest.fixture
def ws(tmp_path):
    return Workspace(tmp_path / "ws")


@pytest.fixture
def store(ws):
    return ArtifactStore(ws, max_output_bytes=1024)


def _prov(tool="t"):
    from datetime import datetime, timezone

    return Provenance(tool=tool, tool_version="0.1.0", backend="b", backend_version=None,
                      params={}, command=[], started_at=datetime.now(timezone.utc), duration_s=0.1)


def test_commit_atomic_and_manifest(ws, store, tmp_path):
    src = tmp_path / "out.ar"
    src.write_bytes(b"payload")
    ref = store.commit(src_path=src, filename="out.ar", kind=ArtifactKind.ARCHIVE,
                       format_name="psrfits", provenance=_prov())
    assert ref.artifact_id.startswith("art_")
    final = ws.root / ref.path
    assert final.read_bytes() == b"payload"
    assert len(ref.sha256) == 64
    assert not src.exists()  # moved, not copied
    manifest = ws.manifest_path.read_text().strip().splitlines()
    rec = json.loads(manifest[-1])
    assert rec["artifact_id"] == ref.artifact_id


def test_quota_exceeded(ws, tmp_path):
    store = ArtifactStore(ws, max_output_bytes=10)
    src = tmp_path / "big.ar"
    src.write_bytes(b"x" * 100)
    with pytest.raises(PsrmcpError) as ei:
        store.commit(src_path=src, filename="big.ar", kind=ArtifactKind.ARCHIVE,
                     format_name="psrfits", provenance=_prov())
    assert ei.value.code == ErrorCode.QUOTA_EXCEEDED


def test_commit_text_and_lookup(ws, store):
    ref = store.commit_text(text="FORMAT 1\n", filename="t.tim", kind=ArtifactKind.TIM,
                            format_name="tempo2", provenance=_prov())
    path = store.lookup_id(ref.artifact_id)
    assert path is not None and path.read_text().startswith("FORMAT 1")
    with pytest.raises(PsrmcpError):
        store.get("art_missing0")


def test_list_filter_and_cursor(ws, store):
    for i in range(5):
        store.commit_text(text=str(i), filename=f"t{i}.tim", kind=ArtifactKind.TIM,
                          format_name="tempo2", provenance=_prov())
    store.commit_text(text="p", filename="p.png", kind=ArtifactKind.PLOT,
                      format_name="png", provenance=_prov())
    page, cur = store.list(kind=ArtifactKind.TIM, limit=3)
    assert len(page) == 3 and cur is not None
    page2, cur2 = store.list(kind=ArtifactKind.TIM, limit=3, cursor=cur)
    assert len(page2) == 2 and cur2 is None
    plots, _ = store.list(kind=ArtifactKind.PLOT)
    assert len(plots) == 1
