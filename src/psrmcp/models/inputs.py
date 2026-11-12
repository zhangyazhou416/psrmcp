"""Input models for all 25 tools (TOOLS.md section 5 schemas, 1:1).

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
    pb_min_s: float | None = Field(default=None, gt=0)
    pb_max_s: float | None = Field(default=None, gt=0)
    flux_min_mjy: float | None = Field(default=None, ge=0)
    gb_min: float | None = Field(default=None, ge=-90, le=90)
    gb_max: float | None = Field(default=None, ge=-90, le=90)
    binary_only: bool = False
    fields: list[str] | None = None
    limit: int = Field(default=50, ge=1, le=200)
    cursor: str | None = None

    @model_validator(mode="after")
    def _check(self) -> "CatalogSearchInput":
        if self.radius_deg is not None and (self.ra_deg is None or self.dec_deg is None):
            raise ValueError("radius_deg must be given together with ra_deg and dec_deg")
        if self.p0_min_s is not None and self.p0_max_s is not None and self.p0_min_s > self.p0_max_s:
            raise ValueError("p0_min_s must not be greater than p0_max_s")
        if self.dm_min is not None and self.dm_max is not None and self.dm_min > self.dm_max:
            raise ValueError("dm_min must not be greater than dm_max")
        if self.pb_min_s is not None and self.pb_max_s is not None and self.pb_min_s > self.pb_max_s:
            raise ValueError("pb_min_s must not be greater than pb_max_s")
        if self.gb_min is not None and self.gb_max is not None and self.gb_min > self.gb_max:
            raise ValueError("gb_min must not be greater than gb_max")
        has_filter = any(v is not None for v in
                         (self.name, self.ra_deg, self.p0_min_s, self.p0_max_s, self.dm_min, self.dm_max,
                          self.pb_min_s, self.pb_max_s, self.flux_min_mjy, self.gb_min, self.gb_max))
        if not has_filter and not self.binary_only:
            raise ValueError("provide at least one filter (name/position/period/DM/binary_only)")
        return self


class CatalogQueryInput(BaseModel):
    """Power-user escape hatch: a raw psrcat `-l` expression (catalog_query).

    Runs against the local psrcat backend only. The expression is sanitized
    (printable ASCII, length-capped) in the adapter; columns come from a
    whitelist, so a malformed expression can at worst fail the query — it
    cannot touch files or spawn anything.
    """

    model_config = FORBID
    expression: str = Field(min_length=3, max_length=500)
    columns: list[str] | None = Field(default=None, max_length=10)
    limit: int = Field(default=50, ge=1, le=200)
    cursor: str | None = None


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
            raise ValueError("one of pulsar or artifact must be provided")
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


class ArchiveTransformInput(BaseModel):
    """Scrunch / clean a folded archive (pam). At least one operation is
    required; the input file is never modified — the result is a new artifact.
    Scrunch targets are given as FINAL counts; an integral reduction factor is
    derived from the original dimensions (infeasible targets are skipped with
    a warning, following profile_standardize's convention)."""

    model_config = FORBID
    archive: str
    root: str | None = None
    fscrunch_to: int | None = Field(default=None, ge=1, le=8192)
    tscrunch_to: int | None = Field(default=None, ge=1, le=10000)
    sum_pols: bool = False
    binscrunch_to: int | None = Field(default=None, ge=1, le=1048576)
    remove_baseline: bool = False
    centre: bool = False
    output_name: str | None = Field(default=None, max_length=128)
    execution_mode: ExecutionMode = ExecutionMode.AUTO

    @model_validator(mode="after")
    def _check(self) -> "ArchiveTransformInput":
        ops = (self.fscrunch_to is not None, self.tscrunch_to is not None,
               self.sum_pols, self.binscrunch_to is not None,
               self.remove_baseline, self.centre)
        if not any(ops):
            raise ValueError("at least one transform operation is required")
        return self


class ArchiveEditInput(BaseModel):
    """Query and/or set archive metadata via psredit. `query` keys report
    values from the INPUT archive (pre-edit); `set` assignments are written to
    a new artifact. Key syntax: colon-separated attribute paths with optional
    [index] selectors (e.g. name, dm, int:mjd, int[0]:wt)."""

    model_config = FORBID
    archive: str
    root: str | None = None
    query: list[str] = Field(default_factory=list, max_length=64)
    set: dict[str, str] = Field(default_factory=dict, max_length=64)
    output_name: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def _check(self) -> "ArchiveEditInput":
        from ..adapters.psrchive import psredit_key_ok

        for key in self.query:
            if not psredit_key_ok(key):
                raise ValueError(f"invalid psredit query key: {key!r}")
        for key in self.set:
            if not psredit_key_ok(key):
                raise ValueError(f"invalid psredit assignment key: {key!r}")
        if not self.query and not self.set:
            raise ValueError("at least one of query or set is required")
        return self


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
            raise ValueError("provide exactly one of mask channels or auto")
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


class ProfileOptimizeInput(BaseModel):
    """Search period and DM for the maximum profile S/N (pdmp). Offsets/
    ranges/steps follow pdmp's semantics: trials run over
    reference + offset +/- half_range stepping by step; None ranges use
    pdmp's natural defaults. The input archive is read-only; results are
    reported as corrections to apply (no archive is written)."""

    model_config = FORBID
    archive: str
    root: str | None = None
    dm_offset: float = 0.0
    dm_half_range: float | None = Field(default=None, ge=0)
    dm_step: float | None = Field(default=None, gt=0)
    period_ms: float | None = Field(default=None, gt=0)
    period_offset_us: float = 0.0
    period_half_range_us: float | None = Field(default=None, ge=0)
    period_step_us: float | None = Field(default=None, gt=0)
    max_channels: int | None = Field(default=None, ge=1, le=8192)
    max_subints: int | None = Field(default=None, ge=1, le=10000)
    standard_profile: str | None = None
    execution_mode: ExecutionMode = ExecutionMode.AUTO


class SinglePulseAnalyzeInput(BaseModel):
    """Single-pulse analysis (psrspa). At least one analysis mode is
    required: phase-resolved histograms, maximum amplitude in phase range,
    linear-polarization vs PA scatter data, or per-pulse detection. The
    input archive is read-only; all outputs are new text artifacts."""

    model_config = FORBID
    archive: str
    root: str | None = None
    histograms: list[Literal["flux", "poldeg", "polang", "linear", "circular"]] = Field(
        default_factory=list, max_length=5
    )
    find_max_amplitude: bool = False
    scatter_linear_pa: bool = False
    find_pulses: bool = False
    pulse_algorithm: str = "above"
    min_pulse_bins: int | None = Field(default=None, ge=16, le=65536)
    phase_ranges: list[float] | None = Field(default=None, min_length=2, max_length=16)
    hist_bins: int | None = Field(default=None, ge=2, le=10000)
    flux_hist_max: float | None = Field(default=None, gt=0)
    pol_threshold: float | None = Field(default=None, gt=0)
    log_scale: bool = False
    mean_offset: float | None = None
    std_profile: str | None = None
    execution_mode: ExecutionMode = ExecutionMode.AUTO

    @model_validator(mode="after")
    def _check_modes(self):
        from ..adapters.psrchive import psrspa_algorithm_ok

        if not (self.histograms or self.find_max_amplitude
                or self.scatter_linear_pa or self.find_pulses):
            raise ValueError(
                "at least one analysis mode is required: histograms, "
                "find_max_amplitude, scatter_linear_pa, or find_pulses"
            )
        if self.find_pulses and (self.histograms or self.find_max_amplitude
                                 or self.scatter_linear_pa):
            raise ValueError(
                "find_pulses is exclusive of the histogram modes: psrspa's -a "
                "pulse finder returns early and silently skips "
                "histograms/find_max_amplitude/scatter"
            )
        if self.phase_ranges:
            vals = self.phase_ranges
            if len(vals) % 2:
                raise ValueError("phase_ranges must be flattened min,max pairs (even count)")
            for i in range(0, len(vals), 2):
                lo, hi = vals[i], vals[i + 1]
                if not (0.0 <= lo < hi <= 1.0):
                    raise ValueError(f"invalid phase range [{lo}, {hi}]: need 0 <= min < max <= 1")
        if self.find_pulses and not psrspa_algorithm_ok(self.pulse_algorithm):
            raise ValueError(
                f"pulse_algorithm {self.pulse_algorithm!r} is not an allowed psrspa "
                "algorithm (above/consecutive/cumulative/set/exponential/normal/minimum, "
                "optionally ':option=value' pairs)"
            )
        return self


class RotationMeasureFitInput(BaseModel):
    """Fit the rotation measure (rmfit). Prerequisite: calibrated data
    (pac), tscrunched and dedispersed. At least one method is required;
    brute_force and rm_range are mutually exclusive (rm_range gives explicit
    search bounds min,max,steps, brute_force uses rmfit's auto bounds).
    quadratic_window / quadratic_per_bin are the Noutsos+2008 pulse phase
    windows in turns (0..1)."""

    model_config = FORBID
    archive: str
    root: str | None = None
    brute_force: bool = False
    rm_range: list[float] | None = Field(default=None, min_length=3, max_length=3)
    refine: bool = False
    quadratic_window: list[float] | None = Field(default=None, min_length=2, max_length=2)
    quadratic_per_bin: list[float] | None = Field(default=None, min_length=2, max_length=2)
    threshold_sigma: float | None = Field(default=None, gt=0)
    rm_max_upper: float | None = Field(default=None, ge=0)
    rm_per_dm: float | None = Field(default=None, gt=0)
    rm_initial: float | None = None
    save_rm_archive: bool = False
    output_name: str | None = Field(default=None, max_length=128)
    execution_mode: ExecutionMode = ExecutionMode.AUTO

    @model_validator(mode="after")
    def _check_methods(self):
        if not (self.brute_force or self.rm_range or self.refine
                or self.quadratic_window or self.quadratic_per_bin):
            raise ValueError(
                "at least one RM estimation method is required: brute_force, "
                "rm_range, refine, quadratic_window, or quadratic_per_bin"
            )
        if self.brute_force and self.rm_range:
            raise ValueError("brute_force and rm_range are mutually exclusive")
        if self.rm_range is not None:
            lo, hi, steps = self.rm_range
            if not lo < hi:
                raise ValueError(f"rm_range min must be < max (got {lo}, {hi})")
            if not (float(steps).is_integer() and 2 <= steps <= 100000):
                raise ValueError(f"rm_range steps must be an integer in [2, 100000] (got {steps})")
        for name in ("quadratic_window", "quadratic_per_bin"):
            window = getattr(self, name)
            if window is not None and not (0.0 <= window[0] < window[1] <= 1.0):
                raise ValueError(f"{name} must be 0 <= x1 < x2 <= 1 (phase turns), got {window}")
        return self


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
            raise ValueError("mjd_range must be ascending")
        if self.freq_range_mhz and self.freq_range_mhz[0] > self.freq_range_mhz[1]:
            raise ValueError("freq_range_mhz must be ascending")
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
