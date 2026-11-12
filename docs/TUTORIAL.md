# psrmcp Tutorial

English | [简体中文](TUTORIAL-zh.md)

This tutorial takes you from zero to a real pulsar data processing run: install → configure → connect an AI client → complete a real pulsar data reduction.

Two usage styles run throughout:

1. **Natural-language driven (recommended)**: once Claude Code / Claude Desktop is connected, just describe what you need in plain language and the AI selects and chains the tools for you;
2. **Direct invocation**: parameter examples for every tool are given alongside, so you can call them precisely from other MCP clients or scripts.

Read first: [FEATURES.md](FEATURES.md) (what it does, all 25 tools at a glance); for field-by-field schemas see [TOOLS.md](TOOLS.md).

## 1. Installation

### 1.1 Pulsar software stack (optional but recommended)

```bash
bash scripts/install_pulsar_stack.sh            # builds everything from source into ~/.pulsar
# common flags: --prefix ~/Pulsar for a custom install location; --with-presto to also build PRESTO;
#           --jobs N for parallelism; resumable, safe to re-run
```

Once installed, `source ~/.pulsar/env.sh` in an interactive terminal to get PATH/TEMPO2/PSRCAT_FILE.
**It runs without the stack too**: the server degrades gracefully, and `pulsar_environment` reports the reason each backend is missing.

### 1.2 The server itself

```bash
git clone <this repo> psrmcp && cd psrmcp
uv sync                                         # Python 3.12+, dependencies go into .venv
uv run psrmcp --init-config ~/.psrmcp.toml      # generates a commented default config
```

### 1.3 Configuration file

Configuration is a single TOML file (the `PSRMCP_CONFIG` environment variable sets the path; `~/.psrmcp.toml` is used if unset). Once the stack is installed, write absolute paths explicitly — a stdio cold start has no shell environment and does not rely on env.sh:

```toml
[server]
workspace = "~/.psrmcp/workspace"   # where artifacts and the job store live; job_timeout_s caps each task

[data]
read_roots = [                      # ★ security boundary: the server can only read data under these directories
  "~/psrdata",
]
allow_absolute_paths = false        # only relative paths inside read_roots are allowed

[backends]
# if omitted, detected from PATH; absolute paths free you from env.sh
dspsr = "~/.pulsar/bin/dspsr"
psrchive_bin = "~/.pulsar/bin"      # directory holding vap/psrstat/pam/pat/psradd/paz
tempo2 = "~/.pulsar/bin/tempo2"
psrcat = "~/.pulsar/bin/psrcat"
pint_enabled = true

[catalog]
mode = "local"                      # local psrcat.db (works offline)
database = "~/.pulsar/psrcat/psrcat.db"
```

Key points:

- **read_roots is the only gateway to your data**: every tool's path arguments resolve inside these directories; `..`, absolute paths, and symlink escapes are all rejected (`E_PATH_DENIED`);
- workspace is the only writable area; artifacts are managed under `art_` IDs, so your data directories stay unpolluted.

## 2. Connecting an AI client

### Claude Code (command line)

```bash
claude mcp add psrmcp \
  -e PSRMCP_CONFIG=~/.psrmcp.toml \
  -- uv --directory /path/to/psrmcp run psrmcp
```

Open a new session and use `/mcp` to confirm `psrmcp` is connected.

### Claude Desktop / other clients (JSON)

```json
{
  "mcpServers": {
    "psrmcp": {
      "command": "uv",
      "args": ["--directory", "/path/to/psrmcp", "run", "psrmcp"],
      "env": { "PSRMCP_CONFIG": "~/.psrmcp.toml" }
    }
  }
}
```

### Verify the connection

Say to the AI:

> Check the pulsar processing environment

The AI calls `pulsar_environment` and comes back with something like:

```
**Server** psrmcp v0.1.0 (MCP 2026-07-28)
**Available backends**: dspsr, pint, psrcat, psrchive, tempo2
**Data directories**: ~/psrdata
```

If a backend is missing you will see a reason for each missing item (e.g. `dspsr: not found in PATH`); just install it as prompted.

## 3. Walkthrough 1: catalog queries (no observational data needed)

A good way to get familiar with the tools first. Say to the AI:

> Look up the basic parameters of millisecond pulsar J0437-4715, then give me 5 pulsars with periods under 5 ms

The corresponding tool calls:

```json
→ catalog_get  {"name": "J0437-4715"}
→ catalog_search {"p0_max_s": 0.005, "limit": 5,
                  "fields": ["name", "p0_s", "dm", "binary"]}
```

Export an ephemeris for timing (a par file, which becomes a reusable artifact):

```json
→ ephemeris_export {"pulsar": "J0437-4715", "format": "tempo2"}
← {"artifact": {"artifact_id": "art_...", ...}}
```

- In later tools the `ephemeris` parameter **accepts all three forms**: this artifact ID, a `.par` relative path under read_roots, or a bare pulsar name (looked up automatically and materialized into a par file);
- When the observing position differs from the catalogue, use `overrides` to override RA/Dec/DM/PEPOCH and other fields; the override is recorded in provenance.

## 4. Walkthrough 2: the complete processing chain for one observation

This is the core workflow. Each step below shows what you say → the tool the AI calls → what you get back. The numbers come from a real end-to-end run of this project (synth.fil: Parkes 1.4 GHz, 128 channels, DM 2.645).

> **No observational data?** `uv run python scripts/make_synth_fil.py` generates a synthetic filterbank (`ws/synth.fil`, with a dispersed pulse at DM 2.645 embedded) for practicing the whole flow.

### Step 1: discover data

> See what's in the data directory

```json
→ data_find {"pattern": "**/*.fil"}
← list of matches (paths, sizes, type identification)
```

### Step 2: dedisperse and fold

> Dedisperse and fold synth.fil into PSRFITS, targeting J0437-4715

```json
→ dedisperse {"input": "synth.fil", "ephemeris": "J0437-4715",
              "mode": "incoherent", "output_format": "psrfits"}
← {"artifact": {"artifact_id": "art_..."}, "dm_used": 2.645, "dimensions": {...}}
```

- DM is read automatically from the ephemeris's `DM` line; you can also set it explicitly with `dm_override` (recorded as a warning and in provenance);
- filterbank input only supports incoherent dedispersion; mistakenly setting `mode: coherent` degrades automatically and emits a warning;
- This is a long task; past a threshold it turns async automatically (see section 7), and the AI waits on its own.

### Step 3: inspect the folded result

> Show the basic info of this archive

```json
→ archive_inspect {"archive": "art_..."}
← header info such as nbin/nchan/npol/nsubint, the band, MJD
```

### Step 4: remove RFI

> First scan for interference, and clean it up if there is any

```json
→ rfi_scan  {"archive": "art_..."}                     # per-channel statistics, flags suspicious channels
→ rfi_zap   {"archive": "art_...", "mask": {"channels": [0, 1, 2]}}
```

Don't want to pick channels by hand? Use auto-flagging:

```json
→ rfi_zap {"archive": "art_...",
           "mask": {"auto": {"method": "psrchive_median", "threshold_sigma": 3.0}}}
```

### Step 5: standard profile and measurements

> Build a standard profile, then measure SNR and pulse width

```json
→ profile_standardize {"archive": "art_..."}           # → standard profile artifact + profile plot
→ profile_measure  {"archive": "art_...", "metrics": ["snr", "width", "flux"]}
← {"snr": 133.9, "w50": ..., "plot": {"artifact_id": "art_..."}}
```

Remember the standard profile artifact ID — the next step needs it (the AI chains this automatically).

### Step 6: extract TOAs

> Extract TOAs using this standard profile

```json
→ toa_extract {"archive": "art_...", "standard_profile": "art_...",
               "algorithm": "PGS", "min_snr": 8}
← {"n_toas": 128, "tim_artifact": {"artifact_id": "art_..."}}
```

The output is a `.tim` file (algorithm can be GIS/PIS/ZPS/SIS/FDM; `mjd_range`/`freq_range_mhz` select a subset).

### Step 7: PINT timing fit

> Fit these TOAs with the J0437-4715 ephemeris

```json
→ timing_fit {"par": "art_<ephemeris>", "tim": "art_<tim>",
              "backend": "pint", "weighting": "1/sigma^2"}
← {"fit": {"reduced_chi2": ..., "wrms_us": ...},
   "parameters": [...], "plot": {...}, "fitted_par": {...}}
```

- Free parameters default to the fit flags in the par file; you can also specify them explicitly with `fit_params: ["F0", "F1", "DM"]`;
- Deliverables: a parameter table (value/uncertainty/fitted-or-not), a residual plot PNG, and the fitted par file.

### Step 8: residual diagnostics

> Any problems with the residuals?

```json
→ timing_diagnose {"par": "art_<fitted par>", "tim": "art_<tim>"}
← {"summary": {"n_outliers": 3, "reduced_chi2": ...},
   "findings": [...], "recommendations": [...]}
```

It outputs structured conclusions: a list of outlier TOAs (>5σ, MAD-based), an interpretation of reduced χ² that is too high/low, whether JUMP/DMX is needed, a note on residual skew — each with a recommended action.

## 5. Walkthrough 3: everyday archive processing

```json
→ archive_convert {"archive": "synth.fil", "output_format": "psrfits"}
→ archive_merge   {"archives": ["art_a", "art_b"], "weighting": "time"}
```

- merge defaults to `require_same_source: true` and rejects mismatched origins outright (`E_SOURCE_MISMATCH`), preventing wrong merges;
- When the input is a degenerate/heterogeneous format, a conversion or downstream dspsr failure returns `E_PROCESS_FAILED` with an attached log artifact (see section 8).

## 6. Artifact and job management

- **Everything is an artifact**: every step's output carries an `art_` ID; just reference IDs across tools, no need to care where files land. `artifact_list {"kind": "archive"}` takes stock at any time;
- **Traceable lineage**: artifact provenance records the full argv, parameters, backend versions, and parent artifact IDs — three months later it can still answer "how was this plot produced";
- **Long tasks**: past a threshold they turn async automatically and return a `job_id`; the AI usually polls on its own, but you can also check progress manually with `job_get {"job_id": "..."}` or abort with `job_cancel` (process-group level, no zombie processes left); after a server restart, unfinished jobs are marked interrupted and are never falsely reported as succeeded.

## 7. Prompting tips

The AI is facing 25 tools with strict schemas, so **describe the goal, not the steps** — that usually works best:

| What you say | What the AI does |
|---|---|
| "Process last night's observation in ~/psrdata, targeting J0437-4715" | Finds the data → looks up the ephemeris → folds → removes RFI → produces measurements, all chained automatically end to end |
| "Just measure the SNR, don't touch the data yet" | Calls only profile_measure, no side effects |
| "Convert that archive from just now to PSRARCHIVE format" | Calls archive_convert with the artifact ID from earlier |
| "The fit result looks off, help me diagnose it" | Runs the full timing_diagnose checks and interprets the findings |

- Refer to earlier artifacts in natural language ("the standard profile from just now"); the AI supplies the correct ID;
- If unsure, have the AI run the read-only tools first (`data_find` / `archive_inspect` / `rfi_scan`), take a look, then decide;
- Every side-effecting operation (writing files, overriding parameters) is called out explicitly in the result via warnings/provenance; nothing happens silently.

## 8. When things go wrong

Errors are structured envelopes, not bare tracebacks:

```json
{"code": "E_PATH_DENIED", "message": "path out of bounds: ../secret",
 "hint": "only relative paths inside read_roots are allowed", "retryable": false}
```

| code | Meaning | Action |
|---|---|---|
| `E_PATH_DENIED` | Path escapes read_roots | Add the data directory to read_roots in the config |
| `E_NOT_FOUND` | File/artifact/pulsar does not exist | Check the name; artifact IDs can be looked up with artifact_list |
| `E_ENV_MISSING` | Backend not installed | Install it per the reason, or source env.sh and re-probe with `refresh: true` |
| `E_PROCESS_FAILED` | Backend command failed | Look at the log artifact pointed to by `log_artifact_id` (tail of stderr plus the full argv) |
| `E_INVALID_INPUT` | Invalid parameters | Fix per the message; schemas in TOOLS.md |
| `E_QUOTA_EXCEEDED` | workspace over quota | Clean up old artifacts or raise limits |
| `EPHEMERIS_MISMATCH` | Ephemeris does not match the data (e.g. no DM) | Provide a .par or use dm_override |

Warnings (non-blocking): `W_DM_OVERRIDE` (DM was explicitly overridden), `W_COHERENT_UNAVAILABLE` (filterbank automatically degraded to incoherent), and so on — all carried through in the result as-is.

## 9. Self-checks and regression

```bash
uv run pytest -q                                   # unit tests, no backends required
PSRMCP_CONTRACT=1 uv run pytest tests/contract     # contract tests, needs the installed stack
uv run python examples/client_stdio.py             # stdio smoke test (19-tool handshake)
PSRMCP_CONFIG=$PWD/config.toml uv run python scripts/live_check.py
                                                   # 9-step end-to-end on the real stack (needs data + stack)
```

## Related documents

- [FEATURES.md](FEATURES.md) — feature overview
- [DESIGN.md](DESIGN.md) — architecture and execution model
- [TOOLS.md](TOOLS.md) — full JSON Schema contracts for all 25 tools
- [INSTALL.md](INSTALL.md) — details of the one-click software stack installer
