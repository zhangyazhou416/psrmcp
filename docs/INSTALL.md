# One-shot pulsar software stack installer (install_pulsar_stack.sh)

English | [简体中文](INSTALL-zh.md)

`scripts/install_pulsar_stack.sh` builds the entire pulsar-astronomy toolchain **from source**
into a single prefix (default `~/Pulsar`; an exported `PSRSOFT_DIR` takes precedence, and
`--prefix` overrides both):

- **No Docker, no conda-forge** (all C/C++/Fortran tools: system deps + source builds)
- **Every source location is field-verified** (2026-10): tempo2 = github ipta, dspsr = SourceForge,
  psrchive = gitlab pulsar-astronomy, psrcat = local tarball + `gcc -fcommon`,
  pgplot = ASTRON mirror; dead locations such as ATNF/Caltech are no longer attempted
- **psrchive/dspsr API drift is handled in the installer**: psrchive ≥ 2026-05 renames such as
  `Signal::* → Convention::*` and `Calibration::Faraday → MEAL::Faraday`;
  the installer patches dspsr idempotently (skipped once the pattern disappears, safe to re-run)
- **conda is isolated automatically**: before building, `~/miniconda3`, `~/anaconda3`, `~/mambaforge`
  are stripped from `PATH` so conda's `python` / `libstdc++` cannot interfere with `configure`
- **CUDA auto-detection**: when `/usr/local/cuda` exists, dspsr automatically enables its CUDA backend

## 1. Prerequisites

| Item | Requirement |
| --- | --- |
| System | Debian-based (Deepin 25 / Debian 12+); other distros print a manual install list and exit |
| Privileges | `sudo` (root only needed for the apt-deps stage) |
| Disk | ≥ 20GB free (full-stack sources + build artifacts ≈ 8–15GB) |
| Time | ≈ 20–40 minutes (20 cores, `-j20`) |
| Tools | gcc/make/git (apt-deps installs them if missing); `uv` optional (falls back to `python3 -m venv`) |
| Manual download | **psrcat tarball** (see the psrcat stage in §3; the ATNF download URL is dead) |

## 2. Quick start

```bash
# See what it would do first (zero side effects: no directories, no apt, no network)
bash scripts/install_pulsar_stack.sh --dry-run

# Full stack (deps + tempo + tempo2 + pgplot + psrchive + dspsr + psrcat + Python venv)
bash scripts/install_pulsar_stack.sh

# Include PRESTO (optional component; failure only warns, never aborts)
bash scripts/install_pulsar_stack.sh --with-presto

# Unattended (CI / script calls must pass --yes)
bash scripts/install_pulsar_stack.sh --yes
```

After the install completes:

```bash
source ~/Pulsar/env.sh       # or: source scripts/dev_env.sh
tempo2 -v
psrchive-config --version
dspsr --version
psrcat -v
cat ~/Pulsar/share/pulsar-stack/selfcheck.json   # machine-readable self-check report
```

## 3. Install stages

| Stage | What it does | On failure |
| --- | --- | --- |
| `preflight` | `LC_ALL=C`; strips conda from PATH; checks gcc/make/git; checks disk ≥ 20GB; creates `$PREFIX/{src,bin,lib,share,logs,.stack,psrcat,tempo2}` | fail aborts |
| `apt-deps` | build-essential, gfortran, autoconf-archive, libgsl-dev, libglib2.0-dev, libpng-dev, libx11-dev, patch, perl, rsync, ninja-build, etc. (see `APT_PKGS` in the script; **excludes** cfitsio/fftw/tcsh — reason below) | fail aborts |
| `python-venv` | `uv venv --python 3.12 $PREFIX/venv`, then installs mcp / pydantic / astropy / pint-pulsar / psrqpy / matplotlib / numpy / pytest at the exact pins from `versions.env` | fail aborts |
| `deps` | builds **fftw 3.2.2 single-precision** (`--enable-float` → libfftw3f) and **cfitsio 3.49 reentrant** (`--enable-reentrant` + `make shared`) from source into the prefix. apt's libfftw3-dev is double-precision only and libcfitsio-dev is non-reentrant; psrchive hard-depends on these two variants, so both must be self-built | fail aborts |
| `tempo` | classic tempo (git.code.sf.net/p/tempo/tempo) → `autoreconf --install` (upstream's `./prepare` is a csh script whose content is exactly this command) → configure/make/install | **optional**: failure only warns |
| `tempo2` | github.com/ipta/tempo2 + checkout pin → **rsyncs the repo's `T2runtime/` to `$TEMPO2=$PREFIX/tempo2`** (before bootstrap) → `./bootstrap` → `./configure --prefix --enable-shared` → make/install | fail aborts |
| `pgplot` | tarball from the ASTRON mirror + `packages/*.patch` from the psrchive source tree; gfortran≥10 needs `-fallow-argument-mismatch`; drivers: PNG/PostScript/XW; `make cpg` for psrplot and friends | **optional**: failure only warns |
| `psrchive` | gitlab pulsar-astronomy + checkout → bootstrap → `./configure --prefix` → make/install (plotting CLIs enabled automatically if pgplot is in place) | fail aborts |
| `dspsr` | git.code.sf.net/p/dspsr/code + checkout → writes `backends.list` (the 28 backends observed to compile, one line) → **applies psrchive API compatibility patches (idempotent)** → **tops up missing m4 macros from the psrchive tree's `.build-commons/autoconf/`** → bootstrap → `./configure --prefix F77=gfortran CFLAGS=-fPIC FFLAGS=-fPIC CPPFLAGS="-I$PREFIX/include -I$PREFIX/include/MEAL" TEMPO2="$PREFIX"` (+ CUDA dirs if present) → make/install | fail aborts |
| `psrcat` | finds a local tarball (`--psrcat-tarball` → `$SRC_DIR` → `~/Downloads` → cwd) → extracts → `gcc -fcommon -O2 -o psrcat *.c -lm` (**no tcsh/makeit needed**) → installs `$PREFIX/bin/psrcat` + `$PREFIX/psrcat/psrcat.db` | fail aborts |
| `presto` | installs meson into the venv → `meson setup build --prefix` → compile/install → `pip install ./python` in the venv | **optional**: failure only warns |
| `self-check` | probes each component's version; writes `$PREFIX/share/pulsar-stack/selfcheck.json`; generates `env.sh`; prints a colored summary table | fail aborts |

> **The configure-time TEMPO2 and the runtime TEMPO2 are two different things**: dspsr's tempo2
> macro looks for headers under `$TEMPO2/include`, so the configure line passes
> `TEMPO2="$PREFIX"` (the install root); at runtime `TEMPO2=$PREFIX/tempo2` (the T2runtime data
> directory — that is what env.sh exports).

On success each stage writes `<prefix>/.stack/<stage>.done`, containing the **git ref + a hash of
the input fingerprint**. On re-runs, as long as the URL, pin, configure flags, and package list
are unchanged, the stage is skipped outright; changing a pin in `versions.env` or switching
`--prefix` automatically triggers a rebuild.

### About pins

The `*_REF` values for tempo2/psrchive/dspsr in `versions.env` are pinned to **commits verified in
a real 2026-10 install** (validated on Deepin 25 / gcc 12) for reproducibility. To track newer
changes, set them to `HEAD` or another commit; for a temporary trial, override with
`--pin-tempo2 <ref>` and friends without touching the file.

## 4. Command-line options

| Option | Default | Description |
| --- | --- | --- |
| `--prefix DIR` | `~/Pulsar` (`$PULSAR_PREFIX` > `$PSRSOFT_DIR`) | install prefix |
| `--jobs N` | `nproc` | parallel build jobs |
| `--with-presto` | off | also install PRESTO (optional stage) |
| `--without STAGE` | — | skip a stage; repeatable, e.g. `--without tempo --without pgplot` |
| `--only STAGES` | — | run only these stages (comma-separated), e.g. `--only tempo2,psrchive` |
| `--psrcat-tarball PATH` | auto-search | path to the local psrcat source package (the ATNF download URL is dead — read this) |
| `--pin-tempo2 REF` | `versions.env` | temporarily override tempo2's pin (`--pin-psrchive`/`--pin-dspsr`/`--pin-presto` work the same way) |
| `--src-dir DIR` | `<prefix>/src` | where sources are stored |
| `--env-file PATH` | `<prefix>/env.sh` | where to write the env file |
| `--dry-run` | off | print the full plan and every command that would run; zero side effects |
| `--yes` / `-y` | off | unattended: skip the one-time apt confirmation (required in non-interactive environments) |
| `--no-color` | off | disable colored output (auto-disabled when not a tty) |
| `-h` / `--help` | — | show help |

## 5. What env.sh provides

`$PREFIX/env.sh` (path changeable via `--env-file`) — follows the common PSRSOFT environment convention:

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

`scripts/dev_env.sh` is a thin wrapper around it: on top it activates the repo's `.venv` (or the
stack venv), suited to interactive shells and contract tests.

> **The MCP server does not need env.sh**: write absolute paths directly in the server's
> `config.toml [backends]`, without depending on environment variables (see [DESIGN.md §8.1](DESIGN.md)):

```toml
[backends]
dspsr = "/home/you/Pulsar/bin/dspsr"
psrchive_bin = "/home/you/Pulsar/bin"
tempo2 = "/home/you/Pulsar/bin/tempo2"
psrcat = "/home/you/Pulsar/bin/psrcat"
pint_enabled = true
tempo2_enabled = true
```

## 6. Troubleshooting

1. **psrcat download fails / tarball missing**
   ATNF's automatic download URL is dead (404 in practice as of 2026-10); the script does not
   download it from the network. Download the `psrcat_pkg` tarball manually from the ATNF psrcat
   page (v2.6.1 verified working), then pass it via `--psrcat-tarball PATH`, or drop it under
   `~/Downloads` / `--src-dir`. The build command is `gcc -fcommon -o psrcat *.c -lm`
   (`-fcommon` is a hard requirement on newer gcc).
2. **dspsr build reports `Signal::Basis` / `Calibration::Faraday` missing**
   psrchive ≥ 2026-05 changed the API; the installer's `patch_dspsr_for_psrchive` patches
   automatically. If upstream dspsr has caught up, the patch is skipped (idempotent). For other
   undefined symbols, look at the tail of `<prefix>/logs/<timestamp>_dspsr.log` and add the
   corresponding sed to the patch function.
3. **dspsr configure reports missing macros (e.g. `PGPLOT_PROG` / `ac_pkg_swig`)**
   dspsr's bootstrap needs the m4 macros in the psrchive source tree's `.build-commons/autoconf/`;
   the installer's `copy_psrchive_m4` tops them up automatically. If the psrchive tree is not at
   `$SRC_DIR/psrchive`, it warns — check where `--src-dir` points.
4. **pgplot build fails (gfortran argument-mismatch errors)**
   Needs perl/patch (libpng-dev and libx11-dev come from apt-deps); the config already adds
   `-fallow-argument-mismatch -std=legacy`. If upstream's patch paths change, check that the
   psrchive source tree's `packages/pgplot.patch` still exists.
5. **T2runtime missing**
   The tempo2 repo ships `T2runtime/`, which the installer rsyncs to `$TEMPO2=$PREFIX/tempo2`;
   if the git checkout is incomplete (no such directory), tempo2 runs but has no clock/ephemeris
   data — fill it in manually.
6. **gitlab / github / sourceforge network problems**
   Every repo has a primary URL + mirror URL; all downloads retry 3 times (backoff 2/5/10 seconds).
7. **conda pollution**
   If the logs show configure picking up conda's python, check whether `~/miniconda3` lives at an
   unusual path (the script only strips directories whose path name contains
   `miniconda3|anaconda3|miniforge3|mambaforge`).

## 7. Re-running a single stage

Stage markers live at `<prefix>/.stack/<stage>.done`; delete the file for a stage and re-run to
force a rebuild:

```bash
rm ~/Pulsar/.stack/psrchive.done         # rebuild psrchive only
bash scripts/install_pulsar_stack.sh --only psrchive --yes
```

You can also trigger a rebuild by switching the ref with `--pin-*` (a changed input fingerprint is
automatically judged as "needs rebuild"):

```bash
bash scripts/install_pulsar_stack.sh --only dspsr --pin-dspsr <commit> --yes
```

Retrying after a PRESTO failure: delete `<prefix>/.stack/presto.skipped` and `<prefix>/.stack/presto.done`, then re-run with `--with-presto`.

## 8. Relation to the rest of the repo

- Contract tests: `PSRMCP_CONTRACT=1 uv run pytest tests/contract` depend on the stack installed
  by this script; `scripts/dev_env.sh` prepares shell + venv in one go.
- The environment self-check tool (`pulsar_environment`) probes the absolute paths in
  `config.toml [backends]`; when the stack is not installed the server degrades gracefully and
  never crashes.
- Design background: [DESIGN.md](DESIGN.md); tool contracts: [TOOLS.md](TOOLS.md).
