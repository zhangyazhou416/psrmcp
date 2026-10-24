"""JobManager: async execution, SQLite persistence, cancel, restart recovery (DESIGN.md §6.3)."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sqlite3
from collections.abc import Callable, Coroutine
from datetime import datetime
from pathlib import Path
from typing import Any

from ..errors import ErrorCode, PsrmcpError
from ..ids import new_job_id
from ..models.common import ErrorBody, JobRef, JobStatus
from .runner import CancelHook
from .workspace import utcnow

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id      TEXT PRIMARY KEY,
    status      TEXT NOT NULL,
    phase       TEXT NOT NULL DEFAULT '',
    progress    REAL NOT NULL DEFAULT 0,
    message     TEXT,
    tool        TEXT NOT NULL DEFAULT '',
    params_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT,
    error_json  TEXT,
    created_at  TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT
);
"""


class _LiveJob:
    __slots__ = ("job_id", "task", "cancel", "tool")

    def __init__(self, job_id: str, task: asyncio.Task, cancel: CancelHook, tool: str) -> None:
        self.job_id = job_id
        self.task = task
        self.cancel = cancel
        self.tool = tool


class JobManager:
    def __init__(self, db_path: Path, *, max_concurrent: int = 2, job_timeout_s: float = 7200.0) -> None:
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.executescript(_SCHEMA)
        self._db.commit()
        self._live: dict[str, _LiveJob] = {}
        self._sem = asyncio.Semaphore(max(1, max_concurrent))
        self._job_timeout = job_timeout_s
        self._progress_hooks: dict[str, Callable[[str, float], None]] = {}
        self.recover()

    # -- recovery -------------------------------------------------------------

    def recover(self) -> int:
        """Mark jobs that were running when the server stopped as interrupted."""
        cur = self._db.execute(
            "UPDATE jobs SET status='interrupted', finished_at=? WHERE status IN ('queued','working')",
            (utcnow().isoformat(),),
        )
        self._db.commit()
        return cur.rowcount

    # -- submission -----------------------------------------------------------

    def submit(
        self,
        tool: str,
        coro_factory: Callable[[Callable[[str, float], None]], Coroutine[Any, Any, dict[str, Any]]],
        params: dict[str, Any],
    ) -> JobRef:
        job_id = new_job_id()
        now = utcnow()
        self._db.execute(
            "INSERT INTO jobs (job_id, status, phase, progress, tool, params_json, created_at)"
            " VALUES (?, 'queued', 'queued', 0, ?, ?, ?)",
            (job_id, tool, json.dumps(params, ensure_ascii=False, default=str), now.isoformat()),
        )
        self._db.commit()

        cancel = CancelHook()
        task = asyncio.create_task(self._run(job_id, tool, coro_factory, cancel), name=f"job:{job_id}")
        self._live[job_id] = _LiveJob(job_id, task, cancel, tool)
        return self.get(job_id)

    async def _run(self, job_id: str, tool: str, coro_factory, cancel: CancelHook) -> None:  # noqa: ANN001
        def progress(phase: str, value: float) -> None:
            self._update(job_id, phase=phase, progress=max(0.0, min(1.0, value)))

        try:
            async with self._sem:
                self._update(job_id, status=JobStatus.WORKING, phase="working", started_at=utcnow())
                result = await asyncio.wait_for(coro_factory(progress), timeout=self._job_timeout)
            self._update(
                job_id,
                status=JobStatus.COMPLETED,
                phase="complete",
                progress=1.0,
                finished_at=utcnow(),
                result_json=json.dumps(result, ensure_ascii=False, default=str),
            )
        except asyncio.TimeoutError:
            err = PsrmcpError(
                ErrorCode.TIMEOUT, f"任务超时({self._job_timeout:.0f}s)", retryable=True
            )
            self._update_error(job_id, err)
        except asyncio.CancelledError:
            self._update(job_id, status=JobStatus.CANCELLED, phase="cancelled", finished_at=utcnow())
        except PsrmcpError as exc:
            self._update_error(job_id, exc)
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("job %s (%s) crashed", job_id, tool)
            self._update_error(job_id, PsrmcpError(ErrorCode.INTERNAL, f"{type(exc).__name__}: {exc}"))
        finally:
            self._live.pop(job_id, None)
            self._progress_hooks.pop(job_id, None)

    # -- queries / control ----------------------------------------------------

    def get(self, job_id: str) -> JobRef:
        row = self._db.execute(
            "SELECT job_id,status,phase,progress,message,created_at,started_at,finished_at,result_json,error_json"
            " FROM jobs WHERE job_id=?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise PsrmcpError(
                ErrorCode.TASK_NOT_FOUND,
                f"任务不存在: {job_id}",
                hint="检查 job_id,或重新提交任务",
            )
        return JobRef(
            job_id=row[0],
            status=JobStatus(row[1]),
            phase=row[2] or row[1],
            progress=row[3],
            message=row[4],
            created_at=datetime.fromisoformat(row[5]),
            started_at=datetime.fromisoformat(row[6]) if row[6] else None,
            finished_at=datetime.fromisoformat(row[7]) if row[7] else None,
            result=json.loads(row[8]) if row[8] else None,
            error=ErrorBody.model_validate(json.loads(row[9])) if row[9] else None,
        )

    def cancel(self, job_id: str, *, force: bool = False) -> JobRef:
        ref = self.get(job_id)
        if ref.status not in (JobStatus.QUEUED, JobStatus.WORKING):
            raise PsrmcpError(
                ErrorCode.JOB_NOT_RUNNING,
                f"任务已结束,状态 {ref.status.value}: {job_id}",
                hint="用 job_get 查询最终结果",
            )
        live = self._live.get(job_id)
        if live is not None:
            # SIGTERM first; force skips the runner's grace period via SIGKILL.
            live.cancel.kill_group(signal.SIGKILL if force else signal.SIGTERM)
            live.task.cancel()
        else:
            # Not live anymore: either queued-but-persisted or already finishing.
            self._update(job_id, status=JobStatus.CANCELLED, phase="cancelled", finished_at=utcnow())
        return self.get(job_id)

    def has_live_processes(self) -> bool:
        return bool(self._live)

    # -- persistence helpers ----------------------------------------------------

    def _update(self, job_id: str, **fields: Any) -> None:
        cols, vals = [], []
        mapping = {
            "status": "status",
            "phase": "phase",
            "progress": "progress",
            "message": "message",
            "result_json": "result_json",
            "error_json": "error_json",
            "started_at": "started_at",
            "finished_at": "finished_at",
        }
        for key, col in mapping.items():
            if key in fields and fields[key] is not None:
                v = fields[key]
                if isinstance(v, JobStatus):
                    v = v.value
                elif isinstance(v, datetime):
                    v = v.isoformat()
                cols.append(f"{col}=?")
                vals.append(v)
        if not cols:
            return
        vals.append(job_id)
        self._db.execute(f"UPDATE jobs SET {', '.join(cols)} WHERE job_id=?", vals)
        self._db.commit()

    def _update_error(self, job_id: str, exc: PsrmcpError) -> None:
        from .workspace import utcnow as _now

        body = {
            "code": exc.code.value,
            "message": exc.message,
            "hint": exc.hint,
            "retryable": exc.retryable,
            "details": exc.details,
            "log_artifact_id": exc.log_artifact_id,
        }
        self._update(
            job_id,
            status=JobStatus.FAILED,
            phase="failed",
            finished_at=_now().isoformat(),
            error_json=json.dumps(body, ensure_ascii=False),
        )
