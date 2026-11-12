"""single_pulse_analyze (TOOLS.md 5.18): psrspa single-pulse analysis."""

from __future__ import annotations

import shutil
import time
from pathlib import Path

from mcp.types import ToolAnnotations

from ..adapters import psrchive as psa
from ..errors import ErrorCode, PsrmcpError
from ..execution.runner import CancelHook
from ..execution.workspace import utcnow
from ..models.common import ArtifactKind, WarningInfo
from ..models.inputs import SinglePulseAnalyzeInput
from ..models.results import (
    MaxAmplitudeRow,
    SinglePulseAnalyzeResult,
    SinglePulseRow,
)
from ._common import AppContext, log_artifact_from_stderr, make_provenance
from .archive import _tool as _psrchive_tool
from .registry import ToolRegistry, ToolSpec


def register(reg: ToolRegistry) -> None:
    reg.add(ToolSpec(
        name="single_pulse_analyze",
        description="Analyse single pulses (psrspa): phase-resolved flux/polarisation histograms "
                    "(one per phase bin in the requested range, bundled into one text artifact), "
                    "maximum amplitude inside phase ranges, linear vs PA scatter data, or per-pulse "
                    "detection (giant-pulse search; mutually exclusive with the histogram modes). "
                    "The input archive is read-only; outputs are new text artifacts. Long task; "
                    "supports async and cancellation.",
        input_model=SinglePulseAnalyzeInput,
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
        handler=_analyze,
        long_task=True,
    ))


def _classify(name: str, prefix: str) -> str:
    """Map a produced file name to its kind: pulses / maxamp / histogram."""
    if name.startswith(f"{prefix}_maxamp"):
        return "maxamp"
    if name.startswith(f"{prefix}_pulses"):
        return "pulses"
    return "histogram"


async def _analyze(ctx: AppContext, inp: SinglePulseAnalyzeInput, progress) -> SinglePulseAnalyzeResult:
    started = utcnow()
    t0 = time.monotonic()
    psr_info = await ctx.backends.require("psrchive")
    binary = _psrchive_tool(ctx, "psrspa")
    resolved = ctx.resolver.resolve(inp.archive, root=inp.root, what="archive")
    std = None
    if inp.std_profile is not None:
        std = ctx.resolver.resolve(inp.std_profile, root=inp.root, what="standard profile")

    # psrspa writes <prefix>_<type>.dat into the process cwd; run on a copy
    # inside the job tmp so nothing leaks into shared directories
    job_tmp = ctx.ws.new_job_tmp(f"spa_{int(time.time() * 1000)}")
    local_src = job_tmp / "spa_src.ar"
    shutil.copy2(resolved.resolved, local_src)
    prefix = "spa"

    argv = psa.psrspa_cmd(
        binary, str(local_src),
        histograms=tuple(inp.histograms),
        find_max_amplitude=inp.find_max_amplitude,
        scatter_linear_pa=inp.scatter_linear_pa,
        find_pulses=inp.find_pulses,
        pulse_algorithm=inp.pulse_algorithm,
        min_pulse_bins=inp.min_pulse_bins,
        phase_ranges=tuple(inp.phase_ranges or ()),
        hist_bins=inp.hist_bins,
        flux_hist_max=inp.flux_hist_max,
        pol_threshold=inp.pol_threshold,
        log_scale=inp.log_scale,
        mean_offset=inp.mean_offset,
        std_profile=str(std.resolved) if std is not None else None,
        prefix=prefix,
    )
    progress("analysing", 0.3)
    run = await ctx.runner.run(
        argv, cwd=job_tmp, timeout_s=ctx.cfg.server.job_timeout_s, cancel=CancelHook()
    )
    if run.exit_code != 0:
        log_id = log_artifact_from_stderr(ctx, job_key="psrspa", stderr=run.stderr_tail(), argv=argv)
        raise PsrmcpError(
            ErrorCode.PROCESS_FAILED,
            f"psrspa failed (exit code {run.exit_code}): {run.stderr_tail(300)}",
            log_artifact_id=log_id,
        )

    produced = sorted(job_tmp.glob(f"{prefix}_*.dat"))
    warnings: list[WarningInfo] = []
    if not produced:
        warnings.append(WarningInfo(
            code="W_PSRSPA_NO_OUTPUT",
            message="psrspa produced no output files for the requested modes; "
                    "histogram modes need a psrspa build with GSL support",
            severity="warning",
        ))

    prov = make_provenance(
        tool="single_pulse_analyze",
        backend="psrspa",
        backend_version=psr_info.version,
        params=inp.model_dump(mode="json"),
        started=started,
        duration_s=time.monotonic() - t0,
        command=argv,
    )

    pulses_artifact = None
    pulses: list[SinglePulseRow] = []
    max_amplitudes: list[MaxAmplitudeRow] = []
    histograms_artifact = None
    parents = [resolved.artifact_id] if resolved.artifact_id else None

    # psrspa writes one histogram file PER PHASE BIN in the requested range
    # (411 files for a 0.3-0.7 range on nbin=1024) — committing each as its
    # own artifact would explode, so they are bundled into one text artifact
    histogram_paths: list[Path] = []
    for path in produced:
        kind = _classify(path.name, prefix)
        # parse BEFORE commit: commit renames the file into the artifact store
        if kind == "maxamp":
            max_amplitudes = [MaxAmplitudeRow(**r) for r in psa.parse_spa_maxamp(path.read_text(errors="replace"))]
            ctx.store.commit(
                src_path=path, filename=path.name, kind=ArtifactKind.OTHER,
                format_name="text", provenance=prov, parents=parents,
            )
        elif kind == "pulses":
            pulses = [SinglePulseRow(**r) for r in psa.parse_spa_pulses(path.read_text(errors="replace"))]
            pulses_artifact = ctx.store.commit(
                src_path=path, filename=path.name, kind=ArtifactKind.OTHER,
                format_name="text", provenance=prov, parents=parents,
            )
        else:
            histogram_paths.append(path)

    if histogram_paths:
        bundle_name = f"{prefix}_histograms.txt"
        bundle_path = job_tmp / bundle_name
        with bundle_path.open("w") as bundle:
            for path in histogram_paths:
                bundle.write(f"# >>> {path.name}\n")
                bundle.write(path.read_text(errors="replace"))
                if not path.read_text(errors="replace").endswith("\n"):
                    bundle.write("\n")
        histograms_artifact = ctx.store.commit(
            src_path=bundle_path, filename=bundle_name, kind=ArtifactKind.OTHER,
            format_name="text", provenance=prov, parents=parents,
        )

    if inp.find_pulses and pulses_artifact is not None and not pulses:
        warnings.append(WarningInfo(
            code="W_PSRSPA_PULSES_UNPARSED",
            message="the pulses output file was produced but no rows could be parsed from it",
            severity="warning",
        ))
    progress("complete", 1.0)
    return SinglePulseAnalyzeResult(
        histograms_artifact=histograms_artifact,
        n_histogram_files=len(histogram_paths),
        pulses_artifact=pulses_artifact,
        pulses=pulses,
        max_amplitudes=max_amplitudes,
        warnings=warnings,
    )
