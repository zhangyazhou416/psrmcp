"""Input models for all 19 tools (TOOLS.md section 5 schemas, 1:1).

All models forbid extra properties, mirroring `additionalProperties: false`.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

FORBID = ConfigDict(extra="forbid")


class ExecutionMode(StrEnum):
    AUTO = "auto"
    SYNC = "sync"
    ASYNC = "async"


class PulsarEnvironmentInput(BaseModel):
    model_config = FORBID
    refresh: bool = False


class DataFindInput(BaseModel):
    model_config = FORBID
    pattern: str = Field(min_length=1, max_length=256)
    root: str | None = None
    max_depth: int = Field(default=3, ge=1, le=10)
    max_results: int = Field(default=100, ge=1, le=1000)
    extensions: list[str] | None = None
    modified_after: str | None = None


class CatalogSearchInput(BaseModel):
    model_config = FORBID
    name: str | None = Field(default=None, min_length=1, max_length=64)
    ra_deg: float | None = Field(default=None, ge=0, le=360)
    dec_deg: float | None = Field(default=None, ge=-90, le=90)
    radius_deg: float | None = Field(default=None, gt=0, le=180)
    p0_min_s: float | None = Field(default=None, gt=0)
    p0_max_s: float | None = Field(default=None, gt=0)
    dm_min: float | None = Field(default=None, ge=0)
    dm_max: float | None = Field(default=None, ge=0)
    binary_only: bool = False
    fields: list[str] | None = None
    limit: int = Field(default=50, ge=1, le=200)
    cursor: str | None = None

    @model_validator(mode="after")
    def _check(self) -> "CatalogSearchInput":
        if self.radius_deg is not None and (self.ra_deg is None or self.dec_deg is None):
            raise ValueError("radius_deg 必须与 ra_deg、dec_deg 同时出现")
        if self.p0_min_s is not None and self.p0_max_s is not None and self.p0_min_s > self.p0_max_s:
            raise ValueError("p0_min_s 不能大于 p0_max_s")
        if self.dm_min is not None and self.dm_max is not None and self.dm_min > self.dm_max:
            raise ValueError("dm_min 不能大于 dm_max")
        has_filter = any(v is not None for v in
                         (self.name, self.ra_deg, self.p0_min_s, self.p0_max_s, self.dm_min, self.dm_max))
        if not has_filter and not self.binary_only:
            raise ValueError("至少提供一个筛选条件(名称/位置/周期/DM/binary_only)")
        return self


class CatalogGetInput(BaseModel):
    model_config = FORBID
    name: str = Field(min_length=1, max_length=64)


class EphemerisOverrides(BaseModel):
    model_config = FORBID
    ra_deg: float | None = Field(default=None, ge=0, le=360)
    dec_deg: float | None = Field(default=None, ge=-90, le=90)
    p0_s: float | None = Field(default=None, gt=0)
    f0_hz: float | None = Field(default=None, gt=0)
    dm: float | None = Field(default=None, ge=0)
    pepoch_mjd: float | None = None
    posepoch_mjd: float | None = None


class EphemerisExportInput(BaseModel):
    model_config = FORBID
    pulsar: str | None = Field(default=None, min_length=1, max_length=64)
    artifact: str | None = None
    format: Literal["tempo1", "tempo2"] = "tempo1"
    overrides: EphemerisOverrides | None = None
    output_name: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def _check(self) -> "EphemerisExportInput":
        if not self.pulsar and not self.artifact:
            raise ValueError("pulsar 与 artifact 必须提供其一")
        return self


class ArchiveInspectInput(BaseModel):
    model_config = FORBID
    archive: str
    root: str | None = None
    include_stats: bool = True
    include_ephemeris: bool = True
    include_integrations: bool = False
    max_integrations: int = Field(default=100, ge=1, le=1000)


class ArchiveConvertInput(BaseModel):
    model_config = FORBID
    archive: str
    root: str | None = None
    output_format: Literal["psrfits", "psrarchive", "sigproc", "ascii"]
    output_name: str | None = Field(default=None, max_length=128)


class ArchiveMergeInput(BaseModel):
    model_config = FORBID
    archives: list[str] = Field(min_length=2, max_length=100)
    root: str | None = None
    output_name: str | None = Field(default=None, max_length=128)
    weighting: Literal["none", "time", "snr"] = "none"
    require_same_source: bool = True
    execution_mode: ExecutionMode = ExecutionMode.AUTO


class DedisperseInput(BaseModel):
    model_config = FORBID
    input: str
    root: str | None = None
    ephemeris: str
    mode: Literal["coherent", "incoherent"] = "coherent"
    dm_override: float | None = Field(default=None, gt=0)
    output_format: Literal["psrfits", "psrarchive"] = "psrfits"
    nsubint: int | None = Field(default=None, ge=1, le=10000)
    nchan: int | None = Field(default=None, ge=1, le=8192)
    nbin: int | None = Field(default=None, ge=1, le=1048576)
    threads: int | None = Field(default=None, ge=1, le=64)
    output_name: str | None = Field(default=None, max_length=128)
    execution_mode: ExecutionMode = ExecutionMode.AUTO


class RfiScanInput(BaseModel):
    model_config = FORBID
    archive: str
    root: str | None = None
    method: Literal["psrchive_median", "spectral_kurtosis", "bandpass"] = "psrchive_median"
    threshold_sigma: float = Field(default=3.0, ge=0.5, le=20)
    generate_plot: bool = True


class RfiAutoMask(BaseModel):
    model_config = FORBID
    method: Literal["psrchive_median", "spectral_kurtosis", "bandpass"]
    threshold_sigma: float = Field(default=3.0, ge=0.5, le=20)


class RfiMask(BaseModel):
    model_config = FORBID
    channels: list[int] | None = Field(default=None, min_length=1, max_length=8192)
    auto: RfiAutoMask | None = None

    @model_validator(mode="after")
    def _check(self) -> "RfiMask":
        if (self.channels is None) == (self.auto is None):
            raise ValueError("mask 必须且只能提供 channels 或 auto 之一")
        return self


class RfiZapInput(BaseModel):
    model_config = FORBID
    archive: str
    root: str | None = None
    mask: RfiMask
    output_name: str | None = Field(default=None, max_length=128)


class ProfileStandardizeInput(BaseModel):
    model_config = FORBID
    archive: str
    root: str | None = None
    align: Literal["profile", "peak", "none"] = "profile"
    smooth: bool = False
    nbin: int | None = Field(default=None, ge=1, le=1048576)
    output_name: str | None = Field(default=None, max_length=128)


class ProfileMeasureInput(BaseModel):
    model_config = FORBID
    archive: str
    root: str | None = None
    standard_profile: str | None = None
    metrics: list[str] = Field(default=["snr", "width"], max_length=5)
    generate_plot: bool = True
    plot_type: Literal["profile", "phase_frequency"] = "profile"


class ToaExtractInput(BaseModel):
    model_config = FORBID
    archive: str
    root: str | None = None
    standard_profile: str
    algorithm: Literal["PGS", "GIS", "PIS", "ZPS", "SIS", "FDM"] = "PGS"
    mjd_range: list[float] | None = Field(default=None, min_length=2, max_length=2)
    freq_range_mhz: list[float] | None = Field(default=None, min_length=2, max_length=2)
    min_snr: float = Field(default=0, ge=0)
    output_name: str | None = Field(default=None, max_length=128)
    execution_mode: ExecutionMode = ExecutionMode.AUTO

    @model_validator(mode="after")
    def _check(self) -> "ToaExtractInput":
        if self.mjd_range and self.mjd_range[0] > self.mjd_range[1]:
            raise ValueError("mjd_range 必须升序")
        if self.freq_range_mhz and self.freq_range_mhz[0] > self.freq_range_mhz[1]:
            raise ValueError("freq_range_mhz 必须升序")
        return self


class TimingFitInput(BaseModel):
    model_config = FORBID
    par: str
    tim: str
    root: str | None = None
    backend: Literal["pint", "tempo2"] = "pint"
    fit_params: list[str] | None = Field(default=None, max_length=50)
    weighting: Literal["uniform", "1/sigma^2"] = "1/sigma^2"
    generate_plot: bool = True
    max_iterations: int = Field(default=100, ge=1, le=1000)
    output_name: str | None = Field(default=None, max_length=128)
    execution_mode: ExecutionMode = ExecutionMode.AUTO


class DiagnoseCheck(StrEnum):
    OUTLIERS = "outliers"
    DM = "dm"
    JUMPS = "jumps"
    REDUCED_CHI2 = "reduced_chi2"
    RESIDUAL_DISTRIBUTION = "residual_distribution"


class TimingDiagnoseInput(BaseModel):
    model_config = FORBID
    par: str
    tim: str
    root: str | None = None
    backend: Literal["pint", "tempo2"] = "pint"
    checks: list[DiagnoseCheck] = Field(default_factory=lambda: list(DiagnoseCheck), max_length=5)
    outlier_sigma: float = Field(default=5, ge=1, le=20)
    generate_plot: bool = True


class ArtifactKindArg(StrEnum):
    ARCHIVE = "archive"
    EPHEMERIS = "ephemeris"
    TIM = "tim"
    PAR = "par"
    PROFILE = "profile"
    MASK = "mask"
    PLOT = "plot"
    RESIDUALS = "residuals"
    LOG = "log"
    CATALOG = "catalog"
    JSON = "json"
    OTHER = "other"


class ArtifactListInput(BaseModel):
    model_config = FORBID
    kind: ArtifactKindArg | None = None
    limit: int = Field(default=50, ge=1, le=500)
    cursor: str | None = None
    created_after: str | None = None


class JobGetInput(BaseModel):
    model_config = FORBID
    job_id: str = Field(pattern=r"^job_[A-Za-z0-9_-]{8,64}$")


class JobCancelInput(BaseModel):
    model_config = FORBID
    job_id: str = Field(pattern=r"^job_[A-Za-z0-9_-]{8,64}$")
    force: bool = False
