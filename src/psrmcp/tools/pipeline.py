"""pipeline_reduce (TOOLS.md 5.33): one-call composite reduction chain.

Composes the existing single-step tools — archive_inspect -> dedisperse ->
rfi_scan(/rfi_zap) -> profile_standardize -> profile_measure — by calling
their handlers in sequence and threading artifact IDs through. Per the
composite-tool rule (TOOLS.md 6): every completed step's artifact stays
available, per-step statuses are reported, and a failed run stops the chain
leaving the rest skipped — the same inputs can simply be retried.
"""

from __future__ import annotations

import time

from mcp.types import ToolAnnotations

from ..errors import ErrorCode, PsrmcpError
from ..models.common import ArtifactRef, WarningInfo
from ..models.inputs import (
    ArchiveInspectInput,
    DedisperseInput,
    PipelineReduceInput,
    ProfileMeasureInput,
    ProfileStandardizeInput,
    RfiAutoMask,
    RfiMask,
    RfiScanInput,
    RfiZapInput,
)
from ..models.results import (
    MeasureMetrics,
    PipelineReduceResult,
    PipelineStepReport,
    PipelineStepStatus,
)
from ._common import AppContext
from .archive import _inspect as _inspect_step
from .dedispersion import _run as _fold_step
from .profile import _measure as _measure_step
from .profile import _standardize as _standardize_step
from .registry import ToolRegistry, ToolSpec
from .rfi import _scan as _rfi_scan_step
from .rfi import _zap as _rfi_zap_step


def register(reg: ToolRegistry) -> None:
    reg.add(ToolSpec(
        name="pipeline_reduce",
        description="One-call reduction chain: inspect -> fold (dspsr, DM from pulsar name / "
                    "par / explicit dm) -> RFI (scan, optionally auto-zap) -> standard profile "
                    "-> measure. Every step reports its own status and artifact; completed "
                    "steps survive a failure so the same inputs can simply be retried. Long "
                    "task; supports async and cancellation.",
        input_model=PipelineReduceInput,
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False),
        handler=_reduce,
        long_task=True,
    ))


def _plan(inp: PipelineReduceInput) -> list[tuple[str, str]]:
    """The canonical step order with a skip reason for disabled steps."""
    plan: list[tuple[str, str]] = [("inspect", "")]
    plan.append(("fold", "" if inp.fold else "disabled: fold=false"))
    if inp.rfi_strategy == "none":
        plan += [("rfi_scan", "disabled: rfi_strategy=none"), ("rfi_zap", "disabled: rfi_strategy=none")]
    else:
        plan.append(("rfi_scan", ""))
        plan.append(("rfi_zap", "" if inp.rfi_strategy == "zap" else "disabled: rfi_strategy=scan"))
    plan.append(("standard", "" if inp.build_standard else "disabled: build_standard=false"))
    plan.append(("measure", "" if inp.measure else "disabled: measure=false"))
    return plan


async def _reduce(ctx: AppContext, inp: PipelineReduceInput, progress) -> PipelineReduceResult:
    steps: list[PipelineStepReport] = []
    warnings: list[WarningInfo] = []
    final_ref: ArtifactRef | None = None
    standard_ref: ArtifactRef | None = None
    rfi_report: str | None = None
    metrics: MeasureMetrics | None = None
    measure_plot: str | None = None
    current_archive: str = inp.archive
    failed = False

    async def run(name: str, call):
        """Run one chain step and append its report. Returns the step result
        or None on failure (which stops the chain)."""
        nonlocal failed
        t0 = time.monotonic()
        progress(name, min(0.05 + 0.9 * len(steps) / len(_plan(inp)), 0.95))
        try:
            res = await call()
        except PsrmcpError as exc:
            if exc.code == ErrorCode.CANCELLED:
                raise
            steps.append(PipelineStepReport(
                step=name, status=PipelineStepStatus.FAILED,
                duration_s=time.monotonic() - t0, detail=exc.message[:300]))
            warnings.append(WarningInfo(
                code="W_PIPELINE_STEP_FAILED",
                message=f"step '{name}' failed: {exc.message[:300]}; "
                        "completed steps remain available",
                severity="warning",
            ))
            failed = True
            return None
        except Exception as exc:  # noqa: BLE001 — never lose completed steps
            detail = str(exc)[:300] or exc.__class__.__name__
            steps.append(PipelineStepReport(
                step=name, status=PipelineStepStatus.FAILED,
                duration_s=time.monotonic() - t0, detail=detail))
            warnings.append(WarningInfo(
                code="W_PIPELINE_STEP_FAILED",
                message=f"step '{name}' failed: {detail}; completed steps remain available",
                severity="warning",
            ))
            failed = True
            return None
        return res

    def finish_step(name: str, res, *, artifact_id: str | None = None,
                    detail: str | None = None, t0: float = 0.0) -> None:
        steps.append(PipelineStepReport(
            step=name, status=PipelineStepStatus.OK, artifact_id=artifact_id,
            detail=detail, duration_s=time.monotonic() - t0))

    plan = _plan(inp)

    # 1. inspect — metadata + dimensions of the input
    t0 = time.monotonic()
    res = await run("inspect", lambda: _inspect_step(
        ctx, ArchiveInspectInput(archive=inp.archive, root=inp.root), progress))
    if failed:
        return _finish(True, None, None, rfi_report, metrics, measure_plot,
                       steps, warnings, plan, "inspect")
    d = res.dimensions
    finish_step("inspect", res,
                detail=f"nsub={d.nsubint} nchan={d.nchan} npol={d.npol} nbin={d.nbin}",
                t0=t0)

    # 2. fold (dspsr) — unless the input is already folded
    if inp.fold:
        t0 = time.monotonic()
        res = await run("fold", lambda: _fold_step(ctx, DedisperseInput(
            input=inp.archive, root=inp.root,
            ephemeris=inp.ephemeris or inp.pulsar_name or "",
            dm_override=inp.dm,
        ), progress))
        if failed:
            return _finish(False, None, None, rfi_report, metrics, measure_plot,
                           steps, warnings, plan, "fold")
        final_ref = res.artifact
        current_archive = res.artifact.artifact_id
        finish_step("fold", res, artifact_id=current_archive,
                    detail=f"DM={res.dm_used:.4f}", t0=t0)
    else:
        steps.append(PipelineStepReport(step="fold", status=PipelineStepStatus.SKIPPED,
                                        detail="disabled: fold=false"))

    # 3. RFI scan (+ optional auto-zap)
    if inp.rfi_strategy != "none":
        t0 = time.monotonic()
        res = await run("rfi_scan", lambda: _rfi_scan_step(ctx, RfiScanInput(
            archive=current_archive, method=inp.rfi_method,
            threshold_sigma=inp.rfi_threshold_sigma, generate_plot=True), progress))
        if failed:
            return _finish(False, final_ref, None, rfi_report, metrics, measure_plot,
                           steps, warnings, plan, "rfi_scan")
        rfi_report = res.plot_artifact
        mask = res.suggested_mask
        finish_step("rfi_scan", res, artifact_id=rfi_report,
                    detail=f"{mask.n_channels} suspicious channel(s) ({mask.fraction:.1%})",
                    t0=t0)

        if inp.rfi_strategy == "zap" and res.suggested_mask.n_channels == 0:
            steps.append(PipelineStepReport(
                step="rfi_zap", status=PipelineStepStatus.SKIPPED,
                detail="no suspicious channels flagged by rfi_scan"))
        elif inp.rfi_strategy == "zap":
            t0 = time.monotonic()
            res = await run("rfi_zap", lambda: _rfi_zap_step(ctx, RfiZapInput(
                archive=current_archive,
                mask=RfiMask(auto=RfiAutoMask(method=inp.rfi_method,
                                              threshold_sigma=inp.rfi_threshold_sigma)),
            ), progress))
            if failed:
                return _finish(False, final_ref, None, rfi_report, metrics, measure_plot,
                               steps, warnings, plan, "rfi_zap")
            final_ref = res.artifact
            current_archive = res.artifact.artifact_id
            finish_step("rfi_zap", res, artifact_id=current_archive,
                        detail=f"zapped {res.n_channels_zapped} channel(s)", t0=t0)
    else:
        steps += [PipelineStepReport(step="rfi_scan", status=PipelineStepStatus.SKIPPED,
                                     detail="disabled: rfi_strategy=none"),
                  PipelineStepReport(step="rfi_zap", status=PipelineStepStatus.SKIPPED,
                                     detail="disabled: rfi_strategy=none")]

    # 4. standard profile
    if inp.build_standard:
        t0 = time.monotonic()
        res = await run("standard", lambda: _standardize_step(ctx, ProfileStandardizeInput(
            archive=current_archive), progress))
        if failed:
            return _finish(False, final_ref, None, rfi_report, metrics, measure_plot,
                           steps, warnings, plan, "standard")
        standard_ref = res.artifact
        finish_step("standard", res, artifact_id=standard_ref.artifact_id,
                    detail=(f"S/N={res.snr:.1f}" if res.snr is not None else None), t0=t0)
    else:
        steps.append(PipelineStepReport(step="standard", status=PipelineStepStatus.SKIPPED,
                                        detail="disabled: build_standard=false"))

    # 5. measure
    if inp.measure:
        t0 = time.monotonic()
        res = await run("measure", lambda: _measure_step(ctx, ProfileMeasureInput(
            archive=current_archive, standard_profile=standard_ref.artifact_id
            if standard_ref else None, generate_plot=True), progress))
        if not failed:
            metrics = res.metrics
            measure_plot = res.plot_artifact
            finish_step("measure", res, artifact_id=measure_plot,
                        detail=(f"S/N={metrics.snr:.1f}" if metrics.snr is not None else None),
                        t0=t0)

    return _finish(not failed, final_ref, standard_ref, rfi_report, metrics,
                   measure_plot, steps, warnings, plan, None)


def _finish(succeeded: bool, final_ref, standard_ref, rfi_report, metrics,
            measure_plot, steps, warnings, plan, failed_at: str | None) -> PipelineReduceResult:
    # every planned step must appear exactly once: disabled steps carry their
    # reason, steps after a failure explain where the chain stopped
    seen = {s.step for s in steps}
    for name, reason in plan:
        if name in seen:
            continue
        steps.append(PipelineStepReport(
            step=name, status=PipelineStepStatus.SKIPPED,
            detail=reason or f"skipped: chain stopped at '{failed_at}'"))
    # canonical order = the plan order, regardless of when each report was made
    order = {name: i for i, (name, _) in enumerate(plan)}
    steps.sort(key=lambda s: order.get(s.step, len(order)))
    return PipelineReduceResult(
        succeeded=succeeded,
        final_archive=final_ref,
        standard_profile=standard_ref,
        rfi_report_artifact=rfi_report,
        measure_metrics=metrics,
        measure_plot_artifact=measure_plot,
        steps=steps,
        warnings=warnings,
    )
