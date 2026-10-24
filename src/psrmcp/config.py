"""Configuration loading (DESIGN.md §8.1). TOML file + defaults; failures raise E_CONFIG."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .errors import config_error

CONFIG_ENV_VAR = "PSRMCP_CONFIG"

DEFAULT_CONFIG_PATH = "~/.psrmcp.toml"


@dataclass
class ServerConfig:
    transport: str = "stdio"
    host: str = "127.0.0.1"
    port: int = 8765
    workspace: Path = Path("~/.psrmcp/workspace")
    long_job_threshold_s: float = 20.0
    job_timeout_s: float = 7200.0
    max_concurrent_jobs: int = 2


@dataclass
class DataConfig:
    read_roots: list[Path] = field(default_factory=list)
    allow_absolute_paths: bool = False


@dataclass
class BackendsConfig:
    dspsr: str | None = None
    psrchive_bin: str | None = None
    tempo2: str | None = None
    psrcat: str | None = None
    pint_enabled: bool = True
    tempo2_enabled: bool = True


@dataclass
class CatalogConfig:
    mode: str = "auto"  # local | online | auto
    database: Path = Path("~/.psrmcp/psrcat.db")
    cache_ttl_days: int = 30


@dataclass
class LimitsConfig:
    max_threads: int = 8
    max_input_gb: float = 50.0
    max_output_gb: float = 100.0
    max_result_kb: int = 256

    @property
    def max_input_bytes(self) -> int:
        return int(self.max_input_gb * 1024**3)

    @property
    def max_output_bytes(self) -> int:
        return int(self.max_output_gb * 1024**3)

    @property
    def max_result_bytes(self) -> int:
        return self.max_result_kb * 1024


@dataclass
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    data: DataConfig = field(default_factory=DataConfig)
    backends: BackendsConfig = field(default_factory=BackendsConfig)
    catalog: CatalogConfig = field(default_factory=CatalogConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    source_path: Path | None = None


def default_config_path() -> Path:
    return Path(os.environ.get(CONFIG_ENV_VAR, DEFAULT_CONFIG_PATH)).expanduser()


def load_config(path: str | Path | None = None) -> Config:
    """Load config from TOML; missing file is fine (all defaults), invalid file is E_CONFIG."""
    cfg = Config()
    if path is None:
        path = os.environ.get(CONFIG_ENV_VAR)
        if not path:
            return cfg
    cfg_path = Path(path).expanduser()
    if not cfg_path.exists():
        if os.environ.get(CONFIG_ENV_VAR):  # explicitly configured but missing -> error
            raise config_error(f"配置文件不存在: {cfg_path}")
        return cfg
    try:
        raw = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise config_error(f"配置文件解析失败: {cfg_path}: {exc}") from exc
    _apply(cfg, raw)
    cfg.source_path = cfg_path
    return cfg


def _apply(cfg: Config, raw: dict) -> None:  # noqa: C901
    problems: list[str] = []

    def bad(msg: str) -> None:
        problems.append(msg)

    section = raw.get("server", {})
    if not isinstance(section, dict):
        bad("[server] 必须是表")
    else:
        srv = cfg.server
        for key, target in (
            ("transport", "transport"),
            ("host", "host"),
            ("port", "port"),
            ("workspace", "workspace"),
            ("long_job_threshold_s", "long_job_threshold_s"),
            ("job_timeout_s", "job_timeout_s"),
            ("max_concurrent_jobs", "max_concurrent_jobs"),
        ):
            if key in section:
                _set(section, key, target, srv, bad)
        # workspace accepts "~" strings from TOML; normalize to expanded Path.
        if isinstance(srv.workspace, str):
            srv.workspace = Path(srv.workspace).expanduser()
        if srv.workspace == Path("~/.psrmcp/workspace"):
            srv.workspace = srv.workspace  # already a Path default

    section = raw.get("data", {})
    if not isinstance(section, dict):
        bad("[data] 必须是表")
    else:
        if "read_roots" in section:
            roots = section["read_roots"]
            if not isinstance(roots, list) or not all(isinstance(r, str) for r in roots):
                bad("data.read_roots 必须是字符串数组")
            else:
                cfg.data.read_roots = [Path(r).expanduser() for r in roots]
        if "allow_absolute_paths" in section:
            _set(section, "allow_absolute_paths", "allow_absolute_paths", cfg.data, bad)

    section = raw.get("backends", {})
    if not isinstance(section, dict):
        bad("[backends] 必须是表")
    else:
        for key in ("dspsr", "psrchive_bin", "tempo2", "psrcat"):
            if key in section:
                v = section[key]
                if v is not None and not isinstance(v, str):
                    bad(f"backends.{key} 必须是字符串或 null")
                else:
                    setattr(cfg.backends, key, Path(v).expanduser() if v else None)
        for key in ("pint_enabled", "tempo2_enabled"):
            if key in section:
                _set(section, key, key, cfg.backends, bad)

    section = raw.get("catalog", {})
    if not isinstance(section, dict):
        bad("[catalog] 必须是表")
    else:
        if "mode" in section:
            if section["mode"] not in ("local", "online", "auto"):
                bad("catalog.mode 必须是 local|online|auto")
            else:
                cfg.catalog.mode = section["mode"]
        if "database" in section:
            cfg.catalog.database = Path(section["database"]).expanduser()
        if "cache_ttl_days" in section:
            _set(section, "cache_ttl_days", "cache_ttl_days", cfg.catalog, bad)

    section = raw.get("limits", {})
    if not isinstance(section, dict):
        bad("[limits] 必须是表")
    else:
        for key in ("max_threads", "max_input_gb", "max_output_gb", "max_result_kb"):
            if key in section:
                _set(section, key, key, cfg.limits, bad)

    if problems:
        raise config_error("配置无效: " + "; ".join(problems), details={"problems": problems})


def _set(section: dict, key: str, attr: str, target: object, bad) -> None:  # noqa: ANN001
    try:
        setattr(target, attr, section[key])
    except (TypeError, ValueError):
        bad(f"{key} 类型无效")


def workspace_root(cfg: Config) -> Path:
    return cfg.server.workspace.expanduser()
