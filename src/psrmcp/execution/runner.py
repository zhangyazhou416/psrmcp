"""ProcessRunner: argv-only subprocess execution with timeouts, process-group kill,
rlimits and audit logging (DESIGN.md §2, §7.1)."""

from __future__ import annotations

import asyncio
import logging
import os
import resource
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import ErrorCode, PsrmcpError, env_missing

log = logging.getLogger(__name__)

_KILL_GRACE_S = 5.0
_DEFAULT_TIMEOUT_S = 60.0
_STDERR_TAIL_CHARS = 8000


@dataclass
class RunResult:
    argv: list[str]
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    duration_s: float
    timed_out: bool = False
    cancelled: bool = False

    @property
    def stdout_text(self) -> str:
        return self.stdout.decode("utf-8", errors="replace")

    @property
    def stderr_text(self) -> str:
        return self.stderr.decode("utf-8", errors="replace")

    def stderr_tail(self, limit: int = _STDERR_TAIL_CHARS) -> str:
        text = self.stderr_text
        return text[-limit:]


@dataclass
class CancelHook:
    """Set by the JobManager so job_cancel can kill the running process group."""

    process_group_pid: int | None = field(default=None)
    killed: bool = field(default=False)

    def kill_group(self, sig: int = signal.SIGTERM) -> bool:
        pid = self.process_group_pid
        if pid is None or self.killed:
            return False
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            return False
        return True


def _apply_rlimits(max_output_bytes: int | None) -> None:  # pragma: no cover - runs in child
    # NOTE: no RLIMIT_NPROC here — it counts processes per UID, not per child,
    # so clamping it can SIGINT BLAS thread spawning on multi-user desktops.
    # Thread caps are enforced via BLAS env vars instead (see run()).
    if not max_output_bytes:
        return
    try:
        _soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
    except (ValueError, OSError):
        return
    cap = int(max_output_bytes)
    if hard != resource.RLIM_INFINITY:
        cap = min(cap, hard)
    try:
        # Only lower the soft limit; RLIMIT_FSIZE is the resource *ID*, not a size.
        resource.setrlimit(resource.RLIMIT_FSIZE, (cap, hard))
    except (ValueError, OSError):
        pass


class ProcessRunner:
    """Runs external tools. Never uses a shell; argv arrays only."""

    def __init__(self, *, default_timeout_s: float = _DEFAULT_TIMEOUT_S, max_threads: int | None = None,
                 max_output_bytes: int | None = None, audit_log: Path | None = None) -> None:
        self._default_timeout = default_timeout_s
        self._max_threads = max_threads
        self._max_output_bytes = max_output_bytes
        self._audit_log = audit_log

    async def run(
        self,
        argv: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout_s: float | None = None,
        cancel: CancelHook | None = None,
        stdin_bytes: bytes | None = None,
    ) -> RunResult:
        if not argv:
            raise ValueError("argv must be non-empty")
        if any(not isinstance(a, str) for a in argv):
            raise ValueError("argv elements must be strings")

        binary = argv[0]
        if os.sep in binary or Path(binary).is_absolute():
            if not Path(binary).exists():
                raise env_missing(binary, "二进制不存在")
        # PATH lookup happens in exec; a missing binary surfaces as FileNotFoundError below.

        timeout = timeout_s if timeout_s is not None else self._default_timeout
        started = time.monotonic()
        child_env = os.environ.copy()
        if self._max_threads:
            # Cap BLAS/OpenMP pools instead of RLIMIT_NPROC (see _apply_rlimits).
            for var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
                        "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
                child_env.setdefault(var, str(int(self._max_threads)))
        if env:
            child_env.update(env)

        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd) if cwd else None,
            env=child_env,
            stdin=asyncio.subprocess.PIPE if stdin_bytes is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,  # own process group, so killpg cannot hit us
            preexec_fn=self._child_setup,  # noqa: PLW1509 - rlimits must be set pre-exec
        )
        if cancel is not None:
            cancel.process_group_pid = proc.pid

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(stdin_bytes), timeout=timeout)
            timed_out = False
        except asyncio.TimeoutError:
            timed_out = True
            await self._kill_group(proc, cancel)
            stdout, stderr = b"", b""
        except asyncio.CancelledError:
            if cancel is not None:
                cancel.killed = True
            await self._kill_group(proc, cancel)
            raise

        duration = time.monotonic() - started
        result = RunResult(
            argv=argv,
            exit_code=proc.returncode,
            stdout=stdout,
            stderr=stderr,
            duration_s=duration,
            timed_out=timed_out,
        )
        self._audit(result, cwd)
        if timed_out:
            raise PsrmcpError(
                ErrorCode.TIMEOUT,
                f"命令超时({timeout:.0f}s): {Path(binary).name}",
                retryable=True,
                hint="改用 async 执行,或减小数据量/增加超时",
                details={"argv": argv, "stderr_tail": result.stderr_tail()},
            )
        return result

    def _child_setup(self) -> None:  # pragma: no cover - runs in child process
        _apply_rlimits(self._max_output_bytes)

    @staticmethod
    async def _kill_group(proc: asyncio.subprocess.Process, cancel: CancelHook | None) -> None:
        pgid = proc.pid
        for sig, wait in ((signal.SIGTERM, _KILL_GRACE_S), (signal.SIGKILL, None)):
            try:
                os.killpg(pgid, sig)
            except (ProcessLookupError, PermissionError):
                break
            try:
                await asyncio.wait_for(proc.wait(), timeout=wait)
                break
            except asyncio.TimeoutError:
                continue
        if cancel is not None:
            cancel.killed = True

    def _audit(self, result: RunResult, cwd: Path | None) -> None:
        if self._audit_log is None:
            return
        line = (
            f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} "
            f"rc={result.exit_code} dur={result.duration_s:.2f}s cwd={cwd} argv={result.argv}\n"
        )
        try:
            with self._audit_log.open("a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError:  # pragma: no cover - audit must never break execution
            log.warning("audit log write failed", exc_info=True)
