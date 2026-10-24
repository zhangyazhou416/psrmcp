"""Stdio smoke client: initialize -> tools/list -> pulsar_environment -> resource read.

Usage:
    uv run python examples/client_stdio.py [--config path/to/config.toml]

Exits non-zero if fewer than 19 tools are listed or the round-trip fails.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

EXPECTED_TOOLS = {
    "pulsar_environment", "data_find", "catalog_search", "catalog_get", "ephemeris_export",
    "archive_inspect", "archive_convert", "archive_merge", "dedisperse",
    "rfi_scan", "rfi_zap", "profile_standardize", "profile_measure",
    "toa_extract", "timing_fit", "timing_diagnose",
    "artifact_list", "job_get", "job_cancel",
}


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=None, help="PSRMCP_CONFIG value for the server")
    args = parser.parse_args()

    # mcp SDK 在 env=None 时只传最小默认环境(会丢掉 PSRMCP_CONFIG),
    # 这里显式继承当前 shell 环境,--config 只做覆盖
    env = os.environ.copy()
    if args.config:
        env["PSRMCP_CONFIG"] = args.config
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "psrmcp.server"],
        env=env,
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print(f"connected: {init.server_info.name} v{init.server_info.version}")

            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            missing = EXPECTED_TOOLS - names
            extra = names - EXPECTED_TOOLS
            print(f"tools: {len(names)} listed")
            if missing:
                print(f"MISSING tools: {sorted(missing)}", file=sys.stderr)
                return 1
            if extra:
                print(f"EXTRA tools: {sorted(extra)}", file=sys.stderr)

            result = await session.call_tool("pulsar_environment", {})
            print("pulsar_environment ->")
            print(result.content[0].text if result.content else "(empty)")

            for uri in ("pulsar://environment", "pulsar://docs/reduction"):
                try:
                    res = await session.read_resource(uri)
                    text = res.contents[0].text
                    print(f"resource {uri}: {len(text)} bytes")
                except Exception as exc:  # noqa: BLE001
                    print(f"resource {uri} FAILED: {exc}", file=sys.stderr)
                    return 1

            prompts = await session.list_prompts()
            print(f"prompts: {[p.name for p in prompts.prompts]}")

            print("SMOKE OK")
            return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
