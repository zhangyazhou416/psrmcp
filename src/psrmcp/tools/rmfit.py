"""rotation_measure_fit (TOOLS.md 5.19): rmfit rotation measure fitting."""

from __future__ import annotations

import shutil
import time

from mcp.types import ToolAnnotations

from ..adapters import psrchive as psa
from ..errors import ErrorCode, PsrmcpError
from ..execution.runner import CancelHook
from ..execution.workspace import utcnow
from ..models.common import ArtifactKind, WarningInfo
from ..models.inputs import RotationMeasureFitInput
from ..models.results import RotationMeasureFitResult
from ._common import AppContext, log_artifact_from_stderr, make_provenance
from .archive import _tool as _psrchive_tool
from .registry import ToolRegistry, ToolSpec


def register(reg: ToolRegistry) -> None:
    reg.add(ToolSpec(
        name="rotation_measure_fit",
        description="Fit the rotation measure (rmfit) of a calibrated, tscrunched, dedispersed "
                    "archive: brute-force search over trial RMs, iterative two-halves-of-band "
                    "refinement, or the quadratic (Noutsos+2008) method. Optionally writes a new "
                    "archive artifact with the per-sub-int auxiliary RM applied. Long task; "
                    "supports async and cancellation.",
        input_model=RotationMeasureFitInput,
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
        handler=_rm_fit,
        long_task=True,
    ))


async def _rm_fit(ctx: AppContext, inp: RotationMeasureFitInput, progress) -> RotationMeasureFitResult:
    started = utcnow()
    t0 = time.monotonic()
    psr_info = await ctx.backends.require("psrchive")
    binary = _psrchive_tool(ctx, "rmfit")
    resolved = ctx.resolver.resolve(inp.archive, root=inp.root, what="archive")

    # rmfit writes rmfit.out / <source>.data / the -x unload next to the
    # input file; run on a copy inside the job tmp to keep them contained
    job_tmp = ctx.ws.new_job_tmp(f"rmfit_{int(time.time() * 1000)}")
    local_src = job_tmp / "rm_src.ar"
    shutil.copy2(resolved.resolved, local_src)

    rm_range = (inp.rm_range[0], inp.rm_range[1], int(inp.rm_range[2])) if inp.rm_range else None
    argv = psa.rmfit_cmd(
        binary, str(local_src),
        brute_force=inp.brute_force,
        rm_range=rm_range,
        refine=inp.refine,
        quadratic_window=(inp.quadratic_window[0], inp.quadratic_window[1]) if inp.quadratic_window else None,
        quadratic_per_bin=(inp.quadratic_per_bin[0], inp.quadratic_per_bin[1]) if inp.quadratic_per_bin else None,
        threshold_sigma=inp.threshold_sigma,
        rm_max_upper=inp.rm_max_upper,
        rm_per_dm=inp.rm_per_dm,
        rm_initial=inp.rm_initial,
        save_rm_ext="rmfit" if inp.save_rm_archive else None,
    )
    progress("fitting", 0.3)
    run = await ctx.runner.run(
        argv, cwd=job_tmp, timeout_s=ctx.cfg.server.job_timeout_s, cancel=CancelHook()
    )
    if run.exit_code != 0:
        log_id = log_artifact_from_stderr(ctx, job_key="rmfit", stderr=run.stderr_tail(), argv=argv)
        raise PsrmcpError(
            ErrorCode.PROCESS_FAILED,
            f"rmfit failed (exit code {run.exit_code}): {run.stderr_tail(300)}",
            log_artifact_id=log_id,
        )

    # rmfit reports on both streams (Best RM on stdout, iteration chatter on
    # stderr); parse the combined text
    parsed = psa.parse_rmfit(run.stdout_text + "\n" + run.stderr_text)
    warnings: list[WarningInfo] = []
    if not parsed:
        warnings.append(WarningInfo(
            code="W_RMFIT_UNPARSED",
            message="no RM values could be parsed from rmfit output; inspect the log "
                    "artifact of a re-run with execution_mode=sync",
            severity="warning",
        ))

    prov = make_provenance(
        tool="rotation_measure_fit",
        backend="rmfit",
        backend_version=psr_info.version,
        params=inp.model_dump(mode="json"),
        started=started,
        duration_s=time.monotonic() - t0,
        command=argv,
    )
    parents = [resolved.artifact_id] if resolved.artifact_id else None

    rm_archive = None
    if inp.save_rm_archive:
        out_path = job_tmp / "rm_src.rmfit"
        if out_path.exists():
            rm_archive = ctx.store.commit(
                src_path=out_path,
                filename=(inp.output_name or "rm_calibrated") + ".ar",
                kind=ArtifactKind.ARCHIVE,
                format_name="PSRARCHIVE",
                provenance=prov,
                parents=parents,
            )
        else:
            warnings.append(WarningInfo(
                code="W_RMFIT_NO_ARCHIVE",
                message="save_rm_archive=true but rmfit did not write the RM-corrected archive",
                severity="warning",
            ))

    data_artifacts = []
    side_files = (sorted(job_tmp.glob("rmfit.out"))
                  + sorted(job_tmp.glob("rmfit_results.out"))   # quadratic method table
                  + sorted(job_tmp.glob("all_bestRMs.out"))     # quadratic per-bin RMs
                  + sorted(job_tmp.glob("*.data")))             # refine path: <source>.data
    for extra in side_files:
        data_artifacts.append(ctx.store.commit(
            src_path=extra,
            filename=extra.name,
            kind=ArtifactKind.OTHER,
            format_name="text",
            provenance=prov,
            parents=parents,
        ))

    progress("complete", 1.0)
    return RotationMeasureFitResult(
        best_rm=parsed.get("best_rm"),
        best_rm_error=parsed.get("best_rm_error"),
        search_rm=parsed.get("search_rm"),
        search_rm_error=parsed.get("search_rm_error"),
        refined_rm=parsed.get("refined_rm"),
        converged_iterations=parsed.get("converged_iterations"),
        chi2_reduced=parsed.get("chi2_reduced"),
        rm_archive=rm_archive,
        data_artifacts=data_artifacts,
        warnings=warnings,
    )
