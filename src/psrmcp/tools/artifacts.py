"""artifact_list / artifact_delete / job_get / job_cancel (TOOLS.md 5.33-5.36)."""

from __future__ import annotations

from datetime import datetime

from mcp.types import ToolAnnotations

from ..errors import ErrorCode, PsrmcpError
from ..models.common import ArtifactKind
from ..models.inputs import (
    ArtifactDeleteInput,
    ArtifactListInput,
    JobCancelInput,
    JobGetInput,
)
from ..models.results import (
    ArtifactDeleteResult,
    ArtifactListResult,
    JobCancelResult,
    JobGetResult,
)
from ._common import AppContext
from .registry import ToolRegistry, ToolSpec


def register(reg: ToolRegistry) -> None:
    reg.add(ToolSpec(
        name="artifact_list",
        description="List artifacts in the current workspace (filter by kind, cursor pagination). Read-only.",
        input_model=ArtifactListInput,
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
        handler=_artifact_list,
    ))
    reg.add(ToolSpec(
        name="artifact_delete",
        description="Delete a workspace artifact: the file moves to the workspace trash area "
                    "(kept for 7 days, then purged) and the artifact disappears from "
                    "artifact_list. Requires confirm=true. Artifacts that other artifacts "
                    "reference as a provenance parent are refused until the descendants are "
                    "deleted first. This is the only destructive tool in the suite.",
        input_model=ArtifactDeleteInput,
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False),
        handler=_artifact_delete,
    ))
    reg.add(ToolSpec(
        name="job_get",
        description="Query the status and result of a background job. Read-only.",
        input_model=JobGetInput,
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
        handler=_job_get,
    ))
    reg.add(ToolSpec(
        name="job_cancel",
        description="Cancel a background job (SIGTERM, or SIGKILL when force=true).",
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


async def _artifact_delete(ctx: AppContext, inp: ArtifactDeleteInput, progress):  # noqa: ANN001
    if not inp.confirm:
        raise PsrmcpError(
            ErrorCode.INVALID_INPUT,
            f"artifact_delete requires confirm=true (refused to delete {inp.artifact_id})",
            hint="re-run with confirm=true to move the artifact to the trash area",
        )
    ref = ctx.store.delete(inp.artifact_id)
    return ArtifactDeleteResult(
        artifact_id=ref.artifact_id,
        kind=str(ref.kind),
        size_bytes=ref.size_bytes,
    )


async def _job_get(ctx: AppContext, inp: JobGetInput, progress):  # noqa: ANN001
    return JobGetResult(job=ctx.jobs.get(inp.job_id))


async def _job_cancel(ctx: AppContext, inp: JobCancelInput, progress):  # noqa: ANN001
    return JobCancelResult(job=ctx.jobs.cancel(inp.job_id, force=inp.force))
