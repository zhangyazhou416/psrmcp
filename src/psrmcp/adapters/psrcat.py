"""psrcat adapter: query builders, row parser and ephemeris export.

Verified against psrcat 2.6.1 (real install, 2026-10-24): name selection is
positional (no -name flag); column keywords are raj/decj (ra/dec unknown);
`-o short` rows carry a leading row-number token; there is no -db flag (the
catalogue is located via the PSRCAT_FILE environment variable), no -binary
flag and no cone-search flags — cone and binary filtering happen in Python
(tools/catalog.py) because -boundary mishandles negative DEC. An unknown
parameter inside `-l` exits 0 with NO output and no stderr, so raw
expressions (catalog_query) are identifier-checked up front — otherwise a
typo would silently look like an empty result.
"""

from __future__ import annotations

import re
from typing import Any

from ..errors import invalid_input

# psrcat -c columns -> record fields (TOOLS.md section 2 CatalogRecord).
# raj/decj are the accepted keywords; ver/assoc(tel) do not exist. assoc is the
# catalogue association string and is surfaced under the observatory field
# (closest contract slot; ATNF exposes no per-row observatory).
# `binary` is the real BINARY model (T2/DD/...; isolated pulsars get "*");
# `bincomp` is the companion composition descriptor (e.g. "He[vbv+08]"), not a
# model name.
PSRCAT_COLUMNS: list[str] = "name raj decj p0 f0 p1 dm binary assoc".split()

PSRCAT_FIELD: dict[str, str] = dict(
    kv.split(":", 1)
    for kv in (
        "name:name raj:ra_str decj:dec_str p0:p0_s f0:f0_hz p1:p1 dm:dm "
        "binary:binary_model assoc:observatory"
    ).split()
)

# Extra single-token columns selectable via catalog_search.fields /
# catalog_query.columns. All verified single-token in `-o short` against the
# installed psrcat 2.6.1 (2026-11-04): numeric except none so far, so the
# generic row parser can map tokens positionally. Notably absent: `type`
# (values carry citation suffixes like "HE[bt93],HE[aaa+09f]" and -l string
# equality does not match them — verified empirically).
EXTRA_COLUMNS: tuple[str, ...] = (
    "age", "bsurf", "edot", "dist", "s400", "s1400", "w50", "w10",
    "pb", "a1", "ecc", "om", "gl", "gb",
)


def resolve_columns(fields: list[str]) -> list[str]:
    """Validate requested columns against the whitelist; keep query order.

    The default columns are always included so every row still yields a full
    CatalogRecord; requested extras are appended (deduplicated).
    """
    lowered = [f.strip().lower() for f in fields]
    unknown = [f for f in lowered if f not in PSRCAT_COLUMNS and f not in EXTRA_COLUMNS]
    if unknown:
        raise invalid_input(
            f"unknown psrcat column(s): {', '.join(unknown)}",
            hint=f"allowed extra columns: {', '.join(EXTRA_COLUMNS)}",
        )
    out = list(PSRCAT_COLUMNS)
    for f in lowered:
        if f not in out:
            out.append(f)
    return out


_STRING_LIT_RE = re.compile(r'"[^"]*"|\'[^\']*\'')
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def validate_expression(expression: str) -> str:
    """Sanitize and identifier-check a raw `-l` expression for catalog_query.

    psrcat runs without a shell (argv list), so shell metacharacters are not an
    injection vector; we still reject anything outside printable ASCII to keep
    the expression a single well-formed argument. Because an unknown -l
    parameter makes psrcat exit 0 with no output (verified against 2.6.1),
    every bare identifier must be a known column — string literals are exempt,
    an identifier directly followed by "(" is treated as a function call and
    passes through, and one preceded by a digit (scientific notation `1e3`)
    is part of the number literal.
    """
    expr = expression.strip()
    if not (3 <= len(expr) <= 500):
        raise invalid_input("expression length must be 3-500 characters")
    if any(not (32 <= ord(ch) <= 126) for ch in expr):
        raise invalid_input("expression must contain printable ASCII only (no control characters)")
    known = set(PSRCAT_COLUMNS) | set(EXTRA_COLUMNS)
    literal_spans = [m.span() for m in _STRING_LIT_RE.finditer(expr)]
    for m in _IDENT_RE.finditer(expr):
        name, start, end = m.group(0), m.start(), m.end()
        if any(s <= start < e for s, e in literal_spans):
            continue  # inside a string literal
        if start > 0 and expr[start - 1] in "0123456789._":
            continue  # part of a larger token (e.g. the exponent of 1e3)
        if expr[end:].lstrip().startswith("("):
            continue  # function call
        if name in known:
            continue
        raise invalid_input(
            f"unknown psrcat parameter in expression: {name}",
            hint=(f"psrcat silently returns nothing for unknown -l parameters; "
                  f"allowed columns: {', '.join(sorted(known))}"),
        )
    return expr


def psrcat_base(binary: str) -> list[str]:
    # No -db flag exists: psrcat locates the catalogue via $PSRCAT_FILE
    # (callers set it in the subprocess environment).
    return [binary, "-nohead", "-o", "short"]


def catalog_get_cmd(binary: str, name: str, db_path: str | None = None) -> list[str]:
    # Name selection is positional; -name is not a psrcat flag.
    return psrcat_base(binary) + [name, "-c", " ".join(PSRCAT_COLUMNS)]


def catalog_search_cmd(
    binary: str,
    *,
    conditions: list[str],
    columns: list[str] | None = None,
) -> list[str]:
    # Cone and binary-only filtering are done in Python (no CLI flags; the
    # -boundary alternative fails on negative DEC).
    args = psrcat_base(binary)
    if conditions:
        args += ["-l", " && ".join(conditions)]
    args += ["-c", " ".join(columns or PSRCAT_COLUMNS)]
    return args


def psrcat_ephem_cmd(binary: str, name: str) -> list[str]:
    """`psrcat -e`: complete TEMPO-format ephemeris (BINARY/PB/ECC/CLK/EPHEM...)."""
    return [binary, name, "-e"]


def catalog_query_cmd(binary: str, *, expression: str, columns: list[str] | None = None) -> list[str]:
    """Raw `-l` power query (catalog_query tool). Expression is pre-validated."""
    args = psrcat_base(binary)
    args += ["-l", expression]
    args += ["-c", " ".join(columns or PSRCAT_COLUMNS)]
    return args


def parse_psrcat_row_columns(text: str, columns: list[str]) -> dict[str, Any]:
    """Parse one `-o short` row against an explicit column list.

    Returns a dict keyed by the psrcat COLUMN names (not record field names).
    All whitelisted extras are single-token, so tokens map positionally; if a
    multi-word value still widens the row, surplus tokens are folded into the
    last column (same right-greedy assumption as the default parser).
    """
    tokens = text.split()
    if tokens and tokens[0].isdigit():
        tokens = tokens[1:]
    if len(tokens) < len(columns):
        raise ValueError(f"psrcat row has {len(tokens)} tokens, expected {len(columns)} columns")
    if len(tokens) > len(columns):
        head = tokens[: len(columns) - 1]
        tokens = head + [" ".join(tokens[len(columns) - 1 :])]
    row: dict[str, Any] = {}
    for col, raw in zip(columns, tokens):
        row[col] = _num(raw)
    return row


def parse_psrcat_row(text: str) -> dict[str, Any]:
    """Parse one whitespace-separated psrcat data row into a record dict."""
    tokens = text.split()
    # -o short prepends a per-row line-number token even with -nohead.
    if tokens and tokens[0].isdigit():
        tokens = tokens[1:]
    if len(tokens) < len(PSRCAT_COLUMNS):
        raise ValueError(f"psrcat row has {len(tokens)} columns, expected {len(PSRCAT_COLUMNS)}")
    # assoc(tel) may contain no spaces in -o short mode; if it does, the row is
    # longer: associate tokens greedily from the right (ver is last).
    if len(tokens) > len(PSRCAT_COLUMNS):
        extra = len(tokens) - len(PSRCAT_COLUMNS)
        observatory = " ".join(tokens[8 : 9 + extra])
        tokens = tokens[:8] + [observatory] + tokens[9 + extra :]
    record: dict[str, Any] = {}
    for col, raw in zip(PSRCAT_COLUMNS, tokens):
        field = PSRCAT_FIELD.get(col, col)
        record[field] = _num(raw)
    return record


_PSRCAT_NA = {"*", "-", "INVALID"}


def _num(raw: str) -> Any:
    if raw.strip() in _PSRCAT_NA:
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


# ---------------------------------------------------------------------------
# Ephemeris export. The full ephemeris comes straight from `psrcat -e`
# (verified to contain BINARY/PB/ECC/CLK/EPHEM; loadable by PINT/tempo2
# as-is); build_par_text only serves the synthetic fallback path for setups
# without a local catalogue (online psrqpy) or with a position override.
# ---------------------------------------------------------------------------

RA_HOURS_RE = re.compile(r"^(\d+):(\d+)(?::([\d.]+))?$")  # h:m[:s] — both forms occur in the catalogue


def hms_to_deg(hms: str) -> float:
    m = RA_HOURS_RE.match(hms.strip())
    if not m:
        raise invalid_input(f"Cannot parse RA: {hms!r}")
    h, mi = float(m.group(1)), float(m.group(2))
    sec = float(m.group(3)) if m.group(3) else 0.0
    return (h + mi / 60 + sec / 3600) * 15.0


def hms_to_deg_or_num(hms: str) -> float:
    try:
        return float(hms)
    except ValueError:
        return hms_to_deg(hms)


DMS_RE = re.compile(r"^([+-]?\d+):(\d+(?:\.\d+)?)(?::([\d.]+))?$")  # d:m[.frac][:s]


def dms_to_deg(dms: str) -> float:
    """Parse sign d:m[:s] (psrcat emits two- or three-part DEC); numeric passthrough."""
    m = DMS_RE.match(dms.strip())
    if not m:
        try:
            return float(dms)
        except ValueError as exc:
            raise invalid_input(f"Cannot parse DEC: {dms!r}") from exc
    sign = -1.0 if m.group(1).startswith("-") else 1.0
    d = abs(float(m.group(1)))
    mi = float(m.group(2))
    sec = float(m.group(3)) if m.group(3) else 0.0
    return sign * (d + mi / 60 + sec / 3600)


def build_par_text(
    *,
    name: str,
    ra_deg: float | None,
    dec_deg: float | None,
    p0_s: float,
    dm: float,
    f0_hz: float | None = None,
    p1: float | None = None,
    peepoch_mjd: float | None = None,
    posepoch_mjd: float | None = None,
    binary_model: str | None = None,
    tempo2: bool = False,
) -> str:
    lines = [f"PSRJ            {name}"]
    if ra_deg is not None:
        lines.append(f"RAJ             {_deg_to_hms(ra_deg)}")
    if dec_deg is not None:
        lines.append(f"DECJ            {_deg_to_dms(dec_deg)}")
    lines.append(f"P0              {p0_s!r}")
    if f0_hz is not None:
        lines.append(f"F0              {f0_hz!r}")
    if p1 is not None:
        lines.append(f"P1              {p1!r}")
    lines.append(f"DM              {dm!r}")
    lines.append("PEPOCH          " + (f"{peepoch_mjd!r}" if peepoch_mjd is not None else "50000.0"))
    lines.append("POSEPOCH        " + (f"{posepoch_mjd!r}" if posepoch_mjd is not None else "50000.0"))
    if binary_model:
        lines.append(f"BINARY          {binary_model}")
    lines.append("CLK             " + ("TT(TAI)" if tempo2 else "UTC(NIST)"))
    lines.append("UNITS           " + ("TDB" if tempo2 else "TCB"))
    lines.append("TIMEEPH         " + ("T2 Barycentric" if tempo2 else "FB90"))
    return "\n".join(lines) + "\n"


def _deg_to_hms(deg: float) -> str:
    hours = deg / 15.0
    h = int(hours)
    m_full = (hours - h) * 60
    mi = int(m_full)
    s = (m_full - mi) * 60
    return f"{h:02d}:{mi:02d}:{s:07.4f}"


def _deg_to_dms(deg: float) -> str:
    sign = "-" if deg < 0 else "+"
    a = abs(deg)
    d = int(a)
    m_full = (a - d) * 60
    mi = int(m_full)
    s = (m_full - mi) * 60
    return f"{sign}{d:02d}:{mi:02d}:{s:07.4f}"
