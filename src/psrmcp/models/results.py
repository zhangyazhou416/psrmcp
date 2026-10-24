"""Output models for all 19 tools (TOOLS.md section 5 output schemas)."""

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
        lines = [f"**服务器** {self.server.name} v{self.server.version} (MCP {self.server.mcp_protocol_version})"]
        if ok:
            lines.append(f"**可用后端**: {', '.join(sorted(ok))}")
        if missing:
            lines.append(f"**不可用后端**: {', '.join(sorted(missing))} — 可运行 scripts/install_pulsar_stack.sh 安装")
        lines.append(f"**数据目录**: {', '.join(self.data_roots) if self.data_roots else '(未配置)'}")
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
        head = [f"共 {self.count} 个文件" + ("(已截断)" if self.truncated else "")]
        for f in self.files[:10]:
            head.append(f"- `{f.relative_path}` ({f.detected_format}, {f.size_bytes} bytes)")
        if self.count > 10:
            head.append(f"... 以及其余 {self.count - 10} 个")
        return "\n".join(head)


class CatalogSearchResult(ToolResult):
    records: list[CatalogRecord]
    count: int = Field(ge=0)
    next_cursor: str | None = None
    catalog: CatalogMeta
    warnings: list[WarningInfo] = Field(default_factory=list)

    def summarize(self) -> str:
        lines = [f"匹配 {self.count} 条记录 (星表 v{self.catalog.version}, {self.catalog.mode.value})"]
        for r in self.records[:10]:
            lines.append(f"- **{r.name}** P0={r.p0_s:.6f}s DM={r.dm:.2f} ({r.ra_deg:.3f}, {r.dec_deg:.3f})")
        if self.count > 10:
            lines.append(f"... 以及其余 {self.count - 10} 条")
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
            f"- 位置 = ({r.ra_deg:.5f} deg, {r.dec_deg:.5f} deg)\n"
            f"- 双星: {'是 (' + (r.binary_model or '?') + ')' if r.binary else '否'}\n"
            f"- 可生成 ephemeris: {'是' if self.ephemeris_available else '否'}"
        )


class EphemerisExportResult(ToolResult):
    artifact: ArtifactRef
    format: Literal["tempo1", "tempo2"]
    warnings: list[WarningInfo] = Field(default_factory=list)
    provenance: Provenance

    def summarize(self) -> str:
        return f"已生成 {self.format} 格式 ephemeris: `{self.artifact.artifact_id}`"


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
            f"- 维度: nsub={d.nsubint} nchan={d.nchan} npol={d.npol} nbin={d.nbin}",
        ]
        if o.telescope:
            lines.append(f"- 望远镜: {o.telescope}" + (f" / {o.backend}" if o.backend else ""))
        if o.start_mjd is not None:
            lines.append(f"- MJD {o.start_mjd:.4f} - {o.end_mjd:.4f}" if o.end_mjd else f"- MJD {o.start_mjd:.4f}")
        if o.freq_mhz is not None:
            lines.append(f"- 频率: {o.freq_mhz:.1f} MHz (BW {o.bandwidth_mhz} MHz)" if o.bandwidth_mhz
                         else f"- 频率: {o.freq_mhz:.1f} MHz")
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
        return f"转换完成: `{self.artifact.artifact_id}` ({self.artifact.format}, {self.artifact.size_bytes} bytes)"


class ArchiveMergeResult(ToolResult):
    artifact: ArtifactRef
    n_inputs: int = Field(ge=2)
    dimensions: Dimensions
    warnings: list[WarningInfo] = Field(default_factory=list)

    def summarize(self) -> str:
        d = self.dimensions
        return (f"合并 {self.n_inputs} 个 archive -> `{self.artifact.artifact_id}` "
                f"(nsub={d.nsubint} nchan={d.nchan} nbin={d.nbin})")


class DedisperseResult(ToolResult):
    artifact: ArtifactRef
    dm_used: float = Field(ge=0)
    dimensions: Dimensions
    duration_s: float = Field(ge=0)
    log_artifact: str | None = None
    warnings: list[WarningInfo] = Field(default_factory=list)

    def summarize(self) -> str:
        d = self.dimensions
        lines = [f"消色散完成: `{self.artifact.artifact_id}`,DM = {self.dm_used:.4f} pc cm^-3",
                 f"- 维度: nsub={d.nsubint} nchan={d.nchan} npol={d.npol} nbin={d.nbin}",
                 f"- 耗时 {self.duration_s:.1f}s"]
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
        lines = [f"建议掩膜: {m.n_channels} 个坏通道 ({pct})"]
        if m.channels:
            show = m.channels[:20]
            lines.append("- 通道: " + ",".join(str(c) for c in show) + (" ..." if len(m.channels) > 20 else ""))
        return "\n".join(lines)


class RfiZapResult(ToolResult):
    artifact: ArtifactRef
    n_channels_zapped: int = Field(ge=0)
    zapped_fraction: float = Field(ge=0, le=1)
    warnings: list[WarningInfo] = Field(default_factory=list)

    def summarize(self) -> str:
        return (f"RFI 清除完成: `{self.artifact.artifact_id}`,"
                f"掩掉 {self.n_channels_zapped} 个通道 ({self.zapped_fraction * 100:.1f}%)")


class ProfileStandardizeResult(ToolResult):
    artifact: ArtifactRef
    nbin: int = Field(ge=1)
    snr: float | None = None
    warnings: list[WarningInfo] = Field(default_factory=list)

    def summarize(self) -> str:
        snr = f", S/N = {self.snr:.1f}" if self.snr is not None else ""
        return f"标准轮廓已生成: `{self.artifact.artifact_id}` (nbin={self.nbin}{snr})"


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
        return "轮廓测量: " + (", ".join(parts) if parts else "无可用指标")


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
        lines = [f"TOA 提取完成: `{self.tim_artifact.artifact_id}`,共 {self.n_toas} 个 TOA"]
        if self.mjd_range:
            lines.append(f"- MJD 范围: {self.mjd_range[0]:.4f} - {self.mjd_range[1]:.4f}")
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
            f"计时拟合完成: 新 par `{self.par_artifact.artifact_id}`",
            f"- {f.n_toas} TOA, chi2 = {f.chi2:.2f} / {f.dof} dof (reduced {f.reduced_chi2:.3f})",
            f"- RMS = {f.rms_us:.2f} us, WRMS = {f.wrms_us:.2f} us",
            f"- 拟合参数: {', '.join(p.name for p in fitted) if fitted else '(无)'}",
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
            f"诊断完成: {s.n_toas} TOA, "
            + (f"reduced chi2 = {s.reduced_chi2:.3f}, " if s.reduced_chi2 is not None else "")
            + (f"RMS = {s.rms_us:.2f} us" if s.rms_us is not None else ""),
            f"- 离群点: {s.n_outliers}",
        ]
        for f in self.findings:
            icon = {"info": "-", "warning": "⚠", "error": "✗"}[f.severity]
            lines.append(f"- {icon} [{f.code}] {f.message}")
        for r in self.recommendations:
            lines.append(f"- 建议: {r}")
        return "\n".join(lines)


class ArtifactListResult(ToolResult):
    artifacts: list[ArtifactRef]
    count: int = Field(ge=0)
    next_cursor: str | None = None

    def summarize(self) -> str:
        lines = [f"共 {self.count} 个产物"]
        for a in self.artifacts[:15]:
            lines.append(f"- `{a.artifact_id}` {a.kind.value} {a.format} ({a.size_bytes} bytes)")
        return "\n".join(lines)


class JobGetResult(ToolResult):
    job: JobRef

    def summarize(self) -> str:
        j = self.job
        line = f"任务 `{j.job_id}`: {j.status.value} ({j.phase}, 进度 {j.progress:.0%})"
        if j.error:
            line += f"\n- 错误: [{j.error.code}] {j.error.message}"
        return line


class JobCancelResult(ToolResult):
    job: JobRef

    def summarize(self) -> str:
        return f"任务 `{self.job.job_id}` 状态: {self.job.status.value}"
