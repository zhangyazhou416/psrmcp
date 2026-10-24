"""toa_extract / timing_fit / timing_diagnose (TOOLS.md 5.14-5.16)."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from mcp.types import ToolAnnotations

from ..adapters import pint as pint_bridge
from ..adapters import tempo2 as t2a
from ..errors import ErrorCode, PsrmcpError
from ..execution.runner import CancelHook
from ..execution.workspace import utcnow
from ..models.common import ArtifactKind, WarningInfo
from ..models.inputs import TimingDiagnoseInput, TimingFitInput, ToaExtractInput
from ..models.results import (
    DiagnoseSummary,
    Finding,
    FitStats,
    ParameterRow,
    TimingDiagnoseResult,
    TimingFitResult,
    ToaExtractResult,
    ToaSnrStats,
)
from ._common import AppContext, log_artifact_from_stderr, make_provenance
from .registry import ToolRegistry, ToolSpec
from .archive import _tool as _psrchive_tool

# Tempo2-format TOA line: tokens containing MJD (50000-79999) and freq (100-100000).
_TOA_SPLIT = re.compile(r"\s+")


def register(reg: ToolRegistry) -> None:
    reg.add(ToolSpec(
        name="toa_extract",
        description="用标准轮廓从 archive 提取 TOA(pat),生成 TEMPO 格式 .tim 产物。长任务。",
        input_model=ToaExtractInput,
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False),
        handler=_toa,
        long_task=True,
    ))
    reg.add(ToolSpec(
        name="timing_fit",
        description="PINT/tempo2 计时拟合:par+tim -> 新 par、拟合统计、残差与残差图。长任务。",
        input_model=TimingFitInput,
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False),
        handler=_fit,
        long_task=True,
    ))
    reg.add(ToolSpec(
        name="timing_diagnose",
        description="对 par+tim 做残差与模型诊断(离群点/DM/JUMP/分布),不重新拟合。只读。",
        input_model=TimingDiagnoseInput,
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
        handler=_diagnose,
    ))


def _resolve_par_tim(ctx: AppContext, par: str, tim: str, root: str | None) -> tuple[Path, Path, list[str]]:
    """Resolve par/tim refs; returns paths plus artifact ids (for provenance parents)."""
    ids: list[str] = []

    def one(ref: str) -> Path:
        if ref.startswith("art_"):
            p = ctx.store.lookup_id(ref)
            if p is None:
                raise PsrmcpError(ErrorCode.NOT_FOUND, f"未知 artifact: {ref}")
            ids.append(ref)
            return p
        return ctx.resolver.resolve(ref, root=root, what="par/tim").resolved

    return one(par), one(tim), ids


def _parse_toa_lines(text: str) -> list[dict]:
    """Defensive parse of tempo2-format TOA lines.

    Returns dicts with mjd / freq_mhz / amp (best-effort) plus the raw line so
    re-serialization never loses observatory/flag columns pat emitted.
    """
    toas: list[dict] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("FORMAT", "#", "MODE")):
            continue
        mjd = freq = amp = None
        for tok in _TOA_SPLIT.split(stripped):
            try:
                v = float(tok)
            except ValueError:
                continue
            # MJD: fractional value in a plausible epoch window.
            if mjd is None and 40000.0 < v < 80000.0 and "." in tok:
                mjd = v
                continue
            # Observing frequency: fractional MHz in a radio window.
            if freq is None and 100.0 < v < 100000.0 and abs(v - round(v)) > 1e-6:
                freq = v
                continue
            # Uncertainty/amp-ish column: last small positive fallback.
            if amp is None and 0.0 < v < 1e5:
                amp = v
        if mjd is not None:
            toas.append({"mjd": mjd, "freq_mhz": freq, "amp": amp, "raw": stripped})
    return toas


def _filter_toas(toas: list[dict], inp: ToaExtractInput) -> list[dict]:
    kept: list[dict] = []
    for t in toas:
        if inp.mjd_range is not None and not (inp.mjd_range[0] <= t["mjd"] <= inp.mjd_range[1]):
            continue
        if (
            inp.freq_range_mhz is not None
            and t["freq_mhz"] is not None
            and not (inp.freq_range_mhz[0] <= t["freq_mhz"] <= inp.freq_range_mhz[1])
        ):
            continue
        if inp.min_snr > 0:
            # TODO(contract): pat TOA columns do not carry SNR directly; only
            # apply the floor when an amp-like column was confidently parsed.
            if t["amp"] is None or t["amp"] < inp.min_snr:
                continue
        kept.append(t)
    return kept


async def _toa(ctx: AppContext, inp: ToaExtractInput, progress) -> ToaExtractResult:  # noqa: ANN001
    started = utcnow()
    t0 = time.monotonic()
    psr_info = await ctx.backends.require("psrchive")
    binary = _psrchive_tool(ctx, "pat")
    resolved = ctx.resolver.resolve(inp.archive, root=inp.root, what="archive")
    std = ctx.resolver.resolve(inp.standard_profile, root=inp.root, what="standard_profile")

    from ..adapters.psrchive import pat_cmd

    argv = pat_cmd(binary, str(resolved.resolved), str(std.resolved), algorithm=inp.algorithm)
    progress("extracting", 0.2)
    job_tmp = ctx.ws.new_job_tmp(f"pat_{int(time.time() * 1000)}")
    run = await ctx.runner.run(
        argv, cwd=job_tmp, timeout_s=ctx.cfg.server.job_timeout_s, cancel=CancelHook()
    )
    if run.exit_code != 0:
        log_id = log_artifact_from_stderr(ctx, job_key="pat", stderr=run.stderr_tail(), argv=argv)
        raise PsrmcpError(
            ErrorCode.PROCESS_FAILED,
            f"pat 失败(退出码 {run.exit_code}): {run.stderr_tail(300)}",
            hint="确认 archive 与标准轮廓格式一致(TOOL contract 测试回填旗标)",
            log_artifact_id=log_id,
        )

    toas = _filter_toas(_parse_toa_lines(run.stdout_text), inp)
    tim_text = "FORMAT 1\n" + "".join(t["raw"] + "\n" for t in toas)
    tim_path = job_tmp / "toas.tim"
    tim_path.write_text(tim_text, encoding="utf-8")

    prov = make_provenance(
        tool="toa_extract",
        backend="pat",
        backend_version=psr_info.version,
        params=inp.model_dump(mode="json"),
        started=started,
        duration_s=time.monotonic() - t0,
        command=argv,
    )
    ref = ctx.store.commit(
        src_path=tim_path,
        filename=(inp.output_name or "toas") + ".tim",
        kind=ArtifactKind.TIM,
        format_name="tempo2",
        provenance=prov,
        parents=[p for p in (resolved.artifact_id, std.artifact_id) if p],
    )
    amps = sorted(t["amp"] for t in toas if t["amp"] is not None)
    snr_stats = (
        ToaSnrStats(min=amps[0], median=amps[len(amps) // 2], max=amps[-1]) if amps else None
    )
    mjds = [t["mjd"] for t in toas]
    progress("complete", 1.0)
    return ToaExtractResult(
        tim_artifact=ref,
        n_toas=len(toas),
        mjd_range=[min(mjds), max(mjds)] if mjds else None,
        snr=snr_stats,
        warnings=[],
    )


def _stats_from_payload(payload: dict, warnings: list) -> FitStats:  # noqa: ANN001
    chi2 = payload.get("chi2")
    if chi2 is None:
        warnings.append(WarningInfo(code="W_FIT_CHI2_UNAVAILABLE", message="worker 未返回有限 chi2,记 0"))
        chi2 = 0.0
    dof = int(payload.get("dof") or 0)
    red = payload.get("reduced_chi2")
    return FitStats(
        n_toas=int(payload.get("n_toas") or 0),
        chi2=float(chi2),
        dof=dof,
        reduced_chi2=float(red if red is not None else 0.0),
        rms_us=float(payload.get("rms_us") or 0.0),
        wrms_us=float(payload.get("wrms_us") or 0.0),
    )


def _winfo(code: str, message: str) -> WarningInfo:
    return WarningInfo(code=code, message=message)


async def _fit_pint(ctx: AppContext, inp: TimingFitInput, par_p: Path, tim_p: Path,
                    job_tmp: Path, warnings: list):  # noqa: ANN001
    """Run the PINT worker; returns (resp, argv)."""
    await ctx.backends.require("pint")
    out_par = job_tmp / "postfit.par"
    request = {
        "op": "fit",
        "par": str(par_p),
        "tim": str(tim_p),
        "out_par": str(out_par),
        "fit_params": inp.fit_params,
        "weighting": inp.weighting,
        "max_iterations": inp.max_iterations,
    }
    resp = await pint_bridge.call_worker(
        ctx.runner, request, cwd=job_tmp, python_exe=ctx.python_exe,
        timeout_s=ctx.cfg.server.job_timeout_s,
    )
    for w in resp.get("warnings", []):
        warnings.append(_winfo("W_FIT_WARNING", w))
    if inp.weighting == "uniform":
        warnings.append(_winfo("W_UNIFORM_WEIGHTING", "uniform 权重:所有 TOA 误差设为中位数,统计意义不同于 1/sigma^2"))
    return resp, out_par, ["pint-worker", "op=fit"]


async def _fit_tempo2(ctx: AppContext, inp: TimingFitInput, par_p: Path, tim_p: Path,
                      job_tmp: Path, warnings: list):  # noqa: ANN001
    """Run tempo2 -resid 2; returns (stats, postfit_par_path, argv). TODO(contract) flags."""
    await ctx.backends.require("tempo2")
    binary = ctx.backends.resolve("tempo2")
    argv = t2a.tempo2_fit_cmd(binary, str(par_p), str(tim_p))
    run = await ctx.runner.run(
        argv, cwd=job_tmp, timeout_s=ctx.cfg.server.job_timeout_s, cancel=CancelHook()
    )
    if run.exit_code != 0:
        log_id = log_artifact_from_stderr(ctx, job_key="tempo2", stderr=run.stderr_tail(), argv=argv)
        raise PsrmcpError(
            ErrorCode.PROCESS_FAILED,
            f"tempo2 失败(退出码 {run.exit_code}): {run.stderr_tail(300)}",
            log_artifact_id=log_id,
        )
    stats = t2a.parse_tempo2_output(run.stdout_text)
    warnings.append(_winfo(
        "W_TEMPO2_CONTRACT",
        "tempo2 旗标/输出解析为 TODO(contract),统计字段为最佳猜测;推荐 backend=pint 获得完整结果",
    ))
    postfit = job_tmp / "tempo2.par"  # TODO(contract): tempo2 writes tempo2.par in cwd
    if not postfit.is_file():
        postfit = job_tmp / "postfit.par"
        postfit.write_text(par_p.read_text(encoding="utf-8"), encoding="utf-8")
        warnings.append(_winfo("W_PAR_UNCHANGED", "tempo2 未产出 postfit par,返回输入 par 原样"))
    return stats, postfit, argv


async def _fit(ctx: AppContext, inp: TimingFitInput, progress) -> TimingFitResult:  # noqa: ANN001
    started = utcnow()
    t0 = time.monotonic()
    par_p, tim_p, parent_ids = _resolve_par_tim(ctx, inp.par, inp.tim, inp.root)
    warnings: list[WarningInfo] = []
    job_tmp = ctx.ws.new_job_tmp(f"fit_{int(time.time() * 1000)}")

    progress("fitting", 0.3)
    if inp.backend == "tempo2":
        stats_raw, postfit, argv = await _fit_tempo2(ctx, inp, par_p, tim_p, job_tmp, warnings)
        payload = {
            "n_toas": stats_raw.get("n_toas", 0), "chi2": stats_raw.get("chi2"),
            "dof": stats_raw.get("dof"), "reduced_chi2": None,
            "rms_us": stats_raw.get("rms_us"), "wrms_us": stats_raw.get("rms_us"),
            "series": None, "parameters": [],
        }
    else:
        payload, postfit, argv = await _fit_pint(ctx, inp, par_p, tim_p, job_tmp, warnings)

    stats = _stats_from_payload(payload, warnings)
    backend_info = await ctx.backends.get(inp.backend)
    prov = make_provenance(
        tool="timing_fit",
        backend=inp.backend,
        backend_version=backend_info.version if backend_info else None,
        params=inp.model_dump(mode="json"),
        started=started,
        duration_s=time.monotonic() - t0,
        command=argv,
    )

    base = inp.output_name or "postfit"
    par_ref = ctx.store.commit(
        src_path=postfit,
        filename=base + ".par",
        kind=ArtifactKind.PAR,
        format_name=inp.backend,
        provenance=prov,
        parents=parent_ids,
    )

    # Parameters table (PINT only; tempo2 params are TODO(contract)).
    parameters = [
        ParameterRow(
            name=p["name"], value=float(p["value"]), uncertainty=p.get("uncertainty"),
            units=p.get("units"), fitted=bool(p.get("fitted", False)),
        )
        for p in payload.get("parameters", [])
        if p.get("value") is not None
    ]

    # Residuals artifact + plot (PINT only).
    residuals_id = None
    plot_ids: list[str] = []
    series = payload.get("series")
    if series and series.get("mjd"):
        residuals_id = ctx.store.commit_text(
            text=json.dumps(series, ensure_ascii=False),
            filename=base + "_residuals.json",
            kind=ArtifactKind.RESIDUALS,
            format_name="json",
            provenance=prov,
            parents=[par_ref.artifact_id],
        ).artifact_id
        if inp.generate_plot:
            png = job_tmp / (base + "_residuals.png")
            try:
                from ..plotting.profile_plots import plot_residuals

                plot_residuals(series, png)
                plot_ids.append(ctx.store.commit(
                    src_path=png,
                    filename=png.name,
                    kind=ArtifactKind.PLOT,
                    format_name="png",
                    provenance=prov,
                    parents=[par_ref.artifact_id],
                ).artifact_id)
            except Exception as exc:  # plotting is best-effort, never fatal
                warnings.append(_winfo("W_PLOT_FAILED", f"残差图生成失败: {exc}"))
    elif inp.backend == "tempo2":
        resid_files = sorted(job_tmp.glob("resid2*"))  # TODO(contract): -resid 2 naming
        if resid_files:
            residuals_id = ctx.store.commit(
                src_path=resid_files[0],
                filename=resid_files[0].name,
                kind=ArtifactKind.RESIDUALS,
                format_name="text",
                provenance=prov,
                parents=[par_ref.artifact_id],
            ).artifact_id
        else:
            warnings.append(_winfo("W_RESIDUALS_UNAVAILABLE", "tempo2 未产出 resid2 文件"))

    progress("complete", 1.0)
    return TimingFitResult(
        par_artifact=par_ref,
        fit=stats,
        parameters=parameters,
        residuals_artifact=residuals_id,
        plot_artifacts=plot_ids,
        warnings=warnings,
    )


async def _diagnose(ctx: AppContext, inp: TimingDiagnoseInput, progress) -> TimingDiagnoseResult:  # noqa: ANN001
    started = utcnow()
    t0 = time.monotonic()
    pint_info = await ctx.backends.require("pint")
    par_p, tim_p, _parent_ids = _resolve_par_tim(ctx, inp.par, inp.tim, inp.root)
    warnings: list[WarningInfo] = []

    progress("diagnosing", 0.3)
    job_tmp = ctx.ws.new_job_tmp(f"diag_{int(time.time() * 1000)}")
    request = {
        "op": "diagnose",
        "par": str(par_p),
        "tim": str(tim_p),
        "checks": [c.value for c in inp.checks],
        "outlier_sigma": inp.outlier_sigma,
    }
    resp = await pint_bridge.call_worker(
        ctx.runner, request, cwd=job_tmp, python_exe=ctx.python_exe,
        timeout_s=ctx.cfg.server.job_timeout_s,
    )

    summary = DiagnoseSummary(
        n_toas=int(resp["summary"].get("n_toas") or 0),
        reduced_chi2=resp["summary"].get("reduced_chi2"),
        rms_us=resp["summary"].get("rms_us"),
        n_outliers=int(resp["summary"].get("n_outliers") or 0),
    )
    findings = [
        Finding(
            code=f["code"], severity=f["severity"], message=f["message"],
            details=f.get("details") or {},
        )
        for f in resp.get("findings", [])
    ]
    recommendations = [str(r) for r in resp.get("recommendations", [])]

    plots: list[str] = []
    series = resp.get("series")
    if inp.generate_plot and series and series.get("mjd"):
        png = job_tmp / "diagnose_residuals.png"
        try:
            from ..plotting.profile_plots import plot_residuals

            plot_residuals(series, png)
            prov = make_provenance(
                tool="timing_diagnose",
                backend="pint",
                backend_version=pint_info.version,
                params=inp.model_dump(mode="json"),
                started=started,
                duration_s=time.monotonic() - t0,
                command=None,
            )
            plots.append(ctx.store.commit(
                src_path=png,
                filename=png.name,
                kind=ArtifactKind.PLOT,
                format_name="png",
                provenance=prov,
                parents=_parent_ids,
            ).artifact_id)
        except Exception as exc:  # plotting is best-effort, never fatal
            warnings.append(_winfo("W_PLOT_FAILED", f"诊断图生成失败: {exc}"))

    progress("complete", 1.0)
    return TimingDiagnoseResult(
        summary=summary,
        findings=findings,
        plots=plots,
        recommendations=recommendations,
        warnings=warnings,
    )
