"""Calibration tools (TOOLS.md 5.20/5.21): pac polarization_calibrate + calibrator_database."""

from __future__ import annotations

import re
import shutil
import time
from collections import Counter
from pathlib import Path

from mcp.types import ToolAnnotations

from ..adapters import psrchive as psa
from ..errors import ErrorCode, PsrmcpError
from ..execution.runner import CancelHook
from ..execution.workspace import utcnow
from ..models.common import ArtifactKind, WarningInfo
from ..models.inputs import CalibratorDatabaseInput, PolarizationCalibrateInput
from ..models.results import (
    CalibratorDatabaseEntry,
    CalibratorDatabaseResult,
    PolarizationCalibrateResult,
)
from ._common import AppContext, log_artifact_from_stderr, make_provenance
from .archive import _tool as _psrchive_tool
from .registry import ToolRegistry, ToolSpec

# pac's database scan identifies calibrator files by extension (its built-in
# defaults are cf/pcal/fcal/pfit; we add every extension of the supplied
# files via -u). "txt" is reserved by pac for the database summary itself.
_EXT_OK = re.compile(r"[A-Za-z0-9]{1,16}")


def register(reg: ToolRegistry) -> None:
    reg.add(ToolSpec(
        name="polarization_calibrate",
        description="Polarimetrically (and optionally flux) calibrate a pulsar archive with pac: "
                    "match calibrator observations (PolnCal / FluxCal-On/Off scans) against the "
                    "observation, or apply a pcm/pacv model solution directly. Models: SingleAxis "
                    "(default), Polar, complete Reception. Matching criteria (time/coordinates/"
                    "instrument/frequency/bandwidth) can only be relaxed, not tightened. The input "
                    "archive is read-only; the calibrated archive is a new artifact. Long task; "
                    "supports async and cancellation.",
        input_model=PolarizationCalibrateInput,
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False),
        handler=_calibrate,
        long_task=True,
    ))
    reg.add(ToolSpec(
        name="calibrator_database",
        description="Build a pac calibrator database summary (database.txt artifact) from "
                    "calibrator archive scans: entries are indexed by header metadata (time, "
                    "position, frequency, receiver) so pac can match them with pulsar "
                    "observations. Entries are parsed and returned inline. pac identifies "
                    "calibrator files by extension, so every listed file must have one. Long "
                    "task; supports async and cancellation.",
        input_model=CalibratorDatabaseInput,
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False),
        handler=_database,
        long_task=True,
    ))


def _copy_calibrators(ctx: AppContext, refs: list[str], root: str | None,
                      cals_dir: Path) -> tuple[list[str], list[str]]:
    """Resolve + copy calibrator files into cals_dir; return the set of file
    extensions (for pac's -u scan option) and the artifact ids of the
    resolved calibrators (for provenance parentage)."""
    extensions: list[str] = []
    parent_ids: list[str] = []
    for ref in refs:
        r = ctx.resolver.resolve(ref, root=root, what="calibrator")
        src = Path(r.resolved)
        ext = src.suffix.lstrip(".")
        if not ext or not _EXT_OK.fullmatch(ext) or ext == "txt":
            raise PsrmcpError(
                ErrorCode.INVALID_INPUT,
                f"calibrator file has no usable extension: {ref!r} "
                "(pac's database scan matches calibrator files by extension)",
            )
        if ext not in extensions:
            extensions.append(ext)
        if r.artifact_id:
            parent_ids.append(r.artifact_id)
        shutil.copy2(src, cals_dir / src.name)
    return extensions, parent_ids


async def _calibrate(ctx: AppContext, inp: PolarizationCalibrateInput, progress) -> PolarizationCalibrateResult:
    started = utcnow()
    t0 = time.monotonic()
    psr_info = await ctx.backends.require("psrchive")
    binary = _psrchive_tool(ctx, "pac")
    resolved = ctx.resolver.resolve(inp.archive, root=inp.root, what="archive")

    # pac writes the calibrated archive as <input-basename>.calib[.P] next to
    # the input file; run on a copy inside the job tmp to keep it contained
    job_tmp = ctx.ws.new_job_tmp(f"pac_{time.time_ns()}")
    local_src = job_tmp / "pac_src.ar"
    shutil.copy2(resolved.resolved, local_src)

    cals_dir: str | None = None
    extensions: list[str] | None = None
    model_file: str | None = None
    parents: list[str] = []
    if resolved.artifact_id:
        parents.append(resolved.artifact_id)
    if inp.calibrators:
        cals = job_tmp / "cals"
        cals.mkdir()
        extensions, cal_parents = _copy_calibrators(ctx, inp.calibrators, inp.root, cals)
        parents.extend(cal_parents)
        cals_dir = str(cals)
    else:
        r = ctx.resolver.resolve(inp.model, root=inp.root, what="model solution")
        local_model = job_tmp / ("model_" + Path(r.resolved).name)
        shutil.copy2(r.resolved, local_model)
        model_file = str(local_model)
        if r.artifact_id:
            parents.append(r.artifact_id)

    argv = psa.pac_calibrate_cmd(
        binary, str(local_src),
        cals_dir=cals_dir,
        extensions=extensions,
        model_file=model_file,
        model_type=inp.model_type,
        flux_calibration=inp.flux_calibration,
        match_time=inp.match_time,
        match_coordinates=inp.match_coordinates,
        match_instrument=inp.match_instrument,
        match_frequency=inp.match_frequency,
        match_bandwidth=inp.match_bandwidth,
        allow_opposite_sideband=inp.allow_opposite_sideband,
        match_sequence=inp.match_sequence,
        per_channel_matching=inp.per_channel_matching,
        fscrunch_to_cal=inp.fscrunch_to_cal,
        gain_normalize=inp.gain_normalize,
    )
    progress("calibrating", 0.3)
    run = await ctx.runner.run(
        argv, cwd=job_tmp, timeout_s=ctx.cfg.server.job_timeout_s, cancel=CancelHook()
    )
    if run.exit_code != 0:
        log_id = log_artifact_from_stderr(ctx, job_key="pac", stderr=run.stderr_tail(), argv=argv)
        raise PsrmcpError(
            ErrorCode.PROCESS_FAILED,
            f"pac failed (exit code {run.exit_code}): {run.stderr_tail(300)}",
            log_artifact_id=log_id,
        )

    # pac catches per-file errors internally and still exits 0 — the only
    # reliable success signal is the calibrated output file
    parsed = psa.parse_pac(run.stdout_text + "\n" + run.stderr_text)
    produced = sorted(job_tmp.glob("pac_src.calib*"))
    warnings: list[WarningInfo] = []
    if not produced:
        detail = parsed.get("handling_error") or parsed.get("fluxcal_error") or run.stderr_tail(300)
        log_id = log_artifact_from_stderr(ctx, job_key="pac", stderr=run.stderr_tail(), argv=argv)
        raise PsrmcpError(
            ErrorCode.PROCESS_FAILED,
            f"pac produced no calibrated archive: {detail}",
            log_artifact_id=log_id,
        )

    out_path = produced[0]
    # pac appends 'P' when polarimetric calibration succeeded but flux
    # calibration did not. Expected (and silent here) when the caller
    # disabled flux calibration; worth a warning when flux was requested.
    poln_only = out_path.name.endswith("P")
    if poln_only and inp.flux_calibration:
        warnings.append(WarningInfo(
            code="W_PAC_POLN_ONLY",
            message="only the polarization was calibrated: no matching flux calibrator "
                    "was found (pac appended 'P' to the output name)",
            severity="warning",
        ))
    if parsed.get("no_flux_db"):
        warnings.append(WarningInfo(
            code="W_PAC_NO_FLUXDB",
            message="flux calibration skipped: pac had no calibrator database "
                    "(model solutions are applied without one)",
            severity="warning",
        ))
    if parsed.get("fluxcal_error"):
        warnings.append(WarningInfo(
            code="W_PAC_FLUXCAL_FAILED",
            message=f"flux calibration failed: {parsed['fluxcal_error']}",
            severity="warning",
        ))

    prov = make_provenance(
        tool="polarization_calibrate",
        backend="pac",
        backend_version=psr_info.version,
        params=inp.model_dump(mode="json"),
        started=started,
        duration_s=time.monotonic() - t0,
        command=argv,
    )
    calibrated = ctx.store.commit(
        src_path=out_path,
        filename=(inp.output_name or "calibrated") + ".ar",
        kind=ArtifactKind.ARCHIVE,
        format_name="PSRARCHIVE",
        provenance=prov,
        parents=parents or None,
    )
    progress("complete", 1.0)
    return PolarizationCalibrateResult(
        calibrated_archive=calibrated,
        poln_calibrated=bool(parsed.get("poln_complete") or parsed.get("already_poln")),
        flux_calibrated=bool(parsed.get("flux_complete") or parsed.get("already_flux")),
        poln_calibrator_source=parsed.get("poln_calibrator_from"),
        flux_calibrator_source=parsed.get("flux_calibrator_from"),
        mean_sefd_jy=parsed.get("mean_sefd_jy"),
        warnings=warnings,
    )


async def _database(ctx: AppContext, inp: CalibratorDatabaseInput, progress) -> CalibratorDatabaseResult:
    started = utcnow()
    t0 = time.monotonic()
    psr_info = await ctx.backends.require("psrchive")
    binary = _psrchive_tool(ctx, "pac")

    job_tmp = ctx.ws.new_job_tmp(f"pacdb_{time.time_ns()}")
    cals = job_tmp / "cals"
    cals.mkdir()
    extensions, _ = _copy_calibrators(ctx, inp.calibrators, inp.root, cals)

    db_path = job_tmp / "database.txt"
    argv = psa.pac_database_cmd(binary, str(cals), str(db_path), extensions)
    progress("indexing", 0.3)
    run = await ctx.runner.run(
        argv, cwd=job_tmp, timeout_s=ctx.cfg.server.job_timeout_s, cancel=CancelHook()
    )
    if run.exit_code != 0:
        log_id = log_artifact_from_stderr(ctx, job_key="pacdb", stderr=run.stderr_tail(), argv=argv)
        raise PsrmcpError(
            ErrorCode.PROCESS_FAILED,
            f"pac -w failed (exit code {run.exit_code}): {run.stderr_tail(300)}",
            log_artifact_id=log_id,
        )
    if not db_path.exists():
        log_id = log_artifact_from_stderr(ctx, job_key="pacdb", stderr=run.stderr_tail(), argv=argv)
        raise PsrmcpError(
            ErrorCode.PROCESS_FAILED,
            "pac -w reported success but wrote no database.txt",
            log_artifact_id=log_id,
        )

    rows = psa.parse_dbase_entries(db_path.read_text(errors="replace"))
    warnings: list[WarningInfo] = []
    if not rows:
        warnings.append(WarningInfo(
            code="W_PACDB_EMPTY",
            message="database.txt contains no parseable calibrator entries; "
                    "check that the files are PolnCal/FluxCal observations",
            severity="warning",
        ))

    prov = make_provenance(
        tool="calibrator_database",
        backend="pac",
        backend_version=psr_info.version,
        params=inp.model_dump(mode="json"),
        started=started,
        duration_s=time.monotonic() - t0,
        command=argv,
    )
    database = ctx.store.commit(
        src_path=db_path,
        filename=(inp.output_name or "database") + ".txt",
        kind=ArtifactKind.OTHER,
        format_name="text",
        provenance=prov,
    )
    entry_types = dict(Counter(r["obs_type"] for r in rows))
    entries = [CalibratorDatabaseEntry(**r) for r in rows]
    progress("complete", 1.0)
    return CalibratorDatabaseResult(
        database_artifact=database,
        n_calibrators=len(rows),
        entry_types=entry_types,
        entries=entries,
        warnings=warnings,
    )
