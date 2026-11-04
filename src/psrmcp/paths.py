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
            raise invalid_input(f"{what} contains illegal characters")
        if value.startswith("-"):
            raise invalid_input(f"{what} must not start with '-'")

    def resolve(self, reference: str, *, root: str | None = None, what: str = "path") -> ResolvedInput:
        self._check_text(reference, what)

        if reference.startswith("art_"):
            if self._artifact_lookup is None:
                raise not_found(f"unknown artifact: {reference}")
            found = self._artifact_lookup(reference)
            if found is None:
                raise not_found(f"unknown artifact: {reference}", hint="use artifact_list to list existing artifacts")
            if isinstance(found, ResolvedInput):
                return found
            # store.lookup_id returns a bare path; wrap it to satisfy the resolver contract
            path = Path(found).resolve()
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            return ResolvedInput(
                raw=reference, artifact_id=reference, resolved=path, root=None, size_bytes=size
            )

        if _CONTROL_CHARS_RE.search(reference) or ".." in Path(reference).parts:
            raise path_denied(f"{what} must not contain '..' or control characters")

        candidate = Path(reference)
        if candidate.is_absolute():
            if not self._cfg.data.allow_absolute_paths:
                raise path_denied(
                    f"absolute paths not accepted: {reference}",
                    hint="add the file's directory to data.read_roots in the config, or use a relative path",
                )
            resolved = candidate.resolve()
            for base in self._cfg.data.read_roots:
                base_resolved = base.expanduser().resolve()
                if resolved.is_relative_to(base_resolved):
                    return self._finish(reference, resolved, str(base_resolved))
            raise path_denied(f"path not under any data root: {reference}")

        roots = [r.expanduser().resolve() for r in self._cfg.data.read_roots]
        if not roots:
            raise path_denied("data.read_roots not configured; cannot resolve relative paths")
        if root is not None:
            self._check_text(root, "root")
            roots = [r for r in roots if r.name == root or str(r) == root]
            if not roots:
                raise not_found(f"specified root not found: {root}")

        matches: list[tuple[Path, Path]] = []
        for base in roots:
            resolved = (base / candidate).resolve()
            if resolved.is_relative_to(base):
                if resolved.exists():
                    matches.append((base, resolved))
        if not matches:
            if any((base / candidate).resolve().is_relative_to(base) for base in roots):
                raise not_found(f"file not found: {reference}")
            raise not_found(f"file not found or not under any data root: {reference}")
        if len(matches) > 1:
            raise invalid_input(
                f"path matches multiple data roots: {reference}",
                hint="use the root parameter to select the data root",
            )
        base, resolved = matches[0]
        return self._finish(reference, resolved, str(base))

    def _finish(self, raw: str, resolved: Path, root: str) -> ResolvedInput:
        if not resolved.exists():
            raise not_found(f"file not found: {raw}")
        if not resolved.is_file():
            raise invalid_input(f"not a regular file: {raw}")
        return ResolvedInput(
            raw=raw,
            artifact_id=None,
            resolved=resolved,
            root=root,
            size_bytes=resolved.stat().st_size,
        )
