#!/usr/bin/env bash
#
# install_pulsar_stack.sh — 脉冲星天文软件栈一键源码编译安装器
#
# 目标平台 : Debian 系 Linux(Deepin 25 / Debian 12+),gcc 12+,20 核 / 60GB 内存量级
# 安装内容 : fftw/cfitsio + tempo(可选)+ tempo2 + pgplot(可选)+ psrchive
#            + dspsr + psrcat(+ 可选 PRESTO)+ 隔离 Python venv
# 来源考证 : 全部 URL / configure 旗标均按 2026-10 真实装机成功的流程校准
#            (tempo2=github ipta、dspsr=sourceforge、psrcat=本地 tarball +
#            gcc -fcommon、pgplot=ASTRON 镜像;ATNF/Caltech 官方地址已死)
# 硬性约束 : 全源码编译进单一前缀(默认 ~/Pulsar,尊重已导出的 PSRSOFT_DIR);
#            不用 Docker;不用 conda-forge;MCP 工具的绘图由 Python 端
#            matplotlib 负责,PGPLOT 仅为 psrplot 等 CLI 伙伴工具服务;
#            ~/miniconda3、~/anaconda3、~/mambaforge 会从 PATH 中剔除,
#            避免 conda 的 python / libstdc++ 污染 configure。
#
# 用法     : bash scripts/install_pulsar_stack.sh --help
# 版本/pin : 统一维护在同目录 versions.env
#
# 阶段(顺序固定;每阶段成功后写 $PREFIX/.stack/<stage>.done,输入未变则重跑自动跳过):
#   preflight → apt-deps → python-venv → deps(fftw/cfitsio) → [tempo] → tempo2
#            → [pgplot] → psrchive → dspsr → psrcat → [presto] → self-check

set -Eeuo pipefail

# ===========================================================================
# 常量与全局状态
# ===========================================================================
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
VERSIONS_ENV="$SCRIPT_DIR/versions.env"
SCRIPT_NAME="install_pulsar_stack.sh"

# 注意:刻意不用 readonly —— 重复 source 本脚本(如测试 harness)不会报“只读变量”错
ALL_STAGES=(
  preflight apt-deps python-venv deps tempo tempo2 pgplot psrchive dspsr psrcat presto self-check
)
# apt-deps 要装的系统包(刻意不含 boost-python / vtk;fftw/cfitsio/pgplot 走源码
# 阶段 deps/pgplot —— 系统版 cfitsio 非重入、fftw 无单精度,psrchive/dspsr 需要的是
# 前缀内自建的 3.49/3.2.2)
APT_PKGS=(
  build-essential gfortran git wget curl ca-certificates
  autoconf automake autoconf-archive libtool pkg-config m4 patch perl rsync
  python3-dev libgsl-dev libglib2.0-dev
  libpng-dev libx11-dev unzip xz-utils ninja-build
)
DISK_MIN_GB=20
DOWNLOAD_DELAYS=(2 5 10)

# --- CLI 默认值 -----------------------------------------------------------
PREFIX="${PULSAR_PREFIX:-${PSRSOFT_DIR:-$HOME/Pulsar}}"
JOBS="$(nproc 2>/dev/null || printf '4')"
WITH_PRESTO=0
DRY_RUN=0
ASSUME_YES=0
OPT_NO_COLOR=0
SRC_DIR=""          # 默认 $PREFIX/src,main() 里归一化
ENV_FILE=""         # 默认 $PREFIX/env.sh,main() 里归一化
WITHOUT_LIST=()
ONLY_LIST=()
PIN_TEMPO2=""
PIN_PSRCHIVE=""
PIN_DSPSR=""
PIN_PRESTO=""
PIN_TEMPO=""
PSRCAT_TARBALL=""    # psrcat 本地 tarball;空 = 自动搜索(ATNF 下载地址已失效)

# --- 运行期全局 ------------------------------------------------------------
LOG_DIR="$PREFIX/logs"
STACK_DIR="$PREFIX/.stack"
STAGE_CURRENT=""    # 正在执行的阶段名(空 = 不在阶段内)
STAGE_LOG=""        # 当前阶段日志路径
ERR_CAPTURE=0       # 1 = 处于 run_stage 的失败捕获区(外层 trap 静默)
UV_BIN=""           # 选定的 uv(优先非 conda 路径)
TMP_PATH=""         # 需要在 EXIT 时清理的临时路径

declare -a SUM_STAGE=() SUM_STATUS=() SUM_NOTE=()
declare -a SC_NAME=() SC_OK=() SC_VER=() SC_PATH=()
# ASSUMPTION: 现代 dspsr 树在顶层用 backends.list 列出要编译的后端模块;
# 老树用 config/modules 文件。两处都写(见 select_dspsr_backends)。
DSPSR_BACKENDS=(apsr asp bcpm bpsr caspsr cpsr2 cpsr dummy fits gmrt guppi kat
                lbadr64 lbadr lump lwa mark4 mark5 maxim mwa pdev pmdaq s2 sigproc
                spda1k spigot vdif uwb)

# ===========================================================================
# 颜色与日志
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
# 错误处理
# ===========================================================================
on_error() {
  local rc="$1" line="$2"
  if (( rc < 1 )); then
    rc=1
  fi
  # run_stage 的“捕获区”(外层 set +e)里,失败状态由 run_stage 自己读取
  # PIPESTATUS 处理,外层 trap 只需安静返回,避免重复打印/提前退出。
  if [[ "${ERR_CAPTURE:-0}" == "1" ]]; then
    return 0
  fi
  if [[ -n "${STAGE_CURRENT:-}" ]]; then
    # 阶段内的失败:run_stage 会打印日志 tail,这里只给一行定位信息
    log_err "阶段 ${STAGE_CURRENT} 在 ${SCRIPT_NAME} 第 ${line} 行失败 (exit=${rc});日志: ${STAGE_LOG:-<无>}"
    exit "$rc"
  fi
  log_err "${SCRIPT_NAME} 在第 ${line} 行失败 (exit=${rc})"
  if [[ -n "${STAGE_LOG:-}" && -f "$STAGE_LOG" ]]; then
    log_err "---- 失败阶段日志($STAGE_LOG)末尾 20 行 ----"
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
# 基础助手
# ===========================================================================
cmd_exists() { command -v "$1" >/dev/null 2>&1; }

# 绝对路径化(不依赖 realpath,不做符号链接归一化)
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

# dry-run 下只打印将执行的命令,零副作用
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

# 从 PATH 剔除 conda 目录并去重(sanitize_path <path> → stdout)
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
        log_info "preflight: 从 PATH 剔除 conda 目录: $p"
        continue
        ;;
    esac
    # 去重(注意 IFS=':' 时 ${keep[*]} 以 ':' 连接,定界符必须用 ':')
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

# 剔除 conda + 把 $PREFIX/bin 放到最前(幂等)
apply_clean_path() {
  local clean
  clean="$(sanitize_path "$PATH")"
  export PATH="$clean"
  case ":$PATH:" in
    *":$PREFIX/bin:"*) ;;
    *) export PATH="$PREFIX/bin:$PATH" ;;
  esac
}

# 目标路径(或其最近的已存在祖先)
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

fs_avail_gb() { # fs_avail_gb <probe> → stdout GB;探测失败返回 1
  local probe="$1" kb
  kb="$(df -Pk -- "$probe" 2>/dev/null | awk 'NR==2 {print $4}')"
  if [[ ! "${kb:-}" =~ ^[0-9]+$ ]]; then
    return 1
  fi
  printf '%s' "$(( kb / 1024 / 1024 ))"
}

# 带重试的下载:3 次,退避 2s/5s/10s(download_with_retry <URL> <DEST>)
download_with_retry() {
  local url="$1" dest="$2"
  local attempt delay
  if [[ $DRY_RUN -eq 1 ]]; then
    plan "curl -fL '$url' → '$dest'(失败重试 3 次,退避 2/5/10s)"
    return 0
  fi
  mkdir -p "$(dirname -- "$dest")"
  for attempt in 1 2 3; do
    log_info "下载(第 ${attempt}/3 次): $url"
    rm -f -- "${dest}.part"
    if curl -fL --connect-timeout 30 --max-time 3600 -o "${dest}.part" "$url"; then
      mv -f -- "${dest}.part" "$dest"
      return 0
    fi
    rm -f -- "${dest}.part"
    if (( attempt < 3 )); then
      delay="${DOWNLOAD_DELAYS[$(( attempt - 1 ))]}"
      log_warn "下载失败,${delay}s 后重试: $url"
      sleep "$delay"
    fi
  done
  log_err "下载 3 次均失败: $url"
  return 1
}

# 解压 tar.gz 并把内容装进 <dest>,自动剥掉单层顶层目录(archive_install <tar.gz> <dest>)
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
# git 助手
# ===========================================================================
is_moving_ref() { # HEAD / 默认分支名 → 需要在首次运行时解析成 commit 并写回
  case "$1" in
    HEAD|head|main|master) return 0 ;;
    *) return 1 ;;
  esac
}

# 克隆(或复用)仓库并 checkout 到 <ref>;失败返回 1,回退策略由调用方决定
ensure_repo() { # ensure_repo <dest> <url> <mirror> <ref> <label>
  local dest="$1" url="$2" mirror="$3" ref="$4" label="$5"
  if [[ $DRY_RUN -eq 1 ]]; then
    plan "git clone '$url' → '$dest'(镜像回退: ${mirror:-无}),checkout '$ref'"
    return 0
  fi
  if [[ -d "$dest/.svn" ]]; then
    log_info "复用已有 svn 检出: $dest"
    return 0
  fi
  if [[ ! -d "$dest/.git" ]]; then
    if [[ -e "$dest" ]]; then
      log_warn "$dest 已存在但不是 git 检出,删除后重新克隆"
      rm -rf -- "$dest"
    fi
    mkdir -p "$(dirname -- "$dest")"
    log_info "克隆 $label: $url"
    if ! git clone "$url" "$dest"; then
      if [[ -n "$mirror" ]]; then
        log_warn "主地址克隆失败,尝试镜像: $mirror"
        rm -rf -- "$dest"
        if ! git clone "$mirror" "$dest"; then
          log_err "镜像克隆同样失败: $mirror"
          return 1
        fi
      else
        log_err "克隆失败,且未配置镜像地址: $url"
        return 1
      fi
    fi
  else
    log_info "复用已有仓库: $dest"
    if ! git -C "$dest" fetch --all --tags --prune; then
      log_warn "git fetch 失败(离线?),继续使用本地已有对象"
    fi
  fi
  if is_moving_ref "$ref"; then
    if ! git -C "$dest" pull --ff-only; then
      log_warn "git pull --ff-only 失败,使用仓库当前 HEAD"
    fi
  else
    if ! git -C "$dest" checkout -f "$ref"; then
      log_err "checkout 失败: $ref"
      return 1
    fi
  fi
  return 0
}

# 把 <ref> 解析成 commit(本地 git 操作,不联网)
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
  # 非 git 树(例如 svn 检出)则原样返回
  printf '%s' "$ref"
}

# pin 是 HEAD/分支名时,把解析出的 commit 写回 versions.env,保证下次可复现
write_back_ref() { # write_back_ref <VAR_NAME> <SHA>
  local var="$1" val="$2"
  local stamp tmp next
  if [[ ! -w "$VERSIONS_ENV" ]]; then
    log_warn "versions.env 不可写,跳过写回($var=$val);结果已记录在 stage marker 中"
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
  log_ok "已把 $var 写回 versions.env: $val (resolved $stamp)"
}

# ===========================================================================
# 构建期环境(供 psrchive / dspsr / presto 的 configure / meson 使用)
# ===========================================================================
export_build_env() {
  # TEMPO2 指运行时数据目录(T2runtime 部署点);dspsr 的 configure 例外,
  # 它的宏按 $TEMPO2/include 找头文件,需在 configure 行单独传 TEMPO2="$PREFIX"
  export TEMPO2="$PREFIX/tempo2"
  if [[ -d "$PREFIX/pgplot" ]]; then
    export PGPLOT_DIR="$PREFIX/pgplot"
  fi
  # bootstrap 用 which psrchive-config / tempo2-config 找依赖,所以 $PREFIX/bin 必须在最前
  apply_clean_path
  export LD_LIBRARY_PATH="$PREFIX/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
  export PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
  # ASSUMPTION: 用 CPPFLAGS/LDFLAGS 把自装前缀喂给构建系统,比传一堆 --with-*
  # 更稳(那些标志名在上游版本间会漂移);同时刻意不传 --disable-*,
  # 让 configure 自动探测:pgplot / boost-python 缺失 → 自动关闭绘图与 Python 绑定,
  # 这正是本栈想要的精简构建。
  export CPPFLAGS="-I$PREFIX/include${CPPFLAGS:+ $CPPFLAGS}"
  export LDFLAGS="-L$PREFIX/lib -Wl,-rpath,$PREFIX/lib${LDFLAGS:+ $LDFLAGS}"
}

# autotools bootstrap 入口(defensive:可执行位丢失时退回 bash 调用)
run_bootstrap() {
  local dir="$1"
  if [[ $DRY_RUN -eq 1 && ! -d "$dir" ]]; then
    plan "( cd $dir && ./bootstrap )"
    return 0
  fi
  if [[ -x "$dir/bootstrap" ]]; then
    run_in "$dir" ./bootstrap
  elif [[ -f "$dir/bootstrap" ]]; then
    log_warn "$dir/bootstrap 不可执行,改用 bash 调用"
    run_in "$dir" bash ./bootstrap
  elif [[ -f "$dir/autogen.sh" ]]; then
    log_warn "$dir 缺少 bootstrap,改用 autogen.sh"
    run_in "$dir" bash ./autogen.sh
  else
    log_err "$dir 里找不到 bootstrap / autogen.sh"
    return 1
  fi
}

# ===========================================================================
# 阶段 0:preflight
# ===========================================================================
stage_preflight() {
  export LC_ALL=C
  export LANG=C
  apply_clean_path

  local tool
  for tool in gcc g++ make git sha256sum curl awk; do
    if ! cmd_exists "$tool"; then
      die "preflight: 缺少 $tool(build-essential/git 由 apt-deps 安装;若跳过该阶段请手动安装)"
    fi
  done
  if ! cmd_exists gfortran; then
    log_warn "preflight: 未找到 gfortran(psrchive/tempo2 需要;apt-deps 阶段会安装)"
  fi
  log_info "工具链: gcc $(gcc_version) | make $(make -v 2>/dev/null | awk 'NR==1{print $3}') | bash ${BASH_VERSION} | jobs=$JOBS"

  # 磁盘余量(检查 $PREFIX 所在文件系统)
  local probe avail_gb
  probe="$(nearest_existing "$PREFIX")"
  if avail_gb="$(fs_avail_gb "$probe")"; then
    if (( avail_gb < DISK_MIN_GB )); then
      die "preflight: $probe 所在文件系统仅剩 ${avail_gb}GB 可用,至少需要 ${DISK_MIN_GB}GB"
    fi
    log_ok "磁盘余量: $probe 可用 ${avail_gb}GB (>= ${DISK_MIN_GB}GB)"
  else
    log_warn "preflight: 无法探测 $probe 的可用空间(df 解析失败),跳过磁盘检查"
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
# 阶段 1:apt-deps
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
      die "apt-deps: 未识别的包管理器(无 apt-get/dnf/yum/pacman/zypper)。
请手动安装以下软件包,然后用 --without apt-deps 重跑:
  ${APT_PKGS[*]}"
      ;;
    *)
      die "apt-deps: 暂不支持用 $pm 自动安装(本脚本目前只支持 Debian 系)。
请手动安装以下软件包,然后用 --without apt-deps 重跑:
  ${APT_PKGS[*]}"
      ;;
  esac

  local -a sudo_cmd=()
  if [[ "${EUID}" -ne 0 ]]; then
    if cmd_exists sudo; then
      sudo_cmd=(sudo)
    else
      die "apt-deps: 当前不是 root 且没有 sudo,无法安装系统依赖。
请用 root 重跑,或手动安装后用 --without apt-deps 跳过: ${APT_PKGS[*]}"
    fi
  fi

  # 非交互环境必须显式 --yes
  if [[ $ASSUME_YES -ne 1 ]] && [[ ! -t 0 ]]; then
    die "apt-deps: 检测到非交互环境(stdin 不是 tty),无法二次确认;请加 --yes 重新运行。"
  fi

  run env DEBIAN_FRONTEND=noninteractive "${sudo_cmd[@]}" apt-get update
  run env DEBIAN_FRONTEND=noninteractive "${sudo_cmd[@]}" apt-get install -y "${APT_PKGS[@]}"
}

# apt 的一次性确认(在主进程里问,避免把提示混进阶段日志)
confirm_apt() {
  if [[ $ASSUME_YES -eq 1 ]]; then
    return 0
  fi
  if [[ ! -t 0 ]]; then
    die "即将用 apt 安装系统依赖,但当前是非交互环境;请加 --yes 以无人值守模式继续。"
  fi
  local reply
  printf '%s' "即将用 apt 安装 ${#APT_PKGS[@]} 个系统依赖包(build-essential/gfortran/libcfitsio-dev 等),是否继续? [y/N] " >&2
  if ! read -r reply; then
    die "读取确认失败,已取消"
  fi
  case "$reply" in
    y|Y|yes|YES|Yes) return 0 ;;
    *) die "已取消安装" ;;
  esac
}

# ===========================================================================
# 阶段 2:python-venv
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
    # 最后手段:conda 目录里的 uv。只借用这个二进制,conda 环境仍不进 PATH。
    for cand in "$HOME/miniconda3/bin/uv" "$HOME/anaconda3/bin/uv" "$HOME/mambaforge/bin/uv"; do
      if [[ -x "$cand" ]]; then
        UV_BIN="$cand"
        log_warn "只找到 conda 目录下的 uv: $cand(建议装一个独立 uv 到 ~/.local/bin)"
        break
      fi
    done
  fi
  if [[ -n "$UV_BIN" ]]; then
    log_info "uv: $UV_BIN ($("$UV_BIN" --version 2>/dev/null || printf '版本未知'))"
  else
    log_warn "未找到 uv,将回退为 python3 -m venv"
  fi
}

venv_python() { printf '%s' "$PREFIX/venv/bin/python"; }

# venv_pip <spec...>:优先 uv(快、不依赖 venv 内置 pip),失败逐级回退
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
    log_warn "uv pip 安装失败,回退 venv 内 pip"
  fi
  if [[ -x "$PREFIX/venv/bin/pip" ]]; then
    if "$PREFIX/venv/bin/pip" install "${specs[@]}"; then
      return 0
    fi
    log_warn "venv 内 pip 安装失败,回退 python -m pip"
  fi
  "$(venv_python)" -m pip install "${specs[@]}"
}

# versions.env 里的 PY_* 版本号 → 带包名的 requirement 列表
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
        log_warn "uv venv 失败,回退 python3 -m venv"
      fi
    fi
    if [[ $created -eq 0 ]] && [[ $DRY_RUN -eq 0 ]]; then
      if [[ -z "$py3" ]]; then
        die "python-venv: 既没有可用的 uv,也找不到 python3,无法创建 venv"
      fi
      log_warn "回退为 $py3 -m venv($("$py3" -V 2>&1))"
      run "$py3" -m venv "$PREFIX/venv"
    fi
  else
    log_info "复用已有 venv: $PREFIX/venv"
  fi

  if [[ $DRY_RUN -eq 0 ]] && [[ ! -x "$(venv_python)" ]]; then
    die "python-venv: venv 创建失败($PREFIX/venv/bin/python 不存在)"
  fi

  local -a specs=()
  mapfile -t specs < <(python_pkg_specs)
  log_info "安装 Python 依赖(精确 pin 来自 versions.env): ${specs[*]}"
  venv_pip "${specs[@]}"

  if [[ $DRY_RUN -eq 0 ]]; then
    # uv 建的 venv 默认没有 pip,后续 PRESTO 绑定安装会用到
    if ! "$(venv_python)" -m ensurepip --upgrade >/dev/null 2>&1; then
      log_info "venv 内 ensurepip 不可用(通常 uv 已就绪,忽略)"
    fi
    log_ok "venv 就绪: $("$(venv_python)" -V 2>&1) @ $PREFIX/venv"
  fi
}

# ===========================================================================
# 阶段 3:deps(fftw 3.2.2 单精度 + cfitsio 3.49 重入版;psrchive 的硬依赖)
#   系统包 libfftw3-dev 只有双精度、libcfitsio-dev 非重入,故必须源码自建:
#   fftw --enable-float(libfftw3f)+ cfitsio --enable-reentrant + make shared。
# ===========================================================================
stage_deps() {
  build_fftw
  build_cfitsio
}

build_fftw() {
  local ver="${FFTW_VERSION:-3.2.2}"
  if [[ -e "$PREFIX/lib/libfftw3f.so" || -e "$PREFIX/lib/libfftw3f.a" ]]; then
    log_ok "fftw 单精度库已存在,跳过"
    return 0
  fi
  local tgz="$SRC_DIR/fftw-$ver.tar.gz"
  local src="$SRC_DIR/fftw-$ver"
  if [[ ! -f "$tgz" ]] && ! download_with_retry "${FFTW_URL}" "$tgz"; then
    die "deps: fftw $ver 下载失败($FFTW_URL)"
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
    log_ok "cfitsio 已存在,跳过"
    return 0
  fi
  local tgz="$SRC_DIR/cfitsio-$ver.tar.gz"
  local src="$SRC_DIR/cfitsio-$ver"
  if [[ ! -f "$tgz" ]] && ! download_with_retry "${CFITSIO_URL}" "$tgz"; then
    die "deps: cfitsio $ver 下载失败($CFITSIO_URL)"
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
# 阶段 4:tempo(经典 tempo,可选;部分老工具仍按 $TEMPO 找星历)
# ===========================================================================
stage_tempo() {
  local src="$SRC_DIR/tempo"
  local ref="${PIN_TEMPO:-${TEMPO_REF:-HEAD}}"

  if ! ensure_repo "$src" "${TEMPO_URL}" "" "$ref" "tempo"; then
    die "tempo: 源码获取失败(git.code.sf.net 不可达?)"
  fi
  local resolved
  resolved="$(resolve_repo_ref "$src" "$ref")"
  log_info "tempo @ $resolved"
  if is_moving_ref "$ref" && [[ $DRY_RUN -eq 0 ]]; then
    TEMPO_REF="$resolved"
    write_back_ref TEMPO_REF "$resolved"
  fi

  # 上游没有 bootstrap;./prepare 是 csh 脚本,内容就是 autoreconf --install
  run_in "$src" autoreconf --install
  export_build_env
  run_in "$src" ./configure --prefix="$PREFIX"
  run_in "$src" make -j"$JOBS"
  run_in "$src" make install
}

# ===========================================================================
# 阶段 5:tempo2
# ===========================================================================
stage_tempo2() {
  local src="$SRC_DIR/tempo2"
  local ref="${PIN_TEMPO2:-${TEMPO2_REF:-HEAD}}"

  if ! ensure_repo "$src" "${TEMPO2_URL}" "${TEMPO2_MIRROR_URL:-}" "$ref" "tempo2"; then
    die "tempo2: 源码获取失败(主地址与镜像均失败)"
  fi
  local resolved
  resolved="$(resolve_repo_ref "$src" "$ref")"
  log_info "tempo2 @ $resolved"
  if is_moving_ref "$ref" && [[ $DRY_RUN -eq 0 ]]; then
    # 写回 versions.env,让下一次运行复现同一个 commit
    TEMPO2_REF="$resolved"
    write_back_ref TEMPO2_REF "$resolved"
  fi

  # T2runtime(时钟/星历)先于 bootstrap 就位(实测顺序);--enable-shared 生成
  # libtempo2.so,psrchive/dspsr 动态链接它。
  install_t2runtime "$src"

  run_bootstrap "$src"
  export_build_env
  run_in "$src" ./configure --prefix="$PREFIX" --enable-shared
  run_in "$src" make -j"$JOBS"
  run_in "$src" make install
}

# T2runtime(时钟/星历数据)从 git 仓库 rsync 到 $TEMPO2=$PREFIX/tempo2
# (tempo2 仓库自带 T2runtime/,无需单独下载)
install_t2runtime() {
  local repo="$1"
  local rt="$PREFIX/tempo2"

  if [[ -d "$rt/clock" && -d "$rt/ephemerides" ]]; then
    log_ok "T2runtime 已存在($rt),跳过"
    return 0
  fi
  if [[ $DRY_RUN -eq 1 ]]; then
    plan "rsync $repo/T2runtime/ → $rt(TEMPO2 运行时数据)"
    return 0
  fi
  if [[ ! -d "$repo/T2runtime" ]]; then
    log_warn "仓库里没有 T2runtime/ 目录;tempo2 二进制可用但缺时钟/星历数据,
  实际计时前请手动补齐:TEMPO2=$rt 下应有 clock/ ephemerides/ 等子目录"
    return 0
  fi
  run mkdir -p "$rt"
  run rsync -a "$repo/T2runtime/" "$rt/"
  if [[ -d "$rt/clock" ]]; then
    log_ok "T2runtime 就绪: $rt"
  else
    log_warn "T2runtime 同步后未见 clock/,布局可能与预期不同,请人工检查 $rt"
  fi
}

# ===========================================================================
# 阶段 6:pgplot(可选;psrplot/pas 等绘图 CLI 需要,MCP 自己的图走 matplotlib)
#   Caltech ftp 已死,源码用 ASTRON GitLab 镜像;补丁取自 psrchive 源码树 packages/。
# ===========================================================================
stage_pgplot() {
  if [[ -f "$PREFIX/pgplot/grfont.dat" ]]; then
    log_ok "PGPLOT 已存在($PREFIX/pgplot),跳过"
    return 0
  fi
  local pkg_dir="$SRC_DIR/psrchive/packages"
  local ver="${PGPLOT_VERSION:-5.2}"
  local tgz="$SRC_DIR/pgplot$ver.tar.gz"
  local src="$SRC_DIR/pgplot"

  # patch 文件来自 psrchive 源码树;pgplot 阶段只克隆 psrchive,不构建它
  if [[ ! -f "$pkg_dir/pgplot.patch" || ! -f "$pkg_dir/makemake.sharedcpg.patch" ]]; then
    if ! ensure_repo "$SRC_DIR/psrchive" "${PSRCHIVE_URL}" "${PSRCHIVE_MIRROR_URL:-}" \
         "${PIN_PSRCHIVE:-${PSRCHIVE_REF:-HEAD}}" "psrchive(取补丁)"; then
      log_err "pgplot: 拿不到 psrchive 源码树(packages/*.patch 缺失)"
      return 1
    fi
  fi
  if [[ ! -f "$tgz" ]] && ! download_with_retry "${PGPLOT_URL}" "$tgz"; then
    log_err "pgplot: 下载失败($PGPLOT_URL)"
    return 1
  fi
  rm -rf -- "$src"
  # tarball 顶层就是 pgplot/,在 $SRC_DIR 解开
  run bash -c "cd '$SRC_DIR' && gunzip -c '$tgz' | tar xf -"
  # patch -p0 的路径以 pgplot/ 开头,必须在 $SRC_DIR 下执行
  run_in "$SRC_DIR" patch -p0 --forward < "$pkg_dir/pgplot.patch"
  run_in "$SRC_DIR" patch -p0 --forward < "$pkg_dir/makemake.sharedcpg.patch"

  # 生成配置:g77→gfortran,gfortran>=10 需 -fallow-argument-mismatch
  run_in "$src/sys_linux" cp g77_gcc.conf psrchive.conf
  run perl -pi -e 's/FCOMPL="g77"/FCOMPL="gfortran"/' "$src/sys_linux/psrchive.conf"
  run perl -pi -e 's/FFLAGC="-u -Wall -fPIC -O"/FFLAGC="-Wall -fPIC -O -fallow-argument-mismatch -std=legacy"/' "$src/sys_linux/psrchive.conf"
  run perl -pi -e 's/FFLAGD="-fno-backslash"/FFLAGD="-fno-backslash -fallow-argument-mismatch"/' "$src/sys_linux/psrchive.conf"
  run perl -pi -e 's/XINCL="-I\/usr\/X11R6\/include"/XINCL=""/' "$src/sys_linux/psrchive.conf"
  run perl -pi -e 's/LIBS="-L\/usr\/X11R6\/lib -lX11"/LIBS="-lX11"/' "$src/sys_linux/psrchive.conf"
  # 共享库链接补 X11/PNG/Z(库必须放在对象之后,ld 默认 --as-needed)
  run perl -pi -e 's/SHARED_LIB_LIBS=""/SHARED_LIB_LIBS="-lX11 -lpng -lz"/' "$src/sys_linux/psrchive.conf"

  # 在 $PGPLOT_DIR 里生成 makefile 并编译;选 PNG / PostScript / X-window 驱动
  export PGPLOT_DIR="$PREFIX/pgplot"
  run mkdir -p "$PGPLOT_DIR"
  run cp "$src/drivers.list" "$PGPLOT_DIR/drivers.list"
  run perl -pi -e 's/! PNDRIV/  PNDRIV/' "$PGPLOT_DIR/drivers.list"
  run perl -pi -e 's/! PSDRIV/  PSDRIV/' "$PGPLOT_DIR/drivers.list"
  run perl -pi -e 's/! XWDRIV/  XWDRIV/' "$PGPLOT_DIR/drivers.list"
  run_in "$PGPLOT_DIR" "$src/makemake" "$src" linux psrchive
  # 用系统 libpng,去掉对源码树 ./png.h 的依赖
  run perl -pi -e 's/^pndriv\.o :/# /' "$PGPLOT_DIR/makefile"
  run_in "$PGPLOT_DIR" make
  run_in "$PGPLOT_DIR" make cpg
  run_in "$PGPLOT_DIR" make pgxwin_server
  run_in "$PGPLOT_DIR" make grfont.dat
}

# ===========================================================================
# 阶段 4:psrchive
# ===========================================================================
stage_psrchive() {
  local src="$SRC_DIR/psrchive"
  local ref="${PIN_PSRCHIVE:-${PSRCHIVE_REF:-HEAD}}"

  if ! ensure_repo "$src" "${PSRCHIVE_URL}" "${PSRCHIVE_MIRROR_URL:-}" "$ref" "psrchive"; then
    die "psrchive: 源码获取失败(主地址与镜像均失败)"
  fi
  local resolved
  resolved="$(resolve_repo_ref "$src" "$ref")"
  log_info "psrchive @ $resolved"
  if is_moving_ref "$ref" && [[ $DRY_RUN -eq 0 ]]; then
    PSRCHIVE_REF="$resolved"
    write_back_ref PSRCHIVE_REF "$resolved"
  fi

  if [[ ! -x "$PREFIX/bin/tempo2" ]]; then
    log_warn "psrchive: 未发现 $PREFIX/bin/tempo2,configure 可能关闭 tempo2 相关功能"
  fi

  # ASSUMPTION: bootstrap 需要 autoconf-archive(apt-deps 已装)。不传 --disable-*:
  # 系统里没有 pgplot / boost-python,configure 会自动关闭绘图与 Python 绑定(预期行为)。
  run_bootstrap "$src"
  export_build_env
  run_in "$src" ./configure --prefix="$PREFIX"
  run_in "$src" make -j"$JOBS"
  run_in "$src" make install
}

# ===========================================================================
# 阶段 7:dspsr
# ===========================================================================
# 后端模块选择:实测可编译的完整名单,写入为单行 backends.list;
# 必须写在 bootstrap 之前(bootstrap 据此生成 Makefile 片段)
select_dspsr_backends() {
  local src="$1"
  if [[ $DRY_RUN -eq 1 ]]; then
    plan "写入后端选择: $src/backends.list ← ${DSPSR_BACKENDS[*]}"
    return 0
  fi
  printf '%s\n' "${DSPSR_BACKENDS[*]}" > "$src/backends.list"
  log_info "已写入 $src/backends.list (${DSPSR_BACKENDS[*]})"
}

# psrchive >= 2026-05-01 把 Signal::* 枚举改名 Convention::* 并删除了若干 API,
# dspsr master 尚未跟上 —— 以下补丁幂等(模式消失即跳过),取自实测成功的装机流程。
patch_dspsr_for_psrchive() {
  local src="$1" f
  # a) Signal::Basis / 枚举值 / Conventional → Convention::*
  if git -C "$src" grep -q "Signal::Basis" -- '*.h' '*.C' 2>/dev/null; then
    log_info "dspsr 补丁: Signal::Basis → Convention::Basis"
    git -C "$src" grep -l "Signal::Basis" -- '*.h' '*.C' | while read -r f; do
      sed -i 's/Signal::Basis/Convention::Basis/g' "$src/$f"
    done
  fi
  if git -C "$src" grep -qE "Signal::(Circular|Linear|Elliptical)" -- '*.h' '*.C' 2>/dev/null; then
    log_info "dspsr 补丁: Signal::{Circular,Linear,Elliptical} → Convention::*"
    git -C "$src" grep -lE "Signal::(Circular|Linear|Elliptical)" -- '*.h' '*.C' | while read -r f; do
      sed -i -E 's/Signal::(Circular|Linear|Elliptical)/Convention::\1/g' "$src/$f"
    done
  fi
  if git -C "$src" grep -q "Signal::Conventional" -- '*.h' '*.C' 2>/dev/null; then
    log_info "dspsr 补丁: Signal::Conventional → Convention::Conventional"
    git -C "$src" grep -l "Signal::Conventional" -- '*.h' '*.C' | while read -r f; do
      sed -i 's/Signal::Conventional/Convention::Conventional/g' "$src/$f"
    done
  fi

  # b) Calibration::Faraday 已被移除,等价替代为 MEAL::Faraday
  if git -C "$src" grep -q 'Pulsar/Faraday.h' -- '*.h' 2>/dev/null; then
    log_info "dspsr 补丁: Calibration::Faraday → MEAL::Faraday"
    sed -i 's|#include "Pulsar/Faraday.h"|#include "MEAL/Faraday.h"|; s|Calibration::Faraday birefringence;|MEAL::Faraday birefringence;|' \
      "$src/Signal/General/dsp/Derotation.h"
    sed -i 's|Calibration::Faraday temp;|MEAL::Faraday temp;|' \
      "$src/Signal/General/Derotation.C"
  fi

  # c) Signal::string2Basis 已删除,用 Convention::Basis 的流式解析替代
  f="$src/Kernel/Classes/ASCIIObservation.C"
  if [[ -f "$f" ]] && grep -q "set_basis( Signal::string2Basis(buffer)" "$f" 2>/dev/null; then
    log_info "dspsr 补丁: string2Basis → istringstream"
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

  # d) sigproc 后端:MEAL 类需命名空间限定 + Mount/Directional 新接口
  f="$src/Kernel/Formats/sigproc/SigProcObservation.C"
  if [[ -f "$f" ]] && grep -q "^  Horizon horizon;" "$f" 2>/dev/null; then
    log_info "dspsr 补丁: sigproc MEAL 命名空间 + 新 Mount API"
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

# dspsr 的 bootstrap 依赖 psrchive 的 autoconf 宏;已安装版的 share/aclocal 只带
# psrchive.m4,其余宏从 psrchive 源码树 .build-commons/autoconf/ 补齐
copy_psrchive_m4() {
  local src="$1"
  local pb_m4="$SRC_DIR/psrchive/.build-commons/autoconf"
  if [[ ! -d "$pb_m4" ]]; then
    log_warn "未找到 $pb_m4;若 configure 报宏缺失,请确认 psrchive 源码树存在"
    return 0
  fi
  local m
  for m in ac_pkg_swig.m4 ac_python_devel.m4 compiler.m4 debug.m4 getopt_long.m4 \
           cfitsio.m4 pgplot.m4 local.m4 package.m4 tempo2.m4; do
    if [[ ! -f "$src/config/$m" && -f "$pb_m4/$m" ]]; then
      log_info "补齐缺失宏: $m"
      run cp "$pb_m4/$m" "$src/config/"
    fi
  done
}

stage_dspsr() {
  local src="$SRC_DIR/dspsr"
  local ref="${PIN_DSPSR:-${DSPSR_REF:-HEAD}}"

  if ! ensure_repo "$src" "${DSPSR_URL}" "${DSPSR_MIRROR_URL:-}" "$ref" "dspsr"; then
    die "dspsr: 源码获取失败(主地址与镜像均失败)"
  fi
  local resolved
  resolved="$(resolve_repo_ref "$src" "$ref")"
  log_info "dspsr @ $resolved"
  if is_moving_ref "$ref" && [[ $DRY_RUN -eq 0 ]]; then
    DSPSR_REF="$resolved"
    write_back_ref DSPSR_REF "$resolved"
  fi

  if [[ ! -x "$PREFIX/bin/psrchive-config" ]]; then
    log_warn "dspsr: 未发现 $PREFIX/bin/psrchive-config,bootstrap/configure 大概率失败(请先装好 psrchive)"
  fi

  select_dspsr_backends "$src"
  patch_dspsr_for_psrchive "$src"
  copy_psrchive_m4 "$src"

  run_bootstrap "$src"
  export_build_env
  # configure 期 TEMPO2 指安装根(tempo2 宏按 $TEMPO2/include 找头文件),
  # 运行期 TEMPO2 仍指 $PREFIX/tempo2 数据目录(见 export_build_env);
  # sigproc 后端按平铺名 include MEAL 头,故 CPPFLAGS 追加 -I$PREFIX/include/MEAL
  local -a cfg=(--prefix="$PREFIX" F77=gfortran CFLAGS=-fPIC FFLAGS=-fPIC
                CPPFLAGS="-I$PREFIX/include -I$PREFIX/include/MEAL"
                TEMPO2="$PREFIX")
  if [[ -d /usr/local/cuda ]]; then
    log_info "检测到 /usr/local/cuda,启用 CUDA 支持(dspsr -cuda 0 ...)"
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
        log_warn "dspsr 已安装但 --version/-V 探测失败,请人工确认"
      fi
    fi
  fi
}

# ===========================================================================
# 阶段 8:psrcat
#   ATNF 的下载地址已失效(实测 404):tarball 需手动获取。查找顺序:
#   --psrcat-tarball → $SRC_DIR/psrcat_pkg*.tar.gz → ~/Downloads → 当前目录。
#   编译极简:gcc -fcommon 直编全部 .c(新版 gcc 需 -fcommon 合并重复的
#   tentative 全局定义),无需 tcsh/makeit。
# ===========================================================================
stage_psrcat() {
  if [[ -x "$PREFIX/bin/psrcat" && -f "$PREFIX/psrcat/psrcat.db" ]]; then
    log_ok "psrcat 已安装,跳过"
    return 0
  fi
  local root="$SRC_DIR/psrcat_tar"

  local pkg="" c
  if [[ -n "$PSRCAT_TARBALL" ]]; then
    [[ -f "$PSRCAT_TARBALL" ]] || die "psrcat: --psrcat-tarball 指定的文件不存在: $PSRCAT_TARBALL"
    pkg="$PSRCAT_TARBALL"
  else
    for c in "$SRC_DIR"/psrcat_pkg*.tar.gz "$HOME"/Downloads/psrcat_pkg*.tar.gz ./psrcat_pkg*.tar.gz; do
      if [[ -f "$c" ]]; then pkg="$c"; break; fi
    done
  fi
  if [[ -z "$pkg" ]]; then
    die "psrcat: 未找到 psrcat_pkg*.tar.gz。ATNF 下载地址已失效,请从
  https://www.atnf.csiro.au/research/pulsar/psrcat/ 手动下载源码包,
  用 --psrcat-tarball PATH 指定,或放到 ~/Downloads / $SRC_DIR 下再重跑"
  fi
  log_info "psrcat 源码包: $pkg"

  if [[ $DRY_RUN -eq 1 ]]; then
    plan "解压 $pkg → $root;gcc -fcommon 直编;安装 bin/psrcat 与 psrcat/psrcat.db"
    return 0
  fi

  rm -rf -- "$root"
  archive_install "$pkg" "$root"
  run bash -c "cd '$root' && gcc -fcommon -O2 -o psrcat ./*.c -lm"
  run install -m 0755 "$root/psrcat" "$PREFIX/bin/psrcat"

  local db
  db="$(find "$root" -maxdepth 2 -type f -name psrcat.db 2>/dev/null | head -1)"
  [[ -n "$db" ]] || die "psrcat: 源码包里没有 psrcat.db(请检查 $root)"
  run install -m 0644 "$db" "$PREFIX/psrcat/psrcat.db"

  if ! "$PREFIX/bin/psrcat" -v >/dev/null 2>&1; then
    if ! "$PREFIX/bin/psrcat" -V >/dev/null 2>&1; then
      log_warn "psrcat 已安装但 -v/-V 探测失败,请人工确认"
    fi
  fi
}

# ===========================================================================
# 阶段 9:presto(可选,--with-presto)
# ===========================================================================
stage_presto() {
  local src="$SRC_DIR/presto"
  local tag="${PIN_PRESTO:-${PRESTO_TAG:-v5.2.0}}"

  if ! ensure_repo "$src" "${PRESTO_URL}" "" "$tag" "presto"; then
    log_err "presto: 源码获取失败"
    return 1
  fi

  # meson 装进栈内 venv(不污染系统,也不碰 conda)
  if ! venv_pip meson; then
    log_err "presto: meson 安装失败"
    return 1
  fi
  local meson_bin="$PREFIX/venv/bin/meson"
  if [[ ! -x "$meson_bin" ]]; then
    meson_bin="$(command -v meson || printf 'meson')"
  fi

  export_build_env
  # ASSUMPTION: PRESTO v5.x 用 meson 构建;若上游 meson.build 仍强制要求 PGPLOT
  # (Deepin 没有 pgplot5 包),setup 会失败 —— 本阶段按可选处理:打 WARNING、
  # 写 $PREFIX/.stack/presto.skipped,不影响其余组件。
  if ! run_in "$src" "$meson_bin" setup build --prefix="$PREFIX"; then
    log_warn "presto: meson setup 失败(最常见原因是上游构建脚本强制依赖 PGPLOT)"
    log_warn "        跳过 PRESTO;tempo2/psrchive/dspsr/psrcat 不受影响。需要时手动装 pgplot 后重跑本阶段"
    return 1
  fi
  if ! run_in "$src" "$meson_bin" compile -C build; then
    log_err "presto: 编译失败"
    return 1
  fi
  if ! run_in "$src" "$meson_bin" install -C build; then
    log_err "presto: 安装失败"
    return 1
  fi

  # Python 绑定(非致命)
  if ! venv_pip "$src/python"; then
    log_warn "presto: Python 绑定安装失败(非致命),C 工具已装到 $PREFIX/bin"
  fi
}

# ===========================================================================
# 阶段 8:self-check
# ===========================================================================
probe_run() { # probe_run <exe> <args...> → stdout 首个非空行;失败返回 1
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

# 依次尝试多组参数,返回第一组成功的结果(probe_multi <exe> "<args>" ...)
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

sc_trunc() { # 压成单行并限长,便于表格与 JSON
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
  log_info "逐组件版本探测,并生成 selfcheck.json 与 env.sh"

  if [[ $DRY_RUN -eq 1 ]]; then
    plan "探测 tempo2(-v/-V/--version)、psrchive-config --version、dspsr --version、psrcat -v/-V、venv python import pint+astropy"
    plan "写 $PREFIX/share/pulsar-stack/selfcheck.json"
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
    log_warn "self-check: TEMPO2=$PREFIX/tempo2 下没有 clock/,tempo2 能跑但缺时钟/星历数据"
  fi

  # --- psrchive / dspsr / psrcat ---
  sc_component "psrchive" "$PREFIX/bin/psrchive-config" "--version" "-V" \
    || log_warn "psrchive 探测失败"
  sc_component "dspsr" "$PREFIX/bin/dspsr" "--version" "-V" \
    || log_warn "dspsr 探测失败"
  sc_component "psrcat" "$PREFIX/bin/psrcat" "-v" "-V" "--version" \
    || log_warn "psrcat 探测失败"

  # --- Python 栈(venv)---
  local vpy
  vpy="$(venv_python)"
  local pyver=""
  if [[ -x "$vpy" ]]; then
    if pyver="$("$vpy" -c 'import astropy, pint; print("astropy %s / pint-pulsar %s" % (astropy.__version__, pint.__version__))' </dev/null 2>&1)"; then
      sc_record "python(pint+astropy)" 1 "$(sc_trunc "$pyver")" "$vpy"
    else
      sc_record "python(pint+astropy)" 0 "" "$vpy"
      log_warn "venv python 导入 pint/astropy 失败: $(sc_trunc "$pyver")"
    fi
  else
    sc_record "python(pint+astropy)" 0 "" "$vpy"
    log_warn "venv 不存在: $vpy"
  fi

  # --- PRESTO(可选)---
  if [[ $WITH_PRESTO -eq 1 ]]; then
    if [[ -f "$STACK_DIR/presto.skipped" ]]; then
      sc_record "presto" 0 "skipped(见 presto.skipped)" "$STACK_DIR/presto.skipped"
    elif pyver="$("$vpy" -c 'import presto; print(getattr(presto, "__version__", "unknown"))' </dev/null 2>&1)"; then
      sc_record "presto" 1 "$(sc_trunc "$pyver")" "$vpy"
    elif [[ -x "$PREFIX/bin/prepfold" ]]; then
      sc_record "presto" 1 "(C 工具已装,python 绑定未验证)" "$PREFIX/bin/prepfold"
    else
      sc_record "presto" 0 "" "$PREFIX/bin"
      log_warn "presto 探测失败(既无 python 绑定也无 prepfold)"
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
    log_warn "找不到 python3,无法生成 selfcheck.json(自检数据仍在上面的表格里)"
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
    log_warn "selfcheck.json 生成失败($out)"
  fi
  rm -f -- "$tsv"
  if [[ "${TMP_PATH:-}" == "$tsv" ]]; then
    TMP_PATH=""
  fi
  log_ok "自检报告: $out"
}

env_sh_body() {
  cat <<EOF
# 由 ${SCRIPT_NAME} 生成于 $(now_utc)
# 用法: source ${ENV_FILE}
# 注意:MCP 服务器的 config.toml [backends] 请直接写绝对路径,不依赖本文件(见 docs/DESIGN.md §8.1)。
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
# Python 环境按需激活: source "\$PULSAR_PREFIX/venv/bin/activate"
EOF
}

write_env_file() {
  local target="${ENV_FILE:-$PREFIX/env.sh}"
  if [[ $DRY_RUN -eq 1 ]]; then
    plan "写环境文件 $target:"
    env_sh_body | sed 's/^/    | /' >&2
    return 0
  fi
  mkdir -p "$(dirname -- "$target")"
  env_sh_body > "$target"
  chmod 0644 "$target"
  log_ok "环境文件: $target(source 后即可使用)"
}

# ===========================================================================
# 阶段框架:marker + 日志 + 错误隔离
# ===========================================================================
stage_mode() { # required | optional(presto/tempo/pgplot 失败只警告)
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

# 阶段的“输入指纹”:URL + 配置标志 + 相关环境(ref 不进哈希,单独存 marker)
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

stage_repo_dir() { # 无仓库的阶段返回空串
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

# 阶段当前应处于的 ref(仓库已存在时解析成 commit,否则返回 pin 原值)
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
  log_err "阶段 $name 失败 (exit=$rc)"
  if [[ -n "${STAGE_LOG:-}" && -f "$STAGE_LOG" ]]; then
    log_err "完整日志: $STAGE_LOG"
    log_err "---- 日志末尾 20 行 ----"
    tail -n 20 "$STAGE_LOG" >&2 || true
    log_err "------------------------"
  else
    log_err "(本阶段没有日志文件,可能是 dry-run 或极早期失败)"
  fi
  exit 1
}

run_stage() { # run_stage <stage>
  local name="$1"
  local mode func hash rc
  if ! stage_selected "$name"; then
    log_info "==> 跳过阶段 $name(未选中)"
    record_summary "$name" "SKIP" "未选中"
    return 0
  fi
  mode="$(stage_mode "$name")"
  func="$(stage_func_name "$name")"
  if ! declare -F "$func" >/dev/null 2>&1; then
    die "内部错误:找不到阶段函数 $func"
  fi

  hash="$(spec_hash "$(stage_spec "$name")")"
  if marker_up_to_date "$name" "$hash"; then
    log_ok "==> 阶段 $name 已完成且输入未变化,跳过(marker: $(marker_path "$name"))"
    record_summary "$name" "SKIP" "marker 命中"
    return 0
  fi

  # 兜底目录(--only 单阶段或跳过 preflight 时也能安全安装)
  if [[ $DRY_RUN -eq 0 ]]; then
    mkdir -p "$PREFIX/bin" "$PREFIX/lib" "$PREFIX/share" "$STACK_DIR" "$LOG_DIR" \
      "$PREFIX/catalog" "$PREFIX/runtime" "$SRC_DIR"
  fi

  STAGE_CURRENT="$name"
  STAGE_LOG=""
  rc=0
  # 失败捕获区:外层临时 set +e(否则管道失败会直接退出脚本),
  # 子 shell 里显式恢复 -Eeuo pipefail —— 注意:如果把子 shell 放进 if/|| 这类
  # “errexit 被忽略”的上下文,忽略状态会传播进子 shell,阶段内的中间失败就
  # 不会再中止(实测如此),所以必须用“外层关 -e + 子 shell 显式开 -e”的写法。
  ERR_CAPTURE=1
  set +e
  if [[ $DRY_RUN -eq 1 ]]; then
    log_info "==> [$name] (dry-run) 将执行:"
    ( ERR_CAPTURE=0; set -Eeuo pipefail; "$func" )
    rc="${PIPESTATUS[0]}"
  else
    STAGE_LOG="$LOG_DIR/$(date -u '+%Y%m%dT%H%M%SZ')_${name}.log"
    log_info "==> [$name] 开始(mode=$mode,日志: $STAGE_LOG)"
    # 子 shell 隔离阶段内的变量与失败;tee 同时落盘与上屏
    ( ERR_CAPTURE=0; set -Eeuo pipefail; "$func" ) 2>&1 | tee "$STAGE_LOG"
    rc="${PIPESTATUS[0]}"
  fi
  set -e
  ERR_CAPTURE=0
  STAGE_CURRENT=""

  if (( rc != 0 )); then
    if [[ "$mode" == "optional" ]]; then
      log_warn "可选阶段 $name 失败 (exit=$rc),记录 ${STACK_DIR}/${name}.skipped 后继续"
      if [[ $DRY_RUN -eq 0 ]]; then
        {
          printf 'stage=%s\n' "$name"
          printf 'failed_at=%s\n' "$(now_utc)"
          printf 'exit=%s\n' "$rc"
          printf 'log=%s\n' "$STAGE_LOG"
          printf 'note=可选阶段,失败不影响其余组件;删除本文件后重跑即可再次尝试\n'
        } > "$STACK_DIR/${name}.skipped"
      fi
      record_summary "$name" "FAIL*" "可选阶段已跳过,详见日志"
      return 0
    fi
    stage_fail "$name" "$rc"
  fi

  if [[ $DRY_RUN -eq 1 ]]; then
    record_summary "$name" "PLAN" "dry-run"
  else
    write_marker "$name" "$hash" "$(stage_current_ref "$name")"
    record_summary "$name" "OK" "日志: $STAGE_LOG"
    log_ok "==> [$name] 完成"
  fi
  return 0
}

record_summary() { # record_summary <stage> <status> <note>
  SUM_STAGE+=("$1")
  SUM_STATUS+=("$2")
  SUM_NOTE+=("$3")
}

# ===========================================================================
# 汇总输出
# ===========================================================================
print_summary() {
  local i name status note width_s=12
  printf '\n' >&2
  _log "SUMMARY" "$C_BOLD" "================ 安装汇总 ================"
  for i in "${!SUM_STAGE[@]}"; do
    name="${SUM_STAGE[$i]}"
    if (( ${#name} > width_s )); then
      width_s=${#name}
    fi
  done
  printf '%-*s  %-6s  %s\n' "$width_s" "阶段" "状态" "说明" >&2
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
    _log "SUMMARY" "$C_BOLD" "组件自检"
    printf '%-22s %-6s %-42s %s\n' "组件" "可用" "版本" "路径" >&2
    for i in "${!SC_NAME[@]}"; do
      local ok="no"
      if [[ "${SC_OK[$i]}" == "1" ]]; then
        ok="yes"
      fi
      printf '%-22s %-6s %-42s %s\n' "${SC_NAME[$i]}" "$ok" "${SC_VER[$i]}" "${SC_PATH[$i]}" >&2
    done
  fi

  printf '\n' >&2
  _log "NOTE" "$C_BOLD" "MCP 服务器的 config.toml [backends] 请用绝对路径,不依赖本 env.sh(见 docs/DESIGN.md §8.1)"
  _log "NOTE" "$C_BOLD" "使用前 source ${ENV_FILE:-$PREFIX/env.sh},或直接 source scripts/dev_env.sh"
}

# ===========================================================================
# CLI
# ===========================================================================
usage() {
  cat <<'USAGE'
install_pulsar_stack.sh — 脉冲星天文软件栈一键源码编译安装器

用法:
  bash scripts/install_pulsar_stack.sh [选项]

阶段顺序:
  preflight → apt-deps → python-venv → deps → [tempo] → tempo2 → [pgplot]
            → psrchive → dspsr → psrcat → [presto] → self-check
  每阶段成功后写 <prefix>/.stack/<stage>.done(记录 ref + 参数哈希);
  重跑时若输入未变化则自动跳过。

选项:
  --prefix DIR          安装前缀(默认 ~/Pulsar;优先级 PULSAR_PREFIX > PSRSOFT_DIR)
  --jobs N              并行编译任务数(默认 nproc)
  --with-presto         连 PRESTO 一起装(可选阶段,失败只警告不中止)
  --without STAGE       跳过某阶段,可重复;STAGE ∈
                        preflight|apt-deps|python-venv|deps|tempo|tempo2|pgplot|psrchive|dspsr|psrcat|presto|self-check
  --only STAGES         只跑这些阶段(逗号分隔,如 "tempo2,psrchive";点名 presto 视为启用)
  --pin-tempo2 REF      覆盖 versions.env 的 TEMPO2_REF(commit/tag/分支)
  --pin-psrchive REF    覆盖 PSRCHIVE_REF
  --pin-dspsr REF       覆盖 DSPSR_REF
  --pin-presto REF      覆盖 PRESTO_TAG
  --psrcat-tarball PATH psrcat 本地源码包(ATNF 下载地址已失效,需手动下载
                        psrcat_pkg*.tar.gz;不传则在 --src-dir、~/Downloads、cwd 搜索)
  --src-dir DIR         源码目录(默认 <prefix>/src)
  --env-file PATH       env.sh 输出路径(默认 <prefix>/env.sh)
  --dry-run             只打印完整计划与每条将执行的命令,零副作用(不建目录、不 apt、不联网)
  --yes, -y             无人值守:跳过 apt 一次性确认(非交互环境必加)
  --no-color            关闭彩色输出(非 tty 时自动关闭)
  -h, --help            显示本帮助

示例:
  bash scripts/install_pulsar_stack.sh                 # 全栈(不含 PRESTO)
  bash scripts/install_pulsar_stack.sh --with-presto   # 连 PRESTO 一起装
  bash scripts/install_pulsar_stack.sh --dry-run       # 先看看会做什么
  bash scripts/install_pulsar_stack.sh --only tempo2 --pin-tempo2 <commit>

说明:
  * 版本 / pin / 下载地址集中在 scripts/versions.env;pin 为 HEAD 时,首次运行会
    解析成具体 commit 并写回该文件(带 `# resolved <日期>` 注释)。
  * 不用 Docker、不用 conda-forge;tempo 与 pgplot 为可选阶段(失败只警告)。
  * psrcat 的 ATNF 下载地址已失效:请从 ATNF 网页手动下载 psrcat_pkg tarball
    后用 --psrcat-tarball 指定(或放到 ~/Downloads / --src-dir 下)。
  * 日志:<prefix>/logs/<UTC时间戳>_<stage>.log;
    自检报告:<prefix>/share/pulsar-stack/selfcheck.json。
USAGE
}

parse_args() {
  while (( $# > 0 )); do
    case "$1" in
      --prefix)
        [[ $# -ge 2 ]] || die "--prefix 需要一个目录参数"
        PREFIX="$2"; shift 2 ;;
      --prefix=*)
        PREFIX="${1#*=}"; shift ;;
      --jobs)
        [[ $# -ge 2 ]] || die "--jobs 需要一个数字参数"
        JOBS="$2"; shift 2 ;;
      --jobs=*)
        JOBS="${1#*=}"; shift ;;
      --psrcat-tarball)
        [[ $# -ge 2 ]] || die "--psrcat-tarball 需要一个文件参数"
        PSRCAT_TARBALL="$2"; shift 2 ;;
      --psrcat-tarball=*)
        PSRCAT_TARBALL="${1#*=}"; shift ;;
      --src-dir)
        [[ $# -ge 2 ]] || die "--src-dir 需要一个目录参数"
        SRC_DIR="$2"; shift 2 ;;
      --src-dir=*)
        SRC_DIR="${1#*=}"; shift ;;
      --env-file)
        [[ $# -ge 2 ]] || die "--env-file 需要一个路径参数"
        ENV_FILE="$2"; shift 2 ;;
      --env-file=*)
        ENV_FILE="${1#*=}"; shift ;;
      --with-presto)
        WITH_PRESTO=1; shift ;;
      --without)
        [[ $# -ge 2 ]] || die "--without 需要一个阶段名"
        WITHOUT_LIST+=("$2"); shift 2 ;;
      --without=*)
        WITHOUT_LIST+=("${1#*=}"); shift ;;
      --only)
        [[ $# -ge 2 ]] || die "--only 需要一个阶段列表"
        add_only_list "$2"; shift 2 ;;
      --only=*)
        add_only_list "${1#*=}"; shift ;;
      --pin-tempo2)
        [[ $# -ge 2 ]] || die "--pin-tempo2 需要一个 ref"
        PIN_TEMPO2="$2"; shift 2 ;;
      --pin-tempo2=*)
        PIN_TEMPO2="${1#*=}"; shift ;;
      --pin-psrchive)
        [[ $# -ge 2 ]] || die "--pin-psrchive 需要一个 ref"
        PIN_PSRCHIVE="$2"; shift 2 ;;
      --pin-psrchive=*)
        PIN_PSRCHIVE="${1#*=}"; shift ;;
      --pin-dspsr)
        [[ $# -ge 2 ]] || die "--pin-dspsr 需要一个 ref"
        PIN_DSPSR="$2"; shift 2 ;;
      --pin-dspsr=*)
        PIN_DSPSR="${1#*=}"; shift ;;
      --pin-presto)
        [[ $# -ge 2 ]] || die "--pin-presto 需要一个 ref"
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
        die "未知参数: $1(用 --help 查看用法)" ;;
      *)
        die "多余的位置参数: $1(本脚本不接受位置参数;用 --help 查看用法)" ;;
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

# --only 支持逗号分隔;解析阶段就展开,保证 stage_selected 看到的已是干净列表
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
    die "--jobs 必须是正整数(当前: $JOBS)"
  fi
  for s in "${WITHOUT_LIST[@]}"; do
    if ! stage_is_known "$s"; then
      die "--without 的阶段名无效: $s(可选: ${ALL_STAGES[*]})"
    fi
  done
  if (( ${#ONLY_LIST[@]} == 0 )) && [[ -n "${ONLY_EMPTY_GIVEN:-}" ]]; then
    die "--only 的阶段列表为空"
  fi
  for s in "${ONLY_LIST[@]}"; do
    if ! stage_is_known "$s"; then
      die "--only 的阶段名无效: $s(可选: ${ALL_STAGES[*]})"
    fi
    if [[ "$s" == "presto" ]]; then
      # 显式点名 presto 视为启用该可选阶段
      WITH_PRESTO=1
    fi
  done
}

load_versions() {
  local v
  if [[ ! -f "$VERSIONS_ENV" ]]; then
    die "找不到 $VERSIONS_ENV(脚本需与 versions.env 同目录)"
  fi
  # shellcheck disable=SC1090
  source "$VERSIONS_ENV"
  # 关键变量必须非空,否则 requirement 会拼出坏串
  for v in TEMPO2_URL PSRCHIVE_URL DSPSR_URL PRESTO_URL FFTW_URL CFITSIO_URL \
           PGPLOT_URL TEMPO_URL PY_MCP PY_PYDANTIC PY_ASTROPY PY_PINT PY_PSRQPY PY_MATPLOTLIB PY_NUMPY PY_PYTEST; do
    if [[ -z "${!v:-}" ]]; then
      die "versions.env 缺少或为空: $v"
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

  # 路径归一化
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

  # --- 尽早生效的环境(即使 --without preflight 也必须成立)---
  # LC_ALL=C 与“去 conda 化”的 PATH 是所有 configure 正确工作的前提,
  # 因此放在 main 里,而不只放在 preflight 阶段里。
  export LC_ALL=C
  export LANG=C
  apply_clean_path

  log_info "${SCRIPT_NAME}: prefix=$PREFIX jobs=$JOBS src=$SRC_DIR env=$ENV_FILE"
  if (( ${#ONLY_LIST[@]} > 0 )); then
    log_info "只运行阶段: ${ONLY_LIST[*]}"
  fi
  if (( ${#WITHOUT_LIST[@]} > 0 )); then
    log_info "跳过阶段: ${WITHOUT_LIST[*]}"
  fi
  if [[ $WITH_PRESTO -eq 1 ]]; then
    log_info "PRESTO:启用(可选阶段,失败只警告)"
  fi
  if [[ $DRY_RUN -eq 1 ]]; then
    log_warn "DRY-RUN:仅打印计划,不会建目录、不会 apt、不会联网"
  fi

  # apt 的一次性确认(在主进程里问,避免混进阶段日志)
  if stage_selected "apt-deps" && [[ $DRY_RUN -eq 0 ]] && [[ $ASSUME_YES -ne 1 ]]; then
    confirm_apt
  fi

  if (( ${#ONLY_LIST[@]} > 0 )) && ! stage_selected "preflight"; then
    log_warn "--only 模式:preflight 未选中,请确保 $PREFIX 目录结构已存在(否则请加上 preflight)"
  fi

  local s
  for s in "${ALL_STAGES[@]}"; do
    run_stage "$s"
  done

  # 即使 self-check 被跳过/未选中,也保证 env.sh 存在(幂等)
  write_env_file

  print_summary
}

# 只在直接执行时进入 main;被 source 时不动作(便于复用其中的函数)
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
