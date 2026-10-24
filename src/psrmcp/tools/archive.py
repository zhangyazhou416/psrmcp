"""archive_inspect / archive_convert / archive_merge (TOOLS.md 5.6-5.8)."""

from __future__ import annotations

import shutil
import time
from pathlib import Path

from mcp.types import ToolAnnotations

from ..adapters import psrchive as psa
from ..adapters import psrcat as psc
from ..errors import ErrorCode, PsrmcpError, env_missing, format_unsupported
from ..execution.runner import CancelHook
from ..execution.workspace import utcnow
from ..models.common import ArtifactKind, WarningInfo
from ..models.inputs import ArchiveConvertInput, ArchiveInspectInput, ArchiveMergeInput
from ..models.results import (
    ArchiveConvertResult,
    ArchiveInspectResult,
    ArchiveMergeResult,
    Dimensions,
    IntegrationRow,
    ObservationInfo,
    SourceInfo,
    TimingInfo,
)
from ._common import AppContext, log_artifact_from_stderr, make_provenance
from .registry import ToolRegistry, ToolSpec

_CONV_EXT = {"psrfits": "fits", "psrarchive": "ar"}


def register(reg: ToolRegistry) -> None:
    reg.add(ToolSpec(
        name="archive_inspect",
        description="读取 PSRFITS/PSRARCHIVE 的元数据、维度和基本统计,不修改文件。处理数据前先调用。",
        input_model=ArchiveInspectInput,
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
        handler=_inspect,
    ))
    reg.add(ToolSpec(
        name="archive_convert",
        description="在 PSRFITS 与 PSRARCHIVE 之间转换格式;输入保持不变,输出为新产物。",
        input_model=ArchiveConvertInput,
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False),
        handler=_convert,
    ))
    reg.add(ToolSpec(
        name="archive_merge",
        description="合并多个 archive(默认要求源名一致)。长任务,支持异步与取消。",
        input_model=ArchiveMergeInput,
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False),
        handler=_merge,
        long_task=True,
    ))


async def _inspect(ctx: AppContext, inp: ArchiveInspectInput, progress) -> ArchiveInspectResult:
    started = utcnow()
    t0 = time.monotonic()
    psr_info = await ctx.backends.require("psrchive")
    binary = _tool(ctx, "vap")
    resolved = ctx.resolver.resolve(inp.archive, root=inp.root, what="archive")

    argv = psa.vap_cmd(binary, str(resolved.resolved))
    run = await ctx.runner.run(argv, timeout_s=120.0)
    meta = psa.parse_vap(run.stdout_text)
    if not meta:
        raise format_unsupported(
            f"无法从 vap 输出解析元数据: {resolved.resolved.name}",
            hint="确认文件是 psrchive 可读的 PSRFITS/PSRARCHIVE 格式",
        )

    ra_deg = _ang(meta.get("ra_str"), psc.hms_to_deg_or_num)
    dec_deg = _ang(meta.get("dec_str"), psc.dms_to_deg)
    dims = Dimensions(
        nsubint=int(meta.get("nsubint") or 0),
        nchan=int(meta.get("nchan") or 0),
        npol=0,
        nbin=int(meta.get("nbin") or 0),
    )
    if dims.npol == 0:
        dims.npol = int(meta.get("npol") or 0)
    warnings: list[WarningInfo] = []

    integrations: list[IntegrationRow] | None = None
    if inp.include_integrations:
        integrations = _probe_integrations(ctx, resolved.resolved, inp.max_integrations, warnings)

    prov = make_provenance(
        tool="archive_inspect",
        backend="psrchive",
        backend_version=psr_info.version,
        params=inp.model_dump(mode="json"),
        started=started,
        duration_s=time.monotonic() - t0,
        command=argv,
    )
    return ArchiveInspectResult(
        input={"path": str(resolved.resolved)} | ({"artifact_id": resolved.artifact_id} if resolved.artifact_id else {}),
        format="PSRFITS" if resolved.resolved.suffix.lower() in (".fits", ".sf") else "PSRARCHIVE",
        format_version=None,
        source=SourceInfo(name=str(meta.get("name", "unknown")), ra_deg=ra_deg, dec_deg=dec_deg),
        observation=ObservationInfo(
            telescope=_s(meta.get("telescope")),
            receiver=_s(meta.get("receiver")),
            backend=_s(meta.get("backend")),
            start_mjd=_f(meta.get("start_mjd")),
            end_mjd=(_f(meta.get("start_mjd")) + _f(meta.get("duration_s")) / 86400.0)
            if meta.get("start_mjd") and meta.get("duration_s") else None,
            duration_s=_f(meta.get("duration_s")),
            freq_mhz=_f(meta.get("freq_mhz")),
            bandwidth_mhz=_f(meta.get("bandwidth_mhz")),
        ),
        dimensions=dims,
        timing=TimingInfo(
            dm=_f(meta.get("archive_dm")),
            period_s=_f(meta.get("period_s")),
            ephemeris_present=meta.get("archive_dm") is not None,
        ),
        ephemeris=None,
        integrations=integrations,
        warnings=warnings,
        provenance=prov,
    )


async def _convert(ctx: AppContext, inp: ArchiveConvertInput, progress) -> ArchiveConvertResult:
    started = utcnow()
    t0 = time.monotonic()
    if inp.output_format not in _CONV_EXT:
        raise format_unsupported(
            f"psrconv 不支持输出格式 {inp.output_format}",
            hint="当前支持 psrfits / psrarchive;sigproc/ascii 需要其它工具链",
        )
    psr_info = await ctx.backends.require("psrchive")
    binary = _tool(ctx, "psrconv")
    resolved = ctx.resolver.resolve(inp.archive, root=inp.root, what="archive")

    job_tmp = ctx.ws.new_job_tmp(f"conv_{int(time.time() * 1000)}")
    ext = _CONV_EXT[inp.output_format]
    # psrconv 没有输出目录旗标,产物写在输入文件旁 —— 先把输入复制进 job tmp
    local_src = job_tmp / resolved.resolved.name
    shutil.copy2(resolved.resolved, local_src)
    argv = psa.psrconv_cmd(binary, str(local_src), str(job_tmp), ext)
    run = await ctx.runner.run(argv, cwd=job_tmp, timeout_s=1800.0)
    if run.exit_code != 0:
        log_id = log_artifact_from_stderr(ctx, job_key="psrconv", stderr=run.stderr_tail(), argv=argv)
        raise PsrmcpError(
            ErrorCode.PROCESS_FAILED,
            f"psrconv 失败(退出码 {run.exit_code}): {run.stderr_tail(300)}",
            log_artifact_id=log_id,
        )

    produced = sorted(job_tmp.glob(f"*.{ext}"))
    if not produced:
        raise PsrmcpError(ErrorCode.PROCESS_FAILED, f"psrconv 未产出 .{ext} 文件")
    prov = make_provenance(
        tool="archive_convert",
        backend="psrconv",
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
        format_name=inp.output_format,
        provenance=prov,
        parents=[resolved.artifact_id] if resolved.artifact_id else None,
    )
    return ArchiveConvertResult(artifact=ref, warnings=[])


async def _merge(ctx: AppContext, inp: ArchiveMergeInput, progress) -> ArchiveMergeResult:
    started = utcnow()
    t0 = time.monotonic()
    psr_info = await ctx.backends.require("psrchive")
    binary = _tool(ctx, "psradd")
    resolved = [ctx.resolver.resolve(a, root=inp.root, what="archive") for a in inp.archives]

    if inp.require_same_source:
        names = set()
        for r in resolved:
            vap = await ctx.runner.run(psa.vap_cmd(_tool(ctx, "vap"), str(r.resolved)), timeout_s=120.0)
            meta = psa.parse_vap(vap.stdout_text)
            names.add(str(meta.get("name", "?")))
        if len(names) > 1:
            raise PsrmcpError(
                ErrorCode.SOURCE_MISMATCH,
                f"输入包含不同源: {sorted(names)}",
                hint="确认输入属于同一脉冲星,或设置 require_same_source=false",
            )

    progress("merging", 0.1)
    job_tmp = ctx.ws.new_job_tmp(f"merge_{int(time.time() * 1000)}")
    out_path = Path(str(job_tmp / (inp.output_name or "merged")) + ".ar")
    # 实测 psradd 只有 -o/-O 输出旗标,没有权重旗标:相加权重固定为 psrchive
    # 的自然(SNR 派生)权重,time/snr 显式选项映射不到 —— 记警告
    warnings = []
    if inp.weighting in ("time", "snr"):
        warnings.append(WarningInfo(
            code="W_PSRADD_NATURAL_WEIGHT",
            message=f"psradd 无权重旗标,weighting={inp.weighting} 未生效;实际使用自然(SNR 派生)权重",
            severity="warning",
        ))
    argv = psa.psradd_cmd(binary, [str(r.resolved) for r in resolved], str(out_path))
    run = await ctx.runner.run(
        argv, cwd=job_tmp, timeout_s=ctx.cfg.server.job_timeout_s, cancel=CancelHook()
    )
    progress("writing", 0.8)
    if run.exit_code != 0 or not out_path.exists():
        log_id = log_artifact_from_stderr(ctx, job_key="psradd", stderr=run.stderr_tail(), argv=argv)
        raise PsrmcpError(
            ErrorCode.PROCESS_FAILED,
            f"psradd 失败(退出码 {run.exit_code}): {run.stderr_tail(300)}",
            log_artifact_id=log_id,
        )
    prov = make_provenance(
        tool="archive_merge",
        backend="psradd",
        backend_version=psr_info.version,
        params=inp.model_dump(mode="json"),
        started=started,
        duration_s=time.monotonic() - t0,
        command=argv,
    )
    ref = ctx.store.commit(
        src_path=out_path,
        filename=out_path.name,
        kind=ArtifactKind.ARCHIVE,
        format_name="PSRARCHIVE",
        provenance=prov,
        parents=[r.artifact_id for r in resolved if r.artifact_id],
    )
    dims = Dimensions(nsubint=0, nchan=0, npol=0, nbin=0)
    try:
        vap = await ctx.runner.run(psa.vap_cmd(_tool(ctx, "vap"), str(out_path)), timeout_s=120.0)
        meta = psa.parse_vap(vap.stdout_text)
        dims = Dimensions(
            nsubint=int(meta.get("nsubint") or 0),
            nchan=int(meta.get("nchan") or 0),
            npol=0,
            nbin=int(meta.get("nbin") or 0),
        )
    except PsrmcpError:
        pass
    progress("complete", 1.0)
    return ArchiveMergeResult(artifact=ref, n_inputs=len(resolved), dimensions=dims, warnings=warnings)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _tool(ctx: AppContext, name: str) -> str:
    path = ctx.backends.resolve_psrchive_tool(name)
    if path is None:
        raise env_missing("psrchive", f"psrchive 工具 {name} 未找到")
    return path


def _s(v: object) -> str | None:
    return str(v) if v not in (None, "*", "") else None


def _f(v: object) -> float | None:
    try:
        f = float(v)  # type: ignore[arg-type]
        return None if f != f else f
    except (TypeError, ValueError):
        return None


def _ang(v: object, conv) -> float | None:
    f = _f(v)
    if f is not None:
        return f
    s = _s(v)
    if s is None:
        return None
    try:
        return float(conv(s))
    except Exception:
        return None


def _probe_integrations(ctx: AppContext, path: Path, limit: int, warnings: list[WarningInfo]) -> list[IntegrationRow] | None:
    # TODO(contract): per-integration rows need psrstat sub-integration output;
    # the exact expression syntax gets backfilled after the toolchain exists.
    warnings.append(WarningInfo(
        code="W_INTEGRATIONS_UNAVAILABLE",
        message="include_integrations=true 尚未实现:等待 psrstat 逐子积分输出回填",
        severity="warning",
    ))
    return None
