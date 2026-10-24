"""JobManager: submit/complete, failure capture, cancel, recover, concurrency."""

from __future__ import annotations

import asyncio

import pytest

from psrmcp.errors import ErrorCode, PsrmcpError
from psrmcp.execution.jobs import JobManager
from psrmcp.models.common import JobStatus


@pytest.fixture
def mgr(tmp_path):
    return JobManager(tmp_path / "jobs.sqlite3", max_concurrent=2, job_timeout_s=5.0)


def _factory(result=None, delay=0.0, error=None):
    async def factory(progress):
        if delay:
            await asyncio.sleep(delay)
        if error is not None:
            raise error
        return result or {"ok": True}

    return factory


async def test_submit_completes(mgr):
    ref = mgr.submit("tool_x", _factory({"answer": 42}), params={"a": 1})
    for _ in range(200):
        ref = mgr.get(ref.job_id)
        if ref.status in (JobStatus.COMPLETED, JobStatus.FAILED):
            break
        await asyncio.sleep(0.01)
    assert ref.status == JobStatus.COMPLETED
    assert ref.result == {"answer": 42}
    assert ref.created_at is not None


async def test_failure_captured(mgr):
    ref = mgr.submit("tool_x", _factory(error=PsrmcpError(ErrorCode.PROCESS_FAILED, "boom")), params={})
    for _ in range(200):
        ref = mgr.get(ref.job_id)
        if ref.status == JobStatus.FAILED:
            break
        await asyncio.sleep(0.01)
    assert ref.status == JobStatus.FAILED
    assert ref.error is not None and ref.error.code == ErrorCode.PROCESS_FAILED


def test_job_not_found(mgr):
    with pytest.raises(PsrmcpError) as ei:
        mgr.get("job_missing1234")
    assert ei.value.code == ErrorCode.TASK_NOT_FOUND


async def test_recover_marks_interrupted(tmp_path):
    m1 = JobManager(tmp_path / "j.sqlite3")
    ref = m1.submit("tool_x", _factory(delay=10), params={})
    # Simulate restart: a fresh manager on the same DB recovers working/queued.
    m2 = JobManager(tmp_path / "j.sqlite3")
    after = m2.get(ref.job_id)
    assert after.status == JobStatus.INTERRUPTED


async def test_cancel_running(tmp_path):
    mgr = JobManager(tmp_path / "j.sqlite3", max_concurrent=1, job_timeout_s=30)
    started = asyncio.Event()

    async def factory(progress):
        started.set()
        await asyncio.sleep(30)

    ref = mgr.submit("slow", factory, params={})
    for _ in range(200):
        await asyncio.sleep(0.01)
        if started.is_set():
            break
    out = mgr.cancel(ref.job_id)
    for _ in range(200):
        out = mgr.get(ref.job_id)
        if out.status in (JobStatus.CANCELLED, JobStatus.FAILED):
            break
        await asyncio.sleep(0.01)
    assert out.status == JobStatus.CANCELLED

    with pytest.raises(PsrmcpError) as ei:
        mgr.cancel(ref.job_id)
    assert ei.value.code == ErrorCode.JOB_NOT_RUNNING
