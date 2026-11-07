"""catalog_search / catalog_get / catalog_query / ephemeris_export (TOOLS.md 5.3-5.6).

Primary backend: psrcat binary (offline via PSRCAT_FILE/-db). Fallback (mode
online/auto, psrcat missing): psrqpy against ATNF online, run in a thread.
catalog_query is the raw `-l` power tool and requires the local psrcat.
"""

from __future__ import annotations

import asyncio
import math
import re
from typing import Any

from mcp.types import ToolAnnotations

from .. import __version__
from ..adapters import psrcat as psa
from ..errors import ErrorCode, PsrmcpError, env_missing, not_found
from ..execution.workspace import utcnow
from ..models.common import ArtifactKind, CatalogMeta, CatalogMode, CatalogRecord, WarningInfo
from ..models.inputs import (
    CatalogGetInput,
    CatalogQueryInput,
    CatalogSearchInput,
    EphemerisExportInput,
    EphemerisOverrides,
)
from ..models.results import (
    CatalogGetResult,
    CatalogSearchResult,
    EphemerisExportResult,
)
from ._common import AppContext, make_provenance
from .registry import ToolRegistry, ToolSpec

# catalogue name -> (psrcat column, psrqpy param)
_NAME_FIX = re.compile(r"^(J|B)?(\d{4}[+-]\d{2,4})$", re.IGNORECASE)


def register(reg: ToolRegistry) -> None:
    reg.add(ToolSpec(
        name="catalog_search",
        description="Search the ATNF pulsar catalogue: filter by name, sky cone, period, DM, orbital period, flux, galactic latitude, and binarity. At least one criterion is required.",
        input_model=CatalogSearchInput,
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True),
        handler=_search,
    ))
    reg.add(ToolSpec(
        name="catalog_query",
        description=(
            "Power query against the local psrcat with a raw -l expression, e.g. "
            "\"p0 < 0.005 && s1400 > 5\". Numeric conditions on known psrcat columns "
            "(name p0 f0 p1 dm binary assoc age bsurf edot dist s400 s1400 w50 w10 "
            "pb a1 ecc om gl gb); unknown columns are rejected up front because psrcat "
            "silently returns nothing for them. String comparisons do not match "
            "(values carry citation suffixes and name equality is unsupported) — use "
            "catalog_search for name/position lookups. Requires the local psrcat backend."
        ),
        input_model=CatalogQueryInput,
        # local psrcat only — never reaches the online catalogue
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
        handler=_query,
    ))
    reg.add(ToolSpec(
        name="catalog_get",
        description="Fetch a single pulsar record by exact name (supports J/B names and aliases).",
        input_model=CatalogGetInput,
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True),
        handler=_get,
    ))
    reg.add(ToolSpec(
        name="ephemeris_export",
        description="Generate a TEMPO/TEMPO2-format ephemeris artifact from a catalogue record or an existing .par.",
        input_model=EphemerisExportInput,
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True),
        handler=_export,
    ))


# ---------------------------------------------------------------------------
# shared backend plumbing
# ---------------------------------------------------------------------------

async def _catalog_meta(ctx: AppContext, mode: CatalogMode) -> CatalogMeta:
    version = "unknown"
    source = "psrcat"
    if mode is CatalogMode.ONLINE:
        source = "atnf-online"
    return CatalogMeta(source=source, version=version, retrieved_at=utcnow(), mode=mode)


def _use_psrcat(ctx: AppContext) -> bool:
    if ctx.cfg.catalog.mode == "online":
        return False
    return ctx.backends.resolve("psrcat") is not None


def _psrqpy_available(ctx: AppContext) -> bool:
    if ctx.cfg.catalog.mode == "local":
        return False
    try:
        import psrqpy  # noqa: F401

        return True
    except ImportError:
        return False


def _run_psrcat(ctx: AppContext, argv: list[str]) -> str:
    import os
    import subprocess

    env = None
    database = ctx.cfg.catalog.database
    if database is not None:
        db_path = database.expanduser()
        if db_path.exists():
            # psrcat has no -db flag; it locates the catalogue via PSRCAT_FILE
            env = {**os.environ, "PSRCAT_FILE": str(db_path)}
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=60, env=env)
    if proc.returncode != 0:
        raise PsrmcpError(
            ErrorCode.PROCESS_FAILED,
            f"psrcat query failed: {proc.stderr.strip()[:300]}",
            details={"argv": argv},
        )
    return proc.stdout


async def _run_psrcat_async(ctx: AppContext, argv: list[str]) -> str:
    return await asyncio.to_thread(_run_psrcat, ctx, argv)


def _norm_name(name: str) -> str:
    m = _NAME_FIX.match(name.strip())
    return f"J{m.group(2)}" if m and not name.upper().startswith("J") else name.strip()


# ---------------------------------------------------------------------------
# psrcat backend
# ---------------------------------------------------------------------------

def _record_from_row(row: dict[str, Any]) -> CatalogRecord:
    ra = row.get("ra_str")
    dec = row.get("dec_str")
    # Catalogue wildcard ("*") is already normalized to None by _num; default
    # fallback for angle/numeric fields
    ra_deg = psa.hms_to_deg_or_num(str(ra)) if ra else 0.0
    dec_deg = psa.dms_to_deg(str(dec)) if dec else 0.0
    # The CatalogRecord contract requires p0_s > 0; records without a period
    # (RRATs etc.) use the same sentinel as the psrqpy path
    p0 = _opt_float(row.get("p0_s")) or 1e-9
    dm = _opt_float(row.get("dm")) or 0.0
    model = row.get("binary_model")
    model = None if model in (None, "*", "0") else str(model)
    # Anything beyond the fixed contract slots is a requested extra psrcat
    # column (age, bsurf, pb, s1400, gb, ...) — surface it under extra_fields
    known = {"name", "ra_str", "dec_str", "p0_s", "f0_hz", "p1", "dm",
             "binary_model", "observatory", "catalog_version"}
    extras = {k: v for k, v in row.items() if k not in known}
    return CatalogRecord(
        name=str(row.get("name", "?")),
        ra_deg=ra_deg,
        dec_deg=dec_deg,
        p0_s=p0,
        f0_hz=_opt_float(row.get("f0_hz")),
        p1=_opt_float(row.get("p1")),
        dm=dm,
        binary=model is not None,
        binary_model=model,
        observatory=_opt_str(row.get("observatory")),
        catalog_version=_opt_str(row.get("catalog_version")) or "unknown",
        extra_fields=extras,
    )


def _opt_float(v: Any) -> float | None:
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def _opt_str(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip().strip("*")
    return s or None


async def _search(ctx: AppContext, inp: CatalogSearchInput, progress) -> CatalogSearchResult:  # noqa: ANN001
    if _use_psrcat(ctx):
        return await _search_psrcat(ctx, inp)
    if _psrqpy_available(ctx):
        return await _search_psrqpy(ctx, inp)
    raise env_missing("psrcat", "psrcat not found and psrqpy unavailable (or catalog.mode=local)")


async def _search_psrcat(ctx: AppContext, inp: CatalogSearchInput) -> CatalogSearchResult:
    binary = ctx.backends.resolve("psrcat")
    if binary is None:
        raise env_missing("psrcat", "psrcat binary not found")
    conditions: list[str] = []
    if inp.p0_min_s is not None:
        conditions.append(f"p0 > {inp.p0_min_s}")
    if inp.p0_max_s is not None:
        conditions.append(f"p0 < {inp.p0_max_s}")
    if inp.dm_min is not None:
        conditions.append(f"dm > {inp.dm_min}")
    if inp.dm_max is not None:
        conditions.append(f"dm < {inp.dm_max}")
    if inp.pb_min_s is not None:
        conditions.append(f"pb > {inp.pb_min_s}")
    if inp.pb_max_s is not None:
        conditions.append(f"pb < {inp.pb_max_s}")
    if inp.flux_min_mjy is not None:
        conditions.append(f"s1400 > {inp.flux_min_mjy}")
    if inp.gb_min is not None:
        conditions.append(f"gb > {inp.gb_min}")
    if inp.gb_max is not None:
        conditions.append(f"gb < {inp.gb_max}")
    # Explicitly requested columns plus any filter column, so records show why
    # they matched (all verified single-token in `-o short`; see adapter)
    columns = None
    auto_extras = sorted({c for f, c in
                          ((inp.pb_min_s, "pb"), (inp.pb_max_s, "pb"),
                           (inp.flux_min_mjy, "s1400"),
                           (inp.gb_min, "gb"), (inp.gb_max, "gb")) if f is not None})
    if inp.fields or auto_extras:
        columns = psa.resolve_columns(list(inp.fields or []) + auto_extras)
    # Fetch one extra row to build next_cursor.
    argv = psa.catalog_search_cmd(binary, conditions=conditions, columns=columns)
    out = await _run_psrcat_async(ctx, argv)
    rows = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            if columns is not None:
                row = psa.parse_psrcat_row_columns(line, columns)
                rows.append({psa.PSRCAT_FIELD.get(c, c): v for c, v in row.items()})
            else:
                rows.append(psa.parse_psrcat_row(line))
        except ValueError:
            continue  # header/echo line
    records = [_record_from_row(r) for r in rows]
    # psrcat has no cone-search/binary CLI flags in practice (-boundary is
    # unreliable for negative DEC too), so filtering is done in Python
    if inp.ra_deg is not None and inp.dec_deg is not None and inp.radius_deg is not None:
        records = [
            r for r in records
            if _angular_sep_deg(r.ra_deg, r.dec_deg, inp.ra_deg, inp.dec_deg) <= inp.radius_deg
        ]
    if inp.binary_only:
        records = [r for r in records if r.binary]
    if inp.name:
        want = _norm_name(inp.name)
        records = [r for r in records if r.name == want or want in (r.aliases or [r.name])]

    offset = 0
    if inp.cursor:
        try:
            offset = int(inp.cursor)
        except ValueError:
            offset = 0
    page = records[offset : offset + inp.limit]
    next_cursor = str(offset + inp.limit) if offset + inp.limit < len(records) else None
    return CatalogSearchResult(
        records=page,
        count=len(page),
        next_cursor=next_cursor,
        catalog=await _catalog_meta(ctx, CatalogMode.LOCAL),
        warnings=[],
    )


async def _search_psrqpy(ctx: AppContext, inp: CatalogSearchInput) -> CatalogSearchResult:
    def query() -> list[CatalogRecord]:
        import psrqpy

        params = ["JNAME", "RAJ", "DECJ", "P0", "F0", "P1", "DM", "BINCOMP", "PB", "S1400", "GB"]
        q = psrqpy.QueryATNF(params=params, loadfromdb=False).pandas
        records = []
        for _, row in q.iterrows():
            name = str(row.get("JNAME") or "?")
            if inp.name and _norm_name(inp.name) != name:
                continue
            p0 = float(row.get("P0") or 0.0) or None
            dm = float(row.get("DM") or 0.0) or 0.0
            ra_str, dec_str = str(row.get("RAJ")), str(row.get("DECJ"))
            try:
                ra_deg = psa.hms_to_deg_or_num(ra_str)
                dec_deg = psa.dms_to_deg(dec_str)
            except Exception:
                continue
            if inp.ra_deg is not None and inp.dec_deg is not None and inp.radius_deg is not None:
                if _angular_sep_deg(ra_deg, dec_deg, inp.ra_deg, inp.dec_deg) > inp.radius_deg:
                    continue
            if inp.p0_min_s is not None and (p0 is None or p0 <= inp.p0_min_s):
                continue
            if inp.p0_max_s is not None and (p0 is None or p0 >= inp.p0_max_s):
                continue
            if inp.dm_min is not None and dm <= inp.dm_min:
                continue
            if inp.dm_max is not None and dm >= inp.dm_max:
                continue
            pb = _opt_float(row.get("PB"))
            if inp.pb_min_s is not None and (pb is None or pb <= inp.pb_min_s):
                continue
            if inp.pb_max_s is not None and (pb is None or pb >= inp.pb_max_s):
                continue
            s1400 = _opt_float(row.get("S1400"))
            if inp.flux_min_mjy is not None and (s1400 is None or s1400 <= inp.flux_min_mjy):
                continue
            gb = _opt_float(row.get("GB"))
            if inp.gb_min is not None and (gb is None or gb <= inp.gb_min):
                continue
            if inp.gb_max is not None and (gb is None or gb >= inp.gb_max):
                continue
            bincomp = row.get("BINCOMP")
            is_binary = bincomp not in (None, "*", "0", "nan")
            if inp.binary_only and not is_binary:
                continue
            # Mirror the local path: matched filter columns ride along under
            # extra_fields so records show why they matched
            extras: dict[str, Any] = {}
            if inp.pb_min_s is not None or inp.pb_max_s is not None:
                if pb is not None:
                    extras["pb"] = pb
            if inp.flux_min_mjy is not None and s1400 is not None:
                extras["s1400"] = s1400
            if (inp.gb_min is not None or inp.gb_max is not None) and gb is not None:
                extras["gb"] = gb
            records.append(CatalogRecord(
                name=name,
                ra_deg=ra_deg,
                dec_deg=dec_deg,
                p0_s=p0 or 1e-9,
                f0_hz=_opt_float(row.get("F0")),
                p1=_opt_float(row.get("P1")),
                dm=dm,
                binary=is_binary,
                # ATNF BINCOMP is a companion-composition descriptor
                # (e.g. "He[vbv+08]"), not a BINARY model name, so it must not
                # go into the par
                binary_model=None,
                observatory=None,
                catalog_version=getattr(psrqpy, "__version__", "atnf"),
                extra_fields=extras,
            ))
        return records

    records = await asyncio.to_thread(query)
    offset = 0
    if inp.cursor:
        try:
            offset = int(inp.cursor)
        except ValueError:
            offset = 0
    page = records[offset : offset + inp.limit]
    next_cursor = str(offset + inp.limit) if offset + inp.limit < len(records) else None
    return CatalogSearchResult(
        records=page,
        count=len(page),
        next_cursor=next_cursor,
        catalog=await _catalog_meta(ctx, CatalogMode.ONLINE),
        warnings=[WarningInfo(code="W_ONLINE_QUERY", message="results come from an ATNF online query (psrqpy)", severity="info")],
    )


# ---------------------------------------------------------------------------
# catalog_query: raw `-l` power tool (local psrcat only)
# ---------------------------------------------------------------------------

async def _query(ctx: AppContext, inp: CatalogQueryInput, progress) -> CatalogSearchResult:  # noqa: ANN001
    if not _use_psrcat(ctx):
        raise env_missing(
            "psrcat",
            "catalog_query requires the local psrcat backend (psrcat not found or catalog.mode=online)",
            hint="use catalog_search instead, or install psrcat via scripts/install_pulsar_stack.sh",
        )
    return await _query_psrcat(ctx, inp)


async def _query_psrcat(ctx: AppContext, inp: CatalogQueryInput) -> CatalogSearchResult:
    binary = ctx.backends.resolve("psrcat")
    if binary is None:
        raise env_missing("psrcat", "psrcat binary not found")
    expression = psa.validate_expression(inp.expression)
    columns = psa.resolve_columns(inp.columns) if inp.columns else None
    argv = psa.catalog_query_cmd(binary, expression=expression, columns=columns)
    out = await _run_psrcat_async(ctx, argv)
    rows = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            if columns is not None:
                row = psa.parse_psrcat_row_columns(line, columns)
                rows.append({psa.PSRCAT_FIELD.get(c, c): v for c, v in row.items()})
            else:
                rows.append(psa.parse_psrcat_row(line))
        except ValueError:
            continue  # header/echo line
    records = [_record_from_row(r) for r in rows]
    offset = 0
    if inp.cursor:
        try:
            offset = int(inp.cursor)
        except ValueError:
            offset = 0
    page = records[offset : offset + inp.limit]
    next_cursor = str(offset + inp.limit) if offset + inp.limit < len(records) else None
    return CatalogSearchResult(
        records=page,
        count=len(page),
        next_cursor=next_cursor,
        catalog=await _catalog_meta(ctx, CatalogMode.LOCAL),
        warnings=[],
    )


def _angular_sep_deg(ra1: float, dec1: float, ra2: float, dec2: float) -> float:
    r1, d1, r2, d2 = map(math.radians, (ra1, dec1, ra2, dec2))
    cos = math.sin(d1) * math.sin(d2) + math.cos(d1) * math.cos(d2) * math.cos(r1 - r2)
    return math.degrees(math.acos(max(-1.0, min(1.0, cos))))


async def _get(ctx: AppContext, inp: CatalogGetInput, progress) -> CatalogGetResult:  # noqa: ANN001
    search = CatalogSearchInput(name=inp.name, limit=5)
    if _use_psrcat(ctx):
        result = await _search_psrcat(ctx, search)
    elif _psrqpy_available(ctx):
        result = await _search_psrqpy(ctx, search)
    else:
        raise env_missing("psrcat", "psrcat not found and psrqpy unavailable")
    if not result.records:
        raise not_found(f"{inp.name} not found in the catalogue", hint="use catalog_search to confirm the name")
    record = result.records[0]
    return CatalogGetResult(
        record=record,
        ephemeris_available=True,
        catalog=result.catalog,
        warnings=result.warnings,
    )


async def _psrcat_ephem(ctx: AppContext, name: str) -> str:
    """Full TEMPO ephemeris text from psrcat -e; failure or empty output is a tool error."""
    binary = ctx.backends.resolve("psrcat")
    if binary is None:
        raise env_missing("psrcat", "psrcat binary not found")
    out = await _run_psrcat_async(ctx, psa.psrcat_ephem_cmd(binary, name))
    if "PSRJ" not in out:
        raise PsrmcpError(
            ErrorCode.PROCESS_FAILED,
            f"psrcat -e produced no valid ephemeris: {out.strip()[:120] or '(empty output)'}",
        )
    return out


def _apply_par_overrides(text: str, ov: EphemerisOverrides) -> str:
    """Write numeric overrides line by line into the psrcat ephemeris (preserving in-line alignment)."""
    subs: dict[str, float] = {}
    if ov.dm is not None:
        subs["DM"] = ov.dm
    if ov.f0_hz is not None:
        subs["F0"] = ov.f0_hz
    if ov.p0_s is not None:
        subs["F0"] = 1.0 / ov.p0_s  # the psrcat ephemeris expresses spin as F0
    if ov.pepoch_mjd is not None:
        subs["PEPOCH"] = ov.pepoch_mjd
    if ov.posepoch_mjd is not None:
        subs["POSEPOCH"] = ov.posepoch_mjd
    for key, value in subs.items():
        text = re.sub(
            rf"^{key}(\s+)\S+(.*)$",
            lambda m, k=key, v=value: f"{k}{m.group(1)}{v!r}{m.group(2)}",
            text,
            flags=re.MULTILINE,
        )
    return text


async def _export(ctx: AppContext, inp: EphemerisExportInput, progress) -> EphemerisExportResult:  # noqa: ANN001
    started = utcnow()
    warnings: list[WarningInfo] = []
    tempo2 = inp.format == "tempo2"

    overrides = inp.overrides
    if inp.artifact:
        src = ctx.store.lookup_id(inp.artifact) if inp.artifact.startswith("art_") else None
        if src is None:
            resolved = ctx.resolver.resolve(inp.artifact, what="par")
            src = resolved.resolved
        text = src.read_text(encoding="utf-8", errors="replace")
        out_name = (inp.output_name or f"{inp.artifact}.{inp.format}.par") + ".par"
        prov = make_provenance(
            tool="ephemeris_export",
            backend="psrmcp",
            backend_version=__version__,
            params=inp.model_dump(mode="json"),
            started=started,
            duration_s=0.0,
        )
        ref = ctx.store.commit_text(
            text=text, filename=out_name, kind=ArtifactKind.PAR, format_name=inp.format, provenance=prov
        )
        warnings.append(
            WarningInfo(
                code="W_EPHEMERIS_PASSTHROUGH",
                message="artifact input is copied verbatim; tempo1<->tempo2 conversion requires PINT and is not performed here",
                severity="warning",
            )
        )
        return EphemerisExportResult(artifact=ref, format=inp.format, warnings=warnings, provenance=prov)

    # From the catalogue. The local catalogue exports the full psrcat -e
    # ephemeris directly (with BINARY/PB/ECC/CLK/EPHEM, usable for timing);
    # a position override would require rewriting ELONG/ELAT (ecliptic
    # coordinates), so for honesty we fall back to the synthetic path.
    pos_override = bool(overrides and (overrides.ra_deg is not None or overrides.dec_deg is not None))
    out_name: str
    if _use_psrcat(ctx) and not pos_override:
        text = await _psrcat_ephem(ctx, inp.pulsar or "")
        if overrides is not None:
            text = _apply_par_overrides(text, overrides)
        out_name = (inp.output_name or (inp.pulsar or "ephemeris")) + ".par"
    else:
        get = await _get(ctx, CatalogGetInput(name=inp.pulsar or ""), progress)
        rec = get.record
        ra = overrides.ra_deg if overrides and overrides.ra_deg is not None else rec.ra_deg
        dec = overrides.dec_deg if overrides and overrides.dec_deg is not None else rec.dec_deg
        p0 = overrides.p0_s if overrides and overrides.p0_s is not None else rec.p0_s
        f0 = overrides.f0_hz if overrides and overrides.f0_hz is not None else rec.f0_hz
        dm = overrides.dm if overrides and overrides.dm is not None else rec.dm
        peepoch = overrides.pepoch_mjd if overrides else None
        posepoch = overrides.posepoch_mjd if overrides else None

        text = psa.build_par_text(
            name=rec.name,
            ra_deg=ra,
            dec_deg=dec,
            p0_s=p0,
            dm=dm,
            f0_hz=f0,
            p1=rec.p1,
            peepoch_mjd=peepoch,
            posepoch_mjd=posepoch,
            binary_model=rec.binary_model,
            tempo2=tempo2,
        )
        if rec.binary:
            warnings.append(
                WarningInfo(
                    code="W_BINARY_PARAMS_INCOMPLETE",
                    message="binary parameters (TTASC/ECC/...) were not fully exported from the catalogue; complete them before timing",
                    severity="warning",
                )
            )
        out_name = (inp.output_name or rec.name) + ".par"
    prov = make_provenance(
        tool="ephemeris_export",
        backend="psrcat",
        backend_version=(ctx.backends.cached("psrcat").version if ctx.backends.cached("psrcat") else None),
        params=inp.model_dump(mode="json"),
        started=started,
        duration_s=(utcnow() - started).total_seconds(),
    )
    ref = ctx.store.commit_text(
        text=text, filename=out_name, kind=ArtifactKind.PAR, format_name=inp.format, provenance=prov
    )
    return EphemerisExportResult(artifact=ref, format=inp.format, warnings=warnings, provenance=prov)
