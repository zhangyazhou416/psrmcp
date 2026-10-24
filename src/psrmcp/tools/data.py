"""data_find (TOOLS.md 5.2): whitelisted glob search with format sniffing."""

from __future__ import annotations

import fnmatch
import os
from datetime import datetime
from pathlib import Path

from mcp.types import ToolAnnotations

from ..errors import ErrorCode, PsrmcpError, not_found
from ..execution.workspace import utcnow
from ..models.common import WarningInfo
from ..models.inputs import DataFindInput
from ..models.results import DataFindResult, FoundFile
from ._common import AppContext
from .registry import ToolRegistry, ToolSpec

_DESCRIPTION = (
    "在配置的 read_roots 白名单目录内按 glob 查找脉冲星数据文件并初判格式。"
    "只读、不读取文件内容(仅扩展名与文件头)。处理前用它定位数据。"
)


def register(reg: ToolRegistry) -> None:
    reg.add(
        ToolSpec(
            name="data_find",
            description=_DESCRIPTION,
            input_model=DataFindInput,
            annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
            handler=run,
        )
    )


async def run(ctx: AppContext, inp: DataFindInput, progress) -> DataFindResult:  # noqa: ANN001
    roots = [r.expanduser().resolve() for r in ctx.cfg.data.read_roots]
    if inp.root:
        roots = [r for r in roots if r.name == inp.root or str(r) == inp.root]
        if not roots:
            raise not_found(f"指定的 root 不存在: {inp.root}")
    if not roots:
        raise PsrmcpError(
            ErrorCode.PATH_DENIED,
            "未配置 data.read_roots,无目录可搜索",
            hint="在配置文件 [data].read_roots 中添加数据目录",
        )

    if inp.modified_after:
        try:
            mtime_after = datetime.fromisoformat(inp.modified_after.replace("Z", "+00:00")).timestamp()
        except ValueError as exc:
            raise PsrmcpError(ErrorCode.INVALID_INPUT, f"modified_after 无效: {exc}") from exc
    else:
        mtime_after = None

    warnings: list[WarningInfo] = []
    files: list[FoundFile] = []
    truncated = False

    for root in roots:
        if not root.is_dir():
            warnings.append(WarningInfo(code="W_ROOT_MISSING", message=f"数据根目录不存在: {root}", severity="warning"))
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            depth = len(Path(dirpath).resolve().relative_to(root).parts)
            if depth >= inp.max_depth:
                dirnames[:] = []
            dirnames.sort()
            for fname in sorted(filenames):
                if not fnmatch.fnmatch(fname, inp.pattern):
                    continue
                path = Path(dirpath) / fname
                if inp.extensions and path.suffix not in set(inp.extensions):
                    continue
                try:
                    st = path.stat()
                except OSError:
                    continue
                if mtime_after is not None and st.st_mtime <= mtime_after:
                    continue
                if len(files) >= inp.max_results:
                    truncated = True
                    break
                files.append(
                    FoundFile(
                        root=str(root),
                        relative_path=str(path.relative_to(root)),
                        size_bytes=st.st_size,
                        mtime=datetime.fromtimestamp(st.st_mtime, tz=utcnow().tzinfo).isoformat(),
                        detected_format=_detect_format(path),
                    )
                )
            if truncated:
                break
        if truncated:
            break

    files.sort(key=lambda f: (f.root, f.relative_path))
    return DataFindResult(
        files=files,
        count=len(files),
        truncated=truncated,
        warnings=warnings,
    )


def _detect_format(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".fil":
        return "sigproc"
    if suffix == ".par":
        return "par"
    if suffix == ".tim":
        return "tim"
    if suffix == ".ar":
        return "psrarchive"
    if suffix in (".fits", ".sf"):
        try:
            with path.open("rb") as fh:
                magic = fh.read(9)
            if magic == b"SIMPLE  =":
                return "psrfits"
        except OSError:
            pass
    return "unknown"
