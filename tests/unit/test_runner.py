"""ProcessRunner: true/false exit codes, timeout kill, no shell, stdin."""

from __future__ import annotations

import time

import pytest

from psrmcp.errors import ErrorCode, PsrmcpError
from psrmcp.execution.runner import ProcessRunner


@pytest.fixture
def runner(tmp_path):
    return ProcessRunner(default_timeout_s=5.0, audit_log=tmp_path / "audit.jsonl")


async def test_true(runner):
    r = await runner.run(["true"])
    assert r.exit_code == 0
    assert r.timed_out is False


async def test_false_stderr(runner):
    r = await runner.run(["false"])
    assert r.exit_code != 0


async def test_argv_not_shell(runner):
    # A shell metacharacter must reach the program verbatim, not be interpreted.
    r = await runner.run(["echo", "a; b|c"])
    assert r.stdout_text.strip() == "a; b|c"


async def test_stdin_passthrough(runner):
    r = await runner.run(["cat"], stdin_bytes=b"hello worker")
    assert r.stdout_text == "hello worker"


async def test_timeout_kills(tmp_path):
    runner = ProcessRunner(default_timeout_s=0.2)
    t0 = time.monotonic()
    with pytest.raises(PsrmcpError) as ei:
        await runner.run(["sleep", "30"])
    assert time.monotonic() - t0 < 5
    assert ei.value.code == ErrorCode.TIMEOUT


async def test_missing_binary_env_missing(runner):
    with pytest.raises(PsrmcpError) as ei:
        await runner.run(["/nonexistent/binary-xyz"])
    assert ei.value.code == ErrorCode.ENV_MISSING


async def test_audit_written(runner, tmp_path):
    await runner.run(["true"])
    audit = tmp_path / "audit.jsonl"
    assert audit.exists() and "true" in audit.read_text()


async def test_process_group_kill(tmp_path):
    # The child spawns its own child; both must die on timeout.

    runner = ProcessRunner(default_timeout_s=0.3)
    script = tmp_path / "spawn.sh"
    script.write_text("#!/bin/sh\nsleep 30 &\nwait\n")
    script.chmod(0o755)
    with pytest.raises(PsrmcpError):
        await runner.run([str(script)])
    await __import__("asyncio").sleep(0.1)
    # No leftover process group from that script should remain.
    out = __import__("subprocess").run(["pgrep", "-f", "sleep 30"], capture_output=True)
    assert out.returncode != 0, "timeout must kill the whole process group"
