# psrmcp Feature Overview

English | [简体中文](FEATURES-zh.md)

psrmcp wraps the professional pulsar radio-astronomy processing stack (**dspsr / PSRCHIVE / tempo2 / psrcat / PINT**) as an MCP server, so AI assistants (Claude Code, Claude Desktop, etc.) can drive them directly and safely, covering the full pipeline from raw observation data to timing analysis reports. The AI does not need to memorize each package's command-line flags; instead it calls 20 task-oriented high-level tools, and every result is structured and traceable.

Design principles: **task-oriented, controlled parameters, structured results, full traceability** (architecture in [DESIGN.md](DESIGN.md), per-tool schemas in [TOOLS.md](TOOLS.md)).

## Five backends

| Backend | Purpose |
|---|---|
| PSRCHIVE | archive inspection/processing (vap / psrstat / pam / pat / psradd / paz) |
| dspsr | dedispersion and folding of raw data (filterbank / PSRFITS) |
| tempo2 | pulsar timing (ephemerides, phase prediction) |
| psrcat | local queries of the ATNF pulsar catalogue and ephemeris export |
| PINT | Python timing fits and residual analysis |

Missing backends do not crash the server: `pulsar_environment` reports available/reason for each one, and tools that depend on a missing backend return `E_ENV_MISSING` with a hint.

## The 20 tools (grouped by processing pipeline)

**Environment and data discovery**

| Tool | Purpose |
|---|---|
| `pulsar_environment` | version detection and availability self-check across the five backends; reports workspace/limits |
| `data_find` | scans whitelisted read_roots directories to identify observation data (filterbank / PSRFITS / archive) |

**Catalogue and ephemerides**

| Tool | Purpose |
|---|---|
| `catalog_search` | query the ATNF catalogue by name, by criteria (p0, DM, orbital period, flux, galactic latitude), or by positional radius; extra psrcat columns via `fields` |
| `catalog_query` | power query with a raw psrcat `-l` expression (numeric conditions on any whitelisted column, e.g. `p0 < 0.005 && s1400 > 5`); local psrcat only |
| `catalog_get` | fetch the full parameter row for a single pulsar |
| `ephemeris_export` | export a TEMPO/TEMPO2-format par ephemeris (full binary model via local psrcat `-e`, with field overrides supported) |

**Archive processing**

| Tool | Purpose |
|---|---|
| `archive_inspect` | header info and dimensions (nbin/nchan/npol/nsubint/frequency band) |
| `archive_convert` | PSRFITS ↔ PSRARCHIVE ↔ filterbank format conversion |
| `archive_merge` | merge multiple archives (psradd) |

**Dedispersion**

| Tool | Purpose |
|---|---|
| `dedisperse` ⏱ | dspsr folding; the DM comes from an ephemeris file or `dm_override`, and the ephemeris can be resolved automatically from an artifact ID or a pulsar name |

**Radio-frequency interference (RFI)**

| Tool | Purpose |
|---|---|
| `rfi_scan` | per-channel statistics scan to locate suspicious channels/bands |
| `rfi_zap` | remove RFI by mask (channels/bands) (paz) |

**Profile analysis**

| Tool | Purpose |
|---|---|
| `profile_standardize` | normalize/align to a standard profile and output a profile plot |
| `profile_measure` | measurements such as SNR, W50/W10 pulse widths, and peak flux, with a matplotlib profile plot |

**Timing**

| Tool | Purpose |
|---|---|
| `toa_extract` ⏱ | batch TOA extraction with pat (standard profiles generated automatically); outputs .tim and .par artifacts |
| `timing_fit` ⏱ | PINT weighted fit (1/σ² or uniform); outputs a parameter table, reduced χ², WRMS, a residual plot, and the post-fit par |
| `timing_diagnose` | residual diagnostics: outlier TOAs (MAD-based), high/low reduced χ², presence of JUMP parameters, DM information, distribution skewness/kurtosis, plus processing recommendations |

**Artifacts and job management**

| Tool | Purpose |
|---|---|
| `artifact_list` | list workspace artifacts (filterable by kind) |
| `job_get` | query the status/result of an async job |
| `job_cancel` | cancel a running job (at the process-group level) |

⏱ = long-running job: automatically switches to async above a threshold (returns a `job_id` to poll), supports cancellation, state persisted in SQLite; after a server restart, working becomes interrupted.

## Engineering features

- **Artifact provenance**: every result is an `art_xxx` artifact recording sha256 and full provenance (command argv, parameters, backend versions, parent/child lineage); tools chain pipelines together via artifact IDs.
- **Security sandbox**: path whitelist (rejects `..`/control characters/symlink escapes), commands passed only as argv arrays and never through a shell, rlimits, timeouts that SIGTERM then SIGKILL the process group, stderr tails written to log artifacts.
- **Structured output**: every tool returns a Markdown summary (content) plus structured JSON (structuredContent); errors use a uniform `code/message/hint/retryable/log_artifact_id` envelope, with no raw traceback exposed.
- **Plotting**: all plots use Python matplotlib and are delivered as PNG plot artifacts (profiles, residuals, etc.), with no PGPLOT dependency.
- **Resources and prompts**: `pulsar://environment` (a runtime environment snapshot consistent with the tools), `pulsar://docs/reduction` (the standard reduction workflow); 5 preset prompts: observation workflow, timing analysis, RFI diagnostics, catalogue-to-ephemeris conversion, timing model comparison.
- **Configuration**: `--init-config` generates a TOML template (five sections: server/data/backends/catalog/limits); the `PSRMCP_CONFIG` environment variable sets its path; on a stdio cold start without a shell environment, backends can still be configured directly via absolute paths.

## A complete processing chain

Tell the AI "process this batch of observation data" and it can do the following on its own:

```
pulsar_environment   confirm the backends are available
data_find            find ws/synth.fil
ephemeris_export     query psrcat for the full par ephemeris of J0437-4715
dedisperse           dspsr dedispersion and folding (-E ephemeris builds the phase predictor)
rfi_zap              remove the contaminated channels
profile_standardize  build the standard profile (SNR 133.9)
toa_extract          extract 128 TOAs
timing_fit           PINT fit, yielding reduced χ² and a residual plot
timing_diagnose      diagnose: outlier TOAs, whether DMX/JUMP are needed
```

The artifacts of every step (archives, .tim/.par, plots, logs) carry provenance — traceable and reproducible.

## Related documentation

- [TUTORIAL.md](TUTORIAL.md) — usage tutorial (from installation to a full processing-chain walkthrough)
- [DESIGN.md](DESIGN.md) — architecture and execution model
- [TOOLS.md](TOOLS.md) — complete JSON Schema contracts for all 20 tools
- [INSTALL.md](INSTALL.md) — one-click installation of the software stack
