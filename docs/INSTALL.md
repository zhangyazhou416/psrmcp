# 脉冲星软件栈一键安装(install_pulsar_stack.sh)

`scripts/install_pulsar_stack.sh` 把脉冲星天文工具链**全部从源码**编译进单一前缀
(默认 `~/Pulsar`;若 shell 里已导出 `PSRSOFT_DIR` 则优先用它,可用 `--prefix` 覆盖):

- **不用 Docker、不用 conda-forge**(C/C++/Fortran 工具全部系统依赖 + 源码编译)
- **来源全部实测校准**(2026-10):tempo2 = github ipta、dspsr = SourceForge、
  psrchive = gitlab pulsar-astronomy、psrcat = 本地 tarball + `gcc -fcommon`、
  pgplot = ASTRON 镜像;ATNF/Caltech 等已失效地址不再尝试
- **psrchive/dspsr 的 API 漂移已内建处理**:psrchive ≥ 2026-05 的
  `Signal::* → Convention::*` 改名、`Calibration::Faraday → MEAL::Faraday` 等,
  安装器对 dspsr 打幂等补丁(模式消失即跳过,重复运行安全)
- **自动隔离 conda**:构建前会把 `~/miniconda3`、`~/anaconda3`、`~/mambaforge`
  从 `PATH` 剔除,避免 conda 的 `python` / `libstdc++` 干扰 `configure`
- **CUDA 自动探测**:存在 `/usr/local/cuda` 时 dspsr 自动启用 CUDA 后端

## 1. 前置条件

| 项目 | 要求 |
| --- | --- |
| 系统 | Debian 系(Deepin 25 / Debian 12+);其他发行版会给出手动安装清单后退出 |
| 权限 | `sudo`(仅 apt-deps 阶段需要 root) |
| 磁盘 | ≥ 20GB 空闲(全栈源码 + 编译产物约 8–15GB) |
| 时间 | 约 20–40 分钟(20 核、`-j20`) |
| 工具 | gcc/make/git(缺失时 apt-deps 会装);`uv` 可选(没有则回退 `python3 -m venv`) |
| 手动下载 | **psrcat tarball**(见 §3 psrcat 阶段;ATNF 下载地址已失效) |

## 2. 快速开始

```bash
# 先看看会做什么(零副作用:不建目录、不 apt、不联网)
bash scripts/install_pulsar_stack.sh --dry-run

# 全栈安装(deps + tempo + tempo2 + pgplot + psrchive + dspsr + psrcat + Python venv)
bash scripts/install_pulsar_stack.sh

# 连 PRESTO 一起装(可选组件,失败只警告不中止)
bash scripts/install_pulsar_stack.sh --with-presto

# 无人值守(CI / 脚本里调用必须加 --yes)
bash scripts/install_pulsar_stack.sh --yes
```

安装完成后:

```bash
source ~/Pulsar/env.sh       # 或者: source scripts/dev_env.sh
tempo2 -v
psrchive-config --version
dspsr --version
psrcat -v
cat ~/Pulsar/share/pulsar-stack/selfcheck.json   # 机器可读的自检报告
```

## 3. 安装阶段

| 阶段 | 内容 | 失败行为 |
| --- | --- | --- |
| `preflight` | `LC_ALL=C`;剔除 conda 的 PATH;检查 gcc/make/git;检查磁盘 ≥ 20GB;建 `$PREFIX/{src,bin,lib,share,logs,.stack,psrcat,tempo2}` | 失败即停 |
| `apt-deps` | build-essential、gfortran、autoconf-archive、libgsl-dev、libglib2.0-dev、libpng-dev、libx11-dev、patch、perl、rsync、ninja-build 等(见脚本内 `APT_PKGS`;**不含** cfitsio/fftw/tcsh,原因见下) | 失败即停 |
| `python-venv` | `uv venv --python 3.12 $PREFIX/venv`,再按 `versions.env` 精确 pin 装 mcp / pydantic / astropy / pint-pulsar / psrqpy / matplotlib / numpy / pytest | 失败即停 |
| `deps` | 源码编译 **fftw 3.2.2 单精度**(`--enable-float` → libfftw3f)与 **cfitsio 3.49 重入版**(`--enable-reentrant` + `make shared`)进前缀。apt 的 libfftw3-dev 只有双精度、libcfitsio-dev 非重入,psrchive 硬依赖这两个变体,必须自建 | 失败即停 |
| `tempo` | 经典 tempo(git.code.sf.net/p/tempo/tempo)→ `autoreconf --install`(上游 `./prepare` 是 csh,内容即此命令)→ configure/make/install | **可选**:失败只警告 |
| `tempo2` | github.com/ipta/tempo2 + checkout pin → **rsync 仓库内 `T2runtime/` 到 `$TEMPO2=$PREFIX/tempo2`**(先于 bootstrap)→ `./bootstrap` → `./configure --prefix --enable-shared` → make/install | 失败即停 |
| `pgplot` | ASTRON 镜像 tarball + psrchive 源码树 `packages/*.patch`;gfortran≥10 需 `-fallow-argument-mismatch`;驱动选 PNG/PostScript/XW;`make cpg` 供 psrplot 等使用 | **可选**:失败只警告 |
| `psrchive` | gitlab pulsar-astronomy + checkout → bootstrap → `./configure --prefix` → make/install(pgplot 已就位则自动启用绘图 CLI) | 失败即停 |
| `dspsr` | git.code.sf.net/p/dspsr/code + checkout → 写 `backends.list`(实测可编译的 28 个后端,单行)→ **打 psrchive API 兼容补丁(幂等)** → **从 psrchive 源码树 `.build-commons/autoconf/` 补齐缺失 m4 宏** → bootstrap → `./configure --prefix F77=gfortran CFLAGS=-fPIC FFLAGS=-fPIC CPPFLAGS="-I$PREFIX/include -I$PREFIX/include/MEAL" TEMPO2="$PREFIX"`(+ CUDA 目录若存在)→ make/install | 失败即停 |
| `psrcat` | 查找本地 tarball(`--psrcat-tarball` → `$SRC_DIR` → `~/Downloads` → cwd)→ 解压 → `gcc -fcommon -O2 -o psrcat *.c -lm`(**无需 tcsh/makeit**)→ 安装 `$PREFIX/bin/psrcat` + `$PREFIX/psrcat/psrcat.db` | 失败即停 |
| `presto` | venv 里装 meson → `meson setup build --prefix` → compile/install → venv 里 `pip install ./python` | **可选**:失败只警告 |
| `self-check` | 逐组件版本探测;写 `$PREFIX/share/pulsar-stack/selfcheck.json`;生成 `env.sh`;打印彩色汇总表 | 失败即停 |

> **configure 期 TEMPO2 与运行期 TEMPO2 是两回事**:dspsr 的 tempo2 宏按
> `$TEMPO2/include` 找头文件,所以 configure 行传 `TEMPO2="$PREFIX"`(安装根);
> 而运行期 `TEMPO2=$PREFIX/tempo2`(T2runtime 数据目录,env.sh 里导出的就是它)。

每个阶段成功后写 `<prefix>/.stack/<stage>.done`,内容包含 **git ref + 参数指纹哈希**。
重跑时只要 URL、pin、configure 标志、包清单都没变,该阶段就直接跳过;改了
`versions.env` 里的 pin 或换 `--prefix` 会自动触发重建。

### 关于 pin

`versions.env` 里 tempo2/psrchive/dspsr 的 `*_REF` 已固定为 **2026-10 实测装机成功的
commit**(Deepin 25 / gcc 12 验证通过),保证可复现。想追新改成 `HEAD` 或别的
commit 即可;临时试用用 `--pin-tempo2 <ref>` 等参数覆盖,不改文件。

## 4. 命令行参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--prefix DIR` | `~/Pulsar`(`$PULSAR_PREFIX` > `$PSRSOFT_DIR`) | 安装前缀 |
| `--jobs N` | `nproc` | 并行编译任务数 |
| `--with-presto` | 关 | 连 PRESTO 一起装(可选阶段) |
| `--without STAGE` | — | 跳过某阶段,可重复,如 `--without tempo --without pgplot` |
| `--only STAGES` | — | 只跑这些阶段(逗号分隔),如 `--only tempo2,psrchive` |
| `--psrcat-tarball PATH` | 自动搜索 | psrcat 本地源码包路径(ATNF 下载地址已失效,必看) |
| `--pin-tempo2 REF` | `versions.env` | 临时覆盖 tempo2 的 pin(`--pin-psrchive/--pin-dspsr/--pin-presto` 同理) |
| `--src-dir DIR` | `<prefix>/src` | 源码存放目录 |
| `--env-file PATH` | `<prefix>/env.sh` | 环境文件输出路径 |
| `--dry-run` | 关 | 只打印完整计划与每条将执行的命令,零副作用 |
| `--yes` / `-y` | 关 | 无人值守:跳过 apt 一次性确认(非交互环境必加) |
| `--no-color` | 关 | 关闭彩色输出(非 tty 自动关闭) |
| `-h` / `--help` | — | 帮助 |

## 5. env.sh 提供什么

`$PREFIX/env.sh`(可用 `--env-file` 改路径)——与常见 PSRSOFT 环境约定一致:

```bash
export PULSAR_PREFIX="$HOME/Pulsar"
export PATH="$PULSAR_PREFIX/bin:$PATH"
export LD_LIBRARY_PATH="$PULSAR_PREFIX/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export PKG_CONFIG_PATH="$PULSAR_PREFIX/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
export PSRSOFT_DIR="$PULSAR_PREFIX"
export TEMPO2="$PULSAR_PREFIX/tempo2"
export TEMPO="$PULSAR_PREFIX/tempo"
export PSRCAT_FILE="$PULSAR_PREFIX/psrcat/psrcat.db"
export PGPLOT_DIR="$PULSAR_PREFIX/pgplot"
export PGPLOT_DEV=/xwindow
```

`scripts/dev_env.sh` 是它的薄封装:再叠加激活仓库 `.venv`(或栈内 venv),适合交互 shell 与契约测试。

> **MCP 服务器不需要 env.sh**:服务器的 `config.toml [backends]` 请直接写绝对路径,不依赖环境变量(见 [DESIGN.md §8.1](DESIGN.md)):

```toml
[backends]
dspsr = "/home/you/Pulsar/bin/dspsr"
psrchive_bin = "/home/you/Pulsar/bin"
tempo2 = "/home/you/Pulsar/bin/tempo2"
psrcat = "/home/you/Pulsar/bin/psrcat"
pint_enabled = true
tempo2_enabled = true
```

## 6. 疑难排查

1. **psrcat 下载失败 / tarball 缺失**
   ATNF 的自动下载地址已失效(2026-10 实测 404),脚本不会联网下载。请到
   ATNF psrcat 页面手动下载 `psrcat_pkg` tarball(实测可用版本 v2.6.1),
   然后 `--psrcat-tarball PATH` 指定,或放到 `~/Downloads` / `--src-dir` 下。
   编译命令是 `gcc -fcommon -o psrcat *.c -lm`(`-fcommon` 是新版 gcc 的硬要求)。
2. **dspsr 编译报 `Signal::Basis` / `Calibration::Faraday` 不存在**
   psrchive ≥ 2026-05 改了 API,安装器的 `patch_dspsr_for_psrchive` 会自动打补丁;
   若上游 dspsr 已跟上,补丁自动跳过(幂等)。报其他未定义符号时看
   `<prefix>/logs/<时间戳>_dspsr.log` 尾部,在补丁函数里补对应 sed 即可。
3. **dspsr configure 报宏缺失(如 `PGPLOT_PROG` / `ac_pkg_swig`)**
   dspsr 的 bootstrap 需要 psrchive 源码树 `.build-commons/autoconf/` 里的 m4 宏,
   安装器的 `copy_psrchive_m4` 自动补;若 psrchive 源码树不在 `$SRC_DIR/psrchive`,
   会告警——确认 `--src-dir` 指向的位置。
4. **pgplot 编译失败(gfortran 传参不匹配)**
   依赖 perl/patch(libpng-dev、libx11-dev 由 apt-deps 装);配置里已加
   `-fallow-argument-mismatch -std=legacy`。上游补丁路径变化时检查
   psrchive 源码树 `packages/pgplot.patch` 是否仍然存在。
5. **T2runtime 缺失**
   tempo2 仓库自带 `T2runtime/`,安装器 rsync 到 `$TEMPO2=$PREFIX/tempo2`;
   若 git 检出不完整(无该目录),tempo2 能跑但没有时钟/星历,需手动补齐。
6. **gitlab / github / sourceforge 网络问题**
   每个仓库都有主地址 + 镜像地址;所有下载都有 3 次重试(退避 2/5/10 秒)。
7. **conda 污染**
   如果日志里 configure 选到了 conda 的 python,检查 `~/miniconda3` 是否在非常规路径
   (脚本只剔除路径名含 `miniconda3|anaconda3|miniforge3|mambaforge` 的目录)。

## 7. 重做单个阶段

阶段 marker 在 `<prefix>/.stack/<stage>.done`,删掉对应文件再重跑即可强制重建:

```bash
rm ~/Pulsar/.stack/psrchive.done         # 只重建 psrchive
bash scripts/install_pulsar_stack.sh --only psrchive --yes
```

也可以用 `--pin-*` 换一个 ref 来触发重建(输入指纹变化会自动判定"需要重建"):

```bash
bash scripts/install_pulsar_stack.sh --only dspsr --pin-dspsr <commit> --yes
```

PRESTO 失败后重试:删除 `<prefix>/.stack/presto.skipped` 与 `<prefix>/.stack/presto.done`,再 `--with-presto` 重跑。

## 8. 与仓库其他部分的关系

- 契约测试:`PSRMCP_CONTRACT=1 uv run pytest tests/contract`,依赖本脚本装好的栈;`scripts/dev_env.sh` 可以一次准备 shell + venv。
- 环境自检工具(`pulsar_environment`)按 `config.toml [backends]` 的绝对路径探测,栈未安装时服务器会优雅降级,不会崩溃。
- 设计背景见 [DESIGN.md](DESIGN.md),工具契约见 [TOOLS.md](TOOLS.md)。
