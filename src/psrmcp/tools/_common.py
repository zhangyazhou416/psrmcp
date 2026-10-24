"""Shared helpers for tool handlers: context, provenance, path resolution."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .. import __version__
from ..config import Config
from ..execution.jobs import JobManager
from ..execution.runner import ProcessRunner
from ..execution.tasks import TasksCapability
from ..execution.workspace import ArtifactStore, Workspace, utcnow
from ..models.common import ArtifactKind, Provenance
from ..paths import PathResolver
from ..adapters.base import BackendManager

ProgressFn = Callable[[str, float], None]


def _noop_progress(phase: str, value: float) -> None:
    return None


DEFAULT_PROGRESS: ProgressFn = _noop_progress


@dataclass
class AppContext:
    cfg: Config
    runner: ProcessRunner
    ws: Workspace
    store: ArtifactStore
    jobs: JobManager
    resolver: PathResolver
    backends: BackendManager
    tasks: TasksCapability
    python_exe: str = "python3"
    mcp_protocol_version: str = "2026-07-28"
    audit_log: Path | None = None


def make_provenance(
    *,
    tool: str,
    backend: str,
    backend_version: str | None,
    params: dict[str, Any],
    started: datetime,
    duration_s: float,
    command: list[str] | None = None,
    input_hashes: dict[str, str] | None = None,
) -> Provenance:
    return Provenance(
        tool=tool,
        tool_version=__version__,
        backend=backend,
        backend_version=backend_version,
        params=params,
        command=command or [],
        input_hashes=input_hashes or {},
        started_at=started,
        duration_s=round(duration_s, 3),
    )


def log_artifact_from_stderr(ctx: AppContext, *, job_key: str, stderr: str, argv: list[str]) -> str:
    text = f"$ {' '.join(argv)}\n\n{stderr}"
    return ctx.store.commit_text(
        text=text,
        filename=f"{job_key}.log",
        kind=ArtifactKind.LOG,
        format_name="text",
        provenance=make_provenance(
            tool="log_capture",
            backend="psrmcp",
            backend_version=__version__,
            params={},
            started=utcnow(),
            duration_s=0.0,
        ),
    ).artifact_id


def disk_free_bytes(path: Path) -> int:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return 0
