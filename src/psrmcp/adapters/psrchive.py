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
