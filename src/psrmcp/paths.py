"""Path policy (TOOLS.md §1.2): artifact IDs or whitelisted relative paths, no escapes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .errors import invalid_input, not_found, path_denied

ARTIFACT_ID_RE = re.compile(r"^art_[A-Za-z0-9_-]{8,64}$")
JOB_ID_RE = re.compile(r"^job_[A-Za-z0-9_-]{8,64}$")

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class ResolvedInput:
    """A resolved, safety-checked input reference."""

    raw: str
    artifact_id: str | None
    resolved: Path
    root: str | None
    size_bytes: int


class PathResolver:
    """Resolves artifact IDs / relative paths under configured read roots."""

    def __init__(self, cfg: Config, artifact_lookup=None) -> None:
        self._cfg = cfg
        # artifact_lookup: Callable[[str], ResolvedInput | Path | None] — the
        # store returns bare paths; resolve() wraps them into the contract.
        self._artifact_lookup = artifact_lookup

    def _check_text(self, value: str, what: str) -> None:
        if not value or _CONTROL_CHARS_RE.search(value):
            raise invalid_input(f"{what} 含非法字符")
        if value.startswith("-"):
            raise invalid_input(f"{what} 不能以 '-' 开头")

    def resolve(self, reference: str, *, root: str | None = None, what: str = "路径") -> ResolvedInput:
        self._check_text(reference, what)

        if reference.startswith("art_"):
            if self._artifact_lookup is None:
                raise not_found(f"未知 artifact: {reference}")
            found = self._artifact_lookup(reference)
            if found is None:
                raise not_found(f"未知 artifact: {reference}", hint="用 artifact_list 查看现有产物")
            if isinstance(found, ResolvedInput):
                return found
            # store.lookup_id 返回裸路径,这里包一层补齐 resolver 契约
            path = Path(found).resolve()
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            return ResolvedInput(
                raw=reference, artifact_id=reference, resolved=path, root=None, size_bytes=size
            )

        if _CONTROL_CHARS_RE.search(reference) or ".." in Path(reference).parts:
            raise path_denied(f"{what} 不允许包含 '..' 或控制字符")

        candidate = Path(reference)
        if candidate.is_absolute():
            if not self._cfg.data.allow_absolute_paths:
                raise path_denied(
                    f"不接受绝对路径: {reference}",
                    hint="把文件所在目录加入配置的 data.read_roots,或改用相对路径",
                )
            resolved = candidate.resolve()
            for base in self._cfg.data.read_roots:
                base_resolved = base.expanduser().resolve()
                if resolved.is_relative_to(base_resolved):
                    return self._finish(reference, resolved, str(base_resolved))
            raise path_denied(f"路径不在任何 data root 内: {reference}")

        roots = [r.expanduser().resolve() for r in self._cfg.data.read_roots]
        if not roots:
            raise path_denied("未配置 data.read_roots,无法解析相对路径")
        if root is not None:
            self._check_text(root, "root")
            roots = [r for r in roots if r.name == root or str(r) == root]
            if not roots:
                raise not_found(f"指定的 root 不存在: {root}")

        matches: list[tuple[Path, Path]] = []
        for base in roots:
            resolved = (base / candidate).resolve()
            if resolved.is_relative_to(base):
                if resolved.exists():
                    matches.append((base, resolved))
        if not matches:
            if any((base / candidate).resolve().is_relative_to(base) for base in roots):
                raise not_found(f"文件不存在: {reference}")
            raise not_found(f"文件不存在或不在 data root 内: {reference}")
        if len(matches) > 1:
            raise invalid_input(
                f"路径在多个 data root 中出现: {reference}",
                hint="用 root 参数指定数据根目录",
            )
        base, resolved = matches[0]
        return self._finish(reference, resolved, str(base))

    def _finish(self, raw: str, resolved: Path, root: str) -> ResolvedInput:
        if not resolved.exists():
            raise not_found(f"文件不存在: {raw}")
        if not resolved.is_file():
            raise invalid_input(f"不是常规文件: {raw}")
        return ResolvedInput(
            raw=raw,
            artifact_id=None,
            resolved=resolved,
            root=root,
            size_bytes=resolved.stat().st_size,
        )
