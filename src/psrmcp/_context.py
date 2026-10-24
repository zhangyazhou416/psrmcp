"""AppContext assembly: wire execution kernel pieces together (server.py helper)."""

from __future__ import annotations

import sys

from .adapters.base import BackendManager
from .config import Config
from .execution.jobs import JobManager
from .execution.runner import ProcessRunner
from .execution.tasks import TasksCapability
from .execution.workspace import ArtifactStore, Workspace
from .paths import PathResolver
from .tools._common import AppContext


def build_context(cfg: Config) -> AppContext:
    ws = Workspace(cfg.server.workspace.expanduser())
    runner = ProcessRunner(
        default_timeout_s=60.0,
        max_threads=cfg.limits.max_threads,
        max_output_bytes=int(cfg.limits.max_input_gb * (1 << 30)),
        audit_log=ws.logs_dir / "audit.jsonl",
    )
    store = ArtifactStore(ws, max_output_bytes=int(cfg.limits.max_output_gb * (1 << 30)))
    jobs = JobManager(
        ws.sessions / "jobs.sqlite3",
        max_concurrent=cfg.server.max_concurrent_jobs,
        job_timeout_s=cfg.server.job_timeout_s,
    )
    resolver = PathResolver(cfg, artifact_lookup=store.lookup_id)
    backends = BackendManager(cfg, runner, python_exe=sys.executable)
    return AppContext(
        cfg=cfg,
        runner=runner,
        ws=ws,
        store=store,
        jobs=jobs,
        resolver=resolver,
        backends=backends,
        tasks=TasksCapability(),
        python_exe=sys.executable,
        audit_log=ws.logs_dir / "audit.jsonl",
    )
