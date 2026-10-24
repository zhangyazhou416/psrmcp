# psrmcp

English | [简体中文](README.zh-CN.md)

An MCP (Model Context Protocol) server wrapping the pulsar astronomy software stack (dspsr / psrchive / tempo2 / psrcat / PINT): task-oriented tools, controlled parameters, structured results, and full provenance.

Repositories: <https://gitlab.com/zhangyazhou416/psrmcp> · <https://github.com/zhangyazhou416/psrmcp>

- Overview: [docs/FEATURES.md](docs/FEATURES.md) (what it does, all 19 tools) — Chinese
- Tutorial: [docs/TUTORIAL.md](docs/TUTORIAL.md) (setup, MCP clients, a full processing-chain walkthrough) — Chinese
- Design: [docs/DESIGN.md](docs/DESIGN.md) — Chinese
- Tool contracts: [docs/TOOLS.md](docs/TOOLS.md) (input/output schemas for all 19 tools) — Chinese
- One-click software stack installer: [scripts/install_pulsar_stack.sh](scripts/install_pulsar_stack.sh) (builds everything into `~/.pulsar`, see [docs/INSTALL.md](docs/INSTALL.md) — Chinese)

## Quick start

### Install from PyPI

```bash
pip install psrmcp        # library + the psrmcp CLI entry point
# standalone CLI instead: uv tool install psrmcp  /  pipx install psrmcp

psrmcp --init-config ~/.psrmcp.toml
PSRMCP_CONFIG=~/.psrmcp.toml psrmcp
```

The pulsar software stack is installed separately (below); without it the server still starts and reports missing backends.

### Run from source

```bash
# 1. Install the pulsar software stack (optional; without it the server degrades
#    gracefully and reports each missing backend via pulsar_environment)
bash scripts/install_pulsar_stack.sh          # add --with-presto to also build PRESTO

# 2. Set up the MCP server environment
uv sync

# 3. Generate a default config and start (stdio)
uv run psrmcp --init-config ~/.psrmcp.toml
PSRMCP_CONFIG=~/.psrmcp.toml uv run psrmcp
```

### Connect an MCP client (e.g. Claude Code / Claude Desktop)

```json
{
  "mcpServers": {
    "psrmcp": {
      "command": "uv",
      "args": ["--directory", "/path/to/psrmcp", "run", "psrmcp"],
      "env": {"PSRMCP_CONFIG": "~/.psrmcp.toml"}
    }
  }
}
```

Installed from PyPI, the entry point is simply `"command": "psrmcp"`.

## Development

```bash
uv run pytest -q                                  # unit tests (no backends required)
PSRMCP_CONTRACT=1 uv run pytest tests/contract    # contract tests (needs the installed stack)
```

## Publishing to PyPI

Automatic via CI (PyPI Trusted Publishing / OIDC — no long-lived tokens): [.github/workflows/publish.yml](.github/workflows/publish.yml) is the primary channel, [.gitlab-ci.yml](.gitlab-ci.yml) kept as a fallback.

```bash
git tag v0.1.0 && git push origin v0.1.0    # a tag triggers build & upload (keep tag in sync with version)
```

One-time setup: register a pending publisher on pypi.org (Owner `zhangyazhou416`, Repository `psrmcp`, Workflow `publish.yml`) — see the workflow file header for details. Manual publishing: `uv build && uv publish`.

## Status

v0.1.0 — MVP (Phase 0–2): environment introspection, data discovery, catalog queries, archive inspect/convert/merge, dedispersion, RFI, standard profiles, TOA extraction, PINT timing fits and diagnostics.

## License

[MIT](LICENSE)
