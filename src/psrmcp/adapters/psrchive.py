"""psrchive suite adapter: command builders + parsers.

All flag mappings verified against the real installed stack
(PSRCHIVE 2026-10-16, commit d4b4e2d9c) on 2026-10-24; live behaviour is
re-checked by tests/contract. Parse-only functions are pure and unit-tested
against recorded output samples.
"""

from __future__ import annotations

from typing import Any

# pat -A choices that the installed pat accepts (PACE does NOT exist).
PAT_ALGORITHMS: tuple[str, ...] = ("PGS", "GIS", "PIS", "ZPS", "SIS", "FDM")

# vap keyword -> result field. The installed vap uses "nsub" (nsubint yields
# INVALID); the result field stays "nsubint" for API stability.
VAP_FIELD: dict[str, str] = dict(
    kv.split(":", 1)
    for kv in ["name:name", "ra:ra_str", "dec:dec_str", "dm:archive_dm", "period:period_s", "freq:freq_mhz", "bw:bandwidth_mhz", "nchan:nchan", "npol:npol", "nbin:nbin", "nsub:nsubint", "duration:duration_s", "telescope:telescope", "backend:backend", "receiver:receiver"]
)

# vap -c keyword order (must match VAP_FIELD values). All verified valid.
VAP_KEYS: list[str] = ["name", "ra", "dec", "dm", "period", "freq", "bw", "nchan", "npol", "nbin", "nsub", "duration", "telescope"]

# vap prints these literal strings for missing values / unknown keywords.
_PSRCHIVE_NA = {"INVALID", "-"}


def parse_vap(text: str) -> dict[str, Any]:
    """Parse `vap -c <keys> <file>` output: a header row then one data row.

    The installed vap names its leading column "filename" (older versions
    printed "File"). Missing values print as INVALID. Returns
    keyword->parsed value with best-effort typing; missing values map to None.
    """
    header: str | None = None
    data: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        first = stripped.split()[0].lower()
        if first in ("file", "filename", "mode") or stripped.lower().startswith("vap"):
            header = stripped
            continue
        data = stripped  # last data row wins (vap prints one row per file)
    if data is None:
        return {}
    values = data.split()
    if header is not None:
        htoks = header.split()
        if len(htoks) == len(values) and htoks[0].lower() in ("file", "filename"):
            values = values[1:]
    fields: dict[str, Any] = {}
    if len(values) >= len(VAP_KEYS):
        for key, raw in zip(VAP_KEYS, values):
            fields[VAP_FIELD.get(key, key)] = _coerce(raw)
    elif values:
        # Column count mismatch: keep raw tokens under the keyword names.
        for key, raw in zip(VAP_KEYS, values):
            fields[VAP_FIELD.get(key, key)] = raw
        fields["W_vap_column_mismatch"] = True
    return fields


def _coerce(raw: str) -> Any:
    if raw.strip().upper() in _PSRCHIVE_NA:
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


def parse_psrstat(text: str) -> dict[str, Any]:
    """Parse `psrstat -c <exprs> <file>` output.

    The installed psrstat prints one row per archive:
    `<filename> snr=133.86 wt=- ...` (missing values print as "-").
    Returns expression->value (None when missing); the leading filename is
    exposed under "_filename". Also tolerates the -Q bare-value variant
    (`<filename> v1 v2 ...`) by collecting bare tokens under "_positional".
    """
    out: dict[str, Any] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        positional: list[str] = []
        for tok in stripped.split():
            key, sep, val = tok.partition("=")
            if sep and key:
                out[key.strip()] = _coerce(val.strip())
            else:
                if out.get("_filename") is None and ("/" in tok or "." in tok):
                    out["_filename"] = tok
                else:
                    positional.append(tok)
        if positional:
            out.setdefault("_positional", []).extend(_coerce(t) for t in positional)
    return out


# ---------------------------------------------------------------------------
# Command builders. Every function returns a pure argv list — no shell.
# ---------------------------------------------------------------------------


def vap_cmd(binary: str, archive: str, *, extra_keys: list[str] | None = None) -> list[str]:
    keys = list(VAP_KEYS)
    if extra_keys:
        keys += [k for k in extra_keys if k not in keys]
    return [binary, "-c", ",".join(keys), archive]


def psrstat_cmd(binary: str, archive: str, expressions: list[str]) -> list[str]:
    # No -Q: plain mode prints "key=value" pairs, which parse self-describingly.
    return [binary, "-c", ",".join(expressions), archive]


# psrconv -o names resolved via Archive::new_Archive (agent get_name):
# "PSRFITS" / "Timer" / "ASCII". EPN is deliberately absent: in PSRCHIVE,
# EPNArchive::unload is not implemented, so psrconv can read EPN but never
# write it.
PSRCONV_FORMAT: dict[str, str] = {
    "psrfits": "PSRFITS",
    "psrarchive": "Timer",
    "ascii": "ASCII",
}


def psrconv_cmd(binary: str, source: str, out_ext: str, output_format: str) -> list[str]:
    # NOTE: psrconv writes <input-basename>.<ext> NEXT TO THE INPUT FILE — it
    # has no output-directory flag. Callers must copy the source into their job
    # tmp dir and run there (the file lands in the process cwd).
    # -o is passed explicitly: without it psrconv silently falls back to
    # PSRFITS on cfitsio builds even when a Timer/ASCII output was intended.
    return [binary, "-o", PSRCONV_FORMAT[output_format], "-e", out_ext, source]


def psradd_cmd(binary: str, sources: list[str], out_path: str) -> list[str]:
    # psradd has -o fname (no -N); addition weighting is psrchive's natural
    # (SNR-derived) weighting — there are no weighting flags to pass.
    return [binary, "-o", out_path, *sources]


def paz_cmd(binary: str, source: str, channel_mask: str, out_dir: str) -> list[str]:
    # paz -z takes a comma-separated channel list ("0,3,10-12"). Without an
    # output flag paz writes nothing; "-e paz" writes <basename>.paz NEXT TO
    # THE INPUT FILE. Callers copy the source into their job tmp dir first
    # and glob *.paz there (out_dir parameter is only the documented intent).
    return [binary, "-z", channel_mask, "-e", "paz", source]


def pat_cmd(binary: str, source: str, standard: str, *, algorithm: str = "PGS",
            tempo2_format: bool = True) -> list[str]:
    if algorithm not in PAT_ALGORITHMS:
        raise ValueError(f"pat algorithm must be one of {PAT_ALGORITHMS}, got {algorithm!r}")
    args = [binary, "-f", "tempo2" if tempo2_format else "tempo1"]
    args += ["-A", algorithm]
    args += ["-s", standard]
    args.append(source)
    return args


def pam_cmd(binary: str, source: str, out_ext: str, *, bin_factor: int | None = None,
            tscrunch: bool = True) -> list[str]:
    # pam -T tscrunches to one subint; -e ext writes <basename>.<ext> into the
    # process cwd. Used to build a standard profile (pam -T -e std src.ar).
    args = [binary]
    if tscrunch:
        args.append("-T")
    if bin_factor is not None and bin_factor > 1:
        args += ["-b", str(bin_factor)]
    args += ["-e", out_ext, source]
    return args


def pam_scrunch_cmd(
    binary: str,
    source: str,
    out_ext: str,
    *,
    freq_factor: int | None = None,
    time_factor: int | None = None,
    sum_pols: bool = False,
    bin_factor: int | None = None,
    remove_baseline: bool = False,
    centre: bool = False,
) -> list[str]:
    """archive_transform backend (pam). -F/-T/-b take integral SCRUNCH FACTORS
    (2 = halve the resolution; factor 1 is a no-op and omitted), matching the
    documented `pam -FTp` idiom; -p sums polarisations, -r removes the
    off-pulse baseline, -c centres the profile. -e ext writes
    <basename>.<ext> into the process cwd (same contract as pam_cmd).
    TODO(contract): re-verify -F/-T factor semantics against the installed
    pam in tests/contract before the first release that exposes them."""
    args = [binary]
    for flag, factor in (("-F", freq_factor), ("-T", time_factor), ("-b", bin_factor)):
        if factor is not None and factor > 1:
            args += [flag, str(factor)]
    if sum_pols:
        args.append("-p")
    if remove_baseline:
        args.append("-r")
    if centre:
        args.append("-c")
    args += ["-e", out_ext, source]
    return args


def psredit_get_cmd(binary: str, source: str, keys: list[str]) -> list[str]:
    # -q and -c must be SEPARATE argv tokens: the installed psredit's option
    # parser mis-reads the merged short form "-qc" (it swallows the 'c' and
    # then reports the query keys "not found" while still exiting 0;
    # verified against PSRCHIVE 2026-10-16 on 2026-11-13).
    return [binary, "-q", "-c", ",".join(keys), source]


def psredit_set_cmd(binary: str, source: str, assignments: dict[str, str]) -> list[str]:
    # -e edit writes the modified archive as <basename>.edit NEXT TO THE
    # SOURCE (not in the process cwd; standard output option: the extension
    # is replaced).
    return [binary, "-c", ",".join(f"{k}={v}" for k, v in assignments.items()),
            "-e", "edit", source]


def pdmp_cmd(
    binary: str,
    source: str,
    *,
    dm_offset: float = 0.0,
    dm_half_range: float | None = None,
    dm_step: float | None = None,
    period_ms: float | None = None,
    period_offset_us: float = 0.0,
    period_half_range_us: float | None = None,
    period_step_us: float | None = None,
    max_channels: int | None = None,
    max_subints: int | None = None,
    std_profile: str | None = None,
) -> list[str]:
    # -g /null: compute and print the best-fit values without opening a
    # PGPLOT window (psrmcp policy: plots come from matplotlib, not PGPLOT).
    args = [binary, "-g", "/null"]
    if period_ms is not None:
        args += ["-p", str(period_ms)]
    if period_offset_us:
        args += ["-po", str(period_offset_us)]
    if period_half_range_us is not None:
        args += ["-pr", str(period_half_range_us)]
    if period_step_us is not None:
        args += ["-ps", str(period_step_us)]
    if dm_offset:
        args += ["-do", str(dm_offset)]
    if dm_half_range is not None:
        args += ["-dr", str(dm_half_range)]
    if dm_step is not None:
        args += ["-ds", str(dm_step)]
    if max_channels is not None:
        args += ["-mc", str(max_channels)]
    if max_subints is not None:
        args += ["-ms", str(max_subints)]
    if std_profile is not None:
        args += ["-s", std_profile]
    args.append(source)
    return args


PDMP_PATTERNS: dict[str, tuple[str, ...]] = {
    # substrings matched case-insensitively against "Label = value" lines
    "best_period_ms": ("period",),
    "best_dm": ("dm",),
    "best_snr": ("signal-to-noise", "snr", "s/n"),
}


def parse_pdmp(text: str) -> dict[str, Any]:
    """Parse pdmp's stdout (text mode, -g /null): best-effort extraction of
    "label = value [+/- err]" lines. pdmp labels drifted across releases, so
    matching is by substring (see PDMP_PATTERNS); unknown lines are ignored.
    The first match per field wins; errors attach as <field>_error."""
    out: dict[str, Any] = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        label, _, rhs = line.partition("=")
        low = label.strip().lower()
        value = rhs.strip().split()[0] if rhs.strip() else ""
        try:
            num = float(value)
        except ValueError:
            continue
        err: float | None = None
        rest = rhs.strip().split()[1:]
        if rest and rest[0] in ("+/-", "±"):
            try:
                err = float(rest[1])
            except (IndexError, ValueError):
                err = None
        for field, needles in PDMP_PATTERNS.items():
            if field in out:
                continue
            if any(n in low for n in needles):
                out[field] = num
                if err is not None:
                    out[f"{field}_error"] = err
                break
    return out


def psredit_key_ok(key: str) -> bool:
    """Allowlist for psredit attribute paths: letters/digits/underscore,
    colon-separated segments, optional [index] / [i-j] selectors."""
    import re

    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(\[[0-9,\-]+\])?(:[A-Za-z_][A-Za-z0-9_]*(\[[0-9,\-]+\])?)*", key))


# ---------------------------------------------------------------------------
# psrspa (single-pulse analysis)
# ---------------------------------------------------------------------------

# -a algorithm names accepted by the installed psrspa (ProfileWeightFunction
# registry, per the builtin -help=algorithm text). The name may be followed
# by ":option=value" configuration, e.g. "consecutive:threshold=4".
SPA_ALGORITHMS: tuple[str, ...] = (
    "above", "consecutive", "cumulative", "set", "exponential", "normal", "minimum",
)


def psrspa_algorithm_ok(algorithm: str) -> bool:
    import re

    return bool(re.fullmatch(
        r"(above|consecutive|cumulative|set|exponential|normal|minimum)"
        r"(:[A-Za-z0-9_]+=[A-Za-z0-9_.+-]+)*",
        algorithm,
    ))


def psrspa_cmd(
    binary: str,
    source: str,
    *,
    histograms: tuple[str, ...] = (),
    find_max_amplitude: bool = False,
    scatter_linear_pa: bool = False,
    find_pulses: bool = False,
    pulse_algorithm: str = "above",
    min_pulse_bins: int | None = None,
    phase_ranges: tuple[float, ...] = (),
    hist_bins: int | None = None,
    flux_hist_max: float | None = None,
    pol_threshold: float | None = None,
    log_scale: bool = False,
    mean_offset: float | None = None,
    std_profile: str | None = None,
    prefix: str = "spa",
) -> list[str]:
    # Histogram outputs are plain text (<prefix>_<type>_<bins>.dat in the
    # process cwd); no PGPLOT is involved, so no plotting flag is needed.
    args = [binary]
    flag_for = {"flux": "-hf", "poldeg": "-hd", "polang": "-ha", "linear": "-hl", "circular": "-hc"}
    for hist in histograms:
        args.append(flag_for[hist])
    if find_max_amplitude:
        args.append("-fm")
    if scatter_linear_pa:
        args.append("-sla")
    if find_pulses:
        args += ["-a", pulse_algorithm]
        if min_pulse_bins is not None:
            args += ["-N", str(min_pulse_bins)]
    if phase_ranges:
        args += ["-pr", ",".join(f"{v:g}" for v in phase_ranges)]
    if hist_bins is not None:
        args += ["-b", str(hist_bins)]
    if flux_hist_max is not None:
        args += ["-F", str(flux_hist_max)]
    if pol_threshold is not None:
        args += ["-pt", str(pol_threshold)]
    if log_scale:
        args.append("-l")
    if mean_offset is not None:
        args += ["-m", str(mean_offset)]
    if std_profile is not None:
        args += ["-s", std_profile]
    args += ["-pfix", prefix, "-e", "dat"]
    args.append(source)
    return args


# Output file suffixes psrspa writes, keyed by what produced them. The file
# name pattern is <prefix>_<type>_<bins>.dat (histograms) / <prefix>_maxamp.dat
# / <prefix>_pulses.dat.
SPA_HISTOGRAM_TYPES: tuple[str, ...] = ("flux", "poldeg", "polang", "linpol", "cirpol", "linvsang")


def parse_spa_maxamp(text: str) -> list[dict[str, Any]]:
    """Parse <prefix>_maxamp.dat rows:
    `file subint bscrunch (bin_min bin_max max_bin max_amp b_sigma){n_ranges}`.
    bin_min/bin_max are PHASE BIN INDICES (psrspa writes the -pr range as
    bins, verified against the installed build). One dict per (entry, range);
    `file` is kept as "archive_file"."""
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        tokens = stripped.split()
        if len(tokens) < 3:
            continue
        head, rest = tokens[:3], tokens[3:]
        try:
            subint, bscrunch = int(head[1]), int(head[2])
        except ValueError:
            continue
        for i in range(0, len(rest) - 4, 5):
            try:
                rows.append({
                    "archive_file": head[0],
                    "subint": subint,
                    "bscrunch_factor": bscrunch,
                    "bin_min": int(float(rest[i])),
                    "bin_max": int(float(rest[i + 1])),
                    "max_bin": int(float(rest[i + 2])),
                    "max_amplitude": float(rest[i + 3]),
                    "b_sigma": float(rest[i + 4]),
                })
            except ValueError:
                break
    return rows


def parse_spa_pulses(text: str) -> list[dict[str, Any]]:
    """Parse <prefix>_pulses.dat rows:
    `file subint flux phase width bscrunch_factor`."""
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        tokens = stripped.split()
        if len(tokens) < 6:
            continue
        try:
            rows.append({
                "archive_file": tokens[0],
                "subint": int(tokens[1]),
                "flux": float(tokens[2]),
                "phase": float(tokens[3]),
                "width_bins": float(tokens[4]),
                "bscrunch_factor": int(tokens[5]),
            })
        except ValueError:
            continue
    return rows


# ---------------------------------------------------------------------------
# rmfit (rotation measure fitting)
# ---------------------------------------------------------------------------

def rmfit_cmd(
    binary: str,
    source: str,
    *,
    brute_force: bool = False,
    rm_range: tuple[float, float, int] | None = None,
    refine: bool = False,
    quadratic_window: tuple[float, float] | None = None,
    quadratic_per_bin: tuple[float, float] | None = None,
    threshold_sigma: float | None = None,
    rm_max_upper: float | None = None,
    rm_per_dm: float | None = None,
    rm_initial: float | None = None,
    save_rm_ext: str | None = None,
) -> list[str]:
    # No -D/-K: rmfit prints its results as text; PGPLOT plotting is left out
    # by policy (plots come from matplotlib).
    args = [binary]
    if rm_range is not None:
        args += ["-m", f"{rm_range[0]:g},{rm_range[1]:g},{rm_range[2]:d}"]
    elif brute_force:
        args.append("-t")
    if refine:
        args.append("-r")
    if quadratic_window is not None:
        args += ["-w", f"{quadratic_window[0]:g},{quadratic_window[1]:g}"]
    if quadratic_per_bin is not None:
        args += ["-p", f"{quadratic_per_bin[0]:g},{quadratic_per_bin[1]:g}"]
    if threshold_sigma is not None:
        args += ["-T", str(threshold_sigma)]
    if rm_max_upper is not None:
        args += ["-u", str(rm_max_upper)]
    if rm_per_dm is not None:
        args += ["-U", str(rm_per_dm)]
    if rm_initial is not None:
        args += ["-R", str(rm_initial)]
    if save_rm_ext is not None:
        args += ["-x", save_rm_ext]
    args.append(source)
    return args


RMFIT_PATTERNS: dict[str, tuple[str, ...]] = {
    # substrings matched against "label = value [+/- err]" lines (stderr+stdout)
    "best_rm": ("best rm",),
    "search_rm": ("estimated rm",),
    "refined_rm": ("final rotation measure",),
}


def parse_rmfit(text: str) -> dict[str, Any]:
    """Best-effort parse of rmfit's mixed stdout/stderr text. Recognises:
    `Best RM is: X [+/- E]`, `rmfit: estimated RM = X [+/- E]`,
    `final rotation measure = X`, `converged in N iterations`,
    `Chi-squared = C / F = R`. First match per field wins; errors attach as
    <field>_error."""
    out: dict[str, Any] = {}

    def _value_error(rhs: str) -> tuple[float, float | None] | None:
        tokens = rhs.strip().split()
        if not tokens:
            return None
        try:
            num = float(tokens[0])
        except ValueError:
            return None
        err: float | None = None
        if len(tokens) >= 2 and tokens[1] in ("+/-", "±") and len(tokens) >= 3:
            try:
                err = float(tokens[2])
            except ValueError:
                err = None
        return num, err

    for line in text.splitlines():
        low = line.strip().lower()
        if "converged in" in low:
            try:
                out.setdefault("converged_iterations", int(low.split("converged in")[1].split()[0]))
            except (IndexError, ValueError):
                pass
        if "chi-squared" in low and "=" in line:
            try:
                # "Chi-squared = C / F = R" -> reduced is the last number
                numbers = [t for t in line.replace("=", "/").split("/") if t.strip()]
                out.setdefault("chi2_reduced", float(numbers[-1].strip()))
            except (IndexError, ValueError):
                pass
        # "Best RM is: X +/- E" separates label from value with a colon;
        # every other pattern uses "label = value". Normalise the colon form
        # so the same label scan handles both.
        scan = line
        if "=" not in line and ":" in line:
            head, _, tail = line.partition(":")
            if "best rm" in head.lower():
                scan = f"{head} = {tail}"
        if "=" in scan:
            label, _, rhs = scan.partition("=")
            low_label = label.strip().lower()
            ve = _value_error(rhs)
            if ve is None:
                continue
            for field, needles in RMFIT_PATTERNS.items():
                if field in out:
                    continue
                if any(n in low_label for n in needles):
                    out[field] = ve[0]
                    if ve[1] is not None:
                        out[f"{field}_error"] = ve[1]
                    break
    return out


# ---------------------------------------------------------------------------
# pac (polarization + flux calibration)
# ---------------------------------------------------------------------------

# Model selection flags verified against pac.C case blocks: -s = Polar model
# (CalibratorTypes::van02_EqA1), -S = complete Reception model
# (CalibratorTypes::ovhb04); SingleAxis is the default (no flag).
PAC_MODEL_FLAGS: dict[str, str] = {"singleaxis": "", "polar": "-s", "reception": "-S"}


def pac_database_cmd(binary: str, cals_dir: str, database_path: str,
                     extensions: list[str]) -> list[str]:
    # -w writes the database summary; -k names it explicitly (otherwise it is
    # written next to the searched tree); -p selects the searched directory
    # (the whole tree below it is scanned); -u adds calibrator file
    # extensions to the default scan set (cf/pcal/fcal/pfit). -u takes ONE
    # space-separated argument.
    args = [binary, "-w", "-p", cals_dir, "-k", database_path]
    if extensions:
        args += ["-u", " ".join(extensions)]
    return args


def pac_calibrate_cmd(
    binary: str,
    source: str,
    *,
    cals_dir: str | None = None,
    extensions: list[str] | None = None,
    model_file: str | None = None,
    model_type: str = "singleaxis",
    flux_calibration: bool = True,
    match_time: bool = True,
    match_coordinates: bool = True,
    match_instrument: bool = True,
    match_frequency: bool = True,
    match_bandwidth: bool = True,
    allow_opposite_sideband: bool = False,
    match_sequence: str = "any",
    per_channel_matching: bool = False,
    fscrunch_to_cal: bool = False,
    gain_normalize: bool = False,
    out_ext: str = "calib",
) -> list[str]:
    # Calibrate mode. With cals_dir the calibrator database is (re)built
    # in memory from the calibrator files each run (-p, no -w — pac only
    # saves the scan time, the fit happens either way; -u adds the supplied
    # extensions to the scan set exactly as in -w mode); with model_file a
    # pcm/pacv solution archive is applied directly (-A). The matching
    # flags only RELAX criteria: *_match=False disables that criterion
    # entirely; there is no way to tighten pac's defaults.
    args = [binary]
    if cals_dir is not None:
        args += ["-p", cals_dir]
        if extensions:
            args += ["-u", " ".join(extensions)]
    if model_file is not None:
        args += ["-A", model_file]
    model_flag = PAC_MODEL_FLAGS.get(model_type)
    if model_flag:
        args.append(model_flag)
    if not flux_calibration:
        args.append("-P")
    if not match_time:
        args.append("-T")
    if not match_coordinates:
        args.append("-c")
    if not match_instrument:
        args.append("-Z")
    if not match_frequency:
        args.append("-F")
    if not match_bandwidth:
        args.append("-b")
    if allow_opposite_sideband:
        args.append("-o")
    if match_sequence == "before":
        args += ["-m", "b"]
    elif match_sequence == "after":
        args += ["-m", "a"]
    if per_channel_matching:
        args.append("-a")
    if fscrunch_to_cal:
        args.append("-g")
    if gain_normalize:
        args.append("-G")
    args += ["-e", out_ext]
    args.append(source)
    return args


def parse_pac(text: str) -> dict[str, Any]:
    """Parse pac's combined stdout/stderr (best effort). pac reports each
    step as "pac: <sentence>", with multi-line continuations indented by a
    tab ("PolnCalibrator constructed from:" followed by the file list).
    Per-file errors are caught inside pac and printed as
    "pac: Error while handling <file>:" / "pac: Could not flux calibrate
    <file>" while the process STILL EXITS 0 — callers must therefore check
    that the calibrated output file exists."""
    out: dict[str, Any] = {
        "poln_calibrator_from": None,
        "flux_calibrator_from": None,
        "mean_sefd_jy": None,
        "already_poln": False,
        "already_flux": False,
        "poln_complete": False,
        "flux_complete": False,
        "no_flux_db": False,
        "fluxcal_error": None,
        "handling_error": None,
        "unloaded": [],
    }
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        low = stripped.lower()
        if low.startswith("pac: polncalibrator constructed from"):
            out["poln_calibrator_from"] = "; ".join(_pac_continuation(lines, i)) or stripped
        elif low.startswith("pac: fluxcalibrator constructed from"):
            out["flux_calibrator_from"] = "; ".join(_pac_continuation(lines, i)) or stripped
        elif "mean sefd" in low and "=" in stripped:
            try:
                out["mean_sefd_jy"] = float(stripped.rsplit("=", 1)[1].split()[0])
            except (IndexError, ValueError):
                pass
        elif low.endswith("already poln calibrated"):
            out["already_poln"] = True
        elif low.endswith("already flux calibrated"):
            out["already_flux"] = True
        elif low == "pac: poln calibration complete":
            out["poln_complete"] = True
        elif low == "pac: flux calibration complete":
            out["flux_complete"] = True
        elif low == "pac: not performing flux calibration (no database).":
            out["no_flux_db"] = True
        elif low.startswith("pac: could not flux calibrate"):
            out["fluxcal_error"] = _pac_error_block(lines, i, "pac: could not flux calibrate")
        elif low.startswith("pac: error while handling"):
            out["handling_error"] = _pac_error_block(lines, i, "pac: error while handling")
        elif "calibrated archive" in low and low.endswith("unloaded"):
            # "pac: Calibrated archive <name> unloaded"
            tokens = stripped.split()
            if len(tokens) >= 5:
                out["unloaded"].append(tokens[3])
        i += 1
    return out


def _pac_continuation(lines: list[str], start: int) -> list[str]:
    """Indented continuation lines after a "pac: ..." headline (e.g. the
    calibrator file list under 'constructed from:')."""
    parts: list[str] = []
    j = start + 1
    while j < len(lines):
        nxt = lines[j]
        if not nxt.strip() or not nxt[:1].isspace():
            break
        parts.append(nxt.strip())
        j += 1
    return parts


def _pac_error_block(lines: list[str], start: int, lead: str) -> str:
    """File + indented error text of an in-file pac error headline."""
    raw = lines[start].strip()
    idx = raw.lower().index(lead) + len(lead)
    parts = [raw[idx:].strip().rstrip(":").strip()]
    parts += _pac_continuation(lines, start)
    return "; ".join(p for p in parts if p)


def parse_dbase_entries(text: str) -> list[dict[str, Any]]:
    """Parse a pac calibrator database.txt: two header lines
    ("Pulsar::Database::path <dir>", "Pulsar::Database # of entries = N")
    followed by one whitespace-separated line per calibrator:
    `filename type RA DEC MJD bandwidth freq nchan instrument receiver`
    (filename stored relative to the header path; directories were verified
    in Database.C). Interpolator entries have an MJD range with '-' in the
    MJD field and no nchan column. Malformed lines are skipped, mirroring
    Database::load's discarding behaviour."""
    entries: list[dict[str, Any]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("Pulsar::Database"):
            continue
        tok = stripped.split()
        interpolator = "-" in tok[4] if len(tok) > 4 else False
        if interpolator:
            if len(tok) < 9:
                continue
            nchan: int | None = None
            instrument, receiver = tok[6], tok[7]
        else:
            if len(tok) < 10:
                continue
            try:
                nchan = int(tok[7])
            except ValueError:
                continue
            instrument, receiver = tok[8], tok[9]
        try:
            bandwidth = float(tok[5])
            frequency = float(tok[6])
        except (IndexError, ValueError):
            continue
        entries.append({
            "filename": tok[0],
            "obs_type": tok[1],
            "ra_str": tok[2],
            "dec_str": tok[3],
            "mjd": tok[4],
            "bandwidth_mhz": bandwidth,
            "freq_mhz": frequency,
            "nchan": nchan,
            "instrument": instrument,
            "receiver": receiver,
        })
    return entries
