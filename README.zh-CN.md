# psrmcp

[English](README.md) | 简体中文

把脉冲星天文软件链(dspsr / psrchive / tempo2 / psrcat / PINT)封装成 MCP(Model Context Protocol)Server:面向任务的工具、参数受控、结果结构化、全程可追溯。

仓库:<https://gitlab.com/zhangyazhou416/psrmcp>
仓库:<https://github.com/zhangyazhou416/psrmcp>

- 功能总览:[docs/FEATURES.md](docs/FEATURES.md)(能做什么、19 个工具一览)
- 使用教程:[docs/TUTORIAL.md](docs/TUTORIAL.md)(安装配置、接入客户端、完整处理链实战)
- 设计方案:[docs/DESIGN.md](docs/DESIGN.md)
- 工具契约:[docs/TOOLS.md](docs/TOOLS.md)(19 个工具的输入/输出 Schema)
- 一键安装脉冲星软件栈:[scripts/install_pulsar_stack.sh](scripts/install_pulsar_stack.sh)(全源码编译进 `~/.pulsar`,详见 [docs/INSTALL.md](docs/INSTALL.md))

## 快速开始

```bash
# 1. 安装脉冲星软件栈(可选,缺省时服务器会优雅降级并在 pulsar_environment 中报告)
bash scripts/install_pulsar_stack.sh          # 加 --with-presto 连 PRESTO 一起装

# 2. 准备 MCP 服务器环境
uv sync

# 3. 生成默认配置并启动(stdio)
uv run psrmcp --init-config ~/.psrmcp.toml
PSRMCP_CONFIG=~/.psrmcp.toml uv run psrmcp
```

### 接入 MCP 客户端(如 Claude Code / Desktop)

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

## 开发

```bash
uv run pytest -q                        # 单元测试(不需要安装任何后端)
PSRMCP_CONTRACT=1 uv run pytest tests/contract   # 契约测试(需要已安装软件栈)
```

## 发布到 PyPI

CI 自动发布(PyPI Trusted Publishing / OIDC,CI 中无长期凭据):主通道为 [.github/workflows/publish.yml](.github/workflows/publish.yml),[.gitlab-ci.yml](.gitlab-ci.yml) 备用。

```bash
git tag v0.1.0 && git push origin v0.1.0    # 打 tag 即构建并上传(tag 与 version 保持一致)
```

首次发布前需在 pypi.org 登记 pending publisher(Owner `zhangyazhou416`、Repository `psrmcp`、Workflow `publish.yml`),见 workflow 文件顶部注释。手动发布:`uv build && uv publish`。

## 状态

v0.1.0 — MVP(Phase 0–2):环境自检、数据发现、星表查询、archive 检查/转换/合并、消色散、RFI、标准轮廓、TOA、PINT 计时拟合与诊断。

## 许可

[MIT](LICENSE)
