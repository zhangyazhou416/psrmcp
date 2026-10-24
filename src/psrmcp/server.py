"""Server entrypoint: AppContext assembly + low-level MCP wiring (stdio)."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from . import SERVER_NAME, __version__
from .config import Config, load_config
from ._context import build_context
from .prompts import get_prompt, list_prompts
from .resources import list_resources, read_resource
from .tools.registry import ToolRegistry
from .tools import (
    archive as tools_archive,
    artifacts as tools_artifacts,
    catalog as tools_catalog,
    data as tools_data,
    dedispersion as tools_dedispersion,
    profile as tools_profile,
    rfi as tools_rfi,
    system as tools_system,
    timing as tools_timing,
)

log = logging.getLogger(__name__)

_TOOL_MODULES = (
    tools_system, tools_data, tools_catalog, tools_archive,
    tools_dedispersion, tools_rfi, tools_profile, tools_timing, tools_artifacts,
)


def build_registry(ctx) -> ToolRegistry:
    reg = ToolRegistry(ctx)
    for mod in _TOOL_MODULES:
        mod.register(reg)
    return reg


def create_server(cfg: Config):
    """Assemble the low-level MCP Server with all 19 tools / resources / prompts.

    SDK >= 2 takes handler callables in the constructor (the decorator API is
    gone): on_list_tools / on_call_tool / on_list_resources / on_read_resource /
    on_list_prompts / on_get_prompt.
    """
    import mcp_types as types

    ctx = build_context(cfg)
    registry = build_registry(ctx)

    async def on_list_tools(_ctx, _params) -> types.ListToolsResult:
        return types.ListToolsResult(tools=registry.mcp_tools())

    async def on_call_tool(_ctx, params) -> types.CallToolResult:
        return await registry.dispatch(params.name, params.arguments)

    async def on_list_resources(_ctx, _params) -> types.ListResourcesResult:
        return types.ListResourcesResult(resources=list_resources(ctx))

    async def on_read_resource(_ctx, params) -> types.ReadResourceResult:
        contents = await read_resource(ctx, str(params.uri))
        return types.ReadResourceResult(contents=contents)

    async def on_list_prompts(_ctx, _params) -> types.ListPromptsResult:
        return types.ListPromptsResult(prompts=list_prompts())

    async def on_get_prompt(_ctx, params) -> types.GetPromptResult:
        return types.GetPromptResult(messages=get_prompt(params.name, params.arguments))

    server = Server(
        SERVER_NAME,
        version=__version__,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
        on_list_resources=on_list_resources,
        on_read_resource=on_read_resource,
        on_list_prompts=on_list_prompts,
        on_get_prompt=on_get_prompt,
    )

    # Context travels with the server object for tests and callers that need it.
    server.pulsar_ctx = ctx  # type: ignore[attr-defined]
    server.pulsar_registry = registry  # type: ignore[attr-defined]
    return server


async def run_stdio(cfg: Config) -> None:
    server = create_server(cfg)
    init_opts = server.create_initialization_options()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, init_opts, raise_exceptions=False)


def _init_config_template() -> str:
    from textwrap import dedent

    return dedent("""\
        # psrmcp 配置模板(DESIGN.md 8.1)。写好各 [backends] 绝对路径后,
        # 用 PSRMCP_CONFIG=<path> psrmcp 启动;不指定时全用默认值+PATH 探测。
        [server]
        transport = "stdio"
        workspace = "~/.psrmcp/workspace"
        long_job_threshold_s = 20
        job_timeout_s = 7200
        max_concurrent_jobs = 2

        [data]
        read_roots = ["~/data"]
        allow_absolute_paths = false

        [backends]
        # 安装脚本后按 ~/.pulsar/env.sh 与 selfcheck.json 填绝对路径。
        dspsr = "dspsr"
        psrchive_bin = ""
        tempo2 = "tempo2"
        psrcat = "psrcat"
        pint_enabled = true
        tempo2_enabled = true

        [catalog]
        mode = "auto"            # local | online | auto
        database = ""            # psrcat.db 绝对路径;空则用 PATH 里的 psrcat
        cache_ttl_days = 30

        [limits]
        max_threads = 8
        max_input_gb = 50
        max_output_gb = 10
        max_result_kb = 512
    """)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="psrmcp", description="脉冲星数据处理 MCP Server")
    parser.add_argument("--config", type=Path, default=None, help="TOML 配置路径(PSRMCP_CONFIG 亦可)")
    parser.add_argument("--init-config", type=Path, default=None, metavar="PATH",
                        help="写出配置模板到 PATH 后退出")
    parser.add_argument("--log-level", default="WARNING", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args(argv)

    if args.init_config is not None:
        if args.init_config.exists():
            print(f"已存在,不覆盖: {args.init_config}", file=sys.stderr)
            return 1
        args.init_config.parent.mkdir(parents=True, exist_ok=True)
        args.init_config.write_text(_init_config_template(), encoding="utf-8")
        print(f"配置模板已写出: {args.init_config}")
        return 0

    logging.basicConfig(level=args.log_level, stream=sys.stderr,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = load_config(args.config)
    import asyncio

    try:
        asyncio.run(run_stdio(cfg))
    except KeyboardInterrupt:
        log.info("server stopped by KeyboardInterrupt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
