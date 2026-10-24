"""rfi_scan / rfi_zap (TOOLS.md 5.10-5.11)."""

from __future__ import annotations

import time

from mcp.types import ToolAnnotations

from ..adapters import psrchive as psa
from ..errors import ErrorCode, PsrmcpError, env_missing, format_unsupported
from ..execution.workspace import utcnow
from ..models.common import ArtifactKind, WarningInfo
from ..models.inputs import RfiScanInput, RfiZapInput
from ..models.results import RfiScanResult, RfiZapResult, SuggestedMask
from ._common import AppContext, log_artifact_from_stderr, make_provenance
from .registry import ToolRegistry, ToolSpec
from .archive import _tool as _psrchive_tool


def register(reg: ToolRegistry) -> None:
    reg.add(ToolSpec(
        name="rfi_scan",
        description="分析 archive 的通道统计,给出建议坏通道掩膜。只读,不修改数据。",
        input_model=RfiScanInput,
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
        handler=_scan,
    ))
    reg.add(ToolSpec(
        name="rfi_zap",
        description="对 archive 应用通道掩膜生成新 archive;支持显式通道列表或自动检测。",
        input_model=RfiZapInput,
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False),
        handler=_zap,
    ))


def _channels_to_ranges(channels: list[int]) -> str:
    """[0,1,2,7,9,10] -> "0-2,7,9-10" (paz -z syntax)."""
    if not channels:
        return ""
    chans = sorted(set(channels))
    parts = []
    start = prev = chans[0]
    for c in chans[1:]:
        if c == prev + 1:
            prev = c
            continue
        parts.append(f"{start}-{prev}" if prev > start else f"{start}")
        start = prev = c
    parts.append(f"{start}-{prev}" if prev > start else f"{start}")
    return ",".join(parts)


def _flag_stats(ctx: AppContext, path, threshold_sigma: float, nchan_hint: int) -> tuple[list[int], dict]:
    """Per-channel statistics via psrstat; returns (bad_channels, statistics).

    TODO(contract): psrchive per-channel weight/statistics output syntax needs
    verification on the installed version. Raises E_PROCESS_FAILED with an
    explicit hint when the installed psrstat cannot provide the data.
    """
    raise PsrmcpError(
        ErrorCode.PROCESS_FAILED,
        "rfi_scan 的逐通道统计依赖 psrstat 逐通道输出,契约测试回填前不可用",
        hint="先安装软件栈并运行契约测试,或使用 rfi_zap 显式通道列表",
        retryable=False,
    )


async def _scan(ctx: AppContext, inp: RfiScanInput, progress) -> RfiScanResult:  # noqa: ANN001
    started = utcnow()
    t0 = time.monotonic()
    psr_info = await ctx.backends.require("psrchive")
    resolved = ctx.resolver.resolve(inp.archive, root=inp.root, what="archive")

    if inp.method == "spectral_kurtosis":
        raise env_missing("clfd", "spectral_kurtosis 方法依赖 clfd,当前未安装")

    vap_binary = _psrchive_tool(ctx, "vap")
    vap = await ctx.runner.run(psa.vap_cmd(vap_binary, str(resolved.resolved)), timeout_s=120.0)
    meta = psa.parse_vap(vap.stdout_text)
    nchan = int(meta.get("nchan") or 0)
    if nchan <= 0:
        raise format_unsupported(f"无法确定通道数: {resolved.resolved.name}")

    progress("measuring", 0.3)
    bad, stats = _flag_stats(ctx, resolved.resolved, inp.threshold_sigma, nchan)

    plot_artifact = None
    warnings: list[WarningInfo] = []
    if inp.generate_plot and bad:
        plot_artifact = _plot_mask(ctx, bad, nchan, resolved.resolved.name, warnings)

    prov = make_provenance(
        tool="rfi_scan",
        backend="psrchive",
        backend_version=psr_info.version,
        params=inp.model_dump(mode="json"),
        started=started,
        duration_s=time.monotonic() - t0,
    )
    _ = prov
    return RfiScanResult(
        suggested_mask=SuggestedMask(
            channels=bad,
            fraction=len(bad) / nchan,
            n_channels=len(bad),
        ),
        statistics=stats,
        plot_artifact=plot_artifact,
        warnings=warnings,
    )


async def _zap(ctx: AppContext, inp: RfiZapInput, progress) -> RfiZapResult:  # noqa: ANN001
    started = utcnow()
    t0 = time.monotonic()
    psr_info = await ctx.backends.require("psrchive")
    binary = _psrchive_tool(ctx, "paz")
    resolved = ctx.resolver.resolve(inp.archive, root=inp.root, what="archive")

    if inp.mask.auto is not None:
        vap_binary = _psrchive_tool(ctx, "vap")
        vap = await ctx.runner.run(psa.vap_cmd(vap_binary, str(resolved.resolved)), timeout_s=120.0)
        meta = psa.parse_vap(vap.stdout_text)
        nchan = int(meta.get("nchan") or 0)
        bad, _stats = _flag_stats(ctx, resolved.resolved, inp.mask.auto.threshold_sigma, nchan)
        channels = bad
    else:
        channels = list(inp.mask.channels or [])

    if not channels:
        raise PsrmcpError(ErrorCode.INVALID_INPUT, "掩膜为空:没有要 zap 的通道")

    nchan_total = 0
    vap_binary = _psrchive_tool(ctx, "vap")
    vap = await ctx.runner.run(psa.vap_cmd(vap_binary, str(resolved.resolved)), timeout_s=120.0)
    meta = psa.parse_vap(vap.stdout_text)
    nchan_total = int(meta.get("nchan") or 0)

    job_tmp = ctx.ws.new_job_tmp(f"paz_{int(time.time() * 1000)}")
    # paz 把 <输入名>.paz 写到输入文件旁(不是进程 cwd)—— 先复制进 job tmp
    import shutil

    local_src = job_tmp / resolved.resolved.name
    shutil.copy2(resolved.resolved, local_src)
    argv = psa.paz_cmd(binary, str(local_src), _channels_to_ranges(channels), str(job_tmp))
    progress("zapping", 0.5)
    run = await ctx.runner.run(argv, cwd=job_tmp, timeout_s=1800.0)
    produced = sorted(job_tmp.glob("*.paz")) + sorted(job_tmp.glob("*.ar")) + sorted(job_tmp.glob("*.fits"))
    if run.exit_code != 0 or not produced:
        log_id = log_artifact_from_stderr(ctx, job_key="paz", stderr=run.stderr_tail(), argv=argv)
        raise PsrmcpError(
            ErrorCode.PROCESS_FAILED,
            f"paz 失败(退出码 {run.exit_code}): {run.stderr_tail(300)}",
            log_artifact_id=log_id,
        )
    prov = make_provenance(
        tool="rfi_zap",
        backend="paz",
        backend_version=psr_info.version,
        params=inp.model_dump(mode="json"),
        started=started,
        duration_s=time.monotonic() - t0,
        command=argv,
    )
    ref = ctx.store.commit(
        src_path=produced[0],
        filename=produced[0].name,
        kind=ArtifactKind.ARCHIVE,
        format_name="PSRARCHIVE",
        provenance=prov,
        parents=[resolved.artifact_id] if resolved.artifact_id else None,
    )
    return RfiZapResult(
        artifact=ref,
        n_channels_zapped=len(channels),
        zapped_fraction=len(channels) / nchan_total if nchan_total else 0.0,
        warnings=[],
    )


def _plot_mask(ctx: AppContext, bad: list[int], nchan: int, name: str, warnings: list[WarningInfo]) -> str | None:
    try:
        from ..plotting.profile_plots import plot_channel_mask

        png = ctx.ws.new_job_tmp(f"rfiplot_{int(time.time() * 1000)}") / "mask.png"
        plot_channel_mask(bad, nchan, png, title=f"RFI mask: {name}")
        from ..execution.workspace import utcnow as _now

        ref = ctx.store.commit(
            src_path=png,
            filename="mask.png",
            kind=ArtifactKind.PLOT,
            format_name="png",
            provenance=make_provenance(
                tool="rfi_scan",
                backend="matplotlib",
                backend_version=None,
                params={"nchan": nchan},
                started=_now(),
                duration_s=0.0,
            ),
        )
        return ref.artifact_id
    except Exception as exc:
        warnings.append(WarningInfo(
            code="W_PLOT_FAILED",
            message=f"掩膜图生成失败: {exc}",
            severity="info",
        ))
        return None
