"""psrcat adapter: query builders, row parser and ephemeris export.

Verified against psrcat 2.6.1 (real install, 2026-10-24): name selection is
positional (no -name flag); column keywords are raj/decj (ra/dec unknown);
`-o short` rows carry a leading row-number token; there is no -db flag (the
catalogue is located via the PSRCAT_FILE environment variable), no -binary
flag and no cone-search flags — cone and binary filtering happen in Python
(tools/catalog.py) because -boundary mishandles negative DEC.
"""

from __future__ import annotations

import re
from typing import Any

from ..errors import invalid_input

# psrcat -c columns -> record fields (TOOLS.md section 2 CatalogRecord).
# raj/decj are the accepted keywords; ver/assoc(tel) do not exist. assoc is the
# catalogue association string and is surfaced under the observatory field
# (closest contract slot; ATNF exposes no per-row observatory).
# `binary` 列是真正的 BINARY 模型(T2/DD/...;孤立脉冲星为 "*");
# `bincomp` 是伴星成分描述符(如 "He[vbv+08]"),不能当模型名用。
PSRCAT_COLUMNS: list[str] = "name raj decj p0 f0 p1 dm binary assoc".split()

PSRCAT_FIELD: dict[str, str] = dict(
    kv.split(":", 1)
    for kv in (
        "name:name raj:ra_str decj:dec_str p0:p0_s f0:f0_hz p1:p1 dm:dm "
        "binary:binary_model assoc:observatory"
    ).split()
)


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
# Ephemeris export. 完整星历直接用 `psrcat -e`(实测含 BINARY/PB/ECC/
# CLK/EPHEM,PINT/tempo2 可直接加载);build_par_text 只服务无本地星表
# (psrqpy 在线)或带位置 override 的合成回退路径。
# ---------------------------------------------------------------------------

RA_HOURS_RE = re.compile(r"^(\d+):(\d+)(?::([\d.]+))?$")  # h:m[:s] —— 星表里两种都出现


def hms_to_deg(hms: str) -> float:
    m = RA_HOURS_RE.match(hms.strip())
    if not m:
        raise invalid_input(f"无法解析 RA: {hms!r}")
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
            raise invalid_input(f"无法解析 DEC: {dms!r}") from exc
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
