"""Output models for all 27 tools (TOOLS.md section 5 output schemas)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .common import (
    ArtifactRef,
    CatalogMeta,
    CatalogRecord,
    JobRef,
    Provenance,
    WarningInfo,
)

FORBID = ConfigDict(extra="forbid")


class ToolResult(BaseModel):
    """Base for synchronous completed results."""

    model_config = FORBID
    result_type: Literal["completed"] = "completed"

    def summarize(self) -> str:
        return ""


class Dimensions(BaseModel):
    model_config = FORBID
    nsubint: int = Field(ge=0)
    nchan: int = Field(ge=0)
    npol: int = Field(ge=0)
    nbin: int = Field(ge=0)


class EnvironmentServerInfo(BaseModel):
    model_config = FORBID
    name: str
    version: str
    mcp_protocol_version: str


class WorkspaceInfo(BaseModel):
    model_config = FORBID
    root: str
    free_bytes: int = Field(ge=0)
    quota_bytes: int = Field(ge=0)


class LimitsInfo(BaseModel):
    model_config = FORBID
    max_threads: int = Field(ge=1)
    max_input_bytes: int = Field(ge=0)
    max_output_bytes: int = Field(ge=0)
    job_timeout_s: int = Field(ge=1)


class EnvironmentResult(ToolResult):
    server: EnvironmentServerInfo
    backends: dict[str, Any]
    data_roots: list[str]
    workspace: WorkspaceInfo
    catalog: CatalogMeta | None = None
    limits: LimitsInfo
    warnings: list[WarningInfo] = Field(default_factory=list)

    def summarize(self) -> str:
        ok = [n for n, b in self.backends.items() if isinstance(b, dict) and b.get("available")]
        missing = [n for n, b in self.backends.items() if isinstance(b, dict) and not b.get("available")]
        lines = [f"**Server** {self.server.name} v{self.server.version} (MCP {self.server.mcp_protocol_version})"]
        if ok:
            lines.append(f"**Available backends**: {', '.join(sorted(ok))}")
        if missing:
            lines.append(f"**Unavailable backends**: {', '.join(sorted(missing))} — run scripts/install_pulsar_stack.sh to install")
        lines.append(f"**Data roots**: {', '.join(self.data_roots) if self.data_roots else '(not configured)'}")
        return "\n".join(lines)


class FoundFile(BaseModel):
    model_config = FORBID
    root: str
    relative_path: str
    size_bytes: int = Field(ge=0)
    mtime: str
    detected_format: Literal["psrfits", "psrarchive", "sigproc", "par", "tim", "unknown"]


class DataFindResult(ToolResult):
    files: list[FoundFile]
    count: int = Field(ge=0)
    truncated: bool
    warnings: list[WarningInfo] = Field(default_factory=list)

    def summarize(self) -> str:
        head = [f"{self.count} files" + (" (truncated)" if self.truncated else "")]
        for f in self.files[:10]:
            head.append(f"- `{f.relative_path}` ({f.detected_format}, {f.size_bytes} bytes)")
        if self.count > 10:
            head.append(f"... and {self.count - 10} more")
        return "\n".join(head)


class CatalogSearchResult(ToolResult):
    records: list[CatalogRecord]
    count: int = Field(ge=0)
    next_cursor: str | None = None
    catalog: CatalogMeta
    warnings: list[WarningInfo] = Field(default_factory=list)

    def summarize(self) -> str:
        lines = [f"{self.count} matching records (catalog v{self.catalog.version}, {self.catalog.mode.value})"]
        for r in self.records[:10]:
            line = f"- **{r.name}** P0={r.p0_s:.6f}s DM={r.dm:.2f} ({r.ra_deg:.3f}, {r.dec_deg:.3f})"
            if r.extra_fields:
                line += " " + " ".join(f"{k}={v}" for k, v in r.extra_fields.items())
            lines.append(line)
        if self.count > 10:
            lines.append(f"... and {self.count - 10} more")
        return "\n".join(lines)


class CatalogGetResult(ToolResult):
    record: CatalogRecord
    ephemeris_available: bool
    catalog: CatalogMeta
    warnings: list[WarningInfo] = Field(default_factory=list)

    def summarize(self) -> str:
        r = self.record
        return (
            f"**{r.name}**\n"
            f"- P0 = {r.p0_s:.9f} s, DM = {r.dm:.3f} pc cm^-3\n"
            f"- Position = ({r.ra_deg:.5f} deg, {r.dec_deg:.5f} deg)\n"
            f"- Binary: {'yes (' + (r.binary_model or '?') + ')' if r.binary else 'no'}\n"
            f"- Ephemeris exportable: {'yes' if self.ephemeris_available else 'no'}"
        )


class EphemerisExportResult(ToolResult):
    artifact: ArtifactRef
    format: Literal["tempo1", "tempo2"]
    warnings: list[WarningInfo] = Field(default_factory=list)
    provenance: Provenance

    def summarize(self) -> str:
        return f"Generated {self.format} ephemeris: `{self.artifact.artifact_id}`"


class SourceInfo(BaseModel):
    model_config = FORBID
    name: str
    ra_deg: float | None = None
    dec_deg: float | None = None


class ObservationInfo(BaseModel):
    model_config = FORBID
    telescope: str | None = None
    receiver: str | None = None
    backend: str | None = None
    start_mjd: float | None = None
    end_mjd: float | None = None
    duration_s: float | None = None
    freq_mhz: float | None = None
    bandwidth_mhz: float | None = None


class TimingInfo(BaseModel):
    model_config = FORBID
    dm: float | None = None
    rm: float | None = None
    period_s: float | None = None
    ephemeris_present: bool = False


class EphemerisPointer(BaseModel):
    model_config = FORBID
    artifact_id: str
    source_name: str | None = None
    format: str | None = None


class IntegrationRow(BaseModel):
    model_config = FORBID
    index: int = Field(ge=0)
    mjd: float | None = None
    duration_s: float | None = None
    snr: float | None = None


class ArchiveInspectResult(ToolResult):
    input: dict[str, Any] | None = None
    format: str
    format_version: str | None = None
    source: SourceInfo
    observation: ObservationInfo
    dimensions: Dimensions
    timing: TimingInfo | None = None
    ephemeris: EphemerisPointer | None = None
    integrations: list[IntegrationRow] | None = None
    warnings: list[WarningInfo] = Field(default_factory=list)
    provenance: Provenance

    def summarize(self) -> str:
        s, o, d = self.source, self.observation, self.dimensions
        lines = [
            f"**{s.name}** — {self.format}",
            f"- Dimensions: nsub={d.nsubint} nchan={d.nchan} npol={d.npol} nbin={d.nbin}",
        ]
        if o.telescope:
            lines.append(f"- Telescope: {o.telescope}" + (f" / {o.backend}" if o.backend else ""))
        if o.start_mjd is not None:
            lines.append(f"- MJD {o.start_mjd:.4f} - {o.end_mjd:.4f}" if o.end_mjd else f"- MJD {o.start_mjd:.4f}")
        if o.freq_mhz is not None:
            lines.append(f"- Frequency: {o.freq_mhz:.1f} MHz (BW {o.bandwidth_mhz} MHz)" if o.bandwidth_mhz
                         else f"- Frequency: {o.freq_mhz:.1f} MHz")
        if self.timing:
            t = self.timing
            if t.dm is not None:
                lines.append(f"- DM: {t.dm:.4f}" + (f", P = {t.period_s*1000:.3f} ms" if t.period_s else ""))
        for w in self.warnings:
            lines.append(f"- ⚠ {w.message}")
        return "\n".join(lines)


class ArchiveConvertResult(ToolResult):
    artifact: ArtifactRef
    warnings: list[WarningInfo] = Field(default_factory=list)

    def summarize(self) -> str:
        return f"Conversion complete: `{self.artifact.artifact_id}` ({self.artifact.format}, {self.artifact.size_bytes} bytes)"


class ArchiveMergeResult(ToolResult):
    artifact: ArtifactRef
    n_inputs: int = Field(ge=2)
    dimensions: Dimensions
    warnings: list[WarningInfo] = Field(default_factory=list)

    def summarize(self) -> str:
        d = self.dimensions
        return (f"Merged {self.n_inputs} archives -> `{self.artifact.artifact_id}` "
                f"(nsub={d.nsubint} nchan={d.nchan} nbin={d.nbin})")


class ArchiveTransformResult(ToolResult):
    artifact: ArtifactRef
    dimensions: Dimensions
    warnings: list[WarningInfo] = Field(default_factory=list)

    def summarize(self) -> str:
        d = self.dimensions
        lines = [f"Transform complete: `{self.artifact.artifact_id}` "
                 f"(nsub={d.nsubint} nchan={d.nchan} npol={d.npol} nbin={d.nbin})"]
        for w in self.warnings:
            lines.append(f"- ⚠ {w.message}")
        return "\n".join(lines)


class ArchiveEditResult(ToolResult):
    artifact: ArtifactRef | None = None
    values: dict[str, Any] = Field(default_factory=dict)
    warnings: list[WarningInfo] = Field(default_factory=list)

    def summarize(self) -> str:
        lines: list[str] = []
        if self.values:
            lines.append("Queried values (input archive, pre-edit):")
            lines += [f"- {k} = {v}" for k, v in self.values.items()]
        if self.artifact is not None:
            lines.append(f"Edited archive written: `{self.artifact.artifact_id}`")
        for w in self.warnings:
            lines.append(f"- ⚠ {w.message}")
        return "\n".join(lines) or "No query results."


class DedisperseResult(ToolResult):
    artifact: ArtifactRef
    dm_used: float = Field(ge=0)
    dimensions: Dimensions
    duration_s: float = Field(ge=0)
    log_artifact: str | None = None
    warnings: list[WarningInfo] = Field(default_factory=list)

    def summarize(self) -> str:
        d = self.dimensions
        lines = [f"Dedispersion complete: `{self.artifact.artifact_id}`, DM = {self.dm_used:.4f} pc cm^-3",
                 f"- Dimensions: nsub={d.nsubint} nchan={d.nchan} npol={d.npol} nbin={d.nbin}",
                 f"- Took {self.duration_s:.1f}s"]
        for w in self.warnings:
            lines.append(f"- ⚠ {w.message}")
        return "\n".join(lines)


class SuggestedMask(BaseModel):
    model_config = FORBID
    channels: list[int] = Field(default_factory=list)
    fraction: float = Field(ge=0, le=1)
    n_channels: int = Field(ge=0)


class RfiScanResult(ToolResult):
    suggested_mask: SuggestedMask
    statistics: dict[str, Any]
    plot_artifact: str | None = None
    warnings: list[WarningInfo] = Field(default_factory=list)

    def summarize(self) -> str:
        m = self.suggested_mask
        pct = f"{m.fraction * 100:.1f}%"
        lines = [f"Suggested mask: {m.n_channels} bad channels ({pct})"]
        if m.channels:
            show = m.channels[:20]
            lines.append("- Channels: " + ",".join(str(c) for c in show) + (" ..." if len(m.channels) > 20 else ""))
        return "\n".join(lines)


class RfiZapResult(ToolResult):
    artifact: ArtifactRef
    n_channels_zapped: int = Field(ge=0)
    zapped_fraction: float = Field(ge=0, le=1)
    warnings: list[WarningInfo] = Field(default_factory=list)

    def summarize(self) -> str:
        return (f"RFI zapping complete: `{self.artifact.artifact_id}`,"
                f"zapped {self.n_channels_zapped} channels ({self.zapped_fraction * 100:.1f}%)")


class ProfileStandardizeResult(ToolResult):
    artifact: ArtifactRef
    nbin: int = Field(ge=1)
    snr: float | None = None
    warnings: list[WarningInfo] = Field(default_factory=list)

    def summarize(self) -> str:
        snr = f", S/N = {self.snr:.1f}" if self.snr is not None else ""
        return f"Standardized profile written: `{self.artifact.artifact_id}` (nbin={self.nbin}{snr})"


class WidthInfo(BaseModel):
    model_config = FORBID
    fwhm_bins: float | None = None
    fwhm_deg: float | None = None
    fwhm_ms: float | None = None


class MeasureMetrics(BaseModel):
    model_config = FORBID
    snr: float | None = None
    width: WidthInfo | None = None
    flux_mjy: float | None = None
    polarization: dict[str, Any] | None = None
    dm: float | None = None


class ProfileMeasureResult(ToolResult):
    metrics: MeasureMetrics
    plot_artifact: str | None = None
    warnings: list[WarningInfo] = Field(default_factory=list)

    def summarize(self) -> str:
        m = self.metrics
        parts = []
        if m.snr is not None:
            parts.append(f"S/N = {m.snr:.1f}")
        if m.width:
            if m.width.fwhm_ms is not None:
                parts.append(f"FWHM = {m.width.fwhm_ms:.3f} ms")
            elif m.width.fwhm_bins is not None:
                parts.append(f"FWHM = {m.width.fwhm_bins:.1f} bins")
        if m.flux_mjy is not None:
            parts.append(f"flux = {m.flux_mjy:.2f} mJy")
        return "Profile measurement: " + (", ".join(parts) if parts else "no metrics available")


class ProfileOptimizeResult(ToolResult):
    """Best period/DM found by the S/N search (pdmp). Values are best-effort
    parses of pdmp's text output; a None field means the label was not found
    in this pdmp build's output (see warnings)."""

    best_period_ms: float | None = None
    period_error_ms: float | None = None
    best_dm: float | None = None
    dm_error: float | None = None
    best_snr: float | None = None
    warnings: list[WarningInfo] = Field(default_factory=list)

    def summarize(self) -> str:
        parts: list[str] = []
        if self.best_period_ms is not None:
            parts.append(f"P = {self.best_period_ms:.6f} ms"
                         + (f" +/- {self.period_error_ms:.2g} ms" if self.period_error_ms is not None else ""))
        if self.best_dm is not None:
            parts.append(f"DM = {self.best_dm:.4f} pc cm^-3"
                         + (f" +/- {self.dm_error:.2g}" if self.dm_error is not None else ""))
        if self.best_snr is not None:
            parts.append(f"S/N = {self.best_snr:.1f}")
        lines = ["Period/DM optimization: " + (", ".join(parts) if parts else "no best-fit values parsed")]
        for w in self.warnings:
            lines.append(f"- ⚠ {w.message}")
        return "\n".join(lines)


class MaxAmplitudeRow(BaseModel):
    """One entry of psrspa's <prefix>_maxamp.dat (GSL-compatible layout):
    the highest-amplitude phase bin inside the requested bin range for one
    sub-integration. bin_min/bin_max/max_bin are phase bin indices."""
    model_config = FORBID
    archive_file: str
    subint: int = Field(ge=0)
    bscrunch_factor: int = Field(ge=1)
    bin_min: int = Field(ge=0)
    bin_max: int = Field(ge=0)
    max_bin: int = Field(ge=0)
    max_amplitude: float
    b_sigma: float | None = None


class SinglePulseRow(BaseModel):
    """One detected single pulse (psrspa <prefix>_pulses.dat): flux is in
    the archive's amplitude units, phase/width in phase bins."""
    model_config = FORBID
    archive_file: str
    subint: int = Field(ge=0)
    flux: float
    phase: float
    width_bins: float = Field(ge=0)
    bscrunch_factor: int = Field(ge=1)


class SinglePulseAnalyzeResult(ToolResult):
    """Single-pulse analysis outputs (psrspa). psrspa writes one histogram
    file per phase bin in the requested range (hundreds to thousands of
    files), so they are bundled into a single text artifact with
    `# >>> <filename>` section headers. Pulse / max-amplitude rows are
    parsed and returned inline."""

    histograms_artifact: ArtifactRef | None = None
    n_histogram_files: int = 0
    pulses_artifact: ArtifactRef | None = None
    pulses: list[SinglePulseRow] = Field(default_factory=list)
    max_amplitudes: list[MaxAmplitudeRow] = Field(default_factory=list)
    warnings: list[WarningInfo] = Field(default_factory=list)

    def summarize(self) -> str:
        lines: list[str] = []
        if self.pulses:
            fluxes = [p.flux for p in self.pulses]
            lines.append(
                f"Single-pulse analysis: {len(self.pulses)} pulses detected, "
                f"flux {min(fluxes):.4g} - {max(fluxes):.4g}"
            )
        elif self.max_amplitudes:
            amps = [m.max_amplitude for m in self.max_amplitudes]
            lines.append(
                f"Single-pulse analysis: {len(self.max_amplitudes)} max-amplitude "
                f"entries, amplitude {min(amps):.4g} - {max(amps):.4g}"
            )
        elif self.histograms_artifact is not None:
            lines.append(
                f"Single-pulse analysis: {self.n_histogram_files} phase-bin histogram file(s)"
            )
        else:
            lines.append("Single-pulse analysis: no outputs parsed")
        if self.histograms_artifact is not None:
            lines.append(f"- histograms: `{self.histograms_artifact.artifact_id}` (bundled text)")
        if self.pulses_artifact is not None:
            lines.append(f"- pulses: `{self.pulses_artifact.artifact_id}`")
        for w in self.warnings:
            lines.append(f"- ⚠ {w.message}")
        return "\n".join(lines)


class RotationMeasureFitResult(ToolResult):
    """RM fit results (rmfit). Values are best-effort parses of rmfit's text
    output; a None field means the corresponding method was not run or its
    label was not found (see warnings). rm_archive carries the input with
    per-sub-int auxiliary RM applied (only when save_rm_archive=true)."""

    best_rm: float | None = None
    best_rm_error: float | None = None
    search_rm: float | None = None
    search_rm_error: float | None = None
    refined_rm: float | None = None
    converged_iterations: int | None = None
    chi2_reduced: float | None = None
    rm_archive: ArtifactRef | None = None
    data_artifacts: list[ArtifactRef] = Field(default_factory=list)
    warnings: list[WarningInfo] = Field(default_factory=list)

    def summarize(self) -> str:
        parts: list[str] = []
        if self.refined_rm is not None:
            parts.append(f"refined RM = {self.refined_rm:.3f} rad/m^2")
        if self.best_rm is not None:
            parts.append(f"best RM = {self.best_rm:.3f} rad/m^2"
                         + (f" +/- {self.best_rm_error:.2g}" if self.best_rm_error is not None else ""))
        if self.search_rm is not None:
            parts.append(f"search RM = {self.search_rm:.3f} rad/m^2"
                         + (f" +/- {self.search_rm_error:.2g}" if self.search_rm_error is not None else ""))
        if self.converged_iterations is not None:
            parts.append(f"converged in {self.converged_iterations} iterations")
        if self.chi2_reduced is not None:
            parts.append(f"reduced chi^2 = {self.chi2_reduced:.3f}")
        lines = ["Rotation measure fit: " + ("; ".join(parts) if parts else "no RM values parsed")]
        if self.rm_archive is not None:
            lines.append(f"- RM-corrected archive: `{self.rm_archive.artifact_id}`")
        for a in self.data_artifacts:
            lines.append(f"- data table: `{a.artifact_id}`")
        for w in self.warnings:
            lines.append(f"- ⚠ {w.message}")
        return "\n".join(lines)


class CalibratorDatabaseEntry(BaseModel):
    """One line of a pac calibrator database.txt (header metadata of one
    calibrator archive; filename is relative to the database directory)."""
    model_config = FORBID
    filename: str
    obs_type: str
    ra_str: str
    dec_str: str
    mjd: str
    bandwidth_mhz: float
    freq_mhz: float
    nchan: int | None = None
    instrument: str
    receiver: str


class CalibratorDatabaseResult(ToolResult):
    """pac calibrator database summary (database.txt artifact + parsed
    entries). pac writes one entry per calibrator archive found; obs_type is
    pac's classification from the archive header (PolnCal, FluxCal-On,
    FluxCal-Off, ...)."""
    model_config = FORBID
    database_artifact: ArtifactRef
    n_calibrators: int = Field(ge=0)
    entry_types: dict[str, int] = Field(default_factory=dict)
    entries: list[CalibratorDatabaseEntry] = Field(default_factory=list)
    warnings: list[WarningInfo] = Field(default_factory=list)

    def summarize(self) -> str:
        lines = [
            f"Calibrator database: {self.n_calibrators} entr"
            + ("y" if self.n_calibrators == 1 else "ies")
            + f" -> `{self.database_artifact.artifact_id}`"
        ]
        if self.entry_types:
            types = ", ".join(f"{t}: {n}" for t, n in sorted(self.entry_types.items()))
            lines.append(f"- types: {types}")
        for w in self.warnings:
            lines.append(f"- ⚠ {w.message}")
        return "\n".join(lines)


class PolarizationCalibrateResult(ToolResult):
    """pac calibration result. poln_calibrated/flux_calibrated report which
    stages completed (flux calibration is skipped with a warning when no
    flux calibrator matches or when the source is a model solution without a
    calibrator database)."""
    model_config = FORBID
    calibrated_archive: ArtifactRef
    poln_calibrated: bool
    flux_calibrated: bool
    poln_calibrator_source: str | None = None
    flux_calibrator_source: str | None = None
    mean_sefd_jy: float | None = None
    warnings: list[WarningInfo] = Field(default_factory=list)

    def summarize(self) -> str:
        lines = [
            f"Polarization calibration complete: `{self.calibrated_archive.artifact_id}`",
            f"- poln: {'calibrated' if self.poln_calibrated else 'not applied'}"
            + (f" (from {self.poln_calibrator_source})" if self.poln_calibrator_source else ""),
        ]
        if self.flux_calibrated:
            stage = "calibrated"
            if self.mean_sefd_jy is not None:
                stage += f", mean SEFD = {self.mean_sefd_jy:.4g} Jy"
            lines.append(f"- flux: {stage}"
                         + (f" (from {self.flux_calibrator_source})" if self.flux_calibrator_source else ""))
        else:
            lines.append("- flux: not applied")
        for w in self.warnings:
            lines.append(f"- ⚠ {w.message}")
        return "\n".join(lines)


class ToaSnrStats(BaseModel):
    model_config = FORBID
    min: float | None = None
    median: float | None = None
    max: float | None = None


class ToaExtractResult(ToolResult):
    tim_artifact: ArtifactRef
    n_toas: int = Field(ge=0)
    mjd_range: list[float] | None = Field(default=None, min_length=2, max_length=2)
    snr: ToaSnrStats | None = None
    warnings: list[WarningInfo] = Field(default_factory=list)

    def summarize(self) -> str:
        lines = [f"TOA extraction complete: `{self.tim_artifact.artifact_id}`, {self.n_toas} TOAs"]
        if self.mjd_range:
            lines.append(f"- MJD range: {self.mjd_range[0]:.4f} - {self.mjd_range[1]:.4f}")
        return "\n".join(lines)


class FitStats(BaseModel):
    model_config = FORBID
    n_toas: int = Field(ge=0)
    chi2: float = Field(ge=0)
    dof: int = Field(ge=0)
    reduced_chi2: float = Field(ge=0)
    rms_us: float = Field(ge=0)
    wrms_us: float = Field(ge=0)


class ParameterRow(BaseModel):
    model_config = FORBID
    name: str
    value: float
    uncertainty: float | None = None
    units: str | None = None
    fitted: bool


class TimingFitResult(ToolResult):
    par_artifact: ArtifactRef
    fit: FitStats
    parameters: list[ParameterRow]
    residuals_artifact: str | None = None
    plot_artifacts: list[str] = Field(default_factory=list)
    warnings: list[WarningInfo] = Field(default_factory=list)

    def summarize(self) -> str:
        f = self.fit
        fitted = [p for p in self.parameters if p.fitted]
        lines = [
            f"Timing fit complete: new par `{self.par_artifact.artifact_id}`",
            f"- {f.n_toas} TOA, chi2 = {f.chi2:.2f} / {f.dof} dof (reduced {f.reduced_chi2:.3f})",
            f"- RMS = {f.rms_us:.2f} us, WRMS = {f.wrms_us:.2f} us",
            f"- Fitted parameters: {', '.join(p.name for p in fitted) if fitted else '(none)'}",
        ]
        for w in self.warnings:
            lines.append(f"- ⚠ {w.message}")
        return "\n".join(lines)


class Finding(BaseModel):
    model_config = FORBID
    code: str
    severity: Literal["info", "warning", "error"]
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class DiagnoseSummary(BaseModel):
    model_config = FORBID
    n_toas: int = Field(ge=0)
    reduced_chi2: float | None = None
    rms_us: float | None = None
    n_outliers: int = Field(ge=0)


class TimingDiagnoseResult(ToolResult):
    summary: DiagnoseSummary
    findings: list[Finding]
    plots: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    warnings: list[WarningInfo] = Field(default_factory=list)

    def summarize(self) -> str:
        s = self.summary
        lines = [
            f"Diagnosis complete: {s.n_toas} TOA, "
            + (f"reduced chi2 = {s.reduced_chi2:.3f}, " if s.reduced_chi2 is not None else "")
            + (f"RMS = {s.rms_us:.2f} us" if s.rms_us is not None else ""),
            f"- Outliers: {s.n_outliers}",
        ]
        for f in self.findings:
            icon = {"info": "-", "warning": "⚠", "error": "✗"}[f.severity]
            lines.append(f"- {icon} [{f.code}] {f.message}")
        for r in self.recommendations:
            lines.append(f"- Recommendation: {r}")
        return "\n".join(lines)


class ArtifactListResult(ToolResult):
    artifacts: list[ArtifactRef]
    count: int = Field(ge=0)
    next_cursor: str | None = None

    def summarize(self) -> str:
        lines = [f"{self.count} artifacts"]
        for a in self.artifacts[:15]:
            lines.append(f"- `{a.artifact_id}` {a.kind.value} {a.format} ({a.size_bytes} bytes)")
        return "\n".join(lines)


class JobGetResult(ToolResult):
    job: JobRef

    def summarize(self) -> str:
        j = self.job
        line = f"Job `{j.job_id}`: {j.status.value} ({j.phase}, progress {j.progress:.0%})"
        if j.error:
            line += f"\n- Error: [{j.error.code}] {j.error.message}"
        return line


class JobCancelResult(ToolResult):
    job: JobRef

    def summarize(self) -> str:
        return f"Job `{self.job.job_id}` status: {self.job.status.value}"
