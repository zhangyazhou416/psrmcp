#!/usr/bin/env bash
#
# dev_env.sh — minimal loader for dev/interactive shells
#
# Usage:
#   source scripts/dev_env.sh              # default prefix ~/Pulsar
#   source scripts/dev_env.sh /opt/pulsar  # explicit install prefix
#
# Does three things:
#   1. source <prefix>/env.sh (PATH / LD_LIBRARY_PATH / TEMPO2 / PSRCAT_FILE)
#   2. activate a Python venv: repo .venv (uv sync dev env) first, else <prefix>/venv
#   3. print a short summary to confirm which backends are visible
#
# Idempotent, safe to source repeatedly; for interactive shells and contract tests
# only, not for MCP server deployment (the server's config.toml [backends] uses
# absolute paths directly; see docs/DESIGN.md §8.1).

_pulsar_dev_prefix="${1:-${PULSAR_PREFIX:-${PSRSOFT_DIR:-$HOME/Pulsar}}}"
_pulsar_dev_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"

if [[ -f "$_pulsar_dev_prefix/env.sh" ]]; then
  # shellcheck disable=SC1091
  . "$_pulsar_dev_prefix/env.sh"
else
  echo "[dev_env] $_pulsar_dev_prefix/env.sh not found — run first: bash scripts/install_pulsar_stack.sh" >&2
fi

if [[ -f "$_pulsar_dev_root/.venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  . "$_pulsar_dev_root/.venv/bin/activate"
elif [[ -f "$_pulsar_dev_prefix/venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  . "$_pulsar_dev_prefix/venv/bin/activate"
else
  echo "[dev_env] no venv found (repo $_pulsar_dev_root/.venv or $_pulsar_dev_prefix/venv)" >&2
fi

{
  echo "[dev_env] PULSAR_PREFIX=$_pulsar_dev_prefix"
  echo "[dev_env] TEMPO2=${TEMPO2:-<unset>}  PSRCAT_FILE=${PSRCAT_FILE:-<unset>}"
  for _pulsar_dev_tool in tempo2 psrchive-config dspsr psrcat; do
    if command -v "$_pulsar_dev_tool" >/dev/null 2>&1; then
      echo "[dev_env]   $_pulsar_dev_tool -> $(command -v "$_pulsar_dev_tool")"
    else
      echo "[dev_env]   $_pulsar_dev_tool -> missing (server degrades gracefully)"
    fi
  done
  unset _pulsar_dev_tool
} >&2

unset _pulsar_dev_prefix _pulsar_dev_root
