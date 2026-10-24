#!/usr/bin/env bash
#
# dev_env.sh — 开发/交互环境的迷你加载器
#
# 用法:
#   source scripts/dev_env.sh              # 用默认前缀 ~/Pulsar
#   source scripts/dev_env.sh /opt/pulsar  # 显式指定安装前缀
#
# 做三件事:
#   1. source <prefix>/env.sh(PATH / LD_LIBRARY_PATH / TEMPO2 / PSRCAT_FILE)
#   2. 激活 Python venv:优先仓库 .venv(uv sync 的开发环境),否则 <prefix>/venv
#   3. 打印一条简短摘要,方便确认后端是否可见
#
# 幂等,可反复 source;只用于交互 shell 与契约测试,不用于 MCP 服务器部署
# (服务器的 config.toml [backends] 直接写绝对路径,见 docs/DESIGN.md §8.1)。

_pulsar_dev_prefix="${1:-${PULSAR_PREFIX:-${PSRSOFT_DIR:-$HOME/Pulsar}}}"
_pulsar_dev_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"

if [[ -f "$_pulsar_dev_prefix/env.sh" ]]; then
  # shellcheck disable=SC1091
  . "$_pulsar_dev_prefix/env.sh"
else
  echo "[dev_env] 未找到 $_pulsar_dev_prefix/env.sh —— 请先运行: bash scripts/install_pulsar_stack.sh" >&2
fi

if [[ -f "$_pulsar_dev_root/.venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  . "$_pulsar_dev_root/.venv/bin/activate"
elif [[ -f "$_pulsar_dev_prefix/venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  . "$_pulsar_dev_prefix/venv/bin/activate"
else
  echo "[dev_env] 未找到 venv(仓库 $_pulsar_dev_root/.venv 或 $_pulsar_dev_prefix/venv)" >&2
fi

{
  echo "[dev_env] PULSAR_PREFIX=$_pulsar_dev_prefix"
  echo "[dev_env] TEMPO2=${TEMPO2:-<未设置>}  PSRCAT_FILE=${PSRCAT_FILE:-<未设置>}"
  for _pulsar_dev_tool in tempo2 psrchive-config dspsr psrcat; do
    if command -v "$_pulsar_dev_tool" >/dev/null 2>&1; then
      echo "[dev_env]   $_pulsar_dev_tool -> $(command -v "$_pulsar_dev_tool")"
    else
      echo "[dev_env]   $_pulsar_dev_tool -> 缺失(服务器会优雅降级)"
    fi
  done
  unset _pulsar_dev_tool
} >&2

unset _pulsar_dev_prefix _pulsar_dev_root
