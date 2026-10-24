"""catalog_search / catalog_get / ephemeris_export (TOOLS.md 5.3-5.5).

Primary backend: psrcat binary (offline via PSRCAT_FILE/-db). Fallback (mode
online/auto, psrcat missing): psrqpy against ATNF online, run in a thread.
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
        description="查询 ATNF 脉冲星星表:按名称、位置锥、周期、DM、双星筛选。至少一个条件。",
        input_model=CatalogSearchInput,
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True),
        handler=_search,
    ))
    reg.add(ToolSpec(
        name="catalog_get",
        description="按名称精确获取单个脉冲星记录(支持 J/B 名称与别名)。",
        input_model=CatalogGetInput,
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True),
        handler=_get,
    ))
    reg.add(ToolSpec(
        name="ephemeris_export",
        description="从星表记录或已有 .par 生成 TEMPO/TEMPO2 格式 ephemeris 产物。",
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
            # psrcat 没有 -db 旗标,靠 PSRCAT_FILE 找星表
            env = {**os.environ, "PSRCAT_FILE": str(db_path)}
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=60, env=env)
    if proc.returncode != 0:
        raise PsrmcpError(
            ErrorCode.PROCESS_FAILED,
            f"psrcat 查询失败: {proc.stderr.strip()[:300]}",
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
    # 星表通配("*")经 _num 已归一为 None;角度/数值字段缺省兜底
    ra_deg = psa.hms_to_deg_or_num(str(ra)) if ra else 0.0
    dec_deg = psa.dms_to_deg(str(dec)) if dec else 0.0
    # CatalogRecord 契约要求 p0_s > 0;无周期记录(RRAT 等)用 psrqpy 路径同款哨兵
    p0 = _opt_float(row.get("p0_s")) or 1e-9
    dm = _opt_float(row.get("dm")) or 0.0
    model = row.get("binary_model")
    model = None if model in (None, "*", "0") else str(model)
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
    raise env_missing("psrcat", "未找到 psrcat 且 psrqpy 不可用(或 catalog.mode=local)")


async def _search_psrcat(ctx: AppContext, inp: CatalogSearchInput) -> CatalogSearchResult:
    binary = ctx.backends.resolve("psrcat")
    if binary is None:
        raise env_missing("psrcat", "未找到 psrcat 二进制")
    conditions: list[str] = []
    if inp.p0_min_s is not None:
        conditions.append(f"p0 > {inp.p0_min_s}")
    if inp.p0_max_s is not None:
        conditions.append(f"p0 < {inp.p0_max_s}")
    if inp.dm_min is not None:
        conditions.append(f"dm > {inp.dm_min}")
    if inp.dm_max is not None:
        conditions.append(f"dm < {inp.dm_max}")
    # Fetch one extra row to build next_cursor.
    argv = psa.catalog_search_cmd(binary, conditions=conditions)
    out = await _run_psrcat_async(ctx, argv)
    rows = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(psa.parse_psrcat_row(line))
        except ValueError:
            continue  # header/echo line
    records = [_record_from_row(r) for r in rows]
    # 实测 psrcat 无锥搜索/双星 CLI 旗标(-boundary 对负 DEC 也不可靠),
    # 过滤在 Python 端完成
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

        params = ["JNAME", "RAJ", "DECJ", "P0", "F0", "P1", "DM", "BINCOMP"]
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
            bincomp = row.get("BINCOMP")
            is_binary = bincomp not in (None, "*", "0", "nan")
            if inp.binary_only and not is_binary:
                continue
            records.append(CatalogRecord(
                name=name,
                ra_deg=ra_deg,
                dec_deg=dec_deg,
                p0_s=p0 or 1e-9,
                f0_hz=_opt_float(row.get("F0")),
                p1=_opt_float(row.get("P1")),
                dm=dm,
                binary=is_binary,
                # ATNF BINCOMP 是伴星成分描述符(如 "He[vbv+08]"),
                # 不是 BINARY 模型名,不能写进 par
                binary_model=None,
                observatory=None,
                catalog_version=getattr(psrqpy, "__version__", "atnf"),
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
        warnings=[WarningInfo(code="W_ONLINE_QUERY", message="结果来自 ATNF 在线查询(psrqpy)", severity="info")],
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
        raise env_missing("psrcat", "未找到 psrcat 且 psrqpy 不可用")
    if not result.records:
        raise not_found(f"星表中没有 {inp.name}", hint="用 catalog_search 模糊查询确认名称")
    record = result.records[0]
    return CatalogGetResult(
        record=record,
        ephemeris_available=True,
        catalog=result.catalog,
        warnings=result.warnings,
    )


async def _psrcat_ephem(ctx: AppContext, name: str) -> str:
    """psrcat -e 的完整 TEMPO 星历文本;失败或空输出视为工具错误。"""
    binary = ctx.backends.resolve("psrcat")
    if binary is None:
        raise env_missing("psrcat", "未找到 psrcat 二进制")
    out = await _run_psrcat_async(ctx, psa.psrcat_ephem_cmd(binary, name))
    if "PSRJ" not in out:
        raise PsrmcpError(
            ErrorCode.PROCESS_FAILED,
            f"psrcat -e 未产出有效星历: {out.strip()[:120] or '(空输出)'}",
        )
    return out


def _apply_par_overrides(text: str, ov: EphemerisOverrides) -> str:
    """把数值类 override 逐行写进 psrcat 星历(保持行内对齐)。"""
    subs: dict[str, float] = {}
    if ov.dm is not None:
        subs["DM"] = ov.dm
    if ov.f0_hz is not None:
        subs["F0"] = ov.f0_hz
    if ov.p0_s is not None:
        subs["F0"] = 1.0 / ov.p0_s  # psrcat 星历以 F0 表达自转
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
                message="artifact 输入按原文复制;tempo1<->tempo2 的格式转换需要 PINT,当前未执行",
                severity="warning",
            )
        )
        return EphemerisExportResult(artifact=ref, format=inp.format, warnings=warnings, provenance=prov)

    # From the catalogue. 本地星表直接导出 psrcat -e 的完整星历(含
    # BINARY/PB/ECC/CLK/EPHEM,可直接计时);位置 override 会要求改写
    # ELONG/ELAT(黄道坐标),诚实起见退回合成路径。
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
                    message="双星参数(TTASC/ECC/...)未从星表完整导出,计时前需补充",
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
