"""MCP resources (DESIGN.md 5.1): environment, catalog, ephemeris, artifact, job, docs.

Large archives are never returned through resources — only metadata and text
byproducts; tools reference artifacts by ID (DESIGN.md 5.1 note).
"""

from __future__ import annotations

import base64

from mcp.types import BlobResourceContents, Resource, TextResourceContents

from .errors import ErrorCode, PsrmcpError, not_found
from .models.inputs import CatalogGetInput, EphemerisExportInput, PulsarEnvironmentInput
from .tools._common import DEFAULT_PROGRESS, AppContext
from .tools.catalog import _export, _get
from .tools.system import run as run_environment

MIME_JSON = "application/json"
MIME_TEXT = "text/plain"
MIME_MD = "text/markdown"
MIME_PNG = "image/png"

_DOCS: dict[str, str] = {
    "reduction": (
        "# 脉冲星单次观测约简流程\n\n"
        "1. `data_find` 定位原始数据；`archive_inspect` 检查元数据。\n"
        "2. `dedisperse` 消色散（需要 DM：来自 archive 头、ephemeris 或 dm_override）。\n"
        "3. `rfi_scan` 评估坏通道，`rfi_zap` 打标志。\n"
        "4. `profile_standardize` 生成标准轮廓，`profile_measure` 测量 SNR/宽度。\n"
        "5. 所有产物通过 artifact ID 引用；长任务用 execution_mode=auto，配合 job_get 轮询。\n\n"
        "安全约束：输入只允许 workspace 白名单路径或 artifact ID；不要把绝对路径传给 read_roots 之外的文件。"
    ),
    "timing": (
        "# 计时分析流程\n\n"
        "1. `ephemeris_export` 从星表导出 .par（或使用已有 par artifact）。\n"
        "2. `toa_extract` 用标准轮廓从各 archive 提取 TOA，合并为 .tim。\n"
        "3. `timing_fit`（backend=pint 推荐）拟合；查看 parameters 与 reduced chi2。\n"
        "4. `timing_diagnose` 检查离群点/JUMP/DM/残差分布，按 recommendations 清理后重拟合。\n\n"
        "uniform 权重会改变统计意义（W_UNIFORM_WEIGHTING 警告），默认 1/sigma^2。"
    ),
    "rfi": (
        "# RFI 处理指南\n\n"
        "- `rfi_scan`（method=spectral_kurtosis 需要 clfd 后端）给出建议 mask 与图。\n"
        "- `rfi_zap` 接受显式 channels（如 [0,1,240-255] 展开后的索引列表）或 auto。\n"
        "- zap 是不可逆打标（写出 .paz），先小范围验证再全量执行；产物 provenance 记录 mask。"
    ),
}


def list_resources(ctx: AppContext) -> list[Resource]:
    """Static entries plus artifacts from the current session."""
    items: list[Resource] = [
        Resource(
            uri="pulsar://environment",
            name="环境与后端状态",
            description="后端可用性、版本、workspace 与限额",
            mimeType=MIME_JSON,
        ),
    ]
    for topic, text in _DOCS.items():
        items.append(Resource(
            uri=f"pulsar://docs/{topic}",
            name=f"指南: {topic}",
            description=text.splitlines()[0].lstrip("# "),
            mimeType=MIME_MD,
        ))
    for ref in ctx.store.iter_index():
        items.append(Resource(
            uri=f"pulsar://artifact/{ref.artifact_id}",
            name=ref.path.rsplit("/", 1)[-1],
            description=f"{ref.kind.value} / {ref.format}",
            mimeType=MIME_PNG if ref.format == "png" else MIME_JSON,
        ))
    return items


async def read_resource(ctx: AppContext, uri: str) -> list[TextResourceContents | BlobResourceContents]:
    scheme, sep, rest = uri.partition("://")
    if scheme != "pulsar" or not sep:
        raise PsrmcpError(
            ErrorCode.INVALID_INPUT, f"不支持的 resource scheme: {scheme or uri}",
            hint="使用 pulsar://environment、pulsar://artifact/{id} 等",
        )
    parts = [p for p in rest.split("/") if p]

    if uri == "pulsar://environment":
        result = await run_environment(ctx, PulsarEnvironmentInput(), DEFAULT_PROGRESS)
        return [TextResourceContents(uri=uri, mimeType=MIME_JSON, text=result.model_dump_json(indent=2))]

    if parts and parts[0] == "docs":
        topic = parts[1] if len(parts) > 1 else ""
        if topic not in _DOCS:
            raise not_found(f"未知文档主题: {topic}(可用: {', '.join(_DOCS)})")
        return [TextResourceContents(uri=uri, mimeType=MIME_MD, text=_DOCS[topic])]

    if parts and parts[0] == "catalog":
        if len(parts) != 2:
            raise PsrmcpError(ErrorCode.INVALID_INPUT, "用法: pulsar://catalog/{name}")
        res = await _get(ctx, CatalogGetInput(name=parts[1]), DEFAULT_PROGRESS)
        return [TextResourceContents(uri=uri, mimeType=MIME_JSON, text=res.record.model_dump_json(indent=2))]

    if parts and parts[0] == "ephemeris":
        if len(parts) != 2:
            raise PsrmcpError(ErrorCode.INVALID_INPUT, "用法: pulsar://ephemeris/{name}")
        res = await _export(
            ctx, EphemerisExportInput(pulsar=parts[1]), DEFAULT_PROGRESS
        )
        path = ctx.ws.root / res.artifact.path
        return [TextResourceContents(uri=uri, mimeType=MIME_TEXT, text=path.read_text(encoding="utf-8"))]

    if parts and parts[0] == "artifact":
        if len(parts) not in (2, 3):
            raise PsrmcpError(ErrorCode.INVALID_INPUT, "用法: pulsar://artifact/{id}[/{log|preview}]")
        ref = ctx.store.get(parts[1])
        if len(parts) == 2:
            return [TextResourceContents(uri=uri, mimeType=MIME_JSON, text=ref.model_dump_json(indent=2))]
        sub = parts[2]
        path = ctx.ws.root / ref.path
        if sub == "log":
            if ref.kind != "log":
                raise PsrmcpError(
                    ErrorCode.NOT_FOUND, f"artifact {ref.artifact_id} 不是日志({ref.kind.value})"
                )
            return [TextResourceContents(uri=uri, mimeType=MIME_TEXT, text=path.read_text(errors="replace"))]
        if sub == "preview":
            if ref.format != "png":
                # DESIGN 5.1: never base64 large binaries; only previews blob out.
                raise PsrmcpError(
                    ErrorCode.FORMAT_UNSUPPORTED,
                    f"artifact {ref.artifact_id} 无预览({ref.format});请引用 artifact ID 用工具处理",
                )
            return [BlobResourceContents(
                uri=uri, mimeType=MIME_PNG,
                blob=base64.b64encode(path.read_bytes()).decode("ascii"),
            )]
        raise PsrmcpError(ErrorCode.INVALID_INPUT, f"未知 artifact 子资源: {sub}")

    if parts and parts[0] == "job":
        if len(parts) != 2:
            raise PsrmcpError(ErrorCode.INVALID_INPUT, "用法: pulsar://job/{id}")
        return [TextResourceContents(
            uri=uri, mimeType=MIME_JSON, text=ctx.jobs.get(parts[1]).model_dump_json(indent=2),
        )]

    raise not_found(f"未知 resource: {uri}")


def docs_topics() -> list[str]:
    return sorted(_DOCS)
