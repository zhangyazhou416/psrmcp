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


def psrconv_cmd(binary: str, source: str, out_dir: str, out_ext: str) -> list[str]:
    # NOTE: psrconv writes <input-basename>.<ext> NEXT TO THE INPUT FILE — it
    # has no output-directory flag. Callers must copy the source into their job
    # tmp dir and run there (out_dir parameter is used only as cwd by callers).
    return [binary, "-e", out_ext, source]


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
    # -qc: quiet — print "key=value" pairs without the leading filename.
    return [binary, "-qc", "-c", ",".join(keys), source]


def psredit_set_cmd(binary: str, source: str, assignments: dict[str, str]) -> list[str]:
    # -e edit writes the modified archive as <basename>.edit in the process
    # cwd (standard output option: the extension is replaced).
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
