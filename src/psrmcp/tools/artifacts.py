"""artifact_list / job_get / job_cancel (TOOLS.md 5.17-5.19)."""

from __future__ import annotations

from datetime import datetime

from mcp.types import ToolAnnotations

from ..models.common import ArtifactKind
from ..models.inputs import ArtifactListInput, JobCancelInput, JobGetInput
from ..models.results import ArtifactListResult, JobCancelResult, JobGetResult
from ._common import AppContext
from .registry import ToolRegistry, ToolSpec


def register(reg: ToolRegistry) -> None:
    reg.add(ToolSpec(
        name="artifact_list",
        description="列出当前 workspace 的产物(可按类型过滤,游标分页)。只读。",
        input_model=ArtifactListInput,
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
        handler=_artifact_list,
    ))
    reg.add(ToolSpec(
        name="job_get",
        description="查询后台任务状态与结果。只读。",
        input_model=JobGetInput,
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
        handler=_job_get,
    ))
    reg.add(ToolSpec(
        name="job_cancel",
        description="取消一个后台任务(SIGTERM,force 时 SIGKILL)。",
        input_model=JobCancelInput,
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False),
        handler=_job_cancel,
    ))


def _parse_created_after(raw: str | None) -> datetime | None:
    if not raw:
        return None
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


async def _artifact_list(ctx: AppContext, inp: ArtifactListInput, progress):  # noqa: ANN001
    kind = ArtifactKind(inp.kind) if inp.kind else None
    page, next_cursor = ctx.store.list(
        kind=kind,
        created_after=_parse_created_after(inp.created_after),
        limit=inp.limit,
        cursor=inp.cursor,
    )
    return ArtifactListResult(artifacts=page, count=len(page), next_cursor=next_cursor)


async def _job_get(ctx: AppContext, inp: JobGetInput, progress):  # noqa: ANN001
    return JobGetResult(job=ctx.jobs.get(inp.job_id))


async def _job_cancel(ctx: AppContext, inp: JobCancelInput, progress):  # noqa: ANN001
    return JobCancelResult(job=ctx.jobs.cancel(inp.job_id, force=inp.force))
