"""profile_standardize / profile_measure (TOOLS.md 5.12-5.13)."""

from __future__ import annotations

import time

from mcp.types import ToolAnnotations

from ..adapters import psrchive as psa
from ..errors import ErrorCode, PsrmcpError
from ..execution.workspace import utcnow
from ..models.common import ArtifactKind, WarningInfo
from ..models.inputs import ProfileMeasureInput, ProfileStandardizeInput
from ..models.results import (
    MeasureMetrics,
    ProfileMeasureResult,
    ProfileStandardizeResult,
    WidthInfo,
)
from ._common import AppContext, log_artifact_from_stderr, make_provenance
from .archive import _f as _optf
from .archive import _tool as _psrchive_tool
from .registry import ToolRegistry, ToolSpec


def register(reg: ToolRegistry) -> None:
    reg.add(ToolSpec(
        name="profile_standardize",
        description="Build a standard profile artifact from an archive, for use by TOA extraction (pat).",
        input_model=ProfileStandardizeInput,
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False),
        handler=_standardize,
    ))
    reg.add(ToolSpec(
        name="profile_measure",
        description="Measure profile metrics from an archive: S/N, width, flux, and more. Read-only.",
        input_model=ProfileMeasureInput,
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
        handler=_measure,
    ))


async def _standardize(ctx: AppContext, inp: ProfileStandardizeInput, progress) -> ProfileStandardizeResult:
    started = utcnow()
    t0 = time.monotonic()
    psr_info = await ctx.backends.require("psrchive")
    # Observed: pas is an interactive "align to standard profile" program and
    # cannot batch-produce standard profiles; the batch path for building one
    # is pam -T (tscrunch to a single subint).
    binary = _psrchive_tool(ctx, "pam")
    resolved = ctx.resolver.resolve(inp.archive, root=inp.root, what="archive")

    warnings: list[WarningInfo] = []
    if inp.align != "profile" or inp.smooth:
        warnings.append(WarningInfo(
            code="W_FLAG_SKIPPED",
            message=f"align={inp.align}/smooth={inp.smooth} have no batch-equivalent flags (pas is interactive); not applied",
            severity="warning",
        ))

    job_tmp = ctx.ws.new_job_tmp(f"pam_{int(time.time() * 1000)}")
    # pam writes <input name>.<ext> into the process cwd; copy the input into
    # the job tmp first to isolate it
    import shutil

    local_src = job_tmp / "standard_src.ar"
    shutil.copy2(resolved.resolved, local_src)

    bin_factor = None
    if inp.nbin is not None:
        vap = await ctx.runner.run(
            psa.vap_cmd(_psrchive_tool(ctx, "vap"), str(local_src)), timeout_s=120.0
        )
        orig_nbin = int(psa.parse_vap(vap.stdout_text).get("nbin") or 0)
        if orig_nbin and inp.nbin < orig_nbin and orig_nbin % inp.nbin == 0:
            bin_factor = orig_nbin // inp.nbin
        else:
            warnings.append(WarningInfo(
                code="W_FLAG_SKIPPED",
                message=f"nbin={inp.nbin} is not reachable via an integral-factor bin scrunch (original nbin={orig_nbin or 'unknown'}); keeping the original resolution",
                severity="warning",
            ))

    argv = psa.pam_cmd(binary, str(local_src), "std", bin_factor=bin_factor, tscrunch=True)
    progress("standardizing", 0.4)
    run = await ctx.runner.run(argv, cwd=job_tmp, timeout_s=1800.0)
    out_path = job_tmp / "standard_src.std"
    if run.exit_code != 0 or not out_path.exists():
        log_id = log_artifact_from_stderr(ctx, job_key="pam", stderr=run.stderr_tail(), argv=argv)
        raise PsrmcpError(
            ErrorCode.PROCESS_FAILED,
            f"pam failed (exit code {run.exit_code}): {run.stderr_tail(300)}",
            log_artifact_id=log_id,
        )

    nbin = inp.nbin
    snr = None
    try:
        stat_binary = _psrchive_tool(ctx, "psrstat")
        stat = await ctx.runner.run(
            psa.psrstat_cmd(stat_binary, str(out_path), ["snr"]), timeout_s=120.0
        )
        snr = _optf(psa.parse_psrstat(stat.stdout_text).get("snr"))
    except PsrmcpError:
        pass
    if nbin is None:
        from .dedispersion import _vap_meta

        meta = await _vap_meta(ctx, out_path)
        nbin = int(meta.get("nbin") or 0) or None
    if nbin is None:
        nbin = 1

    prov = make_provenance(
        tool="profile_standardize",
        backend="pam",
        backend_version=psr_info.version,
        params=inp.model_dump(mode="json"),
        started=started,
        duration_s=time.monotonic() - t0,
        command=argv,
    )
    ref = ctx.store.commit(
        src_path=out_path,
        filename="standard.ar",
        kind=ArtifactKind.PROFILE,
        format_name="PSRARCHIVE",
        provenance=prov,
        parents=[resolved.artifact_id] if resolved.artifact_id else None,
    )
    progress("complete", 1.0)
    return ProfileStandardizeResult(artifact=ref, nbin=nbin, snr=snr, warnings=warnings)


async def _measure(ctx: AppContext, inp: ProfileMeasureInput, progress) -> ProfileMeasureResult:
    started = utcnow()
    t0 = time.monotonic()
    psr_info = await ctx.backends.require("psrchive")
    resolved = ctx.resolver.resolve(inp.archive, root=inp.root, what="archive")
    warnings: list[WarningInfo] = []

    expressions: list[str] = []
    metric_by_index: list[str] = []
    if "snr" in inp.metrics:
        expressions.append("snr")
        metric_by_index.append("snr")
    if "width" in inp.metrics:
        expressions.append("w50")
        metric_by_index.append("w50")

    stats_binary = _psrchive_tool(ctx, "psrstat")
    snr = width = None
    if expressions:
        progress("measuring", 0.4)
        try:
            run = await ctx.runner.run(
                psa.psrstat_cmd(stats_binary, str(resolved.resolved), expressions), timeout_s=120.0
            )
            parsed = psa.parse_psrstat(run.stdout_text)
            for metric in metric_by_index:
                if metric == "snr":
                    snr = _optf(parsed.get("snr"))
                else:
                    w = _optf(parsed.get(metric))
                    if w is not None:
                        width = WidthInfo(fwhm_bins=w)
        except PsrmcpError as exc:
            warnings.append(WarningInfo(code="W_MEASURE_FAILED", message=str(exc.message), severity="warning"))

    dm = None
    if "dm" in inp.metrics:
        from .dedispersion import _vap_meta

        meta = await _vap_meta(ctx, resolved.resolved)
        dm = _optf(meta.get("archive_dm"))

    if "flux" in inp.metrics:
        warnings.append(WarningInfo(
            code="W_FLUX_UNCALIBRATED",
            message="flux calibration requires a calibrator; flux_mjy is not provided yet",
            severity="warning",
        ))
    if "polarization" in inp.metrics:
        warnings.append(WarningInfo(
            code="W_POLARIZATION_SKIPPED",
            message="polarization parameters depend on pac calibration; deferred to a later release",
            severity="warning",
        ))

    plot_artifact = None
    if inp.generate_plot:
        plot_artifact = _plot_profile(ctx, resolved.resolved, inp.plot_type, warnings)

    prov = make_provenance(
        tool="profile_measure",
        backend="psrstat",
        backend_version=psr_info.version,
        params=inp.model_dump(mode="json"),
        started=started,
        duration_s=time.monotonic() - t0,
    )
    _ = prov
    return ProfileMeasureResult(
        metrics=MeasureMetrics(snr=snr, width=width, flux_mjy=None, polarization=None, dm=dm),
        plot_artifact=plot_artifact,
        warnings=warnings,
    )


def _plot_profile(ctx: AppContext, path, plot_type: str, warnings: list[WarningInfo]) -> str | None:
    # TODO(contract): psrplot ascii export + parser gets backfilled with the
    # toolchain; until then profile plots degrade to a warning.
    try:
        raise RuntimeError("psrplot ascii output parsing awaits contract-test backfill")
    except Exception as exc:
        warnings.append(WarningInfo(
            code="W_PLOT_UNAVAILABLE",
            message=f"profile plots are unavailable ({exc}); timing/RFI plots are unaffected",
            severity="info",
        ))
        return None
