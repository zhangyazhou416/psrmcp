# Pulsar Data Processing MCP Server Design Document

English | [简体中文](DESIGN-zh.md)

- Document version: v0.1
- Date: 2026-10-17
- Status: design draft
- Scope: architecture and interface design only; implementation code is out of scope

---

## 1. Positioning and Boundaries

### 1.1 Positioning

An MCP Server that wraps astronomy tools such as `dspsr`, `psrchive`, `PINT`, `tempo2`, and `psrcat` into **task-oriented, parameter-controlled, structured-result** MCP Tools, while also providing Resources and Prompts. Both local stdio and remote Streamable HTTP deployments are supported.

### 1.2 Core goals

- The LLM can complete typical workflows: catalog lookup → data inspection → dedispersion → RFI → standard profile → TOA → timing fit → residual diagnostics
- All operations are traceable: record tool versions, input hashes, command parameters, and output artifacts
- Long-running tasks can run asynchronously, with progress queries and cancellation
- Raw data is not modified by default; all results are written as new immutable artifacts
- Strict limits on paths, commands, and resource usage

### 1.3 Non-goals (not in the first phase)

- No arbitrary shell access / arbitrary CLI flag passthrough
- No real-time observation control
- No search-mode single-pulse pipeline for now (PRESTO, heimdall, etc.; can be extended later)
- No polarization calibration or flux calibration for now (depends on the specific observing system; deferred to a later phase)
- Does not replace expert scientific judgment

---

## 2. Key Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Development language | Python 3.11+ | PINT is native Python; psrchive has Python bindings; the astropy/numpy/matplotlib ecosystem is mature |
| MCP SDK | Official Python SDK / FastMCP | Full support for tools/resources/prompts, progress, cancellation, and structured output |
| Tool wrapping | CLI adapters primarily, Python bindings as a supplement | CLI behavior is stable and reproducible; Python bindings are used for reading, statistics, and plotting |
| PINT integration | Separate subprocess worker | Avoids blocking the event loop, isolates crashes and memory issues, and makes cancellation easy |
| Tool granularity | Task-level tools; not all CLI flags are exposed | Reduces the chance of the LLM picking the wrong tool/parameter; easier to validate and audit |
| Data products | Immutable artifacts | Raw data stays read-only; results are traceable, cacheable, and reproducible |
| Long-running tasks | MCP Tasks extension preferred, job polling as fallback | The specification supports async tasks; still usable when clients lack support |
| Toolchain | Container image with pinned versions | dspsr/psrchive/tempo2 dependencies are complex; containers are the most controllable |
| Security model | Path whitelist + argv arrays + resource limits | Prevents path traversal, command injection, and resource exhaustion |

Machine survey result (2026-10-17): the project directory is empty; `dspsr`, `psrchive`, `PINT`, `tempo2`, and `psrcat` are not installed, and only `astropy/numpy/matplotlib` are available. Phase 0 must therefore first pin the toolchain down, either by containerizing it or by installing it via conda-forge / Spack / from source.

---

## 3. Overall Architecture

```
┌──────────────────────────────────────────────────────────────┐
│ MCP Client (Claude Code / Desktop / Web)                     │
│  tools/call · resources/read · prompts/get                   │
└──────────────────────────────┬───────────────────────────────┘
                               │ stdio / Streamable HTTP
┌──────────────────────────────▼───────────────────────────────┐
│ MCP Server (FastMCP)                                         │
│  · Input validation (Pydantic)                               │
│  · Tool annotations                                          │
│  · Progress / cancellation / Tasks extension                 │
└──────────────────────────────┬───────────────────────────────┘
                               │
┌──────────────────────────────▼───────────────────────────────┐
│ Domain Services                                              │
│  Catalog · Archive · Dedispersion · RFI                      │
│  Profile · Timing · Artifact · Job                           │
└──────────────────────────────┬───────────────────────────────┘
                               │
┌──────────────────────────────▼───────────────────────────────┐
│ Adapters                                                     │
│  dspsr │ psrchive │ PINT worker │ tempo2 │ psrcat            │
└──────────────────────────────┬───────────────────────────────┘
                               │
┌──────────────────────────────▼───────────────────────────────┐
│ Execution Layer                                              │
│  ProcessRunner (argv, timeouts, process group, limits)       │
│  JobManager · Workspace · ArtifactStore                      │
│  SQLite (jobs / artifacts / cache index)                     │
└──────────────────────────────────────────────────────────────┘
```

### 3.1 Recommended directory layout

```
psrmcp/
├── pyproject.toml
├── src/psrmcp/
│   ├── server.py                 # MCP entry point
│   ├── config.py                 # config loading and validation
│   ├── tools/
│   │   ├── system.py
│   │   ├── data.py
│   │   ├── catalog.py
│   │   ├── archive.py
│   │   ├── dedispersion.py
│   │   ├── rfi.py
│   │   ├── profile.py
│   │   ├── timing.py
│   │   └── artifacts.py
│   ├── resources.py
│   ├── prompts.py
│   ├── adapters/
│   │   ├── base.py
│   │   ├── dspsr.py
│   │   ├── psrchive.py
│   │   ├── pint.py
│   │   ├── tempo2.py
│   │   └── psrcat.py
│   ├── execution/
│   │   ├── runner.py
│   │   ├── jobs.py
│   │   ├── tasks.py
│   │   └── workspace.py
│   ├── models/                   # Pydantic output models
│   ├── plotting/
│   └── workers/pint_worker.py
├── tests/
├── docker/
└── docs/
```

---

## 4. MCP Interface Design

### 4.1 Tools overview

| Group | Tool | Purpose | Backend | Type | Long task |
|---|---|---|---|---|---|
| System | `pulsar_environment` | Returns tool versions, available backends, configuration, and limits | All | Read-only | No |
| Data | `data_find` | Find data files under whitelisted directories | Filesystem | Read-only | No |
| Catalog | `catalog_search` | Query by name, position, period, DM, binary status, etc. | psrcat / psrqpy | Read-only | No |
| Catalog | `catalog_get` | Fetch a single pulsar record and its aliases | psrcat / psrqpy | Read-only | No |
| Catalog | `ephemeris_export` | Export a `.par` in TEMPO/TEMPO2 format | psrcat / PINT | Produces artifact | No |
| Archive | `archive_inspect` | Read PSRFITS/PSRARCHIVE metadata and statistics | psrchive | Read-only | No |
| Archive | `archive_convert` | Format conversion | psrconv | Produces artifact | No |
| Archive | `archive_merge` | Merge multiple archives | psradd | Produces artifact | Size-dependent |
| Dedispersion | `dedisperse` | Coherent/incoherent dedispersion and folding | dspsr | Produces artifact | Yes |
| RFI | `rfi_scan` | Identify bad channels and suggest a mask | psrchive / clfd | Read-only | No |
| RFI | `rfi_zap` | Apply a channel mask | psrchive / clfd | Produces artifact | No |
| Profile | `profile_standardize` | Generate a standard profile | pas | Produces artifact | No |
| Profile | `profile_measure` | Measure S/N, width, flux, etc. | psrstat / vap | Read-only | No |
| Timing | `toa_extract` | Extract TOAs from an archive | pat | Produces artifact | Data-volume-dependent |
| Timing | `timing_fit` | PINT/tempo2 timing fit | PINT / tempo2 | Produces artifact | Yes |
| Timing | `timing_diagnose` | Residual, outlier, DM, and JUMP diagnostics | PINT | Read-only | No |
| Artifacts | `artifact_list` | List artifacts in the workspace | ArtifactStore | Read-only | No |
| Jobs | `job_get` | Query async job status (fallback) | JobManager | Read-only | No |
| Jobs | `job_cancel` | Cancel an async job (fallback) | JobManager | Write | No |

> Tool names should stay concise; if a client aggregates multiple MCP servers, a uniform `pulsar_` prefix can be added to avoid conflicts.

### 4.2 Representative tool contracts

#### `archive_inspect`

```json
{
  "name": "archive_inspect",
  "description": "Read PSRFITS/PSRARCHIVE metadata and basic statistics without modifying files. Call this tool before processing to confirm the data content and format.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "archive": {
        "type": "string",
        "description": "artifact ID, or a path relative to a configured directory"
      },
      "include_stats": {"type": "boolean", "default": true}
    },
    "required": ["archive"],
    "additionalProperties": false
  }
}
```

Output structure:

```json
{
  "format": "PSRFITS",
  "source": {"name": "J0437-4715", "ra_deg": 69.316, "dec_deg": -47.252},
  "observation": {
    "telescope": "Parkes",
    "receiver": "...",
    "backend": "...",
    "start_mjd": 59000.1,
    "end_mjd": 59000.2,
    "duration_s": 3600,
    "freq_mhz": 1369.5,
    "bandwidth_mhz": 64
  },
  "dimensions": {"nsubint": 32, "nchan": 512, "npol": 4, "nbin": 1024},
  "timing": {"dm": 2.644, "period_s": 0.005757, "ephemeris_present": true},
  "warnings": [],
  "provenance": {"reader": "psrchive", "version": "..."}
}
```

#### `dedisperse`

Core parameters:

- `input`: raw data or an archive
- `ephemeris`: a `.par` artifact or `pulsar_name`
- `mode`: `coherent` / `incoherent`
- `dm_override`: optional; explicitly overrides the DM and is recorded in provenance
- `output_format`: `psrfits` / `psrarchive`
- `nsubint`, `nchan`, `nbin`: optional output dimensions
- `threads`: bounded by the configured maximum
- `output_name`: optional

Output:

```json
{
  "artifact": {"artifact_id": "art_...", "kind": "archive", "format": "PSRFITS"},
  "dm_used": 2.644,
  "dimensions": {"nsubint": 64, "nchan": 512, "nbin": 1024},
  "warnings": [],
  "log_artifact": "art_..."
}
```

Long task: returns a task handle when the client supports the Tasks extension; otherwise returns a `job_id` to poll via `job_get`.

#### `timing_fit`

Core parameters:

- `par`: `.par` artifact
- `tim`: `.tim` artifact
- `backend`: `pint` / `tempo2`
- `fit_params`: optional; defaults to the fit flags in the par file
- `weighting`: `uniform` / `1/sigma^2`
- `generate_plot`: whether to generate a residual plot
- `max_iterations`: iteration limit

Output:

```json
{
  "par_artifact": "art_...",
  "fit": {
    "n_toas": 1200,
    "chi2": 1150.2,
    "dof": 1180,
    "reduced_chi2": 0.975,
    "rms_us": 1.8,
    "wrms_us": 2.1
  },
  "parameters": [
    {"name": "F0", "value": 173.68, "uncertainty": 1e-10, "fitted": true}
  ],
  "residuals_artifact": "art_...",
  "plot_artifacts": ["art_..."],
  "warnings": []
}
```

### 4.3 Tool design rules

Every tool must satisfy:

- **Clear description**: state when to use it, preconditions, side effects, and whether it is a long task
- **Controlled parameters**: explicit enums, ranges, and units; no arbitrary CLI flags accepted
- **Structured output**: `structuredContent` plus a short text summary
- **Bounded results**: paginated lists, downsampled images, JSON results capped at 256 KB by default
- **No input overwriting**: every operation produces a new artifact
- **Accurate annotations**:
  - Read-only tools: `readOnlyHint: true`
  - Artifact deletion only: `destructiveHint: true`
  - Same input always yields the same output: `idempotentHint: true`
  - Online catalog queries only: `openWorldHint: true`

Do not provide catch-all tools such as `run_raw_command`. If advanced parameters are genuinely needed, use a whitelisted `advanced` object rather than raw strings.

---

## 5. Resources and Prompts

### 5.1 Resources

| URI | Content |
|---|---|
| `pulsar://environment` | Environment, versions, available backends, limits |
| `pulsar://catalog/{name}` | Catalog record JSON |
| `pulsar://ephemeris/{name}` | TEMPO/TEMPO2 `.par` text |
| `pulsar://artifact/{artifact_id}` | Artifact metadata JSON |
| `pulsar://artifact/{artifact_id}/log` | Job log text |
| `pulsar://artifact/{artifact_id}/preview` | Image/preview blob |
| `pulsar://job/{job_id}` | Job status JSON |
| `pulsar://docs/{topic}` | Usage guides, e.g. reduction, timing, rfi |

Note: **do not return large archives as base64 through a resource**. For large files, return only the artifact ID, path, and metadata; subsequent tools reference the artifact ID directly.

### 5.2 Prompts

| Prompt | Parameters | Purpose |
|---|---|---|
| `reduce_observation` | `archive`, `pulsar_name?`, `dm?`, `rfi_policy?` | Guides through "inspect → dedisperse → RFI → standard profile → measure" |
| `timing_analysis` | `par`, `tim`, `backend?` | Guides through fitting, residual checks, and parameter interpretation |
| `diagnose_rfi` | `archive` | Guides bad-channel analysis and zap strategy selection |
| `catalog_to_ephemeris` | `name`, `format?` | Fetch parameters from the catalog and export a `.par` |
| `compare_timing_models` | `par`, `tim` | Compare PINT and tempo2 results (later phase) |

Prompts only provide workflow and safety constraints; the model still selects Tools for actual execution, so CLI commands are never hardcoded into prompts.

---

## 6. Data, Artifact, and Job Models

### 6.1 Artifact

```json
{
  "artifact_id": "art_01J...",
  "kind": "archive",
  "format": "PSRFITS",
  "path": "artifacts/art_01J.../output.ar",
  "size_bytes": 123456789,
  "sha256": "...",
  "created_at": "2026-10-17T12:00:00Z",
  "parents": ["art_input..."],
  "provenance": {
    "tool": "dedisperse",
    "tool_version": "0.1.0",
    "backend": "dspsr 5.x",
    "params": {"mode": "coherent", "dm": 2.644},
    "command": ["dspsr", "-D", "-e", "...", "..."]
  }
}
```

### 6.2 Workspace layout

```
$WORKSPACE/
├── sessions/<session_id>/
│   ├── artifacts/<artifact_id>/<filename>
│   ├── logs/<job_id>.log
│   └── manifest.jsonl
├── cache/<cache_key>/
└── tmp/<job_id>/
```

- Atomic writes: write to a temporary directory first, then rename on success
- Cache key: input file SHA-256 + tool version + normalized parameters + ephemeris hash
- Quotas and TTL: per-session/user disk limits; expired artifacts can be cleaned up
- Raw data directories are mounted read-only

### 6.3 Job / Task model

- Short tasks return synchronously
- Above a threshold (default 20 seconds), switch to async
- When the MCP Tasks extension is supported:
  - `taskId`
  - Status: `working` / `input_required` / `completed` / `failed` / `cancelled`
  - `tasks/get` to poll, `tasks/cancel` to cancel
  - Results are returned after `completed`
- When Tasks is not supported:
  - The tool returns a `job_id`
  - The client calls `job_get` / `job_cancel`
- Progress:
  - Prefer parsing dspsr/psrchive stderr
  - Otherwise report by stage: `validating → dedispersing → folding → writing → complete`
- Cancellation:
  - `SIGTERM` first, then `SIGKILL` after a timeout
  - Kill the process group and clean up temporary artifacts
- Restart:
  - Job metadata is persisted to SQLite
  - Locally running processes are marked `interrupted` after a server restart
  - HPC/Slurm jobs can be re-attached via an external job ID

---

## 7. Security and Scientific Correctness

### 7.1 Security

- **Path whitelist**: only the configured `read_roots` and `workspace_root` are allowed
- Use `Path.resolve()` and check for escapes; reject symlink escapes
- **No shell invocation**: all commands use argv arrays
- Validate file names; reject inputs starting with `-` or containing control characters
- Resource limits: timeouts, max input/output size, max thread count, disk quotas
- Process-level limits: `setrlimit` or cgroups
- HTTP deployment: TLS + OAuth 2.1 / Bearer token, with per-user workspace isolation
- Audit log: record caller, tool, parameters, command, input hashes, exit code, and duration
- Treat data as untrusted input: PSRFITS headers, par files, and log text must be parsed and escaped safely
- Artifact deletion requires explicit confirmation; by default items are moved to a trash area with a retention period

### 7.2 Scientific correctness

- Check that the ephemeris matches the observation: source name, RA/DEC, DM, time range
- DM overrides must be passed explicitly and recorded in provenance
- Be explicit about time systems: MJD UTC, TT(TAI), solar-system barycenter corrections; record the clock files used
- Before TOA extraction, check standard profile and bin-count compatibility
- Before fitting, check the TOA count, parameter constraints, and fit flags to avoid unconstrained fits
- Report reduced chi2, RMS, WRMS, and the outlier fraction, but do not treat reduced chi2 as the sole criterion
- Never drop data silently: report fractions and masks for RFI zapping, weighting, and clipping
- Record all tool versions, input hashes, and command lines to guarantee reproducibility

---

## 8. Configuration and Deployment

### 8.1 Configuration example

```toml
[server]
transport = "stdio"          # stdio | streamable-http
workspace = "~/.psrmcp/workspace"
long_job_threshold_s = 20
job_timeout_s = 7200
max_concurrent_jobs = 2

[data]
read_roots = ["/data/pulsar"]
allow_absolute_paths = false

[backends]
dspsr = "/usr/local/bin/dspsr"
psrchive_bin = "/usr/local/bin"
tempo2 = "/usr/local/bin/tempo2"
psrcat = "/usr/local/bin/psrcat"
pint_enabled = true
tempo2_enabled = true

[catalog]
mode = "local"               # local | online | auto
database = "~/.psrmcp/psrcat.db"
cache_ttl_days = 30

[limits]
max_threads = 8
max_input_gb = 50
max_output_gb = 100
max_result_kb = 256
```

### 8.2 Deployment modes

**Local stdio**

```json
{
  "mcpServers": {
    "pulsar": {
      "command": "uvx",
      "args": ["--from", "psrmcp", "psrmcp"],
      "env": {"PSRMCP_CONFIG": "/path/to/psrmcp.toml"}
    }
  }
}
```

**Container**

```json
{
  "mcpServers": {
    "pulsar": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "-v", "/data/pulsar:/data/pulsar:ro",
        "-v", "/home/user/.psrmcp:/workspace",
        "psrmcp:0.1.0"
      ]
    }
  }
}
```

**Remote service**

- Streamable HTTP
- TLS termination, OAuth 2.1, per-user workspaces
- Limit concurrency, request size, and job queue length
- Read-only data volumes; the container must not access other host paths

### 8.3 Toolchain image

Pinned in the image:

- dspsr / psrchive
- tempo2 and the required plugins
- psrcat and the catalog database
- PINT, astropy, numpy, matplotlib
- Optional: psrqpy, clfd / coastguard, libstempo, psrchive Python bindings

Run an environment self-check at startup and expose the result via `pulsar_environment`; version mismatches produce a clear error instead of failing at runtime.

---

## 9. Testing and Acceptance

### 9.1 Test levels

- **Unit tests**: path policy, parameter validation, argv construction, output parsing, error mapping
- **Contract tests**: call the real dspsr/psrchive/PINT/tempo2 on small test data inside a pinned container
- **Integration tests**: full pipeline on J0437-4715 or B1937+21: inspect → dedisperse → RFI → profile → TOA → fit
- **Security tests**: path traversal, symlink escapes, command-injection strings, oversized files, disk exhaustion
- **Failure tests**: missing binaries, corrupted archives, timeouts, cancellation, version incompatibility
- **Regression tests**: compare key numbers (DM, period, TOA count, fitted parameters) against golden outputs

### 9.2 MVP acceptance criteria

1. `archive_inspect` reads PSRFITS metadata correctly
2. `dedisperse` performs coherent dedispersion on filterbank/archive data and produces PSRFITS
3. `rfi_scan` suggests bad channels, and `rfi_zap` applies the mask
4. `profile_standardize` + `profile_measure` generate a standard profile and measure S/N
5. `toa_extract` produces a `.tim`
6. `timing_fit` completes a PINT fit and returns parameters, residuals, and a residual plot
7. Long tasks report progress, can be cancelled, and leave no leftover temporary files after cancellation
8. Paths outside the whitelist are inaccessible, and arbitrary commands cannot be executed
9. All output carries provenance and is reproducible

---

## 10. Implementation Roadmap

| Phase | Content | Estimate |
|---|---|---|
| Phase 0 | Toolchain containers, project skeleton, configuration, `pulsar_environment`, `data_find`, `archive_inspect`, catalog queries | 1–2 weeks |
| Phase 1 | `archive_convert/merge`, `dedisperse`, `rfi_scan/zap`, `profile_*`, artifact/job infrastructure | 2–3 weeks |
| Phase 2 | `toa_extract`, PINT worker, `timing_fit`, `timing_diagnose`, residual plots | 2–3 weeks |
| Phase 3 | tempo2/libstempo backends, polarization calibration, composite pipeline tools, HTTP/HPC deployment | 3–4 weeks |
| Phase 4 | Search-mode single-pulse, batch processing, multi-user quotas, observability | As needed |

> Estimates assume one developer familiar with the pulsar toolchain; container builds and real-data debugging are usually the largest sources of uncertainty.

---

## 11. Main Risks

| Risk | Impact | Mitigation |
|---|---|---|
| dspsr/psrchive/tempo2 installation and version compatibility | Environment unusable | Pinned container image, startup self-check, recorded versions |
| PINT API changes | Timing fit tools break | Pinned versions, separate worker, contract tests |
| Oversized data files | Memory/disk/client limits exceeded | Streaming, artifact references, result pagination |
| Catalog/ephemeris mismatch | Wrong scientific results | Enforced checks on source name, position, DM; explicit override and recording |
| LLM picks the wrong tool or parameters | Pipeline failure | Task-level tools, enum parameters, clear descriptions, prompt guidance |
| Uneven client support for the Tasks extension | Poor long-task experience | Dual path: Tasks + job polling |
| Path/command injection | Security issues | Whitelist, argv arrays, resource limits, auditing |
| Insufficient reproducibility | Results cannot be verified | Input hashes, tool versions, command lines, and parameters all persisted |

---

## 12. Decisions Requiring Follow-up

1. Deployment shape: single-machine local use, or multi-user shared / HPC?
2. Main data sources and formats: FAST, Parkes, GBT, SKA pathfinders? Which of PSRFITS, PSRARCHIVE, SIGPROC take priority?
3. Is PINT or tempo2 the reference timing backend? Is a cross-comparison of both required?
4. Must catalog queries work offline, or is network access allowed?
5. Are psrchive Python bindings acceptable, or should everything go through the CLI?
6. Does remote deployment need OAuth / multi-user isolation?

---

## References

- [MCP specification 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28)
- [MCP Tasks extension](https://modelcontextprotocol.io/extensions/tasks/overview)
- [DSPSR user manual](https://dspsr.sourceforge.net/manuals/dspsr/)
- [psrchive tools manual](https://psrchive.sourceforge.net/manuals/)
- [PINT documentation](https://nanograv-pint.readthedocs.io/en/latest/)
- [TEMPO2 official documentation](http://www.atnf.csiro.au/research/pulsar/tempo2/)
- [ATNF Pulsar Catalogue](https://www.atnf.csiro.au/research/pulsar/psrcat/)
