"""dedisperse (TOOLS.md 5.9): dspsr folding with DM from ephemeris or override."""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

from mcp.types import ToolAnnotations

from ..adapters import dspsr as dsa
from ..errors import ErrorCode, PsrmcpError
from ..execution.runner import CancelHook
from ..execution.workspace import utcnow
from ..models.common import ArtifactKind, WarningInfo
from ..models.inputs import DedisperseInput
from ..models.results import DedisperseResult, Dimensions
from ._common import AppContext, log_artifact_from_stderr, make_provenance
from .registry import ToolRegistry, ToolSpec
from . import catalog as catalog_tools
from . import archive as archive_tools

_DM_LINE = re.compile(r"^DM\s+([0-9.eE+-]+)", re.MULTILINE | re.IGNORECASE)


def _tempo2_runtime(ctx: AppContext) -> str:
    """tempo2 runtime 目录:环境变量优先,其次按配置的 tempo2 二进制推导
    ($PREFIX/bin/tempo2 → $PREFIX/tempo2)。runtime 布局有两种:T2runtime
    rsync 成目录内容(实测布局,含 clock/ observatory/ 等)或带 T2runtime
    子目录。"""
    from_env = os.environ.get("TEMPO2")
    if from_env:
        return from_env
    binary = ctx.cfg.backends.tempo2
    if binary:
        p = Path(binary).expanduser().resolve()
        for parent in p.parents:
            t2 = parent / "tempo2"
            if t2.is_dir() and any(
                (t2 / marker).exists()
                for marker in ("T2runtime", "clock.dat", "clock", "observatory", "ephemeris")
            ):
                return str(t2)
    return ""


def register(reg: ToolRegistry) -> None:
    reg.add(ToolSpec(
        name="dedisperse",
        description="用 dspsr 对原始数据或 archive 做消色散并折叠,生成新 archive 产物。长任务,支持异步与取消。",
        input_model=DedisperseInput,
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False),
        handler=_run,
        long_task=True,
    ))


async def _run(ctx: AppContext, inp: DedisperseInput, progress) -> DedisperseResult:  # noqa: ANN001
    started = utcnow()
    t0 = time.monotonic()
    dspsr_info = await ctx.backends.require("dspsr")
    binary = ctx.backends.resolve("dspsr")
    if binary is None:
        raise PsrmcpError(ErrorCode.ENV_MISSING, "dspsr 路径解析失败")

    source = ctx.resolver.resolve(inp.input, root=inp.root, what="input")

    # Ephemeris: artifact / par path / pulsar name.
    par_artifact_id: str | None = None
    if inp.ephemeris.startswith("art_"):
        par_path = ctx.store.lookup_id(inp.ephemeris)
        if par_path is None:
            raise PsrmcpError(ErrorCode.NOT_FOUND, f"未知 artifact: {inp.ephemeris}")
        par_artifact_id = inp.ephemeris
    else:
        try:
            resolved_par = ctx.resolver.resolve(inp.ephemeris, root=inp.root, what="ephemeris")
            par_path = resolved_par.resolved
            par_artifact_id = resolved_par.artifact_id
        except PsrmcpError as exc:
            if exc.code != ErrorCode.NOT_FOUND:
                raise
            # Treat as pulsar name: fetch and materialise a par artifact first.
            export = await catalog_tools._export(ctx, _export_input(inp.ephemeris), progress)
            par_path = ctx.store.lookup_id(export.artifact.artifact_id)
            par_artifact_id = export.artifact.artifact_id

    dm = inp.dm_override
    dm_source = "override"
    if dm is None and par_path is not None:
        m = _DM_LINE.search(par_path.read_text(encoding="utf-8", errors="replace"))
        if m:
            dm = float(m.group(1))
            dm_source = "ephemeris"
    if dm is None:
        # Last resort: the DM recorded in the input archive header.
        meta = await _vap_meta(ctx, source.resolved)
        dm = meta.get("archive_dm")
        dm_source = "input_header"
    if dm is None:
        raise PsrmcpError(
            ErrorCode.EPHEMERIS_MISMATCH,
            "无法确定 DM:ephemeris 无 DM 行,输入头也无 DM",
            hint="提供包含 DM 的 .par,或用 dm_override 显式指定",
        )

    warnings: list[WarningInfo] = []
    if inp.dm_override is not None:
        warnings.append(WarningInfo(
            code="W_DM_OVERRIDE",
            message=f"DM 显式覆盖为 {dm:.6f}(来源: {dm_source})",
            severity="warning",
        ))
    if inp.mode == "coherent" and source.resolved.suffix.lower() == ".fil":
        warnings.append(WarningInfo(
            code="W_COHERENT_UNAVAILABLE",
            message="filterbank 输入只能做非相干消色散;mode=coherent 被忽略",
            severity="warning",
        ))

    out_ext = "fits" if inp.output_format == "psrfits" else "ar"
    job_tmp = ctx.ws.new_job_tmp(f"dspsr_{int(time.time() * 1000)}")
    argv = dsa.dedisperse_cmd(
        binary,
        str(source.resolved),
        dm=dm,
        nbin=inp.nbin,
        nchan=inp.nchan,
        nsubint=inp.nsubint,
        out_prefix=str(job_tmp / "dedispersed"),
        out_ext=out_ext,
        threads=min(inp.threads, ctx.cfg.limits.max_threads) if inp.threads else None,
        ephemeris=str(par_path) if par_path is not None else None,
    )
    progress("dedispersing", 0.2)
    run = await ctx.runner.run(
        argv,
        cwd=job_tmp,
        env={"TEMPO2": _tempo2_runtime(ctx)},
        timeout_s=ctx.cfg.server.job_timeout_s,
        cancel=CancelHook(),
    )
    progress("writing", 0.8)
    produced = sorted(p for p in job_tmp.glob(f"*.{out_ext}") if p.is_file())
    if run.exit_code != 0 or not produced:
        log_id = log_artifact_from_stderr(ctx, job_key="dspsr", stderr=run.stderr_tail(), argv=argv)
        detail = f"退出码 {run.exit_code}" if run.exit_code != 0 else "退出码 0 但未产出文件"
        raise PsrmcpError(
            ErrorCode.PROCESS_FAILED,
            f"dspsr 失败({detail}): {run.stderr_tail(300)}",
            log_artifact_id=log_id,
        )
    log_id = log_artifact_from_stderr(ctx, job_key="dspsr", stderr=run.stderr_tail(), argv=argv)

    nsub = inp.nsubint or 0
    dims = Dimensions(nsubint=nsub, nchan=inp.nchan or 0, npol=0, nbin=inp.nbin or 0)
    meta = await _vap_meta(ctx, produced[0])
    if meta:
        dims = Dimensions(
            nsubint=int(meta.get("nsubint") or dims.nsubint),
            nchan=int(meta.get("nchan") or dims.nchan),
            npol=int(meta.get("npol") or 0),
            nbin=int(meta.get("nbin") or dims.nbin),
        )

    prov = make_provenance(
        tool="dedisperse",
        backend="dspsr",
        backend_version=dspsr_info.version,
        params=inp.model_dump(mode="json") | {"dm_resolved": dm, "dm_source": dm_source},
        started=started,
        duration_s=time.monotonic() - t0,
        command=argv,
    )
    ref = ctx.store.commit(
        src_path=produced[0],
        filename=produced[0].name,
        kind=ArtifactKind.ARCHIVE,
        format_name="PSRFITS" if out_ext == "fits" else "PSRARCHIVE",
        provenance=prov,
        parents=[p for p in (source.artifact_id, par_artifact_id) if p],
    )
    progress("complete", 1.0)
    return DedisperseResult(
        artifact=ref,
        dm_used=dm,
        dimensions=dims,
        duration_s=time.monotonic() - t0,
        log_artifact=log_id,
        warnings=warnings,
    )


async def _vap_meta(ctx: AppContext, path: Path) -> dict:
    from ..adapters import psrchive as psa

    try:
        binary = archive_tools._tool(ctx, "vap")
        run = await ctx.runner.run(psa.vap_cmd(binary, str(path)), timeout_s=120.0)
        return psa.parse_vap(run.stdout_text)
    except PsrmcpError:
        return {}


def _export_input(name: str):
    from ..models.inputs import EphemerisExportInput

    return EphemerisExportInput(pulsar=name)
