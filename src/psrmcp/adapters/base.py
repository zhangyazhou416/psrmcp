"""Backend probing and binary resolution (adapters layer, DESIGN.md §3)."""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from ..config import Config
from ..errors import PsrmcpError, env_missing
from ..execution.runner import ProcessRunner
from ..models.common import BackendInfo

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BackendSpec:
    """How to locate and version-check one backend."""

    name: str
    # Ordered candidate binaries: (path-or-name, version_args, version_regex)
    probes: tuple[tuple[str, tuple[str, ...], str], ...] = ()
    capabilities: tuple[str, ...] = ()
    python: bool = False  # python-package backend (PINT), probed via interpreter import


DSPSR_SPEC = BackendSpec(
    name="dspsr",
    probes=(("dspsr", ("--version",), r"([0-9]+\.[0-9]+[.\-][^\s]*|DSPSR v?[0-9][^\s]*)"),),
    capabilities=("dedisperse",),
)

PSRCHIVE_SPEC = BackendSpec(
    name="psrchive",
    probes=(
        # 实测:psrstat/psradd/psrconv 的 --version 打印 "PSRCHIVE <日期> <commit>";
        # psrchive_config 只 dump 配置无版本,vap/pat 不认 --version。
        ("psrstat", ("--version",), r"(PSRCHIVE\s+\S+\s+\S+|[0-9]+\.[^\s]+)"),
        ("psrchive-config", ("--version",), r"([0-9]+\.[^\s]+)"),
    ),
    capabilities=("inspect", "convert", "merge", "zap", "profile", "toa"),
)

TEMPO2_SPEC = BackendSpec(
    name="tempo2",
    probes=(("tempo2", ("-v",), r"(tempo2[^\s]*|[0-9]{4}[^\s]*)"),),
    capabilities=("timing_fit", "timing_diagnose"),
)

PSRCAT_SPEC = BackendSpec(
    name="psrcat",
    probes=(("psrcat", ("-v",), r"([0-9]{4}[^\s]*|[0-9]+\.[^\s]+)"),),
    capabilities=("catalog_search", "catalog_get", "ephemeris_export"),
)

PINT_SPEC = BackendSpec(name="pint", python=True, capabilities=("timing_fit", "timing_diagnose", "ephemeris_export"))

ALL_SPECS: dict[str, BackendSpec] = {
    s.name: s for s in (DSPSR_SPEC, PSRCHIVE_SPEC, TEMPO2_SPEC, PSRCAT_SPEC, PINT_SPEC)
}


class BackendManager:
    """Resolves binaries from config paths or PATH, probes versions, caches results."""

    def __init__(self, cfg: Config, runner: ProcessRunner, *, python_exe: str | None = None) -> None:
        self._cfg = cfg
        self._runner = runner
        self._python_exe = python_exe or "python3"
        self._cache: dict[str, BackendInfo] = {}

    def configured_path(self, name: str) -> str | None:
        b = self._cfg.backends
        return {
            "dspsr": b.dspsr,
            "psrchive": b.psrchive_bin,
            "tempo2": b.tempo2,
            "psrcat": b.psrcat,
        }.get(name)

    def resolve(self, binary: str) -> str | None:
        """Resolve a binary name to an executable path, or None."""
        if "/" in binary:
            return binary if Path(binary).exists() else None
        configured = self.configured_path(binary)
        if configured:
            if Path(configured).is_file():
                return str(configured)
            found = shutil.which(configured)
            if found:
                return found
        return shutil.which(binary)

    def resolve_psrchive_tool(self, tool: str) -> str | None:
        """Resolve a psrchive suite binary (vap, psrstat, ...)."""
        base = self._cfg.backends.psrchive_bin
        if base:
            candidate = Path(base) / tool
            if candidate.is_file():
                return str(candidate)
        return shutil.which(tool)

    async def probe_all(self, *, refresh: bool = False) -> dict[str, BackendInfo]:
        if refresh:
            self._cache.clear()
        for name, spec in ALL_SPECS.items():
            if name not in self._cache:
                self._cache[name] = await self._probe(spec)
        return dict(self._cache)

    async def get(self, name: str, *, refresh: bool = False) -> BackendInfo:
        if refresh or name not in self._cache:
            self._cache[name] = await self._probe(ALL_SPECS[name])
        return self._cache[name]

    def cached(self, name: str) -> BackendInfo | None:
        return self._cache.get(name)

    async def require(self, name: str) -> BackendInfo:
        info = await self.get(name)
        if not info.available:
            reason = info.reason or "不可用"
            raise env_missing(name, reason)
        return info

    async def _probe(self, spec: BackendSpec) -> BackendInfo:
        if spec.python:
            return await self._probe_python(spec)
        for binary, args, regex in spec.probes:
            path = self.resolve_psrchive_tool(binary) if spec.name == "psrchive" else self.resolve(binary)
            if path is None:
                continue
            info = await self._probe_binary(spec, path, list(args), regex)
            if info.available:
                return info
        reasons = {
            "dspsr": "未在 PATH 或配置路径中找到 dspsr",
            "psrchive": "未找到 psrchive 工具链(psrstat/psrchive-config)",
            "tempo2": "未找到 tempo2,或 tempo2 未启用",
            "psrcat": "未找到 psrcat 二进制",
        }
        unavailable_reason = reasons.get(spec.name, f"未找到 {spec.name}")
        if spec.name == "tempo2" and not self._cfg.backends.tempo2_enabled:
            unavailable_reason = "配置中 tempo2_enabled=false"
        return BackendInfo(available=False, reason=unavailable_reason, capabilities=list(spec.capabilities))

    async def _probe_binary(self, spec: BackendSpec, path: str, args: list[str], regex: str) -> BackendInfo:
        try:
            result = await self._runner.run([path, *args], timeout_s=20.0)
        except PsrmcpError as exc:
            return BackendInfo(available=False, path=path, reason=str(exc.message))
        text = (result.stdout_text + result.stderr_text).strip()
        if result.exit_code != 0 and not text:
            return BackendInfo(
                available=False, path=path, reason=f"版本查询退出码 {result.exit_code}"
            )
        m = re.search(regex, text)
        version = m.group(1) if m else text.splitlines()[0][:80] if text else None
        return BackendInfo(available=True, path=path, version=version, capabilities=list(spec.capabilities))

    async def _probe_python(self, spec: BackendSpec) -> BackendInfo:
        if spec.name == "pint" and not self._cfg.backends.pint_enabled:
            return BackendInfo(available=False, reason="配置中 pint_enabled=false")
        try:
            result = await self._runner.run(
                [
                    self._python_exe,
                    "-c",
                    "import importlib;m=importlib.import_module('%s');print(getattr(m,'__version__','?'))"
                    % ("pint" if spec.name == "pint" else spec.name),
                ],
                timeout_s=60.0,
            )
        except PsrmcpError as exc:
            return BackendInfo(available=False, reason=exc.message)
        version = result.stdout_text.strip() or None
        if result.exit_code != 0 or version is None:
            return BackendInfo(
                available=False,
                reason=f"python 包 {spec.name} 导入失败: {result.stderr_tail(400)}",
            )
        return BackendInfo(available=True, version=version, capabilities=list(spec.capabilities))
