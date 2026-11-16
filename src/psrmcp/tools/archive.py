"""archive_inspect / archive_convert / archive_merge / archive_transform /
archive_edit / archive_split / archive_compare (TOOLS.md 5.7-5.13)."""  # archive_convert: psrconv -o PSRFITS/Timer/ASCII

from __future__ import annotations

import re
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
from ..models.inputs import (
    ArchiveCompareInput,
    ArchiveConvertInput,
    ArchiveEditInput,
    ArchiveInspectInput,
    ArchiveMergeInput,
    ArchiveSplitInput,
    ArchiveTransformInput,
)
from ..models.results import (
    ArchiveCompareResult,
    ArchiveConvertResult,
    ArchiveEditResult,
    ArchiveInspectResult,
    ArchiveMergeResult,
    ArchiveSplitResult,
    ArchiveTransformResult,
    Dimensions,
    IntegrationRow,
    ObservationInfo,
    SourceInfo,
    TimingInfo,
)
from ._common import AppContext, log_artifact_from_stderr, make_provenance
from .registry import ToolRegistry, ToolSpec

_CONV_EXT = {"psrfits": "fits", "psrarchive": "ar", "ascii": "txt"}


def register(reg: ToolRegistry) -> None:
    reg.add(ToolSpec(
        name="archive_inspect",
        description="Read metadata, dimensions, and basic statistics from PSRFITS/PSRARCHIVE without modifying the file. Call this before processing data.",
        input_model=ArchiveInspectInput,
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
        handler=_inspect,
    ))
    reg.add(ToolSpec(
        name="archive_convert",
        description="Convert between archive formats via psrconv: psrfits, psrarchive (Timer), or ascii (single-profile text). EPN files can be read but not written (PSRCHIVE has no EPN unload); the input is left untouched and the output is a new artifact.",
        input_model=ArchiveConvertInput,
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False),
        handler=_convert,
    ))
    reg.add(ToolSpec(
        name="archive_merge",
        description="Merge multiple archives (requires matching source names by default). Long task; supports async and cancellation.",
        input_model=ArchiveMergeInput,
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False),
        handler=_merge,
        long_task=True,
    ))
    reg.add(ToolSpec(
        name="archive_transform",
        description="Scrunch or clean a folded archive: reduce frequency channels / sub-integrations / phase bins, sum polarisations, remove the baseline, or centre the profile. Targets are final counts; an integral factor is derived automatically (infeasible targets are skipped with a warning). The input is never modified — output is a new artifact. Long task; supports async and cancellation.",
        input_model=ArchiveTransformInput,
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False),
        handler=_transform,
        long_task=True,
    ))
    reg.add(ToolSpec(
        name="archive_edit",
        description="Query and/or set archive metadata via psredit (e.g. name, dm, freq, bw, int:mjd). Query values are read from the input archive; set assignments are written to a new artifact (the input stays untouched).",
        input_model=ArchiveEditInput,
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False),
        handler=_edit,
    ))
    reg.add(ToolSpec(
        name="archive_split",
        description="Split an archive into multiple archive artifacts via psrsplit: group sub-integrations (per_subint) and/or frequency channels (per_channel) per output file; tail remainders land in a smaller final file. The input is read-only. Long task; supports async and cancellation.",
        input_model=ArchiveSplitInput,
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False),
        handler=_split,
        long_task=True,
    ))
    reg.add(ToolSpec(
        name="archive_compare",
        description="Measure the difference between two archives via psrdiff: reduced chi^2 of `archive` against `reference`, bin-by-bin (optionally per polarization). Pre-scrunching (-p/-f/-t) can reconcile mismatched dimensions before comparing.",
        input_model=ArchiveCompareInput,
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
        handler=_compare,
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
            f"could not parse metadata from vap output: {resolved.resolved.name}",
            hint="confirm the file is PSRFITS/PSRARCHIVE readable by psrchive",
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
        integrations = await _probe_integrations(ctx, resolved.resolved, inp.max_integrations, warnings)

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
            f"psrconv does not support output format {inp.output_format}",
            hint="supported: psrfits, psrarchive (Timer), ascii; sigproc is not a PSRCHIVE format",
        )
    psr_info = await ctx.backends.require("psrchive")
    binary = _tool(ctx, "psrconv")
    resolved = ctx.resolver.resolve(inp.archive, root=inp.root, what="archive")

    job_tmp = ctx.ws.new_job_tmp(f"conv_{int(time.time() * 1000)}")
    ext = _CONV_EXT[inp.output_format]
    # psrconv has no output-directory flag; artifacts land next to the input
    # file — copy the input into the job tmp first
    local_src = job_tmp / resolved.resolved.name
    shutil.copy2(resolved.resolved, local_src)
    argv = psa.psrconv_cmd(binary, str(local_src), ext, inp.output_format)
    run = await ctx.runner.run(argv, cwd=job_tmp, timeout_s=1800.0)
    if run.exit_code != 0:
        log_id = log_artifact_from_stderr(ctx, job_key="psrconv", stderr=run.stderr_tail(), argv=argv)
        raise PsrmcpError(
            ErrorCode.PROCESS_FAILED,
            f"psrconv failed (exit code {run.exit_code}): {run.stderr_tail(300)}",
            log_artifact_id=log_id,
        )

    produced = sorted(job_tmp.glob(f"*.{ext}"))
    if not produced:
        raise PsrmcpError(ErrorCode.PROCESS_FAILED, f"psrconv produced no .{ext} file")
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
                f"inputs contain different sources: {sorted(names)}",
                hint="confirm the inputs belong to the same pulsar, or set require_same_source=false",
            )

    progress("merging", 0.1)
    job_tmp = ctx.ws.new_job_tmp(f"merge_{int(time.time() * 1000)}")
    out_path = Path(str(job_tmp / (inp.output_name or "merged")) + ".ar")
    # In practice psradd only has -o/-O output flags and no weighting flag:
    # the sum uses psrchive's natural (SNR-derived) weights, and the
    # time/snr options cannot be mapped — emit a warning
    warnings = []
    if inp.weighting in ("time", "snr"):
        warnings.append(WarningInfo(
            code="W_PSRADD_NATURAL_WEIGHT",
            message=f"psradd has no weighting flag, weighting={inp.weighting} had no effect; natural (SNR-derived) weights were used",
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
            f"psradd failed (exit code {run.exit_code}): {run.stderr_tail(300)}",
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
    # probe the dimensions BEFORE commit: commit() renames the file into the
    # store, so a vap on out_path afterwards finds nothing (this used to
    # silently report all-zero dimensions)
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
    ref = ctx.store.commit(
        src_path=out_path,
        filename=out_path.name,
        kind=ArtifactKind.ARCHIVE,
        format_name="PSRARCHIVE",
        provenance=prov,
        parents=[r.artifact_id for r in resolved if r.artifact_id],
    )
    progress("complete", 1.0)
    return ArchiveMergeResult(artifact=ref, n_inputs=len(resolved), dimensions=dims, warnings=warnings)


async def _transform(ctx: AppContext, inp: ArchiveTransformInput, progress) -> ArchiveTransformResult:
    started = utcnow()
    t0 = time.monotonic()
    psr_info = await ctx.backends.require("psrchive")
    binary = _tool(ctx, "pam")
    resolved = ctx.resolver.resolve(inp.archive, root=inp.root, what="archive")

    # Probe original dimensions so count targets can be turned into the
    # integral scrunch factors pam expects (same convention as
    # profile_standardize). Infeasible targets are skipped with a warning.
    orig = await _vap_meta_dims(ctx, resolved.resolved)
    warnings: list[WarningInfo] = []

    def _factor(target: int | None, current: int | None, what: str) -> int | None:
        if target is None:
            return None
        if not current:
            warnings.append(WarningInfo(
                code="W_DIMENSIONS_UNKNOWN",
                message=f"{what}: original dimensions unknown, target {target} not applied",
                severity="warning",
            ))
            return None
        if target > current:
            warnings.append(WarningInfo(
                code="W_TARGET_LARGER",
                message=f"{what}: target {target} exceeds original {current}; skipped (scrunching cannot increase resolution)",
                severity="warning",
            ))
            return None
        if current % target:
            warnings.append(WarningInfo(
                code="W_FACTOR_INFEASIBLE",
                message=f"{what}: target {target} is not an integral divisor of original {current}; skipped",
                severity="warning",
            ))
            return None
        return current // target

    freq_factor = _factor(inp.fscrunch_to, orig.get("nchan"), "fscrunch_to")
    time_factor = _factor(inp.tscrunch_to, orig.get("nsubint"), "tscrunch_to")
    bin_factor = _factor(inp.binscrunch_to, orig.get("nbin"), "binscrunch_to")

    job_tmp = ctx.ws.new_job_tmp(f"tr_{int(time.time() * 1000)}")
    local_src = job_tmp / "transform_src.ar"
    shutil.copy2(resolved.resolved, local_src)
    argv = psa.pam_scrunch_cmd(
        binary, str(local_src), "tr",
        freq_factor=freq_factor, time_factor=time_factor, sum_pols=inp.sum_pols,
        bin_factor=bin_factor, remove_baseline=inp.remove_baseline, centre=inp.centre,
    )
    progress("transforming", 0.4)
    run = await ctx.runner.run(
        argv, cwd=job_tmp, timeout_s=ctx.cfg.server.job_timeout_s, cancel=CancelHook()
    )
    out_path = job_tmp / "transform_src.tr"
    if run.exit_code != 0 or not out_path.exists():
        log_id = log_artifact_from_stderr(ctx, job_key="pam", stderr=run.stderr_tail(), argv=argv)
        raise PsrmcpError(
            ErrorCode.PROCESS_FAILED,
            f"pam failed (exit code {run.exit_code}): {run.stderr_tail(300)}",
            log_artifact_id=log_id,
        )

    dims = await _vap_meta_dims(ctx, out_path) or Dimensions(nsubint=0, nchan=0, npol=0, nbin=0)
    prov = make_provenance(
        tool="archive_transform",
        backend="pam",
        backend_version=psr_info.version,
        params=inp.model_dump(mode="json"),
        started=started,
        duration_s=time.monotonic() - t0,
        command=argv,
    )
    ref = ctx.store.commit(
        src_path=out_path,
        filename=(inp.output_name or "transformed") + ".ar",
        kind=ArtifactKind.ARCHIVE,
        format_name="PSRARCHIVE",
        provenance=prov,
        parents=[resolved.artifact_id] if resolved.artifact_id else None,
    )
    progress("complete", 1.0)
    return ArchiveTransformResult(artifact=ref, dimensions=dims, warnings=warnings)


async def _edit(ctx: AppContext, inp: ArchiveEditInput, progress) -> ArchiveEditResult:
    started = utcnow()
    t0 = time.monotonic()
    psr_info = await ctx.backends.require("psrchive")
    binary = _tool(ctx, "psredit")
    resolved = ctx.resolver.resolve(inp.archive, root=inp.root, what="archive")
    warnings: list[WarningInfo] = []

    values: dict[str, object] = {}
    if inp.query:
        argv_q = psa.psredit_get_cmd(binary, str(resolved.resolved), inp.query)
        run = await ctx.runner.run(argv_q, timeout_s=120.0)
        if run.exit_code != 0:
            log_id = log_artifact_from_stderr(ctx, job_key="psredit", stderr=run.stderr_tail(), argv=argv_q)
            raise PsrmcpError(
                ErrorCode.PROCESS_FAILED,
                f"psredit query failed (exit code {run.exit_code}): {run.stderr_tail(300)}",
                log_artifact_id=log_id,
            )
        values = psa.parse_psrstat(run.stdout_text)
        for k in ("_filename", "_positional"):
            values.pop(k, None)
        missing = [k for k in inp.query if k not in values]
        if missing:
            warnings.append(WarningInfo(
                code="W_KEYS_UNRESOLVED",
                message=f"query keys not returned by psredit: {', '.join(missing)}",
                severity="warning",
            ))

    artifact = None
    if inp.set:
        job_tmp = ctx.ws.new_job_tmp(f"edit_{int(time.time() * 1000)}")
        local_src = job_tmp / "edit_src.ar"
        shutil.copy2(resolved.resolved, local_src)
        argv_s = psa.psredit_set_cmd(binary, str(local_src), inp.set)
        progress("editing", 0.4)
        run = await ctx.runner.run(argv_s, cwd=job_tmp, timeout_s=1800.0)
        out_path = job_tmp / "edit_src.edit"
        if run.exit_code != 0 or not out_path.exists():
            log_id = log_artifact_from_stderr(ctx, job_key="psredit", stderr=run.stderr_tail(), argv=argv_s)
            raise PsrmcpError(
                ErrorCode.PROCESS_FAILED,
                f"psredit failed (exit code {run.exit_code}): {run.stderr_tail(300)}",
                log_artifact_id=log_id,
            )
        prov = make_provenance(
            tool="archive_edit",
            backend="psredit",
            backend_version=psr_info.version,
            params=inp.model_dump(mode="json"),
            started=started,
            duration_s=time.monotonic() - t0,
            command=argv_s,
        )
        artifact = ctx.store.commit(
            src_path=out_path,
            filename=(inp.output_name or "edited") + ".ar",
            kind=ArtifactKind.ARCHIVE,
            format_name="PSRARCHIVE",
            provenance=prov,
            parents=[resolved.artifact_id] if resolved.artifact_id else None,
        )

    return ArchiveEditResult(artifact=artifact, values=values, warnings=warnings)


async def _vap_meta_dims(ctx: AppContext, path: Path) -> dict[str, int]:
    """Best-effort dimension probe via vap; returns {} when unavailable."""
    try:
        vap = await ctx.runner.run(psa.vap_cmd(_tool(ctx, "vap"), str(path)), timeout_s=120.0)
        meta = psa.parse_vap(vap.stdout_text)
        return {k: int(meta.get(k) or 0) for k in ("nsubint", "nchan", "npol", "nbin")}
    except PsrmcpError:
        return {}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _tool(ctx: AppContext, name: str) -> str:
    path = ctx.backends.resolve_psrchive_tool(name)
    if path is None:
        raise env_missing("psrchive", f"psrchive tool {name} not found")
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


async def _probe_integrations(ctx: AppContext, path: Path, limit: int,
                              warnings: list[WarningInfo]) -> list[IntegrationRow] | None:
    """Per-sub-integration rows via psrstat's subint loop (contract verified
    on the installed stack, 2026-11-15: index name is `subint`; snr/nbin/
    nchan/freq resolve per row, mjd/duration do not). Returns None with a
    warning when the dimensions cannot be probed."""
    dims = await _vap_meta_dims(ctx, path)
    nsub = dims.get("nsubint") or 0
    if nsub <= 0:
        warnings.append(WarningInfo(
            code="W_INTEGRATIONS_UNAVAILABLE",
            message="could not determine the sub-integration count; "
                    "include_integrations skipped",
            severity="warning",
        ))
        return None
    binary = _tool(ctx, "psrstat")
    argv = psa.psrstat_subints_cmd(binary, str(path), nsub, limit)
    run = await ctx.runner.run(argv, timeout_s=120.0)
    if run.exit_code != 0:
        warnings.append(WarningInfo(
            code="W_INTEGRATIONS_UNAVAILABLE",
            message=f"psrstat subint loop failed (exit code {run.exit_code}); "
                    "include_integrations skipped",
            severity="warning",
        ))
        return None
    rows = [
        IntegrationRow(**r)
        for r in psa.parse_psrstat_subints(run.stdout_text)
    ]
    if nsub > limit:
        warnings.append(WarningInfo(
            code="W_INTEGRATIONS_TRUNCATED",
            message=f"showing the first {limit} of {nsub} sub-integrations "
                    "(raise max_integrations for more)",
            severity="warning",
        ))
    return rows or None


async def _split(ctx: AppContext, inp: ArchiveSplitInput, progress) -> ArchiveSplitResult:
    started = utcnow()
    t0 = time.monotonic()
    psr_info = await ctx.backends.require("psrchive")
    binary = _tool(ctx, "psrsplit")
    resolved = ctx.resolver.resolve(inp.archive, root=inp.root, what="archive")

    # psrsplit writes the chunk files next to the input (naming
    # <stem>.%04d[_%04d].<ext>), swallowing per-file errors while exiting 0 —
    # so run on a staged copy with a distinctive stem and count the outputs.
    job_tmp = ctx.ws.new_job_tmp(f"split_{time.time_ns()}")
    local_src = job_tmp / "split_src.ar"
    shutil.copy2(resolved.resolved, local_src)

    argv = psa.psrsplit_cmd(
        binary, str(local_src),
        per_subint=inp.per_subint,
        per_channel=inp.per_channel,
        strip_extensions=inp.strip_extensions,
    )
    progress("splitting", 0.3)
    run = await ctx.runner.run(
        argv, cwd=job_tmp, timeout_s=ctx.cfg.server.job_timeout_s, cancel=CancelHook()
    )
    chunks = sorted(job_tmp.glob(psa.psrsplit_output_glob("split_src")))
    if run.exit_code != 0 or not chunks:
        detail = run.stderr_tail(300)
        m = _PSRSPLIT_TOO_MANY.search(detail)
        if m:
            detail = (
                f"requested more {'sub-integrations' if 'sub-integrations' in m.group(1) else 'channels'} "
                f"per output file than the archive holds ({m.group(1).strip()})"
            )
            code = ErrorCode.INVALID_INPUT
        else:
            code = ErrorCode.PROCESS_FAILED
        log_id = log_artifact_from_stderr(ctx, job_key="split", stderr=run.stderr_tail(), argv=argv)
        raise PsrmcpError(
            code,
            f"psrsplit produced no chunks ({len(chunks)} found): {detail}",
            log_artifact_id=log_id,
        )

    prov = make_provenance(
        tool="archive_split",
        backend="psrsplit",
        backend_version=psr_info.version,
        params=inp.model_dump(mode="json"),
        started=started,
        duration_s=time.monotonic() - t0,
        command=argv,
    )
    prefix = inp.output_name or "split"
    archives = []
    for i, chunk in enumerate(chunks):
        archives.append(ctx.store.commit(
            src_path=chunk,
            filename=f"{prefix}.{i:04d}.ar",
            kind=ArtifactKind.ARCHIVE,
            format_name="PSRARCHIVE",
            provenance=prov,
            parents=[resolved.artifact_id] if resolved.artifact_id else None,
        ))
    progress("complete", 1.0)
    return ArchiveSplitResult(
        archives=archives,
        n_files=len(archives),
        per_subint=inp.per_subint,
        per_channel=inp.per_channel,
    )


# psrsplit throws InvalidState with this text when -n/-c exceed the archive's
# sub-integrations/channels (still exit 0; caught per-file on the real binary).
_PSRSPLIT_TOO_MANY = re.compile(
    r"More (sub-integrations per output file|channels per output file) requested than available in (\S+)")


async def _compare(ctx: AppContext, inp: ArchiveCompareInput, progress) -> ArchiveCompareResult:
    started = utcnow()
    t0 = time.monotonic()
    psr_info = await ctx.backends.require("psrchive")
    binary = _tool(ctx, "psrdiff")
    archive = ctx.resolver.resolve(inp.archive, root=inp.root, what="archive")
    reference = ctx.resolver.resolve(inp.reference, root=inp.root, what="reference archive")

    job_tmp = ctx.ws.new_job_tmp(f"diff_{time.time_ns()}")
    local_ref = job_tmp / "cmp_ref.ar"
    local_data = job_tmp / "cmp_data.ar"
    shutil.copy2(reference.resolved, local_ref)
    shutil.copy2(archive.resolved, local_data)

    argv = psa.psrdiff_cmd(
        binary, str(local_ref), str(local_data),
        weighted=inp.weighted,
        per_polarization=inp.per_polarization,
        pscrunch=inp.pscrunch,
        fscrunch=inp.fscrunch,
        tscrunch=inp.tscrunch,
    )
    progress("comparing", 0.3)
    run = await ctx.runner.run(
        argv, cwd=job_tmp, timeout_s=ctx.cfg.server.job_timeout_s, cancel=CancelHook()
    )
    # Not mixable: exit 0 with "files differ as follows:" + tab-indented
    # reasons on stdout (verified: 32 vs 128 channels printed three reasons).
    parsed = psa.parse_psrdiff(run.stdout_text)
    if parsed.get("mismatch"):
        reasons = "; ".join(parsed["mismatch_reasons"]) or "archives are not mixable"
        raise PsrmcpError(
            ErrorCode.INVALID_INPUT,
            f"archives cannot be compared: {reasons} "
            "(consider pscrunch/fscrunch/tscrunch to reconcile dimensions)",
        )
    if run.exit_code != 0:
        log_id = log_artifact_from_stderr(ctx, job_key="diff", stderr=run.stderr_tail(), argv=argv)
        raise PsrmcpError(
            ErrorCode.PROCESS_FAILED,
            f"psrdiff failed (exit code {run.exit_code}): {run.stderr_tail(300)}",
            log_artifact_id=log_id,
        )

    chisq = parsed.get("weighted_reduced_chisq") if inp.weighted and not inp.per_polarization \
        else parsed.get("reduced_chisq")
    if chisq is None and not parsed.get("per_polarization"):
        log_id = log_artifact_from_stderr(ctx, job_key="diff", stderr=run.stderr_tail(), argv=argv)
        raise PsrmcpError(
            ErrorCode.PROCESS_FAILED,
            f"psrdiff produced no chi^2 output: {run.stderr_tail(300) or 'empty stdout'}",
            log_artifact_id=log_id,
        )
    if chisq is None:
        # per-polarization mode: aggregate the pol rows into the headline
        chisq = sum(parsed["per_polarization"].values()) / max(len(parsed["per_polarization"]), 1)
    progress("complete", 1.0)
    return ArchiveCompareResult(
        reduced_chisq=chisq,
        weighted=inp.weighted or inp.per_polarization,
        per_polarization=parsed.get("per_polarization") or {},
    )
