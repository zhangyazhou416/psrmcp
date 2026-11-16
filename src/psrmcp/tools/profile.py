"""profile_standardize / profile_measure / profile_optimize / profile_smooth /
profile_stability / profile_pca (TOOLS.md 5.17-5.22)."""

from __future__ import annotations

import shutil
import time

from mcp.types import ToolAnnotations

from ..adapters import psrchive as psa
from ..errors import ErrorCode, PsrmcpError
from ..execution.runner import CancelHook
from ..execution.workspace import utcnow
from ..models.common import ArtifactKind, WarningInfo
from ..models.inputs import (
    ProfileMeasureInput,
    ProfileOptimizeInput,
    ProfilePcaInput,
    ProfileSmoothInput,
    ProfileStabilityInput,
    ProfileStandardizeInput,
)
from ..models.results import (
    MeasureMetrics,
    ProfileMeasureResult,
    ProfileOptimizeResult,
    ProfilePcaResult,
    ProfileSmoothResult,
    ProfileStabilityResult,
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
    reg.add(ToolSpec(
        name="profile_optimize",
        description="Search period and DM for the maximum profile S/N (pdmp). Reports the best period (ms) and DM with uncertainties as corrections to apply; the input archive is never modified. Long task; supports async and cancellation.",
        input_model=ProfileOptimizeInput,
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
        handler=_optimize,
        long_task=True,
    ))
    reg.add(ToolSpec(
        name="profile_smooth",
        description="Denoise a profile archive with psrsmooth's adaptive filters: sinc (Fourier-domain harmonic filtering, default) or wavelet (-W; family/order via wavelet_type, cutoff in sigmas). The denoised archive is a new artifact — a natural step before timing_fit/toa_extract. Long task; supports async and cancellation.",
        input_model=ProfileSmoothInput,
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False),
        handler=_smooth,
        long_task=True,
    ))
    reg.add(ToolSpec(
        name="profile_stability",
        description="Profile stability study (psrstability): archives are per-sub-integration chunks (e.g. from archive_split with per_subint=1); the first `turns` chunks form the running mean and every further chunk emits one averaged-profile artifact (n_profiles = n_archives - turns). Long task; supports async and cancellation.",
        input_model=ProfileStabilityInput,
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False),
        handler=_stability,
        long_task=True,
    ))
    reg.add(ToolSpec(
        name="profile_pca",
        description="Principal-component analysis of sub-integration profile differences (psrpca) against a standard profile: difference-profile and eigenvector archives, eigenvalues/decomposition/covariance tables. The archive needs multiple sub-integrations. Long task; supports async and cancellation.",
        input_model=ProfilePcaInput,
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False),
        handler=_pca,
        long_task=True,
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
        plot_artifact = await _plot_profile(ctx, resolved.resolved, inp.plot_type, psr_info.version, warnings)

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


async def _plot_profile(ctx: AppContext, path, plot_type: str, backend_version: str | None,
                        warnings: list[WarningInfo]) -> str | None:
    """Render the archive with psrplot's headless PNG device (-D <path>/PNG;
    contract verified 2026-11-15). PGPLOT file devices need no display, so
    this stays server-safe; computed charts (residuals, masks) keep using
    matplotlib. Degrades to a warning when rendering fails."""
    import time as _time

    from ..execution.workspace import utcnow as _now

    try:
        binary = _psrchive_tool(ctx, "psrplot")
        job_tmp = ctx.ws.new_job_tmp(f"psrplot_{_time.time_ns()}")
        png_path = job_tmp / "profile.png"
        argv = psa.psrplot_cmd(binary, str(path), str(png_path), plot_type)
        run = await ctx.runner.run(argv, cwd=job_tmp, timeout_s=300.0)
        if run.exit_code != 0 or not png_path.exists():
            raise RuntimeError(
                f"psrplot exit={run.exit_code}: {run.stderr_tail(200) or 'no PNG produced'}")
        ref = ctx.store.commit(
            src_path=png_path,
            filename="profile.png",
            kind=ArtifactKind.PLOT,
            format_name="png",
            provenance=make_provenance(
                tool="profile_measure",
                backend="psrplot",
                backend_version=backend_version,
                params={"plot_type": plot_type},
                started=_now(),
                duration_s=0.0,
                command=argv,
            ),
        )
        return ref.artifact_id
    except PsrmcpError as exc:
        warnings.append(WarningInfo(
            code="W_PLOT_UNAVAILABLE",
            message=f"profile plot unavailable: {exc.message}",
            severity="info",
        ))
        return None
    except Exception as exc:  # noqa: BLE001 — plotting must not fail the tool
        warnings.append(WarningInfo(
            code="W_PLOT_UNAVAILABLE",
            message=f"profile plot unavailable: {exc}",
            severity="info",
        ))
        return None


async def _optimize(ctx: AppContext, inp: ProfileOptimizeInput, progress) -> ProfileOptimizeResult:
    started = utcnow()
    t0 = time.monotonic()
    psr_info = await ctx.backends.require("psrchive")
    binary = _psrchive_tool(ctx, "pdmp")
    resolved = ctx.resolver.resolve(inp.archive, root=inp.root, what="archive")
    std = None
    if inp.standard_profile is not None:
        std = ctx.resolver.resolve(inp.standard_profile, root=inp.root, what="standard profile")

    argv = psa.pdmp_cmd(
        binary, str(resolved.resolved),
        dm_offset=inp.dm_offset, dm_half_range=inp.dm_half_range, dm_step=inp.dm_step,
        period_ms=inp.period_ms, period_offset_us=inp.period_offset_us,
        period_half_range_us=inp.period_half_range_us, period_step_us=inp.period_step_us,
        max_channels=inp.max_channels, max_subints=inp.max_subints,
        std_profile=str(std.resolved) if std is not None else None,
    )
    progress("searching", 0.3)
    run = await ctx.runner.run(
        argv, timeout_s=ctx.cfg.server.job_timeout_s, cancel=CancelHook()
    )
    if run.exit_code != 0:
        log_id = log_artifact_from_stderr(ctx, job_key="pdmp", stderr=run.stderr_tail(), argv=argv)
        raise PsrmcpError(
            ErrorCode.PROCESS_FAILED,
            f"pdmp failed (exit code {run.exit_code}): {run.stderr_tail(300)}",
            log_artifact_id=log_id,
        )

    parsed = psa.parse_pdmp(run.stdout_text)
    warnings: list[WarningInfo] = []
    if not parsed:
        warnings.append(WarningInfo(
            code="W_PDMP_UNPARSED",
            message="no best-fit values could be parsed from pdmp output; inspect the raw stdout via the log artifact of a re-run",
            severity="warning",
        ))
    prov = make_provenance(
        tool="profile_optimize",
        backend="pdmp",
        backend_version=psr_info.version,
        params=inp.model_dump(mode="json"),
        started=started,
        duration_s=time.monotonic() - t0,
        command=argv,
    )
    _ = prov
    return ProfileOptimizeResult(
        best_period_ms=parsed.get("best_period_ms"),
        period_error_ms=parsed.get("best_period_ms_error"),
        best_dm=parsed.get("best_dm"),
        dm_error=parsed.get("best_dm_error"),
        best_snr=parsed.get("best_snr"),
        warnings=warnings,
    )


async def _smooth(ctx: AppContext, inp: ProfileSmoothInput, progress) -> ProfileSmoothResult:
    started = utcnow()
    t0 = time.monotonic()
    psr_info = await ctx.backends.require("psrchive")
    binary = _psrchive_tool(ctx, "psrsmooth")
    resolved = ctx.resolver.resolve(inp.archive, root=inp.root, what="archive")

    # psrsmooth APPENDS ".sm" to the input name and is completely silent on
    # success (no stdout/stderr output at all) — run on a staged copy.
    job_tmp = ctx.ws.new_job_tmp(f"smooth_{time.time_ns()}")
    local_src = job_tmp / "smooth_src.ar"
    shutil.copy2(resolved.resolved, local_src)

    argv = psa.psrsmooth_cmd(
        binary, str(local_src),
        method=inp.method,
        wavelet_type=inp.wavelet_type,
        wavelet_cutoff=inp.wavelet_cutoff,
        normalize=inp.normalize,
    )
    progress("smoothing", 0.3)
    run = await ctx.runner.run(
        argv, cwd=job_tmp, timeout_s=ctx.cfg.server.job_timeout_s, cancel=CancelHook()
    )
    out_path = job_tmp / "smooth_src.ar.sm"
    if run.exit_code != 0 or not out_path.exists():
        log_id = log_artifact_from_stderr(ctx, job_key="smooth", stderr=run.stderr_tail(), argv=argv)
        raise PsrmcpError(
            ErrorCode.PROCESS_FAILED,
            f"psrsmooth produced no smoothed archive "
            f"(exit code {run.exit_code}): {run.stderr_tail(300)}",
            log_artifact_id=log_id,
        )

    prov = make_provenance(
        tool="profile_smooth",
        backend="psrsmooth",
        backend_version=psr_info.version,
        params=inp.model_dump(mode="json"),
        started=started,
        duration_s=time.monotonic() - t0,
        command=argv,
    )
    smoothed = ctx.store.commit(
        src_path=out_path,
        filename=(inp.output_name or "smoothed") + ".ar",
        kind=ArtifactKind.ARCHIVE,
        format_name="PSRARCHIVE",
        provenance=prov,
        parents=[resolved.artifact_id] if resolved.artifact_id else None,
    )
    progress("complete", 1.0)
    return ProfileSmoothResult(smoothed_archive=smoothed, method=inp.method)


async def _stability(ctx: AppContext, inp, progress):
    started = utcnow()
    t0 = time.monotonic()
    psr_info = await ctx.backends.require("psrchive")
    binary = _psrchive_tool(ctx, "psrstability")

    # psrstability aggregates one running mean per input FILE: stage every
    # chunk under a plain sequential name inside the job tmp
    job_tmp = ctx.ws.new_job_tmp(f"stab_{time.time_ns()}")
    out_dir = job_tmp / "out"
    out_dir.mkdir()
    staged: list[str] = []
    parents: list[str] = []
    for i, ref in enumerate(inp.archives):
        r = ctx.resolver.resolve(ref, root=inp.root, what="archive")
        local = job_tmp / f"chunk{i:04d}.ar"
        shutil.copy2(r.resolved, local)
        staged.append(str(local))
        if r.artifact_id:
            parents.append(r.artifact_id)

    argv = psa.psrstability_cmd(
        binary, staged, turns=inp.turns, out_dir=str(out_dir),
        form_invariant=inp.form_invariant,
        preserve_time_resolution=inp.preserve_time_resolution,
    )
    progress("accumulating", 0.4)
    run = await ctx.runner.run(
        argv, cwd=job_tmp, timeout_s=ctx.cfg.server.job_timeout_s, cancel=CancelHook()
    )
    chunks = sorted(out_dir.glob("profile_*.ar"))
    if run.exit_code != 0 or not chunks:
        expected = max(len(staged) - inp.turns, 0)
        log_id = log_artifact_from_stderr(ctx, job_key="stab", stderr=run.stderr_tail(), argv=argv)
        raise PsrmcpError(
            ErrorCode.PROCESS_FAILED,
            f"psrstability produced no averaged profiles "
            f"(expected {expected} from {len(staged)} chunks with turns="
            f"{inp.turns}; exit code {run.exit_code}): {run.stderr_tail(300)}",
            hint="n_profiles = n_archives - turns; provide more chunks than turns",
            log_artifact_id=log_id,
        )

    prov = make_provenance(
        tool="profile_stability",
        backend="psrstability",
        backend_version=psr_info.version,
        params=inp.model_dump(mode="json"),
        started=started,
        duration_s=time.monotonic() - t0,
        command=argv,
    )
    prefix = inp.output_name or "stable"
    profiles = [
        ctx.store.commit(
            src_path=chunk,
            filename=f"{prefix}.{chunk.stem}.ar",
            kind=ArtifactKind.ARCHIVE,
            format_name="PSRARCHIVE",
            provenance=prov,
            parents=parents or None,
        )
        for chunk in chunks
    ]
    progress("complete", 1.0)
    return ProfileStabilityResult(
        profiles=profiles, n_profiles=len(profiles), turns=inp.turns,
    )


_PCA_SUFFIX_KINDS = {
    "_diffs.ar": "archive", "_evecs.ar": "archive", "_rotated_scaled.ar": "archive",
    "_evals.dat": "text", "_decomposition.dat": "text",
    "_covariance.dat": "text", "_profiles.dat": "text",
}


async def _pca(ctx: AppContext, inp, progress):
    started = utcnow()
    t0 = time.monotonic()
    psr_info = await ctx.backends.require("psrchive")
    binary = _psrchive_tool(ctx, "psrpca")
    resolved = ctx.resolver.resolve(inp.archive, root=inp.root, what="archive")
    std = ctx.resolver.resolve(inp.standard_profile, root=inp.root, what="standard profile")

    # psrpca writes <prefix>_* files into the process cwd; run on a staged copy
    job_tmp = ctx.ws.new_job_tmp(f"pca_{time.time_ns()}")
    local_src = job_tmp / "pca_src.ar"
    shutil.copy2(resolved.resolved, local_src)
    local_std = job_tmp / "pca_std.ar"
    shutil.copy2(std.resolved, local_std)

    argv = psa.psrpca_cmd(binary, str(local_src), str(local_std))
    progress("decomposing", 0.4)
    run = await ctx.runner.run(
        argv, cwd=job_tmp, timeout_s=ctx.cfg.server.job_timeout_s, cancel=CancelHook()
    )
    produced = sorted(job_tmp.glob("pca_*"))
    # pca_src.ar / pca_std.ar are our staged copies, not products
    produced = [p for p in produced if p.name not in ("pca_src.ar", "pca_std.ar")]
    if run.exit_code != 0 or not produced:
        log_id = log_artifact_from_stderr(ctx, job_key="pca2", stderr=run.stderr_tail(), argv=argv)
        raise PsrmcpError(
            ErrorCode.PROCESS_FAILED,
            f"psrpca produced no output (exit code {run.exit_code}): {run.stderr_tail(300)}",
            log_artifact_id=log_id,
        )

    prov = make_provenance(
        tool="profile_pca",
        backend="psrpca",
        backend_version=psr_info.version,
        params=inp.model_dump(mode="json"),
        started=started,
        duration_s=time.monotonic() - t0,
        command=argv,
    )
    parents = [p for p in (resolved.artifact_id, std.artifact_id) if p]

    result = ProfilePcaResult()
    for path in produced:
        suffix = path.name[len("pca"):]
        kind = _PCA_SUFFIX_KINDS.get(suffix, "text")
        is_archive = kind == "archive"
        ref = ctx.store.commit(
            src_path=path,
            filename=path.name,
            kind=ArtifactKind.ARCHIVE if is_archive else ArtifactKind.OTHER,
            format_name="PSRARCHIVE" if is_archive else "text",
            provenance=prov,
            parents=parents or None,
        )
        if suffix == "_diffs.ar":
            result.diffs_archive = ref
        elif suffix == "_evecs.ar":
            result.evecs_archive = ref
        elif suffix == "_rotated_scaled.ar":
            result.rotated_scaled_archive = ref
        elif suffix == "_evals.dat":
            result.eigenvalues_artifact = ref.artifact_id
        elif suffix == "_decomposition.dat":
            result.decomposition_artifact = ref.artifact_id
        elif suffix == "_covariance.dat":
            result.covariance_artifact = ref.artifact_id
        elif suffix == "_profiles.dat":
            result.profiles_artifact = ref.artifact_id
    if result.diffs_archive is None:
        result.warnings.append(WarningInfo(
            code="W_PCA_NO_DIFFS",
            message="psrpca produced no difference-profile archive; "
                    "check that the archive has multiple sub-integrations",
            severity="warning",
        ))
    progress("complete", 1.0)
    return result
