"""Workspace + ArtifactStore (DESIGN.md §6.1/§6.2): atomic writes, sha256, manifest."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from ..errors import ErrorCode, PsrmcpError, not_found
from ..ids import new_artifact_id, new_session_id
from ..models.common import ArtifactKind, ArtifactRef, Provenance


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Workspace:
    """Directory layout: sessions/<sid>/{artifacts,logs}, cache/, tmp/."""

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser()
        self.session_id = new_session_id()
        self.sessions = self.root / "sessions"
        self.session_dir = self.sessions / self.session_id
        self.artifacts_dir = self.session_dir / "artifacts"
        self.logs_dir = self.session_dir / "logs"
        self.cache_dir = self.root / "cache"
        self.tmp_dir = self.root / "tmp"
        self.manifest_path = self.session_dir / "manifest.jsonl"
        for p in (self.artifacts_dir, self.logs_dir, self.cache_dir, self.tmp_dir):
            p.mkdir(parents=True, exist_ok=True)

    def new_job_tmp(self, job_id: str) -> Path:
        d = self.tmp_dir / job_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def new_log_file(self, job_id: str) -> Path:
        return self.logs_dir / f"{job_id}.log"


class ArtifactStore:
    """Immutable artifacts: write to tmp, then rename into place. Manifest is append-only."""

    def __init__(self, ws: Workspace, *, max_output_bytes: int) -> None:
        self._ws = ws
        self._max_output_bytes = max_output_bytes
        self._index: dict[str, ArtifactRef] = {}

    # -- lookup ---------------------------------------------------------------

    def get(self, artifact_id: str) -> ArtifactRef:
        ref = self._index.get(artifact_id)
        if ref is None:
            raise not_found(f"未知 artifact: {artifact_id}")
        return ref

    def lookup_id(self, artifact_id: str) -> Path | None:
        ref = self._index.get(artifact_id)
        return None if ref is None else self._ws.root / ref.path  # type: ignore[arg-type]

    def list(
        self,
        *,
        kind: ArtifactKind | None = None,
        created_after: datetime | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[ArtifactRef], str | None]:
        refs = sorted(self._index.values(), key=lambda r: r.created_at)
        if kind is not None:
            refs = [r for r in refs if r.kind == kind]
        if created_after is not None:
            refs = [r for r in refs if r.created_at > created_after]
        start = 0
        if cursor:
            idx = next((i for i, r in enumerate(refs) if r.artifact_id == cursor), -1)
            start = idx + 1 if idx >= 0 else 0
        page = refs[start : start + limit]
        next_cursor = page[-1].artifact_id if start + limit < len(refs) and page else None
        return page, next_cursor

    # -- creation -------------------------------------------------------------

    def commit(
        self,
        *,
        src_path: Path,
        filename: str,
        kind: ArtifactKind,
        format_name: str,
        provenance: Provenance,
        parents: list[str] | None = None,
        cache_key: str | None = None,
    ) -> ArtifactRef:
        src_path = Path(src_path)
        if not src_path.is_file():
            raise not_found(f"待提交的产物不存在: {src_path}")
        size = src_path.stat().st_size
        if size > self._max_output_bytes:
            raise PsrmcpError(
                ErrorCode.QUOTA_EXCEEDED,
                f"产物超出大小限额: {size} > {self._max_output_bytes}",
                hint="调整 limits.max_output_gb 或减小输出维度",
            )

        if cache_key:
            cached = self._cache_fetch(cache_key)
            if cached is not None:
                cached.parents = list(parents or [])
                return cached

        artifact_id = new_artifact_id()
        final_dir = self._ws.artifacts_dir / artifact_id
        final_dir.mkdir(parents=True, exist_ok=False)
        final_path = final_dir / _safe_filename(filename)
        # Same-filesystem rename: atomic and never overwrites (dir is fresh).
        os.rename(src_path, final_path)

        ref = ArtifactRef(
            artifact_id=artifact_id,
            kind=kind,
            format=format_name,
            path=str(final_path.relative_to(self._ws.root)),
            size_bytes=final_path.stat().st_size,
            sha256=hash_file(final_path),
            created_at=utcnow(),
            parents=parents or [],
            provenance=provenance,
        )
        if cache_key:
            self._cache_put(cache_key, final_path)
        self._register(ref)
        return ref

    def commit_text(
        self,
        *,
        text: str,
        filename: str,
        kind: ArtifactKind,
        format_name: str,
        provenance: Provenance,
        parents: list[str] | None = None,
    ) -> ArtifactRef:
        tmp = self._ws.new_job_tmp(f"txt_{os.getpid()}_{time.monotonic_ns()}")
        path = tmp / filename
        path.write_text(text, encoding="utf-8")
        return self.commit(
            src_path=path,
            filename=filename,
            kind=kind,
            format_name=format_name,
            provenance=provenance,
            parents=parents,
        )

    # -- cache ----------------------------------------------------------------

    def _cache_fetch(self, cache_key: str) -> ArtifactRef | None:
        meta = self._ws.cache_dir / cache_key / "meta.json"
        if not meta.exists():
            return None
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
            ref = ArtifactRef.model_validate(data)
            src = self._ws.root / str(ref.path)
            if not src.exists():
                return None
        except Exception:
            return None
        # Re-register under the session so artifact_list sees the reuse.
        self._index[ref.artifact_id] = ref
        self._append_manifest(ref, reused=True)
        return ref

    def _cache_put(self, cache_key: str, final_path: Path) -> None:
        try:
            dst_dir = self._ws.cache_dir / cache_key
            dst_dir.mkdir(parents=True, exist_ok=True)
            os.link(final_path, dst_dir / "payload")
            ref = next(r for r in self._index.values() if r.path and (self._ws.root / r.path) == final_path)
            (dst_dir / "meta.json").write_text(ref.model_dump_json(), encoding="utf-8")
        except OSError:
            shutil.rmtree(self._ws.cache_dir / cache_key, ignore_errors=True)

    # -- internal -------------------------------------------------------------

    def _register(self, ref: ArtifactRef) -> None:
        self._index[ref.artifact_id] = ref
        self._append_manifest(ref)

    def _append_manifest(self, ref: ArtifactRef, *, reused: bool = False) -> None:
        record = {
            "artifact_id": ref.artifact_id,
            "kind": str(ref.kind),
            "format": ref.format,
            "sha256": ref.sha256,
            "size_bytes": ref.size_bytes,
            "created_at": ref.created_at.isoformat(),
            "reused": reused,
        }
        with self._ws.manifest_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def iter_index(self) -> list[ArtifactRef]:
        return list(self._index.values())


def hash_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(chunk):
            h.update(block)
    return h.hexdigest()


def _safe_filename(name: str) -> str:
    name = Path(name).name
    if not name or name.startswith("-") or any(c in name for c in "/\\\x00") or any(ord(c) < 32 for c in name):
        name = "output.bin"
    return name
