"""Bridge to the PINT worker subprocess (JSON over stdin/stdout, one request)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from ..errors import ErrorCode, PsrmcpError
from ..execution.runner import ProcessRunner

_WORKER_MODULE = "psrmcp.workers.pint_worker"


def worker_argv(python_exe: str | None = None) -> list[str]:
    return [python_exe or sys.executable, "-m", _WORKER_MODULE]


async def call_worker(
    runner: ProcessRunner,
    request: dict[str, Any],
    *,
    cwd: Path,
    python_exe: str | None = None,
    timeout_s: float = 600.0,
) -> dict[str, Any]:
    """Run one worker request; returns the parsed response dict."""
    argv = worker_argv(python_exe)
    result = await runner.run(
        argv,
        cwd=cwd,
        stdin_bytes=json.dumps(request).encode("utf-8"),
        timeout_s=timeout_s,
    )
    return parse_worker_output(result.stdout_text)


def parse_worker_output(text: str) -> dict[str, Any]:
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    if not lines:
        raise PsrmcpError(ErrorCode.PROCESS_FAILED, "PINT worker 没有输出")
    try:
        resp = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise PsrmcpError(
            ErrorCode.PROCESS_FAILED,
            f"PINT worker 输出不是 JSON: {exc}",
            details={"stdout_tail": text[-2000:]},
        ) from exc
    if not resp.get("ok", False):
        code_raw = str(resp.get("code", "E_PROCESS_FAILED"))
        try:
            code = ErrorCode(code_raw)
        except ValueError:
            code = ErrorCode.PROCESS_FAILED
        raise PsrmcpError(code, str(resp.get("message", "PINT worker failed")))
    return resp
