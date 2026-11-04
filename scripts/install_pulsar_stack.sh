#!/usr/bin/env bash
#
# install_pulsar_stack.sh — one-shot source-build installer for the pulsar astronomy stack
#
# Target platform : Debian-based Linux (Deepin 25 / Debian 12+), gcc 12+, ~20 cores / 60GB RAM
# Installs        : fftw/cfitsio + tempo (optional) + tempo2 + pgplot (optional) + psrchive
#                   + dspsr + psrcat (+ optional PRESTO) + an isolated Python venv
# Provenance      : every URL / configure flag is calibrated against a real, successful
#                   2026-10 install (tempo2=github ipta, dspsr=sourceforge, psrcat=local
#                   tarball + gcc -fcommon, pgplot=ASTRON mirror; the ATNF/Caltech
#                   official locations are dead)
# Hard rules      : everything builds from source into a single prefix (default ~/Pulsar,
#                   honors an exported PSRSOFT_DIR); no Docker; no conda-forge; MCP tool
#                   plotting is handled by matplotlib on the Python side, PGPLOT only
#                   serves CLI companion tools like psrplot;
#                   ~/miniconda3, ~/anaconda3, ~/mambaforge are stripped from PATH so
#                   conda's python / libstdc++ cannot pollute configure.
#
# Usage      : bash scripts/install_pulsar_stack.sh --help
# Versions   : maintained centrally in versions.env (same directory)
#
# Stages (fixed order; each stage writes $PREFIX/.stack/<stage>.done on success, and
# re-runs with unchanged inputs are skipped automatically):
#   preflight → apt-deps → python-venv → deps (fftw/cfitsio) → [tempo] → tempo2
#            → [pgplot] → psrchive → dspsr → psrcat → [presto] → self-check

set -Eeuo pipefail

# ===========================================================================
# Constants and global state
# ===========================================================================
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
VERSIONS_ENV="$SCRIPT_DIR/versions.env"
SCRIPT_NAME="install_pulsar_stack.sh"

# NOTE: deliberately no readonly — re-sourcing this script (e.g. from a test harness)
# must not fail with "readonly variable" errors
ALL_STAGES=(
  preflight apt-deps python-venv deps tempo tempo2 pgplot psrchive dspsr psrcat presto self-check
)
# System packages installed by apt-deps (deliberately no boost-python / vtk; fftw/cfitsio/pgplot
# go through the source stages deps/pgplot — system cfitsio is non-reentrant and system fftw
# has no single precision, while psrchive/dspsr need the prefix-built 3.49/3.2.2)
APT_PKGS=(
  build-essential gfortran git wget curl ca-certificates
  autoconf automake autoconf-archive libtool pkg-config m4 patch perl rsync
  python3-dev libgsl-dev libglib2.0-dev
  libpng-dev libx11-dev unzip xz-utils ninja-build
)
DISK_MIN_GB=20
DOWNLOAD_DELAYS=(2 5 10)

# --- CLI defaults ---------------------------------------------------------
PREFIX="${PULSAR_PREFIX:-${PSRSOFT_DIR:-$HOME/Pulsar}}"
JOBS="$(nproc 2>/dev/null || printf '4')"
WITH_PRESTO=0
DRY_RUN=0
ASSUME_YES=0
OPT_NO_COLOR=0
SRC_DIR=""          # defaults to $PREFIX/src, normalized in main()
ENV_FILE=""         # defaults to $PREFIX/env.sh, normalized in main()
WITHOUT_LIST=()
ONLY_LIST=()
PIN_TEMPO2=""
PIN_PSRCHIVE=""
PIN_DSPSR=""
PIN_PRESTO=""
PIN_TEMPO=""
PSRCAT_TARBALL=""    # local psrcat tarball; empty = auto-search (ATNF download URL is dead)

# --- Runtime globals -------------------------------------------------------
LOG_DIR="$PREFIX/logs"
STACK_DIR="$PREFIX/.stack"
STAGE_CURRENT=""    # stage currently running (empty = not inside a stage)
STAGE_LOG=""        # log path of the current stage
ERR_CAPTURE=0       # 1 = inside run_stage's failure-capture block (outer trap stays silent)
UV_BIN=""           # chosen uv (non-conda paths preferred)
TMP_PATH=""         # temp path to clean up on EXIT

declare -a SUM_STAGE=() SUM_STATUS=() SUM_NOTE=()
declare -a SC_NAME=() SC_OK=() SC_VER=() SC_PATH=()
# ASSUMPTION: modern dspsr trees list the backend modules to build in a top-level
# backends.list; older trees use a config/modules file. Write both (see select_dspsr_backends).
DSPSR_BACKENDS=(apsr asp bcpm bpsr caspsr cpsr2 cpsr dummy fits gmrt guppi kat
                lbadr64 lbadr lump lwa mark4 mark5 maxim mwa pdev pmdaq s2 sigproc
                spda1k spigot vdif uwb)

# ===========================================================================
# Colors and logging
# ===========================================================================
C_RESET="" C_BOLD="" C_RED="" C_GREEN="" C_YELLOW="" C_BLUE="" C_CYAN=""

setup_colors() {
  if [[ $OPT_NO_COLOR -eq 1 ]] || [[ -n "${NO_COLOR:-}" ]] || [[ ! -t 2 ]]; then
    return 0
  fi
  C_RESET=$'\033[0m'
  C_BOLD=$'\033[1m'
  C_RED=$'\033[31m'
  C_GREEN=$'\033[32m'
  C_YELLOW=$'\033[33m'
  C_BLUE=$'\033[34m'
  C_CYAN=$'\033[36m'
}

now_utc() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }

_log() { # _log <LEVEL> <COLOR> <text...>
  local level="$1" color="$2"
  shift 2
  printf '%s %s[%s]%s %s\n' "$(now_utc)" "$color" "$level" "$C_RESET" "$*" >&2
}

log_info() { _log "INFO" "$C_BLUE" "$*"; }
log_ok()   { _log "OK  " "$C_GREEN" "$*"; }
log_warn() { _log "WARN" "$C_YELLOW" "$*"; }
log_err()  { _log "ERR " "$C_RED" "$*"; }
plan()     { printf '%s[plan]%s %s\n' "$C_CYAN" "$C_RESET" "$*" >&2; }

die() {
  log_err "$*"
  exit 1
}

# ===========================================================================
# Error handling
# ===========================================================================
on_error() {
  local rc="$1" line="$2"
  if (( rc < 1 )); then
    rc=1
  fi
  # Inside run_stage's "capture block" (outer set +e) run_stage reads PIPESTATUS itself;
  # the outer trap just returns quietly, avoiding duplicate output / early exit.
  if [[ "${ERR_CAPTURE:-0}" == "1" ]]; then
    return 0
  fi
  if [[ -n "${STAGE_CURRENT:-}" ]]; then
    # failure inside a stage: run_stage prints the log tail, this is just one locating line
    log_err "stage ${STAGE_CURRENT} failed at ${SCRIPT_NAME} line ${line} (exit=${rc}); log: ${STAGE_LOG:-<none>}"
    exit "$rc"
  fi
  log_err "${SCRIPT_NAME} failed at line ${line} (exit=${rc})"
  if [[ -n "${STAGE_LOG:-}" && -f "$STAGE_LOG" ]]; then
    log_err "---- last 20 lines of the failed stage log ($STAGE_LOG) ----"
    tail -n 20 "$STAGE_LOG" >&2 || true
    log_err "------------------------------------------"
  fi
  exit "$rc"
}
trap 'on_error "$?" "$LINENO"' ERR

on_exit() {
  if [[ -n "${TMP_PATH:-}" && -e "$TMP_PATH" ]]; then
    rm -rf -- "$TMP_PATH" || true
  fi
}
trap on_exit EXIT

# ===========================================================================
# Basic helpers
# ===========================================================================
cmd_exists() { command -v "$1" >/dev/null 2>&1; }

# absolutize (no realpath dependency, no symlink resolution)
abspath() {
  local p="$1"
  case "$p" in
    /*) ;;
    *) p="$PWD/$p" ;;
  esac
  printf '%s' "$p"
}

gcc_version() {
  gcc -dumpfullversion 2>/dev/null || gcc -dumpversion 2>/dev/null || printf 'unknown'
}

# under dry-run only print the command that would run; zero side effects
run() { # run <cmd> <args...>
  if [[ $DRY_RUN -eq 1 ]]; then
    printf '    [dry-run]' >&2
    printf ' %q' "$@" >&2
    printf '\n' >&2
    return 0
  fi
  "$@"
}

run_in() { # run_in <dir> <cmd> <args...>
  local dir="$1"
  shift
  if [[ $DRY_RUN -eq 1 ]]; then
    printf '    [dry-run] ( cd %q &&' "$dir" >&2
    printf ' %q' "$@" >&2
    printf ' )\n' >&2
    return 0
  fi
  (
    cd "$dir" || exit 1
    "$@"
  )
}

# strip conda dirs from PATH and dedupe (sanitize_path <path> → stdout)
sanitize_path() {
  local -a parts=() keep=()
  local IFS=':'
  read -ra parts <<< "$1" || true
  local p
  for p in "${parts[@]}"; do
    if [[ -z "$p" ]]; then
      continue
    fi
    case "$p" in
      *miniconda3*|*anaconda3*|*miniforge3*|*mambaforge*)
        log_info "preflight: stripping conda dir from PATH: $p"
        continue
        ;;
    esac
    # dedupe (note: with IFS=':' ${keep[*]} joins on ':', so the delimiter probe must use ':')
    if (( ${#keep[@]} > 0 )); then
      case ":${keep[*]}:" in
        *":$p:"*) continue ;;
      esac
    fi
    keep+=("$p")
  done
  if (( ${#keep[@]} == 0 )); then
    printf '%s' "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    return 0
  fi
  printf '%s' "$(IFS=':'; printf '%s' "${keep[*]}")"
}

# strip conda + put $PREFIX/bin first (idempotent)
apply_clean_path() {
  local clean
  clean="$(sanitize_path "$PATH")"
  export PATH="$clean"
  case ":$PATH:" in
    *":$PREFIX/bin:"*) ;;
    *) export PATH="$PREFIX/bin:$PATH" ;;
  esac
}

# target path (or its nearest existing ancestor)
nearest_existing() {
  local p="$1" parent
  while [[ ! -e "$p" ]]; do
    parent="$(dirname -- "$p")"
    if [[ "$parent" == "$p" ]]; then
      break
    fi
    p="$parent"
  done
  printf '%s' "$p"
}

fs_avail_gb() { # fs_avail_gb <probe> → stdout GB; returns 1 if the probe fails
  local probe="$1" kb
  kb="$(df -Pk -- "$probe" 2>/dev/null | awk 'NR==2 {print $4}')"
  if [[ ! "${kb:-}" =~ ^[0-9]+$ ]]; then
    return 1
  fi
  printf '%s' "$(( kb / 1024 / 1024 ))"
}

# download with retries: 3 attempts, backoff 2s/5s/10s (download_with_retry <URL> <DEST>)
download_with_retry() {
  local url="$1" dest="$2"
  local attempt delay
  if [[ $DRY_RUN -eq 1 ]]; then
    plan "curl -fL '$url' → '$dest' (3 retries with 2/5/10s backoff)"
    return 0
  fi
  mkdir -p "$(dirname -- "$dest")"
  for attempt in 1 2 3; do
    log_info "download (attempt ${attempt}/3): $url"
    rm -f -- "${dest}.part"
    if curl -fL --connect-timeout 30 --max-time 3600 -o "${dest}.part" "$url"; then
      mv -f -- "${dest}.part" "$dest"
      return 0
    fi
    rm -f -- "${dest}.part"
    if (( attempt < 3 )); then
      delay="${DOWNLOAD_DELAYS[$(( attempt - 1 ))]}"
      log_warn "download failed, retrying in ${delay}s: $url"
      sleep "$delay"
    fi
  done
  log_err "download failed after 3 attempts: $url"
  return 1
}

# extract a tar.gz into <dest>, stripping a single top-level dir (archive_install <tar.gz> <dest>)
archive_install() {
  local archive="$1" dest="$2"
  local tmp
  tmp="$(mktemp -d "${SRC_DIR}/.extract.XXXXXX")"
  TMP_PATH="$tmp"
  tar -xzf "$archive" -C "$tmp"
  local -a tops=()
  mapfile -d '' tops < <(find "$tmp" -mindepth 1 -maxdepth 1 -print0 | sort -z)
  mkdir -p "$dest"
  if (( ${#tops[@]} == 1 )) && [[ -d "${tops[0]}" && ! -L "${tops[0]}" ]]; then
    cp -a "${tops[0]}/." "$dest/"
  else
    cp -a "$tmp/." "$dest/"
  fi
  rm -rf -- "$tmp"
  if [[ "${TMP_PATH:-}" == "$tmp" ]]; then
    TMP_PATH=""
  fi
}

# ===========================================================================
# git helpers
# ===========================================================================
is_moving_ref() { # HEAD / default branch name → must be resolved to a commit on first run and written back
  case "$1" in
    HEAD|head|main|master) return 0 ;;
    *) return 1 ;;
  esac
}

# clone (or reuse) a repo and checkout <ref>; returns 1 on failure, fallback policy is up to the caller
ensure_repo() { # ensure_repo <dest> <url> <mirror> <ref> <label>
  local dest="$1" url="$2" mirror="$3" ref="$4" label="$5"
  if [[ $DRY_RUN -eq 1 ]]; then
    plan "git clone '$url' → '$dest' (mirror fallback: ${mirror:-none}), checkout '$ref'"
    return 0
  fi
  if [[ -d "$dest/.svn" ]]; then
    log_info "reusing existing svn checkout: $dest"
    return 0
  fi
  if [[ ! -d "$dest/.git" ]]; then
    if [[ -e "$dest" ]]; then
      log_warn "$dest exists but is not a git checkout; removing and re-cloning"
      rm -rf -- "$dest"
    fi
    mkdir -p "$(dirname -- "$dest")"
    log_info "cloning $label: $url"
    if ! git clone "$url" "$dest"; then
      if [[ -n "$mirror" ]]; then
        log_warn "primary clone failed, trying mirror: $mirror"
        rm -rf -- "$dest"
        if ! git clone "$mirror" "$dest"; then
          log_err "mirror clone failed too: $mirror"
          return 1
        fi
      else
        log_err "clone failed and no mirror configured: $url"
        return 1
      fi
    fi
  else
    log_info "reusing existing repo: $dest"
    if ! git -C "$dest" fetch --all --tags --prune; then
      log_warn "git fetch failed (offline?); continuing with the local objects"
    fi
  fi
  if is_moving_ref "$ref"; then
    if ! git -C "$dest" pull --ff-only; then
      log_warn "git pull --ff-only failed; using the repo's current HEAD"
    fi
  else
    if ! git -C "$dest" checkout -f "$ref"; then
      log_err "checkout failed: $ref"
      return 1
    fi
  fi
  return 0
}

# resolve <ref> to a commit (local git operations only, no network)
resolve_repo_ref() { # resolve_repo_ref <dest> <ref> → stdout <sha>
  local dest="$1" ref="$2" sha=""
  if [[ $DRY_RUN -eq 1 ]]; then
    printf '%s' "$ref"
    return 0
  fi
  if sha="$(git -C "$dest" rev-parse "${ref}^{commit}" 2>/dev/null)"; then
    printf '%s' "$sha"
    return 0
  fi
  if sha="$(git -C "$dest" rev-parse HEAD 2>/dev/null)"; then
    printf '%s' "$sha"
    return 0
  fi
  # non-git tree (e.g. an svn checkout): return as-is
  printf '%s' "$ref"
}

# when a pin is HEAD/a branch name, write the resolved commit back to versions.env for reproducibility
write_back_ref() { # write_back_ref <VAR_NAME> <SHA>
  local var="$1" val="$2"
  local stamp tmp next
  if [[ ! -w "$VERSIONS_ENV" ]]; then
    log_warn "versions.env not writable, skipping write-back ($var=$val); the result is recorded in the stage marker"
    return 0
  fi
  stamp="$(date -u '+%Y-%m-%d')"
  tmp="${VERSIONS_ENV}.tmp.$$"
  next="$(awk -v var="$var" -v val="$val" -v d="$stamp" -v sn="$SCRIPT_NAME" '
    index($0, var "=") == 1 {
      printf "%s=\"%s\"  # resolved %s by %s\n", var, val, d, sn
      seen = 1
      next
    }
    { print }
    END {
      if (!seen) printf "%s=\"%s\"  # resolved %s by %s\n", var, val, d, sn
    }
  ' "$VERSIONS_ENV")"
  printf '%s\n' "$next" > "$tmp"
  mv -f -- "$tmp" "$VERSIONS_ENV"
  log_ok "wrote $var back to versions.env: $val (resolved $stamp)"
}

# ===========================================================================
# Build-time environment (used by psrchive / dspsr / presto configure / meson)
# ===========================================================================
export_build_env() {
  # TEMPO2 points at the runtime data dir (T2runtime deploy point); dspsr's configure is the
  # exception — its macro looks for headers under $TEMPO2/include, so the configure line
  # passes TEMPO2="$PREFIX" separately
  export TEMPO2="$PREFIX/tempo2"
  if [[ -d "$PREFIX/pgplot" ]]; then
    export PGPLOT_DIR="$PREFIX/pgplot"
  fi
  # bootstrap locates deps via which psrchive-config / tempo2-config, so $PREFIX/bin must come first
  apply_clean_path
  export LD_LIBRARY_PATH="$PREFIX/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
  export PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
  # ASSUMPTION: feeding the self-installed prefix to the build system via CPPFLAGS/LDFLAGS is
  # more stable than passing a pile of --with-* flags (those names drift across upstream
  # versions); also deliberately no --disable-*: let configure auto-detect — a missing
  # pgplot / boost-python turns off plotting and Python bindings automatically,
  # which is exactly the lean build this stack wants.
  export CPPFLAGS="-I$PREFIX/include${CPPFLAGS:+ $CPPFLAGS}"
  export LDFLAGS="-L$PREFIX/lib -Wl,-rpath,$PREFIX/lib${LDFLAGS:+ $LDFLAGS}"
}

# autotools bootstrap entry point (defensive: fall back to bash if the exec bit is lost)
run_bootstrap() {
  local dir="$1"
  if [[ $DRY_RUN -eq 1 && ! -d "$dir" ]]; then
    plan "( cd $dir && ./bootstrap )"
    return 0
  fi
  if [[ -x "$dir/bootstrap" ]]; then
    run_in "$dir" ./bootstrap
  elif [[ -f "$dir/bootstrap" ]]; then
    log_warn "$dir/bootstrap is not executable; invoking via bash"
    run_in "$dir" bash ./bootstrap
  elif [[ -f "$dir/autogen.sh" ]]; then
    log_warn "$dir has no bootstrap; using autogen.sh"
    run_in "$dir" bash ./autogen.sh
  else
    log_err "no bootstrap / autogen.sh found in $dir"
    return 1
  fi
}

# ===========================================================================
# Stage 0: preflight
# ===========================================================================
stage_preflight() {
  export LC_ALL=C
  export LANG=C
  apply_clean_path

  local tool
  for tool in gcc g++ make git sha256sum curl awk; do
    if ! cmd_exists "$tool"; then
      die "preflight: missing $tool (build-essential/git are installed by apt-deps; install manually if you skip that stage)"
    fi
  done
  if ! cmd_exists gfortran; then
    log_warn "preflight: gfortran not found (needed by psrchive/tempo2; installed in the apt-deps stage)"
  fi
  log_info "toolchain: gcc $(gcc_version) | make $(make -v 2>/dev/null | awk 'NR==1{print $3}') | bash ${BASH_VERSION} | jobs=$JOBS"

  # disk headroom (checks the filesystem holding $PREFIX)
  local probe avail_gb
  probe="$(nearest_existing "$PREFIX")"
  if avail_gb="$(fs_avail_gb "$probe")"; then
    if (( avail_gb < DISK_MIN_GB )); then
      die "preflight: the filesystem holding $probe has only ${avail_gb}GB free; at least ${DISK_MIN_GB}GB is required"
    fi
    log_ok "disk headroom: $probe has ${avail_gb}GB free (>= ${DISK_MIN_GB}GB)"
  else
    log_warn "preflight: cannot probe free space of $probe (df parse failed); skipping the disk check"
  fi

  run mkdir -p "$PREFIX"/{src,bin,lib,share,logs,.stack,psrcat,tempo2} \
    "$PREFIX/share/pulsar-stack"

  if [[ $DRY_RUN -eq 0 ]]; then
    {
      printf 'generated_by: %s\n' "$SCRIPT_NAME"
      printf 'generated_at: %s\n' "$(now_utc)"
      printf 'prefix: %s\n' "$PREFIX"
      printf 'jobs: %s\n' "$JOBS"
      printf 'gcc: %s\n' "$(gcc_version)"
      printf 'path: %s\n' "$PATH"
    } > "$PREFIX/share/pulsar-stack/build-info.txt"
  fi
}

# ===========================================================================
# Stage 1: apt-deps
# ===========================================================================
stage_apt_deps() {
  local pm=""
  if cmd_exists apt-get; then
    pm="apt"
  elif cmd_exists dnf; then
    pm="dnf"
  elif cmd_exists yum; then
    pm="yum"
  elif cmd_exists pacman; then
    pm="pacman"
  elif cmd_exists zypper; then
    pm="zypper"
  fi

  case "$pm" in
    apt) : ;;
    "")
      die "apt-deps: unrecognized package manager (no apt-get/dnf/yum/pacman/zypper).
Install the following packages manually, then re-run with --without apt-deps:
  ${APT_PKGS[*]}"
      ;;
    *)
      die "apt-deps: automatic install via $pm is not supported yet (this script currently only supports Debian-based systems).
Install the following packages manually, then re-run with --without apt-deps:
  ${APT_PKGS[*]}"
      ;;
  esac

  local -a sudo_cmd=()
  if [[ "${EUID}" -ne 0 ]]; then
    if cmd_exists sudo; then
      sudo_cmd=(sudo)
    else
      die "apt-deps: not root and no sudo available; cannot install system deps.
Re-run as root, or install manually and skip with --without apt-deps: ${APT_PKGS[*]}"
    fi
  fi

  # non-interactive environments require an explicit --yes
  if [[ $ASSUME_YES -ne 1 ]] && [[ ! -t 0 ]]; then
    die "apt-deps: non-interactive environment detected (stdin is not a tty); cannot confirm; re-run with --yes."
  fi

  run env DEBIAN_FRONTEND=noninteractive "${sudo_cmd[@]}" apt-get update
  run env DEBIAN_FRONTEND=noninteractive "${sudo_cmd[@]}" apt-get install -y "${APT_PKGS[@]}"
}

# one-time apt confirmation (asked in the main process, so the prompt stays out of stage logs)
confirm_apt() {
  if [[ $ASSUME_YES -eq 1 ]]; then
    return 0
  fi
  if [[ ! -t 0 ]]; then
    die "about to install system deps via apt, but this is a non-interactive environment; add --yes to continue unattended."
  fi
  local reply
  printf '%s' "About to install ${#APT_PKGS[@]} system packages via apt (build-essential/gfortran/libcfitsio-dev etc.). Continue? [y/N] " >&2
  if ! read -r reply; then
    die "failed to read confirmation; cancelled"
  fi
  case "$reply" in
    y|Y|yes|YES|Yes) return 0 ;;
    *) die "installation cancelled" ;;
  esac
}

# ===========================================================================
# Stage 2: python-venv
# ===========================================================================
find_uv() {
  UV_BIN=""
  local cand
  if cand="$(command -v uv 2>/dev/null)"; then
    case "$cand" in
      *miniconda3*|*anaconda3*|*miniforge3*|*mambaforge*) : ;;
      *) UV_BIN="$cand" ;;
    esac
  fi
  if [[ -z "$UV_BIN" && -x "$HOME/.local/bin/uv" ]]; then
    UV_BIN="$HOME/.local/bin/uv"
  fi
  if [[ -z "$UV_BIN" ]]; then
    # last resort: uv inside a conda dir. Only borrow that binary; the conda env still stays out of PATH.
    for cand in "$HOME/miniconda3/bin/uv" "$HOME/anaconda3/bin/uv" "$HOME/mambaforge/bin/uv"; do
      if [[ -x "$cand" ]]; then
        UV_BIN="$cand"
        log_warn "only found uv inside a conda dir: $cand (consider installing a standalone uv into ~/.local/bin)"
        break
      fi
    done
  fi
  if [[ -n "$UV_BIN" ]]; then
    log_info "uv: $UV_BIN ($("$UV_BIN" --version 2>/dev/null || printf 'version unknown'))"
  else
    log_warn "uv not found; falling back to python3 -m venv"
  fi
}

venv_python() { printf '%s' "$PREFIX/venv/bin/python"; }

# venv_pip <spec...>: prefer uv (fast, no reliance on the venv's built-in pip); falls back step by step
venv_pip() {
  local -a specs=("$@")
  if [[ $DRY_RUN -eq 1 ]]; then
    if [[ -n "$UV_BIN" ]]; then
      run "$UV_BIN" pip install --python "$(venv_python)" "${specs[@]}"
    else
      run "$(venv_python)" -m pip install "${specs[@]}"
    fi
    return 0
  fi
  if [[ -n "$UV_BIN" ]]; then
    if "$UV_BIN" pip install --python "$(venv_python)" "${specs[@]}"; then
      return 0
    fi
    log_warn "uv pip install failed; falling back to the venv's pip"
  fi
  if [[ -x "$PREFIX/venv/bin/pip" ]]; then
    if "$PREFIX/venv/bin/pip" install "${specs[@]}"; then
      return 0
    fi
    log_warn "venv pip install failed; falling back to python -m pip"
  fi
  "$(venv_python)" -m pip install "${specs[@]}"
}

# PY_* versions from versions.env → requirement list with package names
python_pkg_specs() {
  printf '%s\n' \
    "mcp==${PY_MCP}" \
    "pydantic==${PY_PYDANTIC}" \
    "astropy==${PY_ASTROPY}" \
    "pint-pulsar==${PY_PINT}" \
    "psrqpy==${PY_PSRQPY}" \
    "matplotlib==${PY_MATPLOTLIB}" \
    "numpy==${PY_NUMPY}" \
    "pytest${PY_PYTEST}"
}

stage_python_venv() {
  find_uv

  local py3=""
  if cmd_exists python3.12; then
    py3="$(command -v python3.12)"
  elif cmd_exists python3; then
    py3="$(command -v python3)"
  fi

  if [[ ! -x "$(venv_python)" ]]; then
    local created=0
    if [[ -n "$UV_BIN" ]]; then
      if run "$UV_BIN" venv --python "$VENV_PYTHON" "$PREFIX/venv"; then
        created=1
      else
        log_warn "uv venv failed; falling back to python3 -m venv"
      fi
    fi
    if [[ $created -eq 0 ]] && [[ $DRY_RUN -eq 0 ]]; then
      if [[ -z "$py3" ]]; then
        die "python-venv: no usable uv and no python3 found; cannot create the venv"
      fi
      log_warn "falling back to $py3 -m venv ($("$py3" -V 2>&1))"
      run "$py3" -m venv "$PREFIX/venv"
    fi
  else
    log_info "reusing existing venv: $PREFIX/venv"
  fi

  if [[ $DRY_RUN -eq 0 ]] && [[ ! -x "$(venv_python)" ]]; then
    die "python-venv: venv creation failed ($PREFIX/venv/bin/python does not exist)"
  fi

  local -a specs=()
  mapfile -t specs < <(python_pkg_specs)
  log_info "installing Python deps (exact pins from versions.env): ${specs[*]}"
  venv_pip "${specs[@]}"

  if [[ $DRY_RUN -eq 0 ]]; then
    # uv-created venvs have no pip by default; later PRESTO binding installs need it
    if ! "$(venv_python)" -m ensurepip --upgrade >/dev/null 2>&1; then
      log_info "ensurepip unavailable in the venv (usually uv already provides it; ignoring)"
    fi
    log_ok "venv ready: $("$(venv_python)" -V 2>&1) @ $PREFIX/venv"
  fi
}

# ===========================================================================
# Stage 3: deps (fftw 3.2.2 single-precision + cfitsio 3.49 reentrant; hard deps of psrchive)
#   system libfftw3-dev is double-precision only and libcfitsio-dev is non-reentrant, so both must
#   be built from source: fftw --enable-float (libfftw3f) + cfitsio --enable-reentrant + make shared.
# ===========================================================================
stage_deps() {
  build_fftw
  build_cfitsio
}

build_fftw() {
  local ver="${FFTW_VERSION:-3.2.2}"
  if [[ -e "$PREFIX/lib/libfftw3f.so" || -e "$PREFIX/lib/libfftw3f.a" ]]; then
    log_ok "fftw single-precision library already present, skipping"
    return 0
  fi
  local tgz="$SRC_DIR/fftw-$ver.tar.gz"
  local src="$SRC_DIR/fftw-$ver"
  if [[ ! -f "$tgz" ]] && ! download_with_retry "${FFTW_URL}" "$tgz"; then
    die "deps: fftw $ver download failed ($FFTW_URL)"
  fi
  rm -rf -- "$src"
  archive_install "$tgz" "$src"
  export_build_env
  run_in "$src" ./configure --enable-shared --enable-float --enable-sse \
    --disable-dependency-tracking --prefix="$PREFIX"
  run_in "$src" make clean
  run_in "$src" make -j"$JOBS"
  run_in "$src" make install
}

build_cfitsio() {
  local ver="${CFITSIO_VERSION:-3.49}"
  if [[ -e "$PREFIX/lib/libcfitsio.so" || -e "$PREFIX/lib/libcfitsio.a" ]]; then
    log_ok "cfitsio already present, skipping"
    return 0
  fi
  local tgz="$SRC_DIR/cfitsio-$ver.tar.gz"
  local src="$SRC_DIR/cfitsio-$ver"
  if [[ ! -f "$tgz" ]] && ! download_with_retry "${CFITSIO_URL}" "$tgz"; then
    die "deps: cfitsio $ver download failed ($CFITSIO_URL)"
  fi
  rm -rf -- "$src"
  archive_install "$tgz" "$src"
  export_build_env
  run_in "$src" ./configure --prefix="$PREFIX" --enable-reentrant
  run_in "$src" make clean
  run_in "$src" make -j"$JOBS"
  run_in "$src" make shared
  run_in "$src" make install
}

# ===========================================================================
# Stage 4: tempo (classic tempo, optional; some legacy tools still find ephemerides via $TEMPO)
# ===========================================================================
stage_tempo() {
  local src="$SRC_DIR/tempo"
  local ref="${PIN_TEMPO:-${TEMPO_REF:-HEAD}}"

  if ! ensure_repo "$src" "${TEMPO_URL}" "" "$ref" "tempo"; then
    die "tempo: failed to fetch the source (git.code.sf.net unreachable?)"
  fi
  local resolved
  resolved="$(resolve_repo_ref "$src" "$ref")"
  log_info "tempo @ $resolved"
  if is_moving_ref "$ref" && [[ $DRY_RUN -eq 0 ]]; then
    TEMPO_REF="$resolved"
    write_back_ref TEMPO_REF "$resolved"
  fi

  # upstream has no bootstrap; ./prepare is a csh script that just runs autoreconf --install
  run_in "$src" autoreconf --install
  export_build_env
  run_in "$src" ./configure --prefix="$PREFIX"
  run_in "$src" make -j"$JOBS"
  run_in "$src" make install
}

# ===========================================================================
# Stage 5: tempo2
# ===========================================================================
stage_tempo2() {
  local src="$SRC_DIR/tempo2"
  local ref="${PIN_TEMPO2:-${TEMPO2_REF:-HEAD}}"

  if ! ensure_repo "$src" "${TEMPO2_URL}" "${TEMPO2_MIRROR_URL:-}" "$ref" "tempo2"; then
    die "tempo2: failed to fetch the source (both primary and mirror failed)"
  fi
  local resolved
  resolved="$(resolve_repo_ref "$src" "$ref")"
  log_info "tempo2 @ $resolved"
  if is_moving_ref "$ref" && [[ $DRY_RUN -eq 0 ]]; then
    # write back to versions.env so the next run reproduces the same commit
    TEMPO2_REF="$resolved"
    write_back_ref TEMPO2_REF "$resolved"
  fi

  # T2runtime (clock/ephemerides) goes in before bootstrap (observed working order); --enable-shared
  # builds libtempo2.so, which psrchive/dspsr link dynamically.
  install_t2runtime "$src"

  run_bootstrap "$src"
  export_build_env
  run_in "$src" ./configure --prefix="$PREFIX" --enable-shared
  run_in "$src" make -j"$JOBS"
  run_in "$src" make install
}

# rsync the T2runtime (clock/ephemeris data) from the git repo into $TEMPO2=$PREFIX/tempo2
# (the tempo2 repo ships T2runtime/, no separate download needed)
install_t2runtime() {
  local repo="$1"
  local rt="$PREFIX/tempo2"

  if [[ -d "$rt/clock" && -d "$rt/ephemerides" ]]; then
    log_ok "T2runtime already present ($rt), skipping"
    return 0
  fi
  if [[ $DRY_RUN -eq 1 ]]; then
    plan "rsync $repo/T2runtime/ → $rt (TEMPO2 runtime data)"
    return 0
  fi
  if [[ ! -d "$repo/T2runtime" ]]; then
    log_warn "no T2runtime/ directory in the repo; the tempo2 binary works but lacks clock/ephemeris data.
  Fill it in manually before real timing: TEMPO2=$rt should contain clock/, ephemerides/ etc."
    return 0
  fi
  run mkdir -p "$rt"
  run rsync -a "$repo/T2runtime/" "$rt/"
  if [[ -d "$rt/clock" ]]; then
    log_ok "T2runtime ready: $rt"
  else
    log_warn "no clock/ found after the T2runtime sync; the layout may differ from expected, inspect $rt manually"
  fi
}

# ===========================================================================
# Stage 6: pgplot (optional; needed by plotting CLIs like psrplot/pas, MCP's own plots use matplotlib)
#   Caltech ftp is dead; the source comes from the ASTRON GitLab mirror; patches come from
#   the psrchive source tree's packages/.
# ===========================================================================
stage_pgplot() {
  if [[ -f "$PREFIX/pgplot/grfont.dat" ]]; then
    log_ok "PGPLOT already present ($PREFIX/pgplot), skipping"
    return 0
  fi
  local pkg_dir="$SRC_DIR/psrchive/packages"
  local ver="${PGPLOT_VERSION:-5.2}"
  local tgz="$SRC_DIR/pgplot$ver.tar.gz"
  local src="$SRC_DIR/pgplot"

  # patch files come from the psrchive source tree; the pgplot stage only clones psrchive, never builds it
  if [[ ! -f "$pkg_dir/pgplot.patch" || ! -f "$pkg_dir/makemake.sharedcpg.patch" ]]; then
    if ! ensure_repo "$SRC_DIR/psrchive" "${PSRCHIVE_URL}" "${PSRCHIVE_MIRROR_URL:-}" \
         "${PIN_PSRCHIVE:-${PSRCHIVE_REF:-HEAD}}" "psrchive (for patches)"; then
      log_err "pgplot: cannot get the psrchive source tree (packages/*.patch missing)"
      return 1
    fi
  fi
  if [[ ! -f "$tgz" ]] && ! download_with_retry "${PGPLOT_URL}" "$tgz"; then
    log_err "pgplot: download failed ($PGPLOT_URL)"
    return 1
  fi
  rm -rf -- "$src"
  # the tarball's top level is pgplot/ itself; extract in $SRC_DIR
  run bash -c "cd '$SRC_DIR' && gunzip -c '$tgz' | tar xf -"
  # patch -p0 paths start with pgplot/, so this must run inside $SRC_DIR
  run_in "$SRC_DIR" patch -p0 --forward < "$pkg_dir/pgplot.patch"
  run_in "$SRC_DIR" patch -p0 --forward < "$pkg_dir/makemake.sharedcpg.patch"

  # config tweaks: g77→gfortran; gfortran>=10 needs -fallow-argument-mismatch
  run_in "$src/sys_linux" cp g77_gcc.conf psrchive.conf
  run perl -pi -e 's/FCOMPL="g77"/FCOMPL="gfortran"/' "$src/sys_linux/psrchive.conf"
  run perl -pi -e 's/FFLAGC="-u -Wall -fPIC -O"/FFLAGC="-Wall -fPIC -O -fallow-argument-mismatch -std=legacy"/' "$src/sys_linux/psrchive.conf"
  run perl -pi -e 's/FFLAGD="-fno-backslash"/FFLAGD="-fno-backslash -fallow-argument-mismatch"/' "$src/sys_linux/psrchive.conf"
  run perl -pi -e 's/XINCL="-I\/usr\/X11R6\/include"/XINCL=""/' "$src/sys_linux/psrchive.conf"
  run perl -pi -e 's/LIBS="-L\/usr\/X11R6\/lib -lX11"/LIBS="-lX11"/' "$src/sys_linux/psrchive.conf"
  # shared-lib linking needs X11/PNG/Z added (libs must come after objects; ld defaults to --as-needed)
  run perl -pi -e 's/SHARED_LIB_LIBS=""/SHARED_LIB_LIBS="-lX11 -lpng -lz"/' "$src/sys_linux/psrchive.conf"

  # generate the makefile in $PGPLOT_DIR and build; enable the PNG / PostScript / X-window drivers
  export PGPLOT_DIR="$PREFIX/pgplot"
  run mkdir -p "$PGPLOT_DIR"
  run cp "$src/drivers.list" "$PGPLOT_DIR/drivers.list"
  run perl -pi -e 's/! PNDRIV/  PNDRIV/' "$PGPLOT_DIR/drivers.list"
  run perl -pi -e 's/! PSDRIV/  PSDRIV/' "$PGPLOT_DIR/drivers.list"
  run perl -pi -e 's/! XWDRIV/  XWDRIV/' "$PGPLOT_DIR/drivers.list"
  run_in "$PGPLOT_DIR" "$src/makemake" "$src" linux psrchive
  # use system libpng, dropping the source-tree ./png.h dependency
  run perl -pi -e 's/^pndriv\.o :/# /' "$PGPLOT_DIR/makefile"
  run_in "$PGPLOT_DIR" make
  run_in "$PGPLOT_DIR" make cpg
  run_in "$PGPLOT_DIR" make pgxwin_server
  run_in "$PGPLOT_DIR" make grfont.dat
}

# ===========================================================================
# Stage 4: psrchive
# ===========================================================================
stage_psrchive() {
  local src="$SRC_DIR/psrchive"
  local ref="${PIN_PSRCHIVE:-${PSRCHIVE_REF:-HEAD}}"

  if ! ensure_repo "$src" "${PSRCHIVE_URL}" "${PSRCHIVE_MIRROR_URL:-}" "$ref" "psrchive"; then
    die "psrchive: failed to fetch the source (both primary and mirror failed)"
  fi
  local resolved
  resolved="$(resolve_repo_ref "$src" "$ref")"
  log_info "psrchive @ $resolved"
  if is_moving_ref "$ref" && [[ $DRY_RUN -eq 0 ]]; then
    PSRCHIVE_REF="$resolved"
    write_back_ref PSRCHIVE_REF "$resolved"
  fi

  if [[ ! -x "$PREFIX/bin/tempo2" ]]; then
    log_warn "psrchive: $PREFIX/bin/tempo2 not found; configure may disable tempo2-dependent features"
  fi

  # ASSUMPTION: bootstrap needs autoconf-archive (installed by apt-deps). No --disable-*:
  # without pgplot / boost-python on the system, configure automatically turns off plotting
  # and Python bindings (intended behavior).
  run_bootstrap "$src"
  export_build_env
  run_in "$src" ./configure --prefix="$PREFIX"
  run_in "$src" make -j"$JOBS"
  run_in "$src" make install
}

# ===========================================================================
# Stage 7: dspsr
# ===========================================================================
# Backend module selection: the full list observed to compile, written as a one-line
# backends.list; must run before bootstrap (bootstrap generates Makefile fragments from it)
select_dspsr_backends() {
  local src="$1"
  if [[ $DRY_RUN -eq 1 ]]; then
    plan "writing backend selection: $src/backends.list ← ${DSPSR_BACKENDS[*]}"
    return 0
  fi
  printf '%s\n' "${DSPSR_BACKENDS[*]}" > "$src/backends.list"
  log_info "wrote $src/backends.list (${DSPSR_BACKENDS[*]})"
}

# psrchive >= 2026-05-01 renamed the Signal::* enums to Convention::* and removed several APIs;
# dspsr master has not caught up yet — the patches below are idempotent (skipped once the pattern
# disappears) and come from a real, successful install.
patch_dspsr_for_psrchive() {
  local src="$1" f
  # a) Signal::Basis / enum values / Conventional → Convention::*
  if git -C "$src" grep -q "Signal::Basis" -- '*.h' '*.C' 2>/dev/null; then
    log_info "dspsr patch: Signal::Basis → Convention::Basis"
    git -C "$src" grep -l "Signal::Basis" -- '*.h' '*.C' | while read -r f; do
      sed -i 's/Signal::Basis/Convention::Basis/g' "$src/$f"
    done
  fi
  if git -C "$src" grep -qE "Signal::(Circular|Linear|Elliptical)" -- '*.h' '*.C' 2>/dev/null; then
    log_info "dspsr patch: Signal::{Circular,Linear,Elliptical} → Convention::*"
    git -C "$src" grep -lE "Signal::(Circular|Linear|Elliptical)" -- '*.h' '*.C' | while read -r f; do
      sed -i -E 's/Signal::(Circular|Linear|Elliptical)/Convention::\1/g' "$src/$f"
    done
  fi
  if git -C "$src" grep -q "Signal::Conventional" -- '*.h' '*.C' 2>/dev/null; then
    log_info "dspsr patch: Signal::Conventional → Convention::Conventional"
    git -C "$src" grep -l "Signal::Conventional" -- '*.h' '*.C' | while read -r f; do
      sed -i 's/Signal::Conventional/Convention::Conventional/g' "$src/$f"
    done
  fi

  # b) Calibration::Faraday was removed; the equivalent replacement is MEAL::Faraday
  if git -C "$src" grep -q 'Pulsar/Faraday.h' -- '*.h' 2>/dev/null; then
    log_info "dspsr patch: Calibration::Faraday → MEAL::Faraday"
    sed -i 's|#include "Pulsar/Faraday.h"|#include "MEAL/Faraday.h"|; s|Calibration::Faraday birefringence;|MEAL::Faraday birefringence;|' \
      "$src/Signal/General/dsp/Derotation.h"
    sed -i 's|Calibration::Faraday temp;|MEAL::Faraday temp;|' \
      "$src/Signal/General/Derotation.C"
  fi

  # c) Signal::string2Basis was removed; replaced with a streaming parse via Convention::Basis
  f="$src/Kernel/Classes/ASCIIObservation.C"
  if [[ -f "$f" ]] && grep -q "set_basis( Signal::string2Basis(buffer)" "$f" 2>/dev/null; then
    log_info "dspsr patch: string2Basis → istringstream"
    python3 - "$f" <<'PYDSR1'
import sys
p = sys.argv[1]
s = open(p).read()
old = '    set_basis( Signal::string2Basis(buffer) );'
new = (
    '    // psrchive >= 2026-05-01 removed Signal::string2Basis; stream via Convention::Basis\n'
    '    std::istringstream basis_input (buffer);\n'
    '    Convention::Basis basis;\n'
    '    basis_input >> basis;\n'
    '    set_basis( basis );'
)
assert old in s, 'string2Basis pattern not found'
s = s.replace(old, new)
if '#include <sstream>' not in s:
    s = s.replace('#include <algorithm>', '#include <algorithm>\n#include <sstream>', 1)
open(p, 'w').write(s)
PYDSR1
  fi

  # d) sigproc backend: MEAL classes need namespace qualification + the new Mount/Directional API
  f="$src/Kernel/Formats/sigproc/SigProcObservation.C"
  if [[ -f "$f" ]] && grep -q "^  Horizon horizon;" "$f" 2>/dev/null; then
    log_info "dspsr patch: sigproc MEAL namespaces + new Mount API"
    sed -i 's/^  Horizon horizon;$/  MEAL::Horizon horizon;/; s/^  Directional\* directional = &horizon;$/  MEAL::Directional* directional = \&horizon;/' "$f"
    python3 - "$f" <<'PYDSR2'
import sys
p = sys.argv[1]
s = open(p).read()
old = (
    '    // configure the source coordinates\n'
    '    directional->set_source_coordinates( coordinates );\n'
    '\n'
    '    MJD mjd(tstart);\n'
    '    directional->set_epoch( mjd );'
)
new = (
    '    // configure the source coordinates\n'
    '    // psrchive >= 2026-05: Mount::set_source_coordinates takes two radians args\n'
    '    directional->set_source_coordinates( coordinates.ra().getRadians(),\n'
    '                                         coordinates.dec().getRadians() );\n'
    '\n'
    '    // psrchive >= 2026-05: Directional::set_epoch removed; set local sidereal time\n'
    '    MJD mjd(tstart);\n'
    '    directional->set_local_sidereal_time( mjd.LST( longitude.getDegrees() ) * M_PI / 12.0 );'
)
assert old in s, 'sigproc Mount pattern not found'
open(p, 'w').write(s.replace(old, new))
PYDSR2
  fi
}

# dspsr's bootstrap depends on psrchive's autoconf macros; the installed share/aclocal only
# ships psrchive.m4, so the remaining macros are topped up from the psrchive tree's .build-commons/autoconf/
copy_psrchive_m4() {
  local src="$1"
  local pb_m4="$SRC_DIR/psrchive/.build-commons/autoconf"
  if [[ ! -d "$pb_m4" ]]; then
    log_warn "$pb_m4 not found; if configure reports missing macros, check that the psrchive tree exists"
    return 0
  fi
  local m
  for m in ac_pkg_swig.m4 ac_python_devel.m4 compiler.m4 debug.m4 getopt_long.m4 \
           cfitsio.m4 pgplot.m4 local.m4 package.m4 tempo2.m4; do
    if [[ ! -f "$src/config/$m" && -f "$pb_m4/$m" ]]; then
      log_info "adding missing macro: $m"
      run cp "$pb_m4/$m" "$src/config/"
    fi
  done
}

stage_dspsr() {
  local src="$SRC_DIR/dspsr"
  local ref="${PIN_DSPSR:-${DSPSR_REF:-HEAD}}"

  if ! ensure_repo "$src" "${DSPSR_URL}" "${DSPSR_MIRROR_URL:-}" "$ref" "dspsr"; then
    die "dspsr: failed to fetch the source (both primary and mirror failed)"
  fi
  local resolved
  resolved="$(resolve_repo_ref "$src" "$ref")"
  log_info "dspsr @ $resolved"
  if is_moving_ref "$ref" && [[ $DRY_RUN -eq 0 ]]; then
    DSPSR_REF="$resolved"
    write_back_ref DSPSR_REF "$resolved"
  fi

  if [[ ! -x "$PREFIX/bin/psrchive-config" ]]; then
    log_warn "dspsr: $PREFIX/bin/psrchive-config not found; bootstrap/configure will most likely fail (install psrchive first)"
  fi

  select_dspsr_backends "$src"
  patch_dspsr_for_psrchive "$src"
  copy_psrchive_m4 "$src"

  run_bootstrap "$src"
  export_build_env
  # at configure time TEMPO2 points at the install root (the tempo2 macro looks for headers
  # under $TEMPO2/include); at runtime TEMPO2 still points at the $PREFIX/tempo2 data dir
  # (see export_build_env);
  # the sigproc backend includes MEAL headers by flat name, so CPPFLAGS adds -I$PREFIX/include/MEAL
  local -a cfg=(--prefix="$PREFIX" F77=gfortran CFLAGS=-fPIC FFLAGS=-fPIC
                CPPFLAGS="-I$PREFIX/include -I$PREFIX/include/MEAL"
                TEMPO2="$PREFIX")
  if [[ -d /usr/local/cuda ]]; then
    log_info "found /usr/local/cuda; enabling CUDA support (dspsr -cuda 0 ...)"
    cfg+=(--with-cuda-dir=/usr/local/cuda
          --with-cuda-include-dir=/usr/local/cuda/include
          --with-cuda-lib-dir=/usr/local/cuda/lib64)
  fi
  run_in "$src" ./configure "${cfg[@]}"
  run_in "$src" make -j"$JOBS"
  run_in "$src" make install

  if [[ $DRY_RUN -eq 0 ]]; then
    if ! "$PREFIX/bin/dspsr" --version >/dev/null 2>&1; then
      if ! "$PREFIX/bin/dspsr" -V >/dev/null 2>&1; then
        log_warn "dspsr installed but the --version/-V probe failed; verify manually"
      fi
    fi
  fi
}

# ===========================================================================
# Stage 8: psrcat
#   ATNF's download URL is dead (404 in practice): the tarball must be fetched manually.
#   Search order: --psrcat-tarball → $SRC_DIR/psrcat_pkg*.tar.gz → ~/Downloads → current dir.
#   The build is minimal: compile all .c directly with gcc -fcommon (newer gcc needs -fcommon
#   to merge duplicate tentative global definitions); no tcsh/makeit needed.
# ===========================================================================
stage_psrcat() {
  if [[ -x "$PREFIX/bin/psrcat" && -f "$PREFIX/psrcat/psrcat.db" ]]; then
    log_ok "psrcat already installed, skipping"
    return 0
  fi
  local root="$SRC_DIR/psrcat_tar"

  local pkg="" c
  if [[ -n "$PSRCAT_TARBALL" ]]; then
    [[ -f "$PSRCAT_TARBALL" ]] || die "psrcat: --psrcat-tarball file does not exist: $PSRCAT_TARBALL"
    pkg="$PSRCAT_TARBALL"
  else
    for c in "$SRC_DIR"/psrcat_pkg*.tar.gz "$HOME"/Downloads/psrcat_pkg*.tar.gz ./psrcat_pkg*.tar.gz; do
      if [[ -f "$c" ]]; then pkg="$c"; break; fi
    done
  fi
  if [[ -z "$pkg" ]]; then
    die "psrcat: no psrcat_pkg*.tar.gz found. ATNF's download URL is dead; download the source
  package manually from https://www.atnf.csiro.au/research/pulsar/psrcat/, pass it via
  --psrcat-tarball PATH, or drop it under ~/Downloads / $SRC_DIR and re-run"
  fi
  log_info "psrcat source package: $pkg"

  if [[ $DRY_RUN -eq 1 ]]; then
    plan "extract $pkg → $root; compile directly with gcc -fcommon; install bin/psrcat and psrcat/psrcat.db"
    return 0
  fi

  rm -rf -- "$root"
  archive_install "$pkg" "$root"
  run bash -c "cd '$root' && gcc -fcommon -O2 -o psrcat ./*.c -lm"
  run install -m 0755 "$root/psrcat" "$PREFIX/bin/psrcat"

  local db
  db="$(find "$root" -maxdepth 2 -type f -name psrcat.db 2>/dev/null | head -1)"
  [[ -n "$db" ]] || die "psrcat: no psrcat.db in the source package (check $root)"
  run install -m 0644 "$db" "$PREFIX/psrcat/psrcat.db"

  if ! "$PREFIX/bin/psrcat" -v >/dev/null 2>&1; then
    if ! "$PREFIX/bin/psrcat" -V >/dev/null 2>&1; then
      log_warn "psrcat installed but the -v/-V probe failed; verify manually"
    fi
  fi
}

# ===========================================================================
# Stage 9: presto (optional, --with-presto)
# ===========================================================================
stage_presto() {
  local src="$SRC_DIR/presto"
  local tag="${PIN_PRESTO:-${PRESTO_TAG:-v5.2.0}}"

  if ! ensure_repo "$src" "${PRESTO_URL}" "" "$tag" "presto"; then
    log_err "presto: failed to fetch the source"
    return 1
  fi

  # install meson into the stack's venv (keeps the system clean and never touches conda)
  if ! venv_pip meson; then
    log_err "presto: meson install failed"
    return 1
  fi
  local meson_bin="$PREFIX/venv/bin/meson"
  if [[ ! -x "$meson_bin" ]]; then
    meson_bin="$(command -v meson || printf 'meson')"
  fi

  export_build_env
  # ASSUMPTION: PRESTO v5.x builds with meson; if upstream meson.build still hard-requires PGPLOT
  # (Deepin has no pgplot5 package), setup fails — this stage is treated as optional: emit a
  # WARNING, write $PREFIX/.stack/presto.skipped, and leave the remaining components untouched.
  if ! run_in "$src" "$meson_bin" setup build --prefix="$PREFIX"; then
    log_warn "presto: meson setup failed (the most common cause is an upstream build that hard-requires PGPLOT)"
    log_warn "        skipping PRESTO; tempo2/psrchive/dspsr/psrcat are unaffected. Install pgplot manually and re-run this stage if needed"
    return 1
  fi
  if ! run_in "$src" "$meson_bin" compile -C build; then
    log_err "presto: build failed"
    return 1
  fi
  if ! run_in "$src" "$meson_bin" install -C build; then
    log_err "presto: install failed"
    return 1
  fi

  # Python bindings (non-fatal)
  if ! venv_pip "$src/python"; then
    log_warn "presto: Python bindings install failed (non-fatal); the C tools are in $PREFIX/bin"
  fi
}

# ===========================================================================
# Stage 8: self-check
# ===========================================================================
probe_run() { # probe_run <exe> <args...> → first non-empty stdout line; returns 1 on failure
  local exe="$1"
  shift
  if [[ ! -x "$exe" ]]; then
    return 1
  fi
  local out line
  out="$("$exe" "$@" </dev/null 2>&1 || true)"
  if [[ -z "$out" ]]; then
    return 1
  fi
  line="$(printf '%s\n' "$out" | awk 'NF { sub(/\r$/, ""); print; exit }')"
  if [[ -z "$line" ]]; then
    return 1
  fi
  printf '%s' "$line"
}

# try argument sets in order, return the first successful result (probe_multi <exe> "<args>" ...)
probe_multi() {
  local exe="$1"
  shift
  local spec out
  for spec in "$@"; do
    local -a arr=()
    read -ra arr <<< "$spec" || true
    if out="$(probe_run "$exe" "${arr[@]}")"; then
      printf '%s' "$out"
      return 0
    fi
  done
  return 1
}

sc_record() { # sc_record <name> <0|1> <version> <path>
  SC_NAME+=("$1")
  SC_OK+=("$2")
  SC_VER+=("$3")
  SC_PATH+=("$4")
}

sc_trunc() { # collapse to a single line and cap the length, for tables and JSON
  local s="$1"
  s="${s//$'\n'/ }"
  s="${s//$'\t'/ }"
  printf '%s' "${s:0:60}"
}

sc_component() { # sc_component <name> <exe> <arg-specs...>
  local name="$1" exe="$2"
  shift 2
  local ver=""
  if ver="$(probe_multi "$exe" "$@")"; then
    sc_record "$name" 1 "$(sc_trunc "$ver")" "$exe"
    return 0
  fi
  sc_record "$name" 0 "" "$exe"
  return 1
}

stage_self_check() {
  log_info "probing versions component by component, then writing selfcheck.json and env.sh"

  if [[ $DRY_RUN -eq 1 ]]; then
    plan "probe tempo2 (-v/-V/--version), psrchive-config --version, dspsr --version, psrcat -v/-V, venv python import pint+astropy"
    plan "write $PREFIX/share/pulsar-stack/selfcheck.json"
    write_env_file
    return 0
  fi

  export_build_env

  # --- tempo2 ---
  local t2="$PREFIX/bin/tempo2"
  local last
  if sc_component "tempo2" "$t2" "-v" "-V" "--version"; then
    last=$(( ${#SC_VER[@]} - 1 ))
    log_ok "tempo2: ${SC_VER[$last]}"
  fi
  if [[ ! -d "$PREFIX/tempo2/clock" ]]; then
    log_warn "self-check: no clock/ under TEMPO2=$PREFIX/tempo2; tempo2 runs but lacks clock/ephemeris data"
  fi

  # --- psrchive / dspsr / psrcat ---
  sc_component "psrchive" "$PREFIX/bin/psrchive-config" "--version" "-V" \
    || log_warn "psrchive probe failed"
  sc_component "dspsr" "$PREFIX/bin/dspsr" "--version" "-V" \
    || log_warn "dspsr probe failed"
  sc_component "psrcat" "$PREFIX/bin/psrcat" "-v" "-V" "--version" \
    || log_warn "psrcat probe failed"

  # --- Python stack (venv) ---
  local vpy
  vpy="$(venv_python)"
  local pyver=""
  if [[ -x "$vpy" ]]; then
    if pyver="$("$vpy" -c 'import astropy, pint; print("astropy %s / pint-pulsar %s" % (astropy.__version__, pint.__version__))' </dev/null 2>&1)"; then
      sc_record "python(pint+astropy)" 1 "$(sc_trunc "$pyver")" "$vpy"
    else
      sc_record "python(pint+astropy)" 0 "" "$vpy"
      log_warn "venv python failed to import pint/astropy: $(sc_trunc "$pyver")"
    fi
  else
    sc_record "python(pint+astropy)" 0 "" "$vpy"
    log_warn "venv missing: $vpy"
  fi

  # --- PRESTO (optional) ---
  if [[ $WITH_PRESTO -eq 1 ]]; then
    if [[ -f "$STACK_DIR/presto.skipped" ]]; then
      sc_record "presto" 0 "skipped (see presto.skipped)" "$STACK_DIR/presto.skipped"
    elif pyver="$("$vpy" -c 'import presto; print(getattr(presto, "__version__", "unknown"))' </dev/null 2>&1)"; then
      sc_record "presto" 1 "$(sc_trunc "$pyver")" "$vpy"
    elif [[ -x "$PREFIX/bin/prepfold" ]]; then
      sc_record "presto" 1 "(C tools installed, python bindings unverified)" "$PREFIX/bin/prepfold"
    else
      sc_record "presto" 0 "" "$PREFIX/bin"
      log_warn "presto probe failed (neither python bindings nor prepfold found)"
    fi
  fi

  write_selfcheck_json
  write_env_file
}

write_selfcheck_json() {
  local outdir="$PREFIX/share/pulsar-stack"
  local out="$outdir/selfcheck.json"
  local tsv py="" i
  mkdir -p "$outdir"
  tsv="$(mktemp "$outdir/.selfcheck.XXXXXX")"
  TMP_PATH="$tsv"
  for i in "${!SC_NAME[@]}"; do
    printf '%s\t%s\t%s\t%s\n' "${SC_NAME[$i]}" "${SC_OK[$i]}" "${SC_VER[$i]}" "${SC_PATH[$i]}" >> "$tsv"
  done
  if [[ -x "$(venv_python)" ]]; then
    py="$(venv_python)"
  elif cmd_exists python3; then
    py="$(command -v python3)"
  fi
  if [[ -z "$py" ]]; then
    log_warn "no python3 found; cannot write selfcheck.json (the self-check data is still in the table above)"
    rm -f -- "$tsv"
    TMP_PATH=""
    return 0
  fi
  if ! "$py" - "$tsv" "$out" "$PREFIX" <<'PY'
import datetime
import json
import sys

tsv_path, out_path, prefix = sys.argv[1], sys.argv[2], sys.argv[3]
checked_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
components = []
with open(tsv_path, encoding="utf-8") as fh:
    for raw in fh:
        raw = raw.rstrip("\n")
        if not raw:
            continue
        fields = raw.split("\t")
        if len(fields) != 4:
            continue
        name, avail, version, path = fields
        components.append(
            {
                "component": name,
                "available": avail == "1",
                "version": version or None,
                "path": path or None,
                "checked_at": checked_at,
            }
        )
report = {
    "generator": "install_pulsar_stack.sh",
    "prefix": prefix,
    "checked_at": checked_at,
    "components": components,
}
with open(out_path, "w", encoding="utf-8") as fh:
    json.dump(report, fh, ensure_ascii=False, indent=2)
    fh.write("\n")
PY
  then
    log_warn "failed to write selfcheck.json ($out)"
  fi
  rm -f -- "$tsv"
  if [[ "${TMP_PATH:-}" == "$tsv" ]]; then
    TMP_PATH=""
  fi
  log_ok "self-check report: $out"
}

env_sh_body() {
  cat <<EOF
# Generated by ${SCRIPT_NAME} at $(now_utc)
# Usage: source ${ENV_FILE}
# NOTE: the MCP server's config.toml [backends] should use absolute paths directly and not
# depend on this file (see docs/DESIGN.md §8.1).
export PULSAR_PREFIX="${PREFIX}"
export PATH="\$PULSAR_PREFIX/bin:\$PATH"
export LD_LIBRARY_PATH="\$PULSAR_PREFIX/lib\${LD_LIBRARY_PATH:+:\$LD_LIBRARY_PATH}"
export PKG_CONFIG_PATH="\$PULSAR_PREFIX/lib/pkgconfig\${PKG_CONFIG_PATH:+:\$PKG_CONFIG_PATH}"
export PSRSOFT_DIR="\$PULSAR_PREFIX"
export TEMPO2="\$PULSAR_PREFIX/tempo2"
export TEMPO="\$PULSAR_PREFIX/tempo"
export PSRCAT_FILE="\$PULSAR_PREFIX/psrcat/psrcat.db"
export PGPLOT_DIR="\$PULSAR_PREFIX/pgplot"
export PGPLOT_DEV=/xwindow
# Activate the Python environment as needed: source "\$PULSAR_PREFIX/venv/bin/activate"
EOF
}

write_env_file() {
  local target="${ENV_FILE:-$PREFIX/env.sh}"
  if [[ $DRY_RUN -eq 1 ]]; then
    plan "writing env file $target:"
    env_sh_body | sed 's/^/    | /' >&2
    return 0
  fi
  mkdir -p "$(dirname -- "$target")"
  env_sh_body > "$target"
  chmod 0644 "$target"
  log_ok "env file: $target (ready after sourcing)"
}

# ===========================================================================
# Stage framework: marker + logs + error isolation
# ===========================================================================
stage_mode() { # required | optional (presto/tempo/pgplot failures only warn)
  if [[ "$1" == "presto" || "$1" == "tempo" || "$1" == "pgplot" ]]; then
    printf '%s' "optional"
  else
    printf '%s' "required"
  fi
}

stage_func_name() { printf '%s' "stage_${1//-/_}"; }

stage_selected() { # stage_selected <name>
  local name="$1" s
  if [[ "$name" == "presto" ]] && [[ $WITH_PRESTO -ne 1 ]]; then
    return 1
  fi
  if (( ${#ONLY_LIST[@]} > 0 )); then
    for s in "${ONLY_LIST[@]}"; do
      if [[ "$s" == "$name" ]]; then
        return 0
      fi
    done
    return 1
  fi
  for s in "${WITHOUT_LIST[@]}"; do
    if [[ "$s" == "$name" ]]; then
      return 1
    fi
  done
  return 0
}

# stage "input fingerprint": URL + config flags + relevant env (the ref is not hashed;
# it is stored separately in the marker)
stage_spec() {
  local name="$1"
  case "$name" in
    preflight)
      printf 'preflight|v1|prefix=%s' "$PREFIX"
      ;;
    apt-deps)
      printf 'apt-deps|v1|%s' "${APT_PKGS[*]}"
      ;;
    python-venv)
      printf 'python-venv|v1|py=%s|%s' "$VENV_PYTHON" "$(python_pkg_specs | tr '\n' ' ')"
      ;;
    deps)
      printf 'deps|v1|fftw=%s|cfitsio=%s' "${FFTW_VERSION:-}" "${CFITSIO_VERSION:-}"
      ;;
    tempo)
      printf 'tempo|v1|%s' "${TEMPO_URL:-}"
      ;;
    tempo2)
      printf 'tempo2|v2|%s|%s|runtime=%s|flags=--prefix=%s --enable-shared' \
        "${TEMPO2_URL}" "${TEMPO2_MIRROR_URL:-}" "$PREFIX/tempo2" "$PREFIX"
      ;;
    pgplot)
      printf 'pgplot|v1|%s' "${PGPLOT_URL:-}"
      ;;
    psrchive)
      printf 'psrchive|v1|%s|%s|flags=--prefix=%s' \
        "${PSRCHIVE_URL}" "${PSRCHIVE_MIRROR_URL:-}" "$PREFIX"
      ;;
    dspsr)
      printf 'dspsr|v2|%s|%s|backends=%s|flags=--prefix=%s|cuda=%s' \
        "${DSPSR_URL}" "${DSPSR_MIRROR_URL:-}" \
        "${DSPSR_BACKENDS[*]}" "$PREFIX" "$( [[ -d /usr/local/cuda ]] && printf yes || printf no )"
      ;;
    psrcat)
      printf 'psrcat|v2|tarball=%s' "${PSRCAT_TARBALL:-<auto>}"
      ;;
    presto)
      printf 'presto|v1|%s|tag=%s|flags=--prefix=%s' \
        "${PRESTO_URL}" "${PRESTO_TAG:-}" "$PREFIX"
      ;;
    self-check)
      printf 'self-check|v1|env=%s' "${ENV_FILE:-$PREFIX/env.sh}"
      ;;
    *)
      printf '%s|v1' "$name"
      ;;
  esac
}

spec_hash() { printf '%s' "$1" | sha256sum | awk '{print $1}'; }

stage_repo_dir() { # returns an empty string for stages without a repo
  case "$1" in
    tempo)    printf '%s' "$SRC_DIR/tempo" ;;
    tempo2)   printf '%s' "$SRC_DIR/tempo2" ;;
    psrchive) printf '%s' "$SRC_DIR/psrchive" ;;
    dspsr)    printf '%s' "$SRC_DIR/dspsr" ;;
    presto)   printf '%s' "$SRC_DIR/presto" ;;
    *)        printf '%s' "" ;;
  esac
}

stage_pinned_ref() {
  case "$1" in
    tempo)    printf '%s' "${PIN_TEMPO:-${TEMPO_REF:-HEAD}}" ;;
    tempo2)   printf '%s' "${PIN_TEMPO2:-${TEMPO2_REF:-HEAD}}" ;;
    psrchive) printf '%s' "${PIN_PSRCHIVE:-${PSRCHIVE_REF:-HEAD}}" ;;
    dspsr)    printf '%s' "${PIN_DSPSR:-${DSPSR_REF:-HEAD}}" ;;
    presto)   printf '%s' "${PIN_PRESTO:-${PRESTO_TAG:-v5.2.0}}" ;;
    *)        printf '%s' "-" ;;
  esac
}

# the ref a stage should currently be at (resolved to a commit if the repo exists, else the raw pin)
stage_current_ref() {
  local name="$1" repo ref
  repo="$(stage_repo_dir "$name")"
  if [[ -z "$repo" ]]; then
    printf '%s' "-"
    return 0
  fi
  ref="$(stage_pinned_ref "$name")"
  if [[ -d "$repo/.git" ]]; then
    resolve_repo_ref "$repo" "$ref"
  else
    printf '%s' "$ref"
  fi
}

marker_path() { printf '%s/%s.done' "$STACK_DIR" "$1"; }

write_marker() { # write_marker <stage> <spec-hash> <ref>
  local marker
  marker="$(marker_path "$1")"
  mkdir -p "$STACK_DIR"
  {
    printf '# %s stage marker\n' "$SCRIPT_NAME"
    printf 'stage=%s\n' "$1"
    printf 'spec=%s\n' "$2"
    printf 'ref=%s\n' "$3"
    printf 'created=%s\n' "$(now_utc)"
  } > "$marker"
}

marker_up_to_date() { # marker_up_to_date <stage> <spec-hash>
  local name="$1" hash="$2"
  local marker
  marker="$(marker_path "$name")"
  if [[ ! -f "$marker" ]]; then
    return 1
  fi
  local mhash="" mref="" line
  while IFS= read -r line; do
    case "$line" in
      spec=*) mhash="${line#spec=}" ;;
      ref=*)  mref="${line#ref=}" ;;
    esac
  done < "$marker"
  if [[ -z "$mhash" ]] || [[ "$mhash" != "$hash" ]]; then
    return 1
  fi
  local want
  want="$(stage_current_ref "$name")"
  if [[ -z "$want" ]] || [[ "$want" != "$mref" ]]; then
    return 1
  fi
  return 0
}

stage_fail() { # stage_fail <stage> <rc>
  local name="$1" rc="$2"
  log_err "stage $name failed (exit=$rc)"
  if [[ -n "${STAGE_LOG:-}" && -f "$STAGE_LOG" ]]; then
    log_err "full log: $STAGE_LOG"
    log_err "---- last 20 lines of the log ----"
    tail -n 20 "$STAGE_LOG" >&2 || true
    log_err "------------------------"
  else
    log_err "(no log file for this stage; likely a dry-run or a very early failure)"
  fi
  exit 1
}

run_stage() { # run_stage <stage>
  local name="$1"
  local mode func hash rc
  if ! stage_selected "$name"; then
    log_info "==> skipping stage $name (not selected)"
    record_summary "$name" "SKIP" "not selected"
    return 0
  fi
  mode="$(stage_mode "$name")"
  func="$(stage_func_name "$name")"
  if ! declare -F "$func" >/dev/null 2>&1; then
    die "internal error: stage function $func not found"
  fi

  hash="$(spec_hash "$(stage_spec "$name")")"
  if marker_up_to_date "$name" "$hash"; then
    log_ok "==> stage $name already done and inputs unchanged, skipping (marker: $(marker_path "$name"))"
    record_summary "$name" "SKIP" "marker hit"
    return 0
  fi

  # fallback directories (so single-stage --only runs or a skipped preflight still install safely)
  if [[ $DRY_RUN -eq 0 ]]; then
    mkdir -p "$PREFIX/bin" "$PREFIX/lib" "$PREFIX/share" "$STACK_DIR" "$LOG_DIR" \
      "$PREFIX/catalog" "$PREFIX/runtime" "$SRC_DIR"
  fi

  STAGE_CURRENT="$name"
  STAGE_LOG=""
  rc=0
  # failure-capture block: the outer shell temporarily set +e (otherwise a pipeline failure
  # would exit the script right away), and the subshell explicitly restores -Eeuo pipefail —
  # note: if the subshell sits in an if/|| "errexit ignored" context, the ignored state
  # propagates into the subshell and mid-stage failures no longer abort (observed), so this
  # must be written as "outer -e off + subshell explicitly -e on".
  ERR_CAPTURE=1
  set +e
  if [[ $DRY_RUN -eq 1 ]]; then
    log_info "==> [$name] (dry-run) will run:"
    ( ERR_CAPTURE=0; set -Eeuo pipefail; "$func" )
    rc="${PIPESTATUS[0]}"
  else
    STAGE_LOG="$LOG_DIR/$(date -u '+%Y%m%dT%H%M%SZ')_${name}.log"
    log_info "==> [$name] starting (mode=$mode, log: $STAGE_LOG)"
    # the subshell isolates stage vars and failures; tee writes to disk and screen at once
    ( ERR_CAPTURE=0; set -Eeuo pipefail; "$func" ) 2>&1 | tee "$STAGE_LOG"
    rc="${PIPESTATUS[0]}"
  fi
  set -e
  ERR_CAPTURE=0
  STAGE_CURRENT=""

  if (( rc != 0 )); then
    if [[ "$mode" == "optional" ]]; then
      log_warn "optional stage $name failed (exit=$rc); writing ${STACK_DIR}/${name}.skipped and continuing"
      if [[ $DRY_RUN -eq 0 ]]; then
        {
          printf 'stage=%s\n' "$name"
          printf 'failed_at=%s\n' "$(now_utc)"
          printf 'exit=%s\n' "$rc"
          printf 'log=%s\n' "$STAGE_LOG"
          printf 'note=optional stage; its failure does not affect the other components; delete this file and re-run to try again\n'
        } > "$STACK_DIR/${name}.skipped"
      fi
      record_summary "$name" "FAIL*" "optional stage skipped, see log"
      return 0
    fi
    stage_fail "$name" "$rc"
  fi

  if [[ $DRY_RUN -eq 1 ]]; then
    record_summary "$name" "PLAN" "dry-run"
  else
    write_marker "$name" "$hash" "$(stage_current_ref "$name")"
    record_summary "$name" "OK" "log: $STAGE_LOG"
    log_ok "==> [$name] done"
  fi
  return 0
}

record_summary() { # record_summary <stage> <status> <note>
  SUM_STAGE+=("$1")
  SUM_STATUS+=("$2")
  SUM_NOTE+=("$3")
}

# ===========================================================================
# Summary output
# ===========================================================================
print_summary() {
  local i name status note width_s=12
  printf '\n' >&2
  _log "SUMMARY" "$C_BOLD" "================ Install Summary ================"
  for i in "${!SUM_STAGE[@]}"; do
    name="${SUM_STAGE[$i]}"
    if (( ${#name} > width_s )); then
      width_s=${#name}
    fi
  done
  printf '%-*s  %-6s  %s\n' "$width_s" "Stage" "Status" "Note" >&2
  for i in "${!SUM_STAGE[@]}"; do
    name="${SUM_STAGE[$i]}"
    status="${SUM_STATUS[$i]}"
    note="${SUM_NOTE[$i]}"
    case "$status" in
      OK)    printf '%s' "$C_GREEN" >&2 ;;
      SKIP)  printf '%s' "$C_YELLOW" >&2 ;;
      FAIL*) printf '%s' "$C_RED" >&2 ;;
      *)     printf '%s' "$C_CYAN" >&2 ;;
    esac
    printf '%-*s' "$width_s" "$name" >&2
    printf '%s  %-6s  %s%s\n' "$C_RESET" "$status" "$note" "$C_RESET" >&2
  done

  if (( ${#SC_NAME[@]} > 0 )); then
    printf '\n' >&2
    _log "SUMMARY" "$C_BOLD" "Component self-check"
    printf '%-22s %-6s %-42s %s\n' "Component" "Avail" "Version" "Path" >&2
    for i in "${!SC_NAME[@]}"; do
      local ok="no"
      if [[ "${SC_OK[$i]}" == "1" ]]; then
        ok="yes"
      fi
      printf '%-22s %-6s %-42s %s\n' "${SC_NAME[$i]}" "$ok" "${SC_VER[$i]}" "${SC_PATH[$i]}" >&2
    done
  fi

  printf '\n' >&2
  _log "NOTE" "$C_BOLD" "The MCP server's config.toml [backends] should use absolute paths, not this env.sh (see docs/DESIGN.md §8.1)"
  _log "NOTE" "$C_BOLD" "source ${ENV_FILE:-$PREFIX/env.sh} before use, or just source scripts/dev_env.sh"
}

# ===========================================================================
# CLI
# ===========================================================================
usage() {
  cat <<'USAGE'
install_pulsar_stack.sh — one-shot source-build installer for the pulsar astronomy stack

Usage:
  bash scripts/install_pulsar_stack.sh [options]

Stage order:
  preflight → apt-deps → python-venv → deps → [tempo] → tempo2 → [pgplot]
            → psrchive → dspsr → psrcat → [presto] → self-check
  Each stage writes <prefix>/.stack/<stage>.done on success (recording the ref + spec hash);
  re-runs with unchanged inputs are skipped automatically.

Options:
  --prefix DIR          install prefix (default ~/Pulsar; precedence PULSAR_PREFIX > PSRSOFT_DIR)
  --jobs N              parallel build jobs (default nproc)
  --with-presto         also install PRESTO (optional stage; failure only warns, never aborts)
  --without STAGE       skip a stage; repeatable; STAGE ∈
                        preflight|apt-deps|python-venv|deps|tempo|tempo2|pgplot|psrchive|dspsr|psrcat|presto|self-check
  --only STAGES         run only these stages (comma-separated, e.g. "tempo2,psrchive"; naming presto enables it)
  --pin-tempo2 REF      override versions.env's TEMPO2_REF (commit/tag/branch)
  --pin-psrchive REF    override PSRCHIVE_REF
  --pin-dspsr REF       override DSPSR_REF
  --pin-presto REF      override PRESTO_TAG
  --psrcat-tarball PATH local psrcat source package (ATNF's download URL is dead; fetch
                        psrcat_pkg*.tar.gz manually; without it, search --src-dir, ~/Downloads, cwd)
  --src-dir DIR         source directory (default <prefix>/src)
  --env-file PATH       where to write env.sh (default <prefix>/env.sh)
  --dry-run             print the full plan and every command; zero side effects (no dirs, no apt, no network)
  --yes, -y             unattended: skip the one-time apt confirmation (required for non-interactive runs)
  --no-color            disable colored output (auto-disabled when not a tty)
  -h, --help            show this help

Examples:
  bash scripts/install_pulsar_stack.sh                 # full stack (no PRESTO)
  bash scripts/install_pulsar_stack.sh --with-presto   # include PRESTO
  bash scripts/install_pulsar_stack.sh --dry-run       # see what it would do first
  bash scripts/install_pulsar_stack.sh --only tempo2 --pin-tempo2 <commit>

Notes:
  * Versions / pins / download URLs live in scripts/versions.env; when a pin is HEAD, the
    first run resolves it to a concrete commit and writes it back (with a `# resolved <date>` comment).
  * No Docker, no conda-forge; tempo and pgplot are optional stages (failures only warn).
  * psrcat's ATNF download URL is dead: download the psrcat_pkg tarball manually from the
    ATNF website and pass it via --psrcat-tarball (or drop it under ~/Downloads / --src-dir).
  * Logs: <prefix>/logs/<UTC timestamp>_<stage>.log;
    self-check report: <prefix>/share/pulsar-stack/selfcheck.json.
USAGE
}

parse_args() {
  while (( $# > 0 )); do
    case "$1" in
      --prefix)
        [[ $# -ge 2 ]] || die "--prefix requires a directory argument"
        PREFIX="$2"; shift 2 ;;
      --prefix=*)
        PREFIX="${1#*=}"; shift ;;
      --jobs)
        [[ $# -ge 2 ]] || die "--jobs requires a numeric argument"
        JOBS="$2"; shift 2 ;;
      --jobs=*)
        JOBS="${1#*=}"; shift ;;
      --psrcat-tarball)
        [[ $# -ge 2 ]] || die "--psrcat-tarball requires a file argument"
        PSRCAT_TARBALL="$2"; shift 2 ;;
      --psrcat-tarball=*)
        PSRCAT_TARBALL="${1#*=}"; shift ;;
      --src-dir)
        [[ $# -ge 2 ]] || die "--src-dir requires a directory argument"
        SRC_DIR="$2"; shift 2 ;;
      --src-dir=*)
        SRC_DIR="${1#*=}"; shift ;;
      --env-file)
        [[ $# -ge 2 ]] || die "--env-file requires a path argument"
        ENV_FILE="$2"; shift 2 ;;
      --env-file=*)
        ENV_FILE="${1#*=}"; shift ;;
      --with-presto)
        WITH_PRESTO=1; shift ;;
      --without)
        [[ $# -ge 2 ]] || die "--without requires a stage name"
        WITHOUT_LIST+=("$2"); shift 2 ;;
      --without=*)
        WITHOUT_LIST+=("${1#*=}"); shift ;;
      --only)
        [[ $# -ge 2 ]] || die "--only requires a stage list"
        add_only_list "$2"; shift 2 ;;
      --only=*)
        add_only_list "${1#*=}"; shift ;;
      --pin-tempo2)
        [[ $# -ge 2 ]] || die "--pin-tempo2 requires a ref"
        PIN_TEMPO2="$2"; shift 2 ;;
      --pin-tempo2=*)
        PIN_TEMPO2="${1#*=}"; shift ;;
      --pin-psrchive)
        [[ $# -ge 2 ]] || die "--pin-psrchive requires a ref"
        PIN_PSRCHIVE="$2"; shift 2 ;;
      --pin-psrchive=*)
        PIN_PSRCHIVE="${1#*=}"; shift ;;
      --pin-dspsr)
        [[ $# -ge 2 ]] || die "--pin-dspsr requires a ref"
        PIN_DSPSR="$2"; shift 2 ;;
      --pin-dspsr=*)
        PIN_DSPSR="${1#*=}"; shift ;;
      --pin-presto)
        [[ $# -ge 2 ]] || die "--pin-presto requires a ref"
        PIN_PRESTO="$2"; shift 2 ;;
      --pin-presto=*)
        PIN_PRESTO="${1#*=}"; shift ;;
      --dry-run)
        DRY_RUN=1; shift ;;
      --yes|-y)
        ASSUME_YES=1; shift ;;
      --no-color)
        OPT_NO_COLOR=1; shift ;;
      -h|--help|help)
        usage
        exit 0 ;;
      --)
        shift
        break ;;
      -*)
        die "unknown option: $1 (see --help for usage)" ;;
      *)
        die "unexpected positional argument: $1 (this script takes no positional arguments; see --help)" ;;
    esac
  done
}

stage_is_known() {
  local s
  for s in "${ALL_STAGES[@]}"; do
    if [[ "$s" == "$1" ]]; then
      return 0
    fi
  done
  return 1
}

# --only accepts comma-separated lists; expand at parse time so stage_selected sees a clean list
add_only_list() {
  local -a parts=()
  local part
  IFS=',' read -ra parts <<< "$1" || true
  for part in "${parts[@]}"; do
    if [[ -n "$part" ]]; then
      ONLY_LIST+=("$part")
    fi
  done
  if [[ -n "$1" ]] && (( ${#ONLY_LIST[@]} == 0 )); then
    ONLY_EMPTY_GIVEN=1
  fi
}

validate_args() {
  local s
  if [[ ! "$JOBS" =~ ^[1-9][0-9]*$ ]]; then
    die "--jobs must be a positive integer (got: $JOBS)"
  fi
  for s in "${WITHOUT_LIST[@]}"; do
    if ! stage_is_known "$s"; then
      die "--without: invalid stage name: $s (valid: ${ALL_STAGES[*]})"
    fi
  done
  if (( ${#ONLY_LIST[@]} == 0 )) && [[ -n "${ONLY_EMPTY_GIVEN:-}" ]]; then
    die "--only: stage list is empty"
  fi
  for s in "${ONLY_LIST[@]}"; do
    if ! stage_is_known "$s"; then
      die "--only: invalid stage name: $s (valid: ${ALL_STAGES[*]})"
    fi
    if [[ "$s" == "presto" ]]; then
      # naming presto explicitly enables the optional stage
      WITH_PRESTO=1
    fi
  done
}

load_versions() {
  local v
  if [[ ! -f "$VERSIONS_ENV" ]]; then
    die "cannot find $VERSIONS_ENV (the script must live next to versions.env)"
  fi
  # shellcheck disable=SC1090
  source "$VERSIONS_ENV"
  # key variables must be non-empty, otherwise the requirements get built into bad strings
  for v in TEMPO2_URL PSRCHIVE_URL DSPSR_URL PRESTO_URL FFTW_URL CFITSIO_URL \
           PGPLOT_URL TEMPO_URL PY_MCP PY_PYDANTIC PY_ASTROPY PY_PINT PY_PSRQPY PY_MATPLOTLIB PY_NUMPY PY_PYTEST; do
    if [[ -z "${!v:-}" ]]; then
      die "versions.env missing or empty: $v"
    fi
  done
}

# ===========================================================================
# main
# ===========================================================================
main() {
  parse_args "$@"
  setup_colors
  load_versions
  validate_args

  # normalize paths
  PREFIX="$(abspath "$PREFIX")"
  if [[ -z "$SRC_DIR" ]]; then
    SRC_DIR="$PREFIX/src"
  fi
  SRC_DIR="$(abspath "$SRC_DIR")"
  if [[ -z "$ENV_FILE" ]]; then
    ENV_FILE="$PREFIX/env.sh"
  fi
  ENV_FILE="$(abspath "$ENV_FILE")"
  LOG_DIR="$PREFIX/logs"
  STACK_DIR="$PREFIX/.stack"

  # --- environment that must take effect early (required even with --without preflight) ---
  # LC_ALL=C and a conda-free PATH are prerequisites for every configure to work correctly,
  # so they live in main, not only in the preflight stage.
  export LC_ALL=C
  export LANG=C
  apply_clean_path

  log_info "${SCRIPT_NAME}: prefix=$PREFIX jobs=$JOBS src=$SRC_DIR env=$ENV_FILE"
  if (( ${#ONLY_LIST[@]} > 0 )); then
    log_info "running only stages: ${ONLY_LIST[*]}"
  fi
  if (( ${#WITHOUT_LIST[@]} > 0 )); then
    log_info "skipping stages: ${WITHOUT_LIST[*]}"
  fi
  if [[ $WITH_PRESTO -eq 1 ]]; then
    log_info "PRESTO: enabled (optional stage; failure only warns)"
  fi
  if [[ $DRY_RUN -eq 1 ]]; then
    log_warn "DRY-RUN: printing the plan only; no directories created, no apt, no network"
  fi

  # one-time apt confirmation (asked in the main process, so it stays out of stage logs)
  if stage_selected "apt-deps" && [[ $DRY_RUN -eq 0 ]] && [[ $ASSUME_YES -ne 1 ]]; then
    confirm_apt
  fi

  if (( ${#ONLY_LIST[@]} > 0 )) && ! stage_selected "preflight"; then
    log_warn "--only mode: preflight not selected; make sure the $PREFIX directory layout exists (otherwise add preflight)"
  fi

  local s
  for s in "${ALL_STAGES[@]}"; do
    run_stage "$s"
  done

  # ensure env.sh exists even when self-check was skipped/not selected (idempotent)
  write_env_file

  print_summary
}

# enter main only when executed directly; no-op when sourced (so its functions can be reused)
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
