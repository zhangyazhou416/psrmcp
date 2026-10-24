"""pulsar_environment (TOOLS.md 5.1)."""

from __future__ import annotations

import time

from mcp.types import ToolAnnotations

from .. import SERVER_NAME, __version__
from ..models.common import BackendInfo, CatalogMeta, CatalogMode, WarningInfo
from ..models.inputs import PulsarEnvironmentInput
from ..models.results import EnvironmentResult, EnvironmentServerInfo, LimitsInfo, WorkspaceInfo
from ._common import AppContext, disk_free_bytes
from .registry import ToolRegistry, ToolSpec


def register(reg: ToolRegistry) -> None:
    reg.add(
        ToolSpec(
            name="pulsar_environment",
        description=(
            "返回服务器版本、可用后端(dspsr/psrchive/tempo2/psrcat/pint)、数据根目录、工作区与资源限额。"
            "建议每次会话第一个调用。后端不可用时不报错,而是在 backends 里给出 reason。"
        ),
            input_model=PulsarEnvironmentInput,
            annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
            handler=run,
        )
    )


async def run(ctx: AppContext, inp: PulsarEnvironmentInput, progress) -> EnvironmentResult:  # noqa: ANN001
    started = time.monotonic()
    infos: dict[str, BackendInfo] = await ctx.backends.probe_all(refresh=inp.refresh)

    warnings: list[WarningInfo] = []
    for name, info in sorted(infos.items()):
        if not info.available:
            warnings.append(
                WarningInfo(
                    code="W_BACKEND_MISSING",
                    message=f"后端 {name} 不可用: {info.reason or '未找到'}",
                    severity="info",
                    details={"backend": name},
                )
            )

    workspace = ctx.ws.root
    quota = int(ctx.cfg.limits.max_output_gb * 1024**3)
    catalog_mode = CatalogMode(ctx.cfg.catalog.mode) if ctx.cfg.catalog.mode in ("local", "online") else CatalogMode.CACHE

    result = EnvironmentResult(
        server=EnvironmentServerInfo(
            name=SERVER_NAME,
            version=__version__,
            mcp_protocol_version=ctx.mcp_protocol_version,
        ),
        backends={name: info.model_dump() for name, info in sorted(infos.items())},
        data_roots=[str(p.expanduser()) for p in ctx.cfg.data.read_roots],
        workspace=WorkspaceInfo(
            root=str(workspace),
            free_bytes=disk_free_bytes(workspace),
            quota_bytes=quota,
        ),
        catalog=CatalogMeta(
            source="psrcat" if ctx.backends.cached("psrcat") and infos.get("psrcat", BackendInfo(available=False)).available
            else "atnf-online",
            version="unknown",
            retrieved_at=_utcnow(),
            mode=catalog_mode,
        ),
        limits=LimitsInfo(
            max_threads=ctx.cfg.limits.max_threads,
            max_input_bytes=ctx.cfg.limits.max_input_bytes,
            max_output_bytes=ctx.cfg.limits.max_output_bytes,
            job_timeout_s=int(ctx.cfg.server.job_timeout_s),
        ),
        warnings=warnings,
    )
    _ = started
    return result


def _utcnow():
    from ..execution.workspace import utcnow

    return utcnow()
