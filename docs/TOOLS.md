# Pulsar Data Processing MCP Server Tool Contracts

English | [简体中文](TOOLS-zh.md)

- Document version: v0.1
- Date: 2026-10-17
- Companion document: [DESIGN.md](./DESIGN.md)
- MCP protocol: 2026-07-28
- Schema dialect: JSON Schema Draft 2020-12
- Status: design draft, not yet implemented

---

## 1. General Conventions

### 1.1 Naming and Units

- Tool names and parameter names uniformly use `snake_case`
- All input objects set `additionalProperties: false`
- Numeric fields carry their unit in the name:
  - Time: `_s`, `_ms`, `_us`
  - Frequency: `_mhz`
  - Angle: `_deg`
  - Time coordinate: `_mjd`
  - Size: `_bytes`
- MJD values default to UTC unless the time system is stated otherwise
- Time strings use RFC 3339 / ISO 8601
- Text fields set sensible `minLength` / `maxLength` and reject control characters

### 1.2 Paths and IDs

- `artifact_id`: `art_` prefix; an immutable artifact inside the workspace
- `job_id`: `job_` prefix; an asynchronous job
- Parameters such as `archive`, `par`, `tim` accept:
  - an artifact ID
  - a relative path under the configured `read_roots`
- When a relative path could match multiple roots, use the optional `root` parameter to disambiguate
- Absolute paths are accepted only when `allow_absolute_paths = true`, and must still be inside the allowlist
- `output_name` is only a human-readable label; the server generates a unique path and never overwrites an existing artifact
- Path parameters must not contain `..`, control characters, or NUL

### 1.3 Tool Result Envelope

An MCP `tools/call` returns:

```json
{
  "content": [
    {"type": "text", "text": "Brief human-readable summary"}
  ],
  "structuredContent": {
    "result_type": "completed"
  },
  "isError": false
}
```

Conventions:

- `content`: a Markdown summary, suitable for direct display
- `structuredContent`: strictly conforms to the `outputSchema` in this document
- Images are returned via an `image` block or `resource_link` in `content`
- On error, `isError: true` and `structuredContent.error` is an `ErrorBody`
- Full stderr is never returned in the error message; the complete log goes into `log_artifact_id`

### 1.4 Long-Running Tasks

Tools that may take a long time support `execution_mode`:

- `auto` (default): switches to async when the estimated runtime exceeds `long_job_threshold_s`
- `sync`: waits synchronously; returns `E_TIMEOUT` once `job_timeout_s` is exceeded
- `async`: returns `result_type: "accepted"` and a `JobRef` immediately

Async results are carried in one of two ways:

1. **MCP Tasks extension available**: the protocol layer returns a `CreateTaskResult`; the client operates via `tasks/get` and `tasks/cancel`; the final `result` uses the completed schema in this document
2. **Tasks extension unavailable**: the tool returns `accepted` + `JobRef`; the client polls and cancels with `job_get` / `job_cancel`

`JobRef.status`:

- `queued`
- `working`
- `input_required`
- `completed`
- `failed`
- `cancelled`
- `interrupted` (local process lost after a server restart)

### 1.5 Error Handling

An error result:

```json
{
  "isError": true,
  "structuredContent": {
    "result_type": "failed",
    "error": {
      "code": "E_EPHEMERIS_MISMATCH",
      "message": "Ephemeris source J0437-4715 does not match archive source J1744-1134",
      "hint": "Provide a .par file that matches the observation, or set dm_override explicitly",
      "retryable": false,
      "details": {},
      "log_artifact_id": "art_..."
    }
  }
}
```

Principles:

- Error codes are stable and do not change with implementation details
- `retryable` tells the client explicitly whether a retry is worthwhile
- Problems the user can fix must include a `hint`
- Scientific data mismatches are errors; never continue silently

---

## 2. Common Types

The following `$defs` are referenced by all tool schemas. Implementations may generate and expand them from Pydantic models; this document expresses them with `$ref`.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://psrmcp.local/schemas/common.json",
  "$defs": {
    "ArtifactRef": {
      "type": "object",
      "required": [
        "artifact_id",
        "kind",
        "format",
        "size_bytes",
        "sha256",
        "created_at",
        "provenance"
      ],
      "properties": {
        "artifact_id": {
          "type": "string",
          "pattern": "^art_[A-Za-z0-9_-]{8,64}$"
        },
        "kind": {
          "enum": [
            "archive",
            "ephemeris",
            "tim",
            "par",
            "profile",
            "mask",
            "plot",
            "residuals",
            "log",
            "catalog",
            "json",
            "other"
          ]
        },
        "format": {"type": "string", "minLength": 1},
        "path": {
          "type": "string",
          "description": "Workspace-relative path; clients must not assume the path is directly accessible"
        },
        "size_bytes": {"type": "integer", "minimum": 0},
        "sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
        "created_at": {"type": "string", "format": "date-time"},
        "parents": {
          "type": "array",
          "items": {"type": "string"},
          "default": []
        },
        "provenance": {"$ref": "#/$defs/Provenance"}
      },
      "additionalProperties": false
    },

    "Provenance": {
      "type": "object",
      "required": [
        "tool",
        "tool_version",
        "backend",
        "params",
        "started_at",
        "duration_s"
      ],
      "properties": {
        "tool": {"type": "string"},
        "tool_version": {"type": "string"},
        "backend": {"type": "string"},
        "backend_version": {"type": ["string", "null"]},
        "params": {"type": "object", "additionalProperties": true},
        "command": {
          "type": "array",
          "items": {"type": "string"}
        },
        "input_hashes": {
          "type": "object",
          "additionalProperties": {
            "type": "string",
            "pattern": "^[a-f0-9]{64}$"
          }
        },
        "started_at": {"type": "string", "format": "date-time"},
        "duration_s": {"type": "number", "minimum": 0}
      },
      "additionalProperties": false
    },

    "Warning": {
      "type": "object",
      "required": ["code", "message", "severity"],
      "properties": {
        "code": {"type": "string", "pattern": "^W_[A-Z0-9_]+$"},
        "message": {"type": "string"},
        "severity": {"enum": ["info", "warning", "error"]},
        "details": {"type": "object", "additionalProperties": true}
      },
      "additionalProperties": false
    },

    "JobRef": {
      "type": "object",
      "required": ["job_id", "status", "phase", "progress", "created_at"],
      "properties": {
        "job_id": {
          "type": "string",
          "pattern": "^job_[A-Za-z0-9_-]{8,64}$"
        },
        "status": {
          "enum": [
            "queued",
            "working",
            "input_required",
            "completed",
            "failed",
            "cancelled",
            "interrupted"
          ]
        },
        "phase": {"type": "string"},
        "progress": {"type": "number", "minimum": 0, "maximum": 1},
        "message": {"type": "string"},
        "created_at": {"type": "string", "format": "date-time"},
        "started_at": {
          "type": ["string", "null"],
          "format": "date-time"
        },
        "finished_at": {
          "type": ["string", "null"],
          "format": "date-time"
        },
        "result": {"type": ["object", "null"]},
        "error": {
          "anyOf": [
            {"$ref": "#/$defs/ErrorBody"},
            {"type": "null"}
          ]
        }
      },
      "additionalProperties": false
    },

    "ErrorBody": {
      "type": "object",
      "required": ["code", "message", "retryable"],
      "properties": {
        "code": {
          "enum": [
            "E_CONFIG",
            "E_ENV_MISSING",
            "E_INVALID_INPUT",
            "E_NOT_FOUND",
            "E_PATH_DENIED",
            "E_FORMAT_UNSUPPORTED",
            "E_SOURCE_MISMATCH",
            "E_EPHEMERIS_MISMATCH",
            "E_OUTPUT_CONFLICT",
            "E_PROCESS_FAILED",
            "E_TIMEOUT",
            "E_CANCELLED",
            "E_QUOTA_EXCEEDED",
            "E_TASK_NOT_FOUND",
            "E_JOB_NOT_RUNNING",
            "E_VERSION_INCOMPATIBLE",
            "E_INTERNAL"
          ]
        },
        "message": {"type": "string"},
        "hint": {"type": "string"},
        "retryable": {"type": "boolean"},
        "details": {"type": "object", "additionalProperties": true},
        "log_artifact_id": {"type": "string"}
      },
      "additionalProperties": false
    },

    "BackendInfo": {
      "type": "object",
      "required": ["available"],
      "properties": {
        "available": {"type": "boolean"},
        "path": {"type": ["string", "null"]},
        "version": {"type": ["string", "null"]},
        "reason": {
          "type": ["string", "null"],
          "description": "Reason when unavailable"
        },
        "capabilities": {
          "type": "array",
          "items": {"type": "string"},
          "default": []
        }
      },
      "additionalProperties": false
    },

    "CatalogRecord": {
      "type": "object",
      "required": ["name", "ra_deg", "dec_deg", "p0_s", "dm"],
      "properties": {
        "name": {"type": "string"},
        "aliases": {
          "type": "array",
          "items": {"type": "string"},
          "default": []
        },
        "ra_deg": {"type": "number", "minimum": 0, "maximum": 360},
        "dec_deg": {"type": "number", "minimum": -90, "maximum": 90},
        "p0_s": {"type": "number", "exclusiveMinimum": 0},
        "f0_hz": {"type": ["number", "null"]},
        "p1": {"type": ["number", "null"]},
        "dm": {"type": "number", "minimum": 0},
        "binary": {"type": "boolean"},
        "binary_model": {"type": ["string", "null"]},
        "observatory": {"type": ["string", "null"]},
        "catalog_version": {"type": "string"},
        "extra_fields": {
          "type": "object",
          "additionalProperties": true,
          "default": {},
          "description": "Additional psrcat columns requested via catalog_search.fields / catalog_query.columns, keyed by psrcat column name (age, bsurf, pb, s1400, gb, ...)"
        }
      },
      "additionalProperties": false
    },

    "CatalogMeta": {
      "type": "object",
      "required": ["source", "version", "retrieved_at", "mode"],
      "properties": {
        "source": {"type": "string"},
        "version": {"type": "string"},
        "retrieved_at": {"type": "string", "format": "date-time"},
        "mode": {"enum": ["local", "online", "cache"]}
      },
      "additionalProperties": false
    },

    "AsyncAccepted": {
      "type": "object",
      "required": ["result_type", "job"],
      "properties": {
        "result_type": {"const": "accepted"},
        "job": {"$ref": "#/$defs/JobRef"}
      },
      "additionalProperties": false
    }
  }
}
```

---

## 3. Error Codes

| Error code | Meaning | Retryable | Typical handling |
|---|---|---|---|
| `E_CONFIG` | Invalid or missing configuration | No | Fix the configuration file and restart |
| `E_ENV_MISSING` | Backend binary / Python package unavailable | No | Install or enable the corresponding backend |
| `E_INVALID_INPUT` | Schema or semantic validation failed | No | Correct the parameters |
| `E_NOT_FOUND` | Artifact, file, or pulsar does not exist | No | Check the ID, path, or name |
| `E_PATH_DENIED` | Path is not inside the allowlist | No | Use an allowed data root |
| `E_FORMAT_UNSUPPORTED` | Data format or version unsupported | No | Convert the format or enable the corresponding backend |
| `E_SOURCE_MISMATCH` | Input data source names disagree | No | Confirm the inputs belong to the same pulsar |
| `E_EPHEMERIS_MISMATCH` | Ephemeris does not match the observation | No | Provide the correct `.par` |
| `E_OUTPUT_CONFLICT` | Output name conflicts and overwriting is not allowed | Depends | Use a different `output_name` |
| `E_PROCESS_FAILED` | Backend command execution failed | Depends | Inspect `log_artifact_id` |
| `E_TIMEOUT` | Job exceeded its timeout | Yes | Switch to async or reduce the data volume |
| `E_CANCELLED` | Job was cancelled | Yes | Confirm and rerun |
| `E_QUOTA_EXCEEDED` | Disk / memory / file-size quota exceeded | No | Clean up the workspace or adjust the limits |
| `E_TASK_NOT_FOUND` | job/task ID does not exist or has expired | No | Resubmit the job |
| `E_JOB_NOT_RUNNING` | Cancel requested on a job that already finished | No | Just query its status |
| `E_VERSION_INCOMPATIBLE` | Tool/data version incompatible | No | Align toolchain versions |
| `E_INTERNAL` | Unexpected server error | Depends | Report the issue with the logs attached |

---

## 4. Tool Index and Annotations

| Tool | `readOnlyHint` | `destructiveHint` | `idempotentHint` | `openWorldHint` | Long-running |
|---|---|---|---|---|---|
| `pulsar_environment` | true | false | true | false | no |
| `data_find` | true | false | true | false | no |
| `catalog_search` | true | false | true | true | no |
| `catalog_query` | true | false | true | false | no |
| `catalog_get` | true | false | true | true | no |
| `ephemeris_export` | false | false | true | true | no |
| `archive_inspect` | true | false | true | false | no |
| `archive_convert` | false | false | true | false | no |
| `archive_merge` | false | false | true | false | yes |
| `archive_transform` | false | false | true | false | yes |
| `archive_edit` | false | false | true | false | no |
| `dedisperse` | false | false | true | false | yes |
| `rfi_scan` | true | false | true | false | no |
| `rfi_zap` | false | false | true | false | no |
| `profile_standardize` | false | false | true | false | no |
| `profile_measure` | true | false | true | false | no |
| `profile_optimize` | true | false | true | false | yes |
| `single_pulse_analyze` | true | false | true | false | yes |
| `rotation_measure_fit` | true | false | true | false | yes |
| `polarization_calibrate` | false | false | true | false | yes |
| `calibrator_database` | false | false | true | false | yes |
| `toa_extract` | false | false | true | false | yes |
| `timing_fit` | false | false | true | false | yes |
| `timing_diagnose` | true | false | true | false | no |
| `artifact_list` | true | false | true | false | no |
| `job_get` | true | false | true | false | no |
| `job_cancel` | false | false | false | false | no |

Notes:

- `idempotentHint` means identical inputs and parameters yield equivalent artifacts; artifact IDs may differ, but the content is cacheable and reusable
- No tool **ever overwrites an input file**
- `catalog_search` / `catalog_get` / `ephemeris_export` access the external catalogue in online mode, hence `openWorldHint: true`; `catalog_query` runs against the local psrcat only, hence `openWorldHint: false`
- The future `artifact_delete` will be marked `destructiveHint: true`

---

## 5. Detailed Tool Contracts

### 5.1 `pulsar_environment`

**Purpose**: Returns the server version, MCP protocol version, backend availability, data roots, workspace, and resource limits. Recommended as the first call of every session.

**Backend**: environment probing  
**Long-running**: no  
**Annotations**: `readOnlyHint=true`, `destructiveHint=false`, `idempotentHint=true`, `openWorldHint=false`

#### Input Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://psrmcp.local/schemas/pulsar_environment.input.json",
  "type": "object",
  "properties": {
    "refresh": {
      "type": "boolean",
      "default": false,
      "description": "Re-probe the backends, ignoring results cached at startup"
    }
  },
  "additionalProperties": false
}
```

#### Output Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://psrmcp.local/schemas/pulsar_environment.output.json",
  "type": "object",
  "required": [
    "result_type",
    "server",
    "backends",
    "data_roots",
    "limits",
    "warnings"
  ],
  "properties": {
    "result_type": {"const": "completed"},
    "server": {
      "type": "object",
      "required": ["name", "version", "mcp_protocol_version"],
      "properties": {
        "name": {"type": "string"},
        "version": {"type": "string"},
        "mcp_protocol_version": {"type": "string"}
      },
      "additionalProperties": false
    },
    "backends": {
      "type": "object",
      "additionalProperties": {"$ref": "#/$defs/BackendInfo"}
    },
    "data_roots": {
      "type": "array",
      "items": {"type": "string"}
    },
    "workspace": {
      "type": "object",
      "required": ["root", "free_bytes", "quota_bytes"],
      "properties": {
        "root": {"type": "string"},
        "free_bytes": {"type": "integer", "minimum": 0},
        "quota_bytes": {"type": "integer", "minimum": 0}
      },
      "additionalProperties": false
    },
    "catalog": {"$ref": "#/$defs/CatalogMeta"},
    "limits": {
      "type": "object",
      "required": [
        "max_threads",
        "max_input_bytes",
        "max_output_bytes",
        "job_timeout_s"
      ],
      "properties": {
        "max_threads": {"type": "integer", "minimum": 1},
        "max_input_bytes": {"type": "integer", "minimum": 0},
        "max_output_bytes": {"type": "integer", "minimum": 0},
        "job_timeout_s": {"type": "integer", "minimum": 1}
      },
      "additionalProperties": false
    },
    "warnings": {
      "type": "array",
      "items": {"$ref": "#/$defs/Warning"}
    }
  },
  "additionalProperties": false
}
```

#### Error Codes

`E_CONFIG`, `E_INTERNAL`

---

### 5.2 `data_find`

**Purpose**: Finds data files by glob within the configured `read_roots` and makes a first-pass format guess. Never reads or modifies file contents.

**Backend**: filesystem  
**Long-running**: no  
**Annotations**: `readOnlyHint=true`, `destructiveHint=false`, `idempotentHint=true`, `openWorldHint=false`

#### Input Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://psrmcp.local/schemas/data_find.input.json",
  "type": "object",
  "required": ["pattern"],
  "properties": {
    "pattern": {
      "type": "string",
      "minLength": 1,
      "maxLength": 256,
      "description": "Glob pattern, e.g. '*.ar', 'J0437*', '**/*.fits'"
    },
    "root": {
      "type": "string",
      "description": "Subdirectory under read_roots; omit to search all roots"
    },
    "max_depth": {
      "type": "integer",
      "minimum": 1,
      "maximum": 10,
      "default": 3
    },
    "max_results": {
      "type": "integer",
      "minimum": 1,
      "maximum": 1000,
      "default": 100
    },
    "extensions": {
      "type": "array",
      "items": {
        "type": "string",
        "pattern": "^\\.[A-Za-z0-9]+$"
      },
      "maxItems": 20
    },
    "modified_after": {
      "type": "string",
      "format": "date-time"
    }
  },
  "additionalProperties": false
}
```

#### Output Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://psrmcp.local/schemas/data_find.output.json",
  "type": "object",
  "required": ["result_type", "files", "count", "truncated"],
  "properties": {
    "result_type": {"const": "completed"},
    "files": {
      "type": "array",
      "items": {
        "type": "object",
        "required": [
          "root",
          "relative_path",
          "size_bytes",
          "mtime",
          "detected_format"
        ],
        "properties": {
          "root": {"type": "string"},
          "relative_path": {"type": "string"},
          "size_bytes": {"type": "integer", "minimum": 0},
          "mtime": {"type": "string", "format": "date-time"},
          "detected_format": {
            "enum": [
              "psrfits",
              "psrarchive",
              "sigproc",
              "par",
              "tim",
              "unknown"
            ]
          }
        },
        "additionalProperties": false
      }
    },
    "count": {"type": "integer", "minimum": 0},
    "truncated": {"type": "boolean"},
    "warnings": {
      "type": "array",
      "items": {"$ref": "#/$defs/Warning"}
    }
  },
  "additionalProperties": false
}
```

#### Error Codes

`E_INVALID_INPUT`, `E_PATH_DENIED`, `E_NOT_FOUND`

---

### 5.3 `catalog_search`

**Purpose**: Queries the ATNF/psrcat catalogue. Supports filtering by name, positional cone, period, DM, orbital period, flux density, galactic latitude, and binarity. At least one filter must be given.

**Backend**: psrcat / psrqpy / local cache  
**Long-running**: no  
**Annotations**: `readOnlyHint=true`, `destructiveHint=false`, `idempotentHint=true`, `openWorldHint=true`

#### Input Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://psrmcp.local/schemas/catalog_search.input.json",
  "type": "object",
  "properties": {
    "name": {
      "type": "string",
      "minLength": 1,
      "maxLength": 64
    },
    "ra_deg": {"type": "number", "minimum": 0, "maximum": 360},
    "dec_deg": {"type": "number", "minimum": -90, "maximum": 90},
    "radius_deg": {
      "type": "number",
      "exclusiveMinimum": 0,
      "maximum": 180
    },
    "p0_min_s": {"type": "number", "exclusiveMinimum": 0},
    "p0_max_s": {"type": "number", "exclusiveMinimum": 0},
    "dm_min": {"type": "number", "minimum": 0},
    "dm_max": {"type": "number", "minimum": 0},
    "pb_min_s": {"type": "number", "exclusiveMinimum": 0},
    "pb_max_s": {"type": "number", "exclusiveMinimum": 0},
    "flux_min_mjy": {"type": "number", "minimum": 0},
    "gb_min": {"type": "number", "minimum": -90, "maximum": 90},
    "gb_max": {"type": "number", "minimum": -90, "maximum": 90},
    "binary_only": {"type": "boolean", "default": false},
    "fields": {
      "type": "array",
      "items": {"type": "string"},
      "description": "Extra psrcat columns to return in each record's extra_fields: age bsurf edot dist s400 s1400 w50 w10 pb a1 ecc om gl gb"
    },
    "limit": {
      "type": "integer",
      "minimum": 1,
      "maximum": 200,
      "default": 50
    },
    "cursor": {"type": "string"}
  },
  "additionalProperties": false
}
```

Semantic validation (beyond JSON Schema):

- `radius_deg` must appear together with `ra_deg` and `dec_deg`
- `p0_min_s <= p0_max_s`, `dm_min <= dm_max`, `pb_min_s <= pb_max_s`, `gb_min <= gb_max`
- `fields` accepts the default columns plus the whitelist above; unknown columns are rejected with `E_INVALID_INPUT`
- At least one of `name`, a position constraint, a period/DM/orbital-period/flux/galactic-latitude constraint, or `binary_only=true` must be provided

Notes:

- flux filtering uses the `s1400` column (flux density at 1400 MHz, mJy); records lacking that column are excluded by flux/`pb` filters
- `type` / binary-model string filters are intentionally not offered: catalogue values carry citation suffixes (e.g. `HE[bt93],HE[aaa+09f]`), so string equality does not match (verified empirically)

#### Output Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://psrmcp.local/schemas/catalog_search.output.json",
  "type": "object",
  "required": ["result_type", "records", "count", "catalog", "warnings"],
  "properties": {
    "result_type": {"const": "completed"},
    "records": {
      "type": "array",
      "items": {"$ref": "#/$defs/CatalogRecord"}
    },
    "count": {"type": "integer", "minimum": 0},
    "next_cursor": {"type": ["string", "null"]},
    "catalog": {"$ref": "#/$defs/CatalogMeta"},
    "warnings": {
      "type": "array",
      "items": {"$ref": "#/$defs/Warning"}
    }
  },
  "additionalProperties": false
}
```

#### Error Codes

`E_INVALID_INPUT`, `E_ENV_MISSING`, `E_NOT_FOUND`

---

### 5.4 `catalog_query`

**Purpose**: Power-user escape hatch: runs a raw psrcat `-l` boolean expression against the local catalogue — numeric conditions on any whitelisted psrcat column, combinable with `&&` / `||`, e.g. `p0 < 0.005 && s1400 > 5`. Use it when the structured filters of `catalog_search` do not cover a parameter (e.g. `edot`, `w50`, `ecc`).

**Backend**: local psrcat only (`E_ENV_MISSING` when psrcat is absent or `catalog.mode = "online"`)  
**Long-running**: no  
**Annotations**: `readOnlyHint=true`, `destructiveHint=false`, `idempotentHint=true`, `openWorldHint=false`

#### Input Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://psrmcp.local/schemas/catalog_query.input.json",
  "type": "object",
  "required": [
    "expression"
  ],
  "properties": {
    "expression": {
      "type": "string",
      "minLength": 3,
      "maxLength": 500,
      "description": "Raw psrcat -l condition, e.g. 'p0 < 0.005 && s1400 > 5'. Numeric conditions on known psrcat columns only; unknown columns are rejected up front (psrcat exits silently on them)."
    },
    "columns": {
      "type": "array",
      "items": {"type": "string"},
      "maxItems": 10,
      "description": "psrcat columns to return (default: name raj decj p0 f0 p1 dm binary assoc)."
    },
    "limit": {
      "type": "integer",
      "minimum": 1,
      "maximum": 200,
      "default": 50
    },
    "cursor": {"type": "string"}
  },
  "additionalProperties": false
}
```

Semantic validation (beyond JSON Schema):

- `expression` must contain printable ASCII only (no control characters)
- Every bare identifier in `expression` must be a known psrcat column (`name p0 f0 p1 dm binary assoc age bsurf edot dist s400 s1400 w50 w10 pb a1 ecc om gl gb`); unknown identifiers are rejected with `E_INVALID_INPUT` **before** execution, because psrcat exits 0 with no output for unknown `-l` parameters and a typo would otherwise silently look like an empty result (verified against psrcat 2.6.1)
- String comparisons do not match in `-l`: catalogue values carry citation suffixes (e.g. `type` = `HE[bt93],HE[aaa+09f]`) and `name` equality is unsupported — use `catalog_search` for name/position lookups

#### Output Schema

Identical envelope to `catalog_search` (§5.3): `CatalogSearchResult` with `records` (each a `CatalogRecord`), `count`, `next_cursor`, `catalog`, `warnings`. Columns requested via `columns` beyond the defaults are returned under each record's `extra_fields`, keyed by psrcat column name.

#### Error Codes

`E_INVALID_INPUT`, `E_ENV_MISSING`

---

### 5.5 `catalog_get`

**Purpose**: Fetches a single pulsar record by exact name and determines whether an ephemeris can be produced.

**Backend**: psrcat / psrqpy / local cache  
**Long-running**: no  
**Annotations**: `readOnlyHint=true`, `destructiveHint=false`, `idempotentHint=true`, `openWorldHint=true`

#### Input Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://psrmcp.local/schemas/catalog_get.input.json",
  "type": "object",
  "required": ["name"],
  "properties": {
    "name": {
      "type": "string",
      "minLength": 1,
      "maxLength": 64,
      "description": "For example 'J0437-4715' or 'B1937+21'"
    }
  },
  "additionalProperties": false
}
```

#### Output Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://psrmcp.local/schemas/catalog_get.output.json",
  "type": "object",
  "required": [
    "result_type",
    "record",
    "ephemeris_available",
    "catalog",
    "warnings"
  ],
  "properties": {
    "result_type": {"const": "completed"},
    "record": {"$ref": "#/$defs/CatalogRecord"},
    "ephemeris_available": {"type": "boolean"},
    "catalog": {"$ref": "#/$defs/CatalogMeta"},
    "warnings": {
      "type": "array",
      "items": {"$ref": "#/$defs/Warning"}
    }
  },
  "additionalProperties": false
}
```

#### Error Codes

`E_INVALID_INPUT`, `E_NOT_FOUND`, `E_ENV_MISSING`

---

### 5.6 `ephemeris_export`

**Purpose**: Produces a TEMPO/TEMPO2-format ephemeris from a catalogue record or an existing `.par` artifact. The conversion may lose information and must return warnings.

**Backend**: psrcat / PINT  
**Long-running**: no  
**Annotations**: `readOnlyHint=false`, `destructiveHint=false`, `idempotentHint=true`, `openWorldHint=true`

#### Input Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://psrmcp.local/schemas/ephemeris_export.input.json",
  "type": "object",
  "oneOf": [
    {"required": ["pulsar"]},
    {"required": ["artifact"]}
  ],
  "properties": {
    "pulsar": {
      "type": "string",
      "minLength": 1,
      "maxLength": 64
    },
    "artifact": {
      "type": "string",
      "description": "Artifact ID of an existing .par"
    },
    "format": {
      "enum": ["tempo1", "tempo2"],
      "default": "tempo1"
    },
    "overrides": {
      "type": "object",
      "properties": {
        "ra_deg": {"type": "number", "minimum": 0, "maximum": 360},
        "dec_deg": {"type": "number", "minimum": -90, "maximum": 90},
        "p0_s": {"type": "number", "exclusiveMinimum": 0},
        "f0_hz": {"type": "number", "exclusiveMinimum": 0},
        "dm": {"type": "number", "minimum": 0},
        "pepoch_mjd": {"type": "number"},
        "posepoch_mjd": {"type": "number"}
      },
      "additionalProperties": false
    },
    "output_name": {
      "type": "string",
      "maxLength": 128
    }
  },
  "additionalProperties": false
}
```

#### Output Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://psrmcp.local/schemas/ephemeris_export.output.json",
  "type": "object",
  "required": ["result_type", "artifact", "format", "warnings", "provenance"],
  "properties": {
    "result_type": {"const": "completed"},
    "artifact": {"$ref": "#/$defs/ArtifactRef"},
    "format": {"enum": ["tempo1", "tempo2"]},
    "warnings": {
      "type": "array",
      "items": {"$ref": "#/$defs/Warning"}
    },
    "provenance": {"$ref": "#/$defs/Provenance"}
  },
  "additionalProperties": false
}
```

#### Error Codes

`E_INVALID_INPUT`, `E_NOT_FOUND`, `E_ENV_MISSING`, `E_VERSION_INCOMPATIBLE`

---

### 5.7 `archive_inspect`

**Purpose**: Reads PSRFITS/PSRARCHIVE metadata, dimensions, and basic statistics without modifying the file. Should be called before processing any data.

**Backend**: psrchive  
**Long-running**: no  
**Annotations**: `readOnlyHint=true`, `destructiveHint=false`, `idempotentHint=true`, `openWorldHint=false`

#### Input Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://psrmcp.local/schemas/archive_inspect.input.json",
  "type": "object",
  "required": ["archive"],
  "properties": {
    "archive": {
      "type": "string",
      "description": "Artifact ID or relative path under a read root"
    },
    "root": {
      "type": "string",
      "description": "Specifies the data root when archive is a relative path"
    },
    "include_stats": {"type": "boolean", "default": true},
    "include_ephemeris": {"type": "boolean", "default": true},
    "include_integrations": {"type": "boolean", "default": false},
    "max_integrations": {
      "type": "integer",
      "minimum": 1,
      "maximum": 1000,
      "default": 100
    }
  },
  "additionalProperties": false
}
```

#### Output Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://psrmcp.local/schemas/archive_inspect.output.json",
  "type": "object",
  "required": [
    "result_type",
    "format",
    "source",
    "observation",
    "dimensions",
    "warnings",
    "provenance"
  ],
  "properties": {
    "result_type": {"const": "completed"},
    "input": {
      "type": "object",
      "properties": {
        "artifact_id": {"type": "string"},
        "path": {"type": "string"}
      },
      "additionalProperties": false
    },
    "format": {"type": "string"},
    "format_version": {"type": ["string", "null"]},
    "source": {
      "type": "object",
      "required": ["name"],
      "properties": {
        "name": {"type": "string"},
        "ra_deg": {"type": ["number", "null"]},
        "dec_deg": {"type": ["number", "null"]}
      },
      "additionalProperties": false
    },
    "observation": {
      "type": "object",
      "properties": {
        "telescope": {"type": ["string", "null"]},
        "receiver": {"type": ["string", "null"]},
        "backend": {"type": ["string", "null"]},
        "start_mjd": {"type": ["number", "null"]},
        "end_mjd": {"type": ["number", "null"]},
        "duration_s": {"type": ["number", "null"]},
        "freq_mhz": {"type": ["number", "null"]},
        "bandwidth_mhz": {"type": ["number", "null"]}
      },
      "additionalProperties": false
    },
    "dimensions": {
      "type": "object",
      "required": ["nsubint", "nchan", "npol", "nbin"],
      "properties": {
        "nsubint": {"type": "integer", "minimum": 0},
        "nchan": {"type": "integer", "minimum": 0},
        "npol": {"type": "integer", "minimum": 0},
        "nbin": {"type": "integer", "minimum": 0}
      },
      "additionalProperties": false
    },
    "timing": {
      "type": "object",
      "properties": {
        "dm": {"type": ["number", "null"]},
        "rm": {"type": ["number", "null"]},
        "period_s": {"type": ["number", "null"]},
        "ephemeris_present": {"type": "boolean"}
      },
      "additionalProperties": false
    },
    "ephemeris": {
      "type": ["object", "null"],
      "properties": {
        "artifact_id": {"type": "string"},
        "source_name": {"type": ["string", "null"]},
        "format": {"type": ["string", "null"]}
      },
      "additionalProperties": false
    },
    "integrations": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "index": {"type": "integer", "minimum": 0},
          "mjd": {"type": ["number", "null"]},
          "duration_s": {"type": ["number", "null"]},
          "snr": {"type": ["number", "null"]}
        },
        "additionalProperties": false
      },
      "maxItems": 1000
    },
    "warnings": {
      "type": "array",
      "items": {"$ref": "#/$defs/Warning"}
    },
    "provenance": {"$ref": "#/$defs/Provenance"}
  },
  "additionalProperties": false
}
```

#### Error Codes

`E_INVALID_INPUT`, `E_NOT_FOUND`, `E_PATH_DENIED`, `E_FORMAT_UNSUPPORTED`, `E_PROCESS_FAILED`

---

### 5.8 `archive_convert`

**Purpose**: Converts between archive formats via psrconv: writable targets are `psrfits` (PSRFITS), `psrarchive` (the native Timer format), and `ascii` (single-profile text). EPN files can be read by PSRCHIVE but not written (no EPN unload is implemented); `sigproc` is not a PSRCHIVE format and is rejected up front. The input is left untouched; the output is a new artifact.

**Backend**: psrconv  
**Long-running**: no (very large files may be switched to async)  
**Annotations**: `readOnlyHint=false`, `destructiveHint=false`, `idempotentHint=true`, `openWorldHint=false`

#### Input Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://psrmcp.local/schemas/archive_convert.input.json",
  "type": "object",
  "required": ["archive", "output_format"],
  "properties": {
    "archive": {
      "type": "string",
      "description": "Artifact ID or relative path under a read root"
    },
    "root": {"type": "string"},
    "output_format": {
      "enum": ["psrfits", "psrarchive", "sigproc", "ascii"]
    },
    "output_name": {
      "type": "string",
      "maxLength": 128
    }
  },
  "additionalProperties": false
}
```

#### Output Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://psrmcp.local/schemas/archive_convert.output.json",
  "type": "object",
  "required": ["result_type", "artifact", "warnings"],
  "properties": {
    "result_type": {"const": "completed"},
    "artifact": {"$ref": "#/$defs/ArtifactRef"},
    "warnings": {
      "type": "array",
      "items": {"$ref": "#/$defs/Warning"}
    }
  },
  "additionalProperties": false
}
```

#### Error Codes

`E_INVALID_INPUT`, `E_NOT_FOUND`, `E_FORMAT_UNSUPPORTED`, `E_PROCESS_FAILED`, `E_QUOTA_EXCEEDED`

---

### 5.9 `archive_merge`

**Purpose**: Merges multiple archives. Source names must agree by default; on mismatch returns `E_SOURCE_MISMATCH` unless the check is explicitly disabled.

**Backend**: psradd  
**Long-running**: yes  
**Annotations**: `readOnlyHint=false`, `destructiveHint=false`, `idempotentHint=true`, `openWorldHint=false`

#### Input Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://psrmcp.local/schemas/archive_merge.input.json",
  "type": "object",
  "required": ["archives"],
  "properties": {
    "archives": {
      "type": "array",
      "minItems": 2,
      "maxItems": 100,
      "items": {"type": "string"}
    },
    "root": {"type": "string"},
    "output_name": {"type": "string", "maxLength": 128},
    "weighting": {
      "enum": ["none", "time", "snr"],
      "default": "none"
    },
    "require_same_source": {"type": "boolean", "default": true},
    "execution_mode": {
      "enum": ["auto", "sync", "async"],
      "default": "auto"
    }
  },
  "additionalProperties": false
}
```

#### Output Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://psrmcp.local/schemas/archive_merge.output.json",
  "oneOf": [
    {"$ref": "#/$defs/AsyncAccepted"},
    {
      "type": "object",
      "required": [
        "result_type",
        "artifact",
        "n_inputs",
        "dimensions",
        "warnings"
      ],
      "properties": {
        "result_type": {"const": "completed"},
        "artifact": {"$ref": "#/$defs/ArtifactRef"},
        "n_inputs": {"type": "integer", "minimum": 2},
        "dimensions": {
          "type": "object",
          "required": ["nsubint", "nchan", "npol", "nbin"],
          "properties": {
            "nsubint": {"type": "integer", "minimum": 0},
            "nchan": {"type": "integer", "minimum": 0},
            "npol": {"type": "integer", "minimum": 0},
            "nbin": {"type": "integer", "minimum": 0}
          },
          "additionalProperties": false
        },
        "warnings": {
          "type": "array",
          "items": {"$ref": "#/$defs/Warning"}
        }
      },
      "additionalProperties": false
    }
  ]
}
```

#### Error Codes

`E_INVALID_INPUT`, `E_NOT_FOUND`, `E_SOURCE_MISMATCH`, `E_PROCESS_FAILED`, `E_TIMEOUT`, `E_CANCELLED`, `E_QUOTA_EXCEEDED`

---

### 5.10 `archive_transform`

**Purpose**: Scrunch or clean a folded archive — reduce frequency channels / sub-integrations / phase bins, sum polarisations, remove the off-pulse baseline, or centre the profile. Targets are given as final counts; an integral scrunch factor is derived from the original dimensions (infeasible targets are skipped with a warning). The input file is never modified; the result is a new artifact.

**Backend**: pam  
**Long-running**: yes  
**Annotations**: `readOnlyHint=false`, `destructiveHint=false`, `idempotentHint=true`, `openWorldHint=false`

#### Input Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://psrmcp.local/schemas/archive_transform.input.json",
  "type": "object",
  "required": ["archive"],
  "properties": {
    "archive": {
      "type": "string",
      "description": "Artifact ID or relative path under a read root"
    },
    "root": {"type": "string"},
    "fscrunch_to": {"type": "integer", "minimum": 1, "maximum": 8192},
    "tscrunch_to": {"type": "integer", "minimum": 1, "maximum": 10000},
    "sum_pols": {"type": "boolean", "default": false},
    "binscrunch_to": {"type": "integer", "minimum": 1, "maximum": 1048576},
    "remove_baseline": {"type": "boolean", "default": false},
    "centre": {"type": "boolean", "default": false},
    "output_name": {"type": "string", "maxLength": 128},
    "execution_mode": {"enum": ["auto", "sync", "async"], "default": "auto"}
  },
  "additionalProperties": false
}
```

#### Output Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://psrmcp.local/schemas/archive_transform.output.json",
  "type": "object",
  "required": ["result_type", "artifact", "dimensions", "warnings"],
  "properties": {
    "result_type": {"const": "completed"},
    "artifact": {"$ref": "#/$defs/ArtifactRef"},
    "dimensions": {"$ref": "#/$defs/Dimensions"},
    "warnings": {
      "type": "array",
      "items": {"$ref": "#/$defs/Warning"}
    }
  },
  "additionalProperties": false
}
```

#### Error Codes

`E_INVALID_INPUT`, `E_NOT_FOUND`, `E_ENV_MISSING`, `E_PROCESS_FAILED`, `E_QUOTA_EXCEEDED`

---

### 5.11 `archive_edit`

**Purpose**: Query and/or set archive metadata through psredit (e.g. `name`, `dm`, `freq`, `bw`, `int:mjd`, `int[0]:wt`). Query values are read from the input archive (pre-edit); `set` assignments are written to a new artifact — the input stays untouched.

**Backend**: psredit  
**Long-running**: no  
**Annotations**: `readOnlyHint=false`, `destructiveHint=false`, `idempotentHint=true`, `openWorldHint=false`

#### Input Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://psrmcp.local/schemas/archive_edit.input.json",
  "type": "object",
  "required": ["archive"],
  "properties": {
    "archive": {
      "type": "string",
      "description": "Artifact ID or relative path under a read root"
    },
    "root": {"type": "string"},
    "query": {
      "type": "array",
      "maxItems": 64,
      "items": {"type": "string"},
      "description": "psredit attribute paths to read from the input archive (e.g. name, dm, int:mjd)"
    },
    "set": {
      "type": "object",
      "maxProperties": 64,
      "additionalProperties": {"type": "string"},
      "description": "psredit assignments written to the output artifact (e.g. {\"dm\": \"2.644\"})"
    },
    "output_name": {"type": "string", "maxLength": 128}
  },
  "additionalProperties": false
}
```

#### Output Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://psrmcp.local/schemas/archive_edit.output.json",
  "type": "object",
  "required": ["result_type", "values", "warnings"],
  "properties": {
    "result_type": {"const": "completed"},
    "artifact": {"anyOf": [{"$ref": "#/$defs/ArtifactRef"}, {"type": "null"}]},
    "values": {"type": "object", "additionalProperties": true},
    "warnings": {
      "type": "array",
      "items": {"$ref": "#/$defs/Warning"}
    }
  },
  "additionalProperties": false
}
```

#### Error Codes

`E_INVALID_INPUT`, `E_NOT_FOUND`, `E_ENV_MISSING`, `E_PROCESS_FAILED`

---

### 5.12 `dedisperse`

**Purpose**: Applies coherent or incoherent dedispersion and folds the data with dspsr. Coherent dedispersion requires a matching ephemeris.

**Backend**: dspsr  
**Long-running**: yes  
**Annotations**: `readOnlyHint=false`, `destructiveHint=false`, `idempotentHint=true`, `openWorldHint=false`

#### Input Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://psrmcp.local/schemas/dedisperse.input.json",
  "type": "object",
  "required": ["input", "ephemeris"],
  "properties": {
    "input": {
      "type": "string",
      "description": "Artifact ID / relative path of raw data or an archive"
    },
    "root": {"type": "string"},
    "ephemeris": {
      "type": "string",
      "description": "An artifact ID, a .par path under a read root, or a pulsar name"
    },
    "mode": {
      "enum": ["coherent", "incoherent"],
      "default": "coherent"
    },
    "dm_override": {
      "type": "number",
      "exclusiveMinimum": 0,
      "description": "Explicit DM override; recorded in provenance"
    },
    "output_format": {
      "enum": ["psrfits", "psrarchive"],
      "default": "psrfits"
    },
    "nsubint": {
      "type": "integer",
      "minimum": 1,
      "maximum": 10000
    },
    "nchan": {
      "type": "integer",
      "minimum": 1,
      "maximum": 8192
    },
    "nbin": {
      "type": "integer",
      "minimum": 1,
      "maximum": 1048576
    },
    "threads": {
      "type": "integer",
      "minimum": 1,
      "maximum": 64
    },
    "output_name": {"type": "string", "maxLength": 128},
    "execution_mode": {
      "enum": ["auto", "sync", "async"],
      "default": "auto"
    }
  },
  "additionalProperties": false
}
```

#### Output Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://psrmcp.local/schemas/dedisperse.output.json",
  "oneOf": [
    {"$ref": "#/$defs/AsyncAccepted"},
    {
      "type": "object",
      "required": [
        "result_type",
        "artifact",
        "dm_used",
        "dimensions",
        "duration_s",
        "warnings"
      ],
      "properties": {
        "result_type": {"const": "completed"},
        "artifact": {"$ref": "#/$defs/ArtifactRef"},
        "dm_used": {"type": "number", "minimum": 0},
        "dimensions": {
          "type": "object",
          "required": ["nsubint", "nchan", "npol", "nbin"],
          "properties": {
            "nsubint": {"type": "integer", "minimum": 0},
            "nchan": {"type": "integer", "minimum": 0},
            "npol": {"type": "integer", "minimum": 0},
            "nbin": {"type": "integer", "minimum": 0}
          },
          "additionalProperties": false
        },
        "duration_s": {"type": "number", "minimum": 0},
        "log_artifact": {"type": "string"},
        "warnings": {
          "type": "array",
          "items": {"$ref": "#/$defs/Warning"}
        }
      },
      "additionalProperties": false
    }
  ]
}
```

#### Error Codes

`E_ENV_MISSING`, `E_INVALID_INPUT`, `E_NOT_FOUND`, `E_EPHEMERIS_MISMATCH`, `E_FORMAT_UNSUPPORTED`, `E_PROCESS_FAILED`, `E_TIMEOUT`, `E_CANCELLED`, `E_QUOTA_EXCEEDED`

---

### 5.13 `rfi_scan`

**Purpose**: Analyses per-channel statistics of an archive and produces a suggested bad-channel mask. Read-only; does not modify data.

**Backend**: psrchive / clfd  
**Long-running**: no  
**Annotations**: `readOnlyHint=true`, `destructiveHint=false`, `idempotentHint=true`, `openWorldHint=false`

#### Input Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://psrmcp.local/schemas/rfi_scan.input.json",
  "type": "object",
  "required": ["archive"],
  "properties": {
    "archive": {"type": "string"},
    "root": {"type": "string"},
    "method": {
      "enum": ["psrchive_median", "spectral_kurtosis", "bandpass"],
      "default": "psrchive_median"
    },
    "threshold_sigma": {
      "type": "number",
      "minimum": 0.5,
      "maximum": 20,
      "default": 3.0
    },
    "generate_plot": {"type": "boolean", "default": true}
  },
  "additionalProperties": false
}
```

#### Output Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://psrmcp.local/schemas/rfi_scan.output.json",
  "type": "object",
  "required": ["result_type", "suggested_mask", "statistics", "warnings"],
  "properties": {
    "result_type": {"const": "completed"},
    "suggested_mask": {
      "type": "object",
      "required": ["channels", "fraction", "n_channels"],
      "properties": {
        "channels": {
          "type": "array",
          "items": {"type": "integer", "minimum": 0}
        },
        "fraction": {"type": "number", "minimum": 0, "maximum": 1},
        "n_channels": {"type": "integer", "minimum": 0}
      },
      "additionalProperties": false
    },
    "statistics": {
      "type": "object",
      "additionalProperties": true,
      "description": "Statistics differ by method; always includes flagged_count and threshold at minimum"
    },
    "plot_artifact": {"type": ["string", "null"]},
    "warnings": {
      "type": "array",
      "items": {"$ref": "#/$defs/Warning"}
    }
  },
  "additionalProperties": false
}
```

#### Error Codes

`E_INVALID_INPUT`, `E_NOT_FOUND`, `E_ENV_MISSING`, `E_FORMAT_UNSUPPORTED`, `E_PROCESS_FAILED`

---

### 5.14 `rfi_zap`

**Purpose**: Applies a channel mask to an archive, producing a new archive. Supports an explicit channel list or an automatic method.

**Backend**: psrchive / clfd  
**Long-running**: no  
**Annotations**: `readOnlyHint=false`, `destructiveHint=false`, `idempotentHint=true`, `openWorldHint=false`

#### Input Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://psrmcp.local/schemas/rfi_zap.input.json",
  "type": "object",
  "required": ["archive", "mask"],
  "properties": {
    "archive": {"type": "string"},
    "root": {"type": "string"},
    "mask": {
      "oneOf": [
        {
          "type": "object",
          "required": ["channels"],
          "properties": {
            "channels": {
              "type": "array",
              "minItems": 1,
              "maxItems": 8192,
              "items": {"type": "integer", "minimum": 0}
            }
          },
          "additionalProperties": false
        },
        {
          "type": "object",
          "required": ["auto"],
          "properties": {
            "auto": {
              "type": "object",
              "required": ["method"],
              "properties": {
                "method": {
                  "enum": [
                    "psrchive_median",
                    "spectral_kurtosis",
                    "bandpass"
                  ]
                },
                "threshold_sigma": {
                  "type": "number",
                  "minimum": 0.5,
                  "maximum": 20,
                  "default": 3.0
                }
              },
              "additionalProperties": false
            }
          },
          "additionalProperties": false
        }
      ]
    },
    "output_name": {"type": "string", "maxLength": 128}
  },
  "additionalProperties": false
}
```

#### Output Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://psrmcp.local/schemas/rfi_zap.output.json",
  "type": "object",
  "required": [
    "result_type",
    "artifact",
    "n_channels_zapped",
    "zapped_fraction",
    "warnings"
  ],
  "properties": {
    "result_type": {"const": "completed"},
    "artifact": {"$ref": "#/$defs/ArtifactRef"},
    "n_channels_zapped": {"type": "integer", "minimum": 0},
    "zapped_fraction": {"type": "number", "minimum": 0, "maximum": 1},
    "warnings": {
      "type": "array",
      "items": {"$ref": "#/$defs/Warning"}
    }
  },
  "additionalProperties": false
}
```

#### Error Codes

`E_INVALID_INPUT`, `E_NOT_FOUND`, `E_ENV_MISSING`, `E_PROCESS_FAILED`, `E_QUOTA_EXCEEDED`

---

### 5.15 `profile_standardize`

**Purpose**: Produces a standard profile from an archive for use in TOA extraction.

**Backend**: pas  
**Long-running**: no  
**Annotations**: `readOnlyHint=false`, `destructiveHint=false`, `idempotentHint=true`, `openWorldHint=false`

#### Input Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://psrmcp.local/schemas/profile_standardize.input.json",
  "type": "object",
  "required": ["archive"],
  "properties": {
    "archive": {"type": "string"},
    "root": {"type": "string"},
    "align": {
      "enum": ["profile", "peak", "none"],
      "default": "profile"
    },
    "smooth": {"type": "boolean", "default": false},
    "nbin": {
      "type": "integer",
      "minimum": 1,
      "maximum": 1048576
    },
    "output_name": {"type": "string", "maxLength": 128}
  },
  "additionalProperties": false
}
```

#### Output Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://psrmcp.local/schemas/profile_standardize.output.json",
  "type": "object",
  "required": ["result_type", "artifact", "nbin", "warnings"],
  "properties": {
    "result_type": {"const": "completed"},
    "artifact": {"$ref": "#/$defs/ArtifactRef"},
    "nbin": {"type": "integer", "minimum": 1},
    "snr": {"type": ["number", "null"]},
    "warnings": {
      "type": "array",
      "items": {"$ref": "#/$defs/Warning"}
    }
  },
  "additionalProperties": false
}
```

#### Error Codes

`E_INVALID_INPUT`, `E_NOT_FOUND`, `E_ENV_MISSING`, `E_PROCESS_FAILED`, `E_QUOTA_EXCEEDED`

---

### 5.16 `profile_measure`

**Purpose**: Measures profile metrics of an archive, such as S/N, FWHM, flux density, and polarization parameters. Read-only.

**Backend**: psrstat / vap / psrchive Python bindings  
**Long-running**: no  
**Annotations**: `readOnlyHint=true`, `destructiveHint=false`, `idempotentHint=true`, `openWorldHint=false`

#### Input Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://psrmcp.local/schemas/profile_measure.input.json",
  "type": "object",
  "required": ["archive"],
  "properties": {
    "archive": {"type": "string"},
    "root": {"type": "string"},
    "standard_profile": {
      "type": "string",
      "description": "Optional; artifact ID of a standard profile"
    },
    "metrics": {
      "type": "array",
      "items": {
        "enum": ["snr", "width", "flux", "polarization", "dm"]
      },
      "default": ["snr", "width"],
      "maxItems": 5
    },
    "generate_plot": {"type": "boolean", "default": true},
    "plot_type": {
      "enum": ["profile", "phase_frequency"],
      "default": "profile"
    }
  },
  "additionalProperties": false
}
```

#### Output Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://psrmcp.local/schemas/profile_measure.output.json",
  "type": "object",
  "required": ["result_type", "metrics", "warnings"],
  "properties": {
    "result_type": {"const": "completed"},
    "metrics": {
      "type": "object",
      "properties": {
        "snr": {"type": ["number", "null"]},
        "width": {
          "type": ["object", "null"],
          "properties": {
            "fwhm_bins": {"type": ["number", "null"]},
            "fwhm_deg": {"type": ["number", "null"]},
            "fwhm_ms": {"type": ["number", "null"]}
          },
          "additionalProperties": false
        },
        "flux_mjy": {"type": ["number", "null"]},
        "polarization": {
          "type": ["object", "null"],
          "additionalProperties": true
        },
        "dm": {"type": ["number", "null"]}
      },
      "additionalProperties": false
    },
    "plot_artifact": {"type": ["string", "null"]},
    "warnings": {
      "type": "array",
      "items": {"$ref": "#/$defs/Warning"}
    }
  },
  "additionalProperties": false
}
```

#### Error Codes

`E_INVALID_INPUT`, `E_NOT_FOUND`, `E_FORMAT_UNSUPPORTED`, `E_PROCESS_FAILED`

---

### 5.17 `profile_optimize`

**Purpose**: Search period and DM for the maximum profile S/N (pdmp). Reports the best period (ms) and DM with uncertainties as corrections to apply; the input archive is never modified.

**Backend**: pdmp  
**Long-running**: yes  
**Annotations**: `readOnlyHint=true`, `destructiveHint=false`, `idempotentHint=true`, `openWorldHint=false`

#### Input Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://psrmcp.local/schemas/profile_optimize.input.json",
  "type": "object",
  "required": ["archive"],
  "properties": {
    "archive": {
      "type": "string",
      "description": "Artifact ID or relative path under a read root"
    },
    "root": {"type": "string"},
    "dm_offset": {"type": "number", "default": 0.0},
    "dm_half_range": {"type": "number", "minimum": 0},
    "dm_step": {"type": "number", "exclusiveMinimum": 0},
    "period_ms": {"type": "number", "exclusiveMinimum": 0},
    "period_offset_us": {"type": "number", "default": 0.0},
    "period_half_range_us": {"type": "number", "minimum": 0},
    "period_step_us": {"type": "number", "exclusiveMinimum": 0},
    "max_channels": {"type": "integer", "minimum": 1, "maximum": 8192},
    "max_subints": {"type": "integer", "minimum": 1, "maximum": 10000},
    "standard_profile": {"type": "string"},
    "execution_mode": {"enum": ["auto", "sync", "async"], "default": "auto"}
  },
  "additionalProperties": false
}
```

#### Output Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://psrmcp.local/schemas/profile_optimize.output.json",
  "type": "object",
  "required": ["result_type", "warnings"],
  "properties": {
    "result_type": {"const": "completed"},
    "best_period_ms": {"anyOf": [{"type": "number"}, {"type": "null"}]},
    "period_error_ms": {"anyOf": [{"type": "number"}, {"type": "null"}]},
    "best_dm": {"anyOf": [{"type": "number"}, {"type": "null"}]},
    "dm_error": {"anyOf": [{"type": "number"}, {"type": "null"}]},
    "best_snr": {"anyOf": [{"type": "number"}, {"type": "null"}]},
    "warnings": {
      "type": "array",
      "items": {"$ref": "#/$defs/Warning"}
    }
  },
  "additionalProperties": false
}
```

#### Error Codes

`E_INVALID_INPUT`, `E_NOT_FOUND`, `E_ENV_MISSING`, `E_PROCESS_FAILED`

---

### 5.18 `single_pulse_analyze`

**Purpose**: Single-pulse analysis (psrspa). At least one analysis mode is required, and the modes split into two exclusive groups: (a) phase-resolved histograms (flux / polarisation degree / PA / linear / circular), maximum amplitude inside phase ranges, and linear-vs-PA scatter data; (b) per-pulse detection (`find_pulses`, giant-pulse search with an allowlisted finder algorithm) — psrspa's `-a` returns early and silently skips group (a), so the tool rejects the combination up front. Phase-resolved histograms are written one file per phase bin in the requested range (e.g. 411 files for range 0.3-0.7 on nbin=1024); they are bundled into a single text artifact with `# >>> <filename>` section headers. Max-amplitude entries carry phase bin indices (not turns), verified against the installed build. Histogram modes require a psrspa build with GSL support.

**Backend**: psrspa  
**Long-running**: yes  
**Annotations**: `readOnlyHint=true`, `destructiveHint=false`, `idempotentHint=true`, `openWorldHint=false`

#### Input Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://psrmcp.local/schemas/single_pulse_analyze.input.json",
  "type": "object",
  "required": [
    "archive"
  ],
  "properties": {
    "archive": {
      "type": "string",
      "description": "Artifact ID or relative path under a read root"
    },
    "root": {
      "type": "string"
    },
    "histograms": {
      "type": "array",
      "items": {
        "enum": [
          "flux",
          "poldeg",
          "polang",
          "linear",
          "circular"
        ],
        "type": "string"
      },
      "maxItems": 5,
      "default": []
    },
    "find_max_amplitude": {
      "type": "boolean",
      "default": false
    },
    "scatter_linear_pa": {
      "type": "boolean",
      "default": false
    },
    "find_pulses": {
      "type": "boolean",
      "default": false
    },
    "pulse_algorithm": {
      "type": "string",
      "default": "above"
    },
    "min_pulse_bins": {
      "type": "integer",
      "minimum": 16,
      "maximum": 65536
    },
    "phase_ranges": {
      "type": "array",
      "items": {
        "type": "number"
      },
      "minItems": 2,
      "maxItems": 16
    },
    "hist_bins": {
      "type": "integer",
      "minimum": 2,
      "maximum": 10000
    },
    "flux_hist_max": {
      "type": "number",
      "exclusiveMinimum": 0
    },
    "pol_threshold": {
      "type": "number",
      "exclusiveMinimum": 0
    },
    "log_scale": {
      "type": "boolean",
      "default": false
    },
    "mean_offset": {
      "type": "number"
    },
    "std_profile": {
      "type": "string"
    },
    "execution_mode": {
      "enum": [
        "auto",
        "sync",
        "async"
      ],
      "default": "auto"
    }
  },
  "additionalProperties": false
}
```

#### Output Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://psrmcp.local/schemas/single_pulse_analyze.output.json",
  "type": "object",
  "required": ["result_type", "n_histogram_files", "pulses", "max_amplitudes", "warnings"],
  "properties": {
    "result_type": {"const": "completed"},
    "histograms_artifact": {"anyOf": [{"$ref": "#/$defs/ArtifactRef"}, {"type": "null"}]},
    "n_histogram_files": {"type": "integer", "minimum": 0},
    "pulses_artifact": {"anyOf": [{"$ref": "#/$defs/ArtifactRef"}, {"type": "null"}]},
    "pulses": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["archive_file", "subint", "flux", "phase", "width_bins", "bscrunch_factor"],
        "properties": {
          "archive_file": {"type": "string"},
          "subint": {"type": "integer", "minimum": 0},
          "flux": {"type": "number"},
          "phase": {"type": "number"},
          "width_bins": {"type": "number", "minimum": 0},
          "bscrunch_factor": {"type": "integer", "minimum": 1}
        },
        "additionalProperties": false
      }
    },
    "max_amplitudes": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["archive_file", "subint", "bscrunch_factor", "bin_min", "bin_max", "max_bin", "max_amplitude"],
        "properties": {
          "archive_file": {"type": "string"},
          "subint": {"type": "integer", "minimum": 0},
          "bscrunch_factor": {"type": "integer", "minimum": 1},
          "bin_min": {"type": "integer", "minimum": 0},
          "bin_max": {"type": "integer", "minimum": 0},
          "max_bin": {"type": "integer", "minimum": 0},
          "max_amplitude": {"type": "number"},
          "b_sigma": {"anyOf": [{"type": "number"}, {"type": "null"}]}
        },
        "additionalProperties": false
      }
    },
    "warnings": {
      "type": "array",
      "items": {"$ref": "#/$defs/Warning"}
    }
  },
  "additionalProperties": false
}

#### Error Codes

`E_INVALID_INPUT`, `E_NOT_FOUND`, `E_ENV_MISSING`, `E_PROCESS_FAILED`

---

### 5.19 `rotation_measure_fit`

**Purpose**: Fit the rotation measure (rmfit) of a calibrated, tscrunched, dedispersed archive. Methods (at least one): brute-force search over trial RMs (`brute_force` for auto bounds or `rm_range` for explicit `min,max,steps` bounds — mutually exclusive), iterative two-halves-of-band refinement (`refine`), or the quadratic method by Noutsos et al. 2008 (`quadratic_window` / `quadratic_per_bin`, phase windows in turns). `save_rm_archive=true` additionally writes a new archive artifact with the per-sub-int auxiliary RM applied. rmfit's text output is parsed defensively; RM values that cannot be parsed are reported via `W_RMFIT_UNPARSED`.

**Backend**: rmfit  
**Long-running**: yes  
**Annotations**: `readOnlyHint=true`, `destructiveHint=false`, `idempotentHint=true`, `openWorldHint=false`

#### Input Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://psrmcp.local/schemas/rotation_measure_fit.input.json",
  "type": "object",
  "required": [
    "archive"
  ],
  "properties": {
    "archive": {
      "type": "string",
      "description": "Artifact ID or relative path under a read root"
    },
    "root": {
      "type": "string"
    },
    "brute_force": {
      "type": "boolean",
      "default": false
    },
    "rm_range": {
      "type": "array",
      "items": {
        "type": "number"
      },
      "minItems": 3,
      "maxItems": 3
    },
    "refine": {
      "type": "boolean",
      "default": false
    },
    "quadratic_window": {
      "type": "array",
      "items": {
        "type": "number"
      },
      "minItems": 2,
      "maxItems": 2
    },
    "quadratic_per_bin": {
      "type": "array",
      "items": {
        "type": "number"
      },
      "minItems": 2,
      "maxItems": 2
    },
    "threshold_sigma": {
      "type": "number",
      "exclusiveMinimum": 0
    },
    "rm_max_upper": {
      "type": "number",
      "minimum": 0
    },
    "rm_per_dm": {
      "type": "number",
      "exclusiveMinimum": 0
    },
    "rm_initial": {
      "type": "number"
    },
    "save_rm_archive": {
      "type": "boolean",
      "default": false
    },
    "output_name": {
      "type": "string",
      "maxLength": 128
    },
    "execution_mode": {
      "enum": [
        "auto",
        "sync",
        "async"
      ],
      "default": "auto"
    }
  },
  "additionalProperties": false
}
```

#### Output Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://psrmcp.local/schemas/rotation_measure_fit.output.json",
  "type": "object",
  "required": ["result_type", "data_artifacts", "warnings"],
  "properties": {
    "result_type": {"const": "completed"},
    "best_rm": {"anyOf": [{"type": "number"}, {"type": "null"}]},
    "best_rm_error": {"anyOf": [{"type": "number"}, {"type": "null"}]},
    "search_rm": {"anyOf": [{"type": "number"}, {"type": "null"}]},
    "search_rm_error": {"anyOf": [{"type": "number"}, {"type": "null"}]},
    "refined_rm": {"anyOf": [{"type": "number"}, {"type": "null"}]},
    "converged_iterations": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
    "chi2_reduced": {"anyOf": [{"type": "number"}, {"type": "null"}]},
    "rm_archive": {"anyOf": [{"$ref": "#/$defs/ArtifactRef"}, {"type": "null"}]},
    "data_artifacts": {"type": "array", "items": {"$ref": "#/$defs/ArtifactRef"}},
    "warnings": {
      "type": "array",
      "items": {"$ref": "#/$defs/Warning"}
    }
  },
  "additionalProperties": false
}
```

#### Error Codes

`E_INVALID_INPUT`, `E_NOT_FOUND`, `E_ENV_MISSING`, `E_PROCESS_FAILED`

---

### 5.20 `polarization_calibrate`

**Purpose**: Polarimetrically (and optionally flux) calibrate a pulsar archive with pac. Exactly one calibration source: `calibrators` (PolnCal / FluxCal-On / FluxCal-Off scans; the calibrator database is rebuilt in memory per call, so each call is self-contained) or `model` (a pcm/pacv solution archive applied directly). Models: `singleaxis` (default), `polar` (`-s`), `reception` (`-S`). The `*_match` flags can only RELAX pac's matching criteria (each disables one criterion); `match_sequence` restricts to calibrators observed before/after the pulsar. pac catches per-file errors internally and still exits 0, so success is verified by the presence of the calibrated output; when polarimetric calibration succeeds but flux calibration does not, pac appends `P` to the output name and `W_PAC_POLN_ONLY` / `W_PAC_FLUXCAL_FAILED` is reported (the `P` suffix is expected and silent when `flux_calibration=false`). Model solutions run without a calibrator database, so flux calibration is skipped with `W_PAC_NO_FLUXDB`.

**Backend**: pac  
**Long-running**: yes  
**Annotations**: `readOnlyHint=false`, `destructiveHint=false`, `idempotentHint=true`, `openWorldHint=false`

#### Input Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://psrmcp.local/schemas/polarization_calibrate.input.json",
  "type": "object",
  "required": [
    "archive"
  ],
  "properties": {
    "archive": {
      "type": "string",
      "description": "Pulsar observation: artifact ID or relative path under a read root"
    },
    "root": {
      "type": "string"
    },
    "calibrators": {
      "type": "array",
      "items": {
        "type": "string"
      },
      "minItems": 1,
      "maxItems": 64,
      "description": "Calibrator scan archives (PolnCal / FluxCal-On / FluxCal-Off); exclusive with model"
    },
    "model": {
      "type": "string",
      "description": "pcm/pacv calibrator solution archive applied directly (-A); exclusive with calibrators"
    },
    "model_type": {
      "enum": [
        "polar",
        "reception",
        "singleaxis"
      ],
      "default": "singleaxis"
    },
    "flux_calibration": {
      "type": "boolean",
      "default": true
    },
    "match_time": {
      "type": "boolean",
      "default": true,
      "description": "false disables pac's time matching criterion (-T)"
    },
    "match_coordinates": {
      "type": "boolean",
      "default": true,
      "description": "false disables pac's sky-coordinate matching criterion (-c)"
    },
    "match_instrument": {
      "type": "boolean",
      "default": true,
      "description": "false disables pac's instrument matching criterion (-Z)"
    },
    "match_frequency": {
      "type": "boolean",
      "default": true,
      "description": "false disables pac's centre-frequency matching criterion (-F)"
    },
    "match_bandwidth": {
      "type": "boolean",
      "default": true,
      "description": "false disables pac's bandwidth matching criterion (-b)"
    },
    "allow_opposite_sideband": {
      "type": "boolean",
      "default": false
    },
    "match_sequence": {
      "enum": [
        "after",
        "any",
        "before"
      ],
      "default": "any"
    },
    "per_channel_matching": {
      "type": "boolean",
      "default": false
    },
    "fscrunch_to_cal": {
      "type": "boolean",
      "default": false
    },
    "gain_normalize": {
      "type": "boolean",
      "default": false
    },
    "output_name": {
      "type": "string",
      "maxLength": 128
    },
    "execution_mode": {
      "enum": [
        "auto",
        "sync",
        "async"
      ],
      "default": "auto"
    }
  },
  "additionalProperties": false
}
```

#### Output Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://psrmcp.local/schemas/polarization_calibrate.output.json",
  "type": "object",
  "required": ["result_type", "calibrated_archive", "poln_calibrated", "flux_calibrated", "warnings"],
  "properties": {
    "result_type": {"const": "completed"},
    "calibrated_archive": {"$ref": "#/$defs/ArtifactRef"},
    "poln_calibrated": {"type": "boolean"},
    "flux_calibrated": {"type": "boolean"},
    "poln_calibrator_source": {"anyOf": [{"type": "string"}, {"type": "null"}]},
    "flux_calibrator_source": {"anyOf": [{"type": "string"}, {"type": "null"}]},
    "mean_sefd_jy": {"anyOf": [{"type": "number"}, {"type": "null"}]},
    "warnings": {
      "type": "array",
      "items": {"$ref": "#/$defs/Warning"}
    }
  },
  "additionalProperties": false
}
```

#### Error Codes

`E_INVALID_INPUT`, `E_NOT_FOUND`, `E_ENV_MISSING`, `E_PROCESS_FAILED`

---

### 5.21 `calibrator_database`

**Purpose**: Build a pac calibrator database summary (`database.txt` artifact) from calibrator archive scans. Entries are indexed by header metadata (time, position, frequency, receiver) so pac can match them against pulsar observations; all entries are parsed and returned inline (`entries` with `obs_type` from the archive header, e.g. PolnCal / FluxCal-On / FluxCal-Off, plus per-type counts in `entry_types`). pac identifies calibrator files by extension, so every listed file must have one (`E_INVALID_INPUT` otherwise); the supplied extensions are added to pac's default scan set (cf/pcal/fcal/pfit) via `-u`. "No calibrators found" exits non-zero and surfaces as `E_PROCESS_FAILED`.

**Backend**: pac  
**Long-running**: yes  
**Annotations**: `readOnlyHint=false`, `destructiveHint=false`, `idempotentHint=true`, `openWorldHint=false`

#### Input Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://psrmcp.local/schemas/calibrator_database.input.json",
  "type": "object",
  "required": [
    "calibrators"
  ],
  "properties": {
    "calibrators": {
      "type": "array",
      "items": {
        "type": "string"
      },
      "minItems": 1,
      "maxItems": 64,
      "description": "Calibrator scan archives (PolnCal / FluxCal-On / FluxCal-Off / pfit solutions); each file must have an extension"
    },
    "root": {
      "type": "string"
    },
    "output_name": {
      "type": "string",
      "maxLength": 128
    },
    "execution_mode": {
      "enum": [
        "auto",
        "sync",
        "async"
      ],
      "default": "auto"
    }
  },
  "additionalProperties": false
}
```

#### Output Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://psrmcp.local/schemas/calibrator_database.output.json",
  "type": "object",
  "required": ["result_type", "database_artifact", "n_calibrators", "warnings"],
  "properties": {
    "result_type": {"const": "completed"},
    "database_artifact": {"$ref": "#/$defs/ArtifactRef"},
    "n_calibrators": {"type": "integer", "minimum": 0},
    "entry_types": {"type": "object", "additionalProperties": {"type": "integer"}},
    "entries": {"type": "array", "items": {"$ref": "#/$defs/CalibratorDatabaseEntry"}},
    "warnings": {
      "type": "array",
      "items": {"$ref": "#/$defs/Warning"}
    }
  },
  "additionalProperties": false
}
```

#### Error Codes

`E_INVALID_INPUT`, `E_NOT_FOUND`, `E_ENV_MISSING`, `E_PROCESS_FAILED`

---

### 5.22 `toa_extract`

**Purpose**: Extracts TOAs from an archive against a standard profile and produces a TEMPO `.tim` file.

**Backend**: pat  
**Long-running**: yes  
**Annotations**: `readOnlyHint=false`, `destructiveHint=false`, `idempotentHint=true`, `openWorldHint=false`

#### Input Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://psrmcp.local/schemas/toa_extract.input.json",
  "type": "object",
  "required": ["archive", "standard_profile"],
  "properties": {
    "archive": {"type": "string"},
    "root": {"type": "string"},
    "standard_profile": {"type": "string"},
    "algorithm": {
      "enum": ["PGS", "GIS", "PIS", "ZPS", "SIS", "FDM"],
      "default": "PGS"
    },
    "mjd_range": {
      "type": "array",
      "minItems": 2,
      "maxItems": 2,
      "items": {"type": "number"}
    },
    "freq_range_mhz": {
      "type": "array",
      "minItems": 2,
      "maxItems": 2,
      "items": {"type": "number", "minimum": 0}
    },
    "min_snr": {
      "type": "number",
      "minimum": 0,
      "default": 0
    },
    "output_name": {"type": "string", "maxLength": 128},
    "execution_mode": {
      "enum": ["auto", "sync", "async"],
      "default": "auto"
    }
  },
  "additionalProperties": false
}
```

Semantic validation:

- `mjd_range[0] <= mjd_range[1]`
- `freq_range_mhz[0] <= freq_range_mhz[1]`

#### Output Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://psrmcp.local/schemas/toa_extract.output.json",
  "oneOf": [
    {"$ref": "#/$defs/AsyncAccepted"},
    {
      "type": "object",
      "required": [
        "result_type",
        "tim_artifact",
        "n_toas",
        "warnings"
      ],
      "properties": {
        "result_type": {"const": "completed"},
        "tim_artifact": {"$ref": "#/$defs/ArtifactRef"},
        "n_toas": {"type": "integer", "minimum": 0},
        "mjd_range": {
          "type": ["array", "null"],
          "items": {"type": "number"},
          "minItems": 2,
          "maxItems": 2
        },
        "snr": {
          "type": ["object", "null"],
          "properties": {
            "min": {"type": ["number", "null"]},
            "median": {"type": ["number", "null"]},
            "max": {"type": ["number", "null"]}
          },
          "additionalProperties": false
        },
        "warnings": {
          "type": "array",
          "items": {"$ref": "#/$defs/Warning"}
        }
      },
      "additionalProperties": false
    }
  ]
}
```

#### Error Codes

`E_INVALID_INPUT`, `E_NOT_FOUND`, `E_ENV_MISSING`, `E_PROCESS_FAILED`, `E_TIMEOUT`, `E_CANCELLED`, `E_QUOTA_EXCEEDED`

---

### 5.23 `timing_fit`

**Purpose**: Runs a timing fit on `.par` + `.tim` with PINT or tempo2, returning the updated `.par`, fit statistics, residuals, and residual plots.

**Backend**: PINT worker / tempo2  
**Long-running**: yes  
**Annotations**: `readOnlyHint=false`, `destructiveHint=false`, `idempotentHint=true`, `openWorldHint=false`

#### Input Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://psrmcp.local/schemas/timing_fit.input.json",
  "type": "object",
  "required": ["par", "tim"],
  "properties": {
    "par": {
      "type": "string",
      "description": "Artifact ID or .par relative path under a read root"
    },
    "tim": {
      "type": "string",
      "description": "Artifact ID or .tim relative path under a read root"
    },
    "root": {"type": "string"},
    "backend": {
      "enum": ["pint", "tempo2"],
      "default": "pint"
    },
    "fit_params": {
      "type": "array",
      "items": {
        "type": "string",
        "pattern": "^[A-Za-z0-9_]+$"
      },
      "maxItems": 50,
      "description": "Defaults to the fit flags in the par file; when given explicitly, every entry must resolve in the par"
    },
    "weighting": {
      "enum": ["uniform", "1/sigma^2"],
      "default": "1/sigma^2"
    },
    "generate_plot": {"type": "boolean", "default": true},
    "max_iterations": {
      "type": "integer",
      "minimum": 1,
      "maximum": 1000,
      "default": 100
    },
    "output_name": {"type": "string", "maxLength": 128},
    "execution_mode": {
      "enum": ["auto", "sync", "async"],
      "default": "auto"
    }
  },
  "additionalProperties": false
}
```

#### Output Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://psrmcp.local/schemas/timing_fit.output.json",
  "oneOf": [
    {"$ref": "#/$defs/AsyncAccepted"},
    {
      "type": "object",
      "required": [
        "result_type",
        "par_artifact",
        "fit",
        "parameters",
        "warnings"
      ],
      "properties": {
        "result_type": {"const": "completed"},
        "par_artifact": {"$ref": "#/$defs/ArtifactRef"},
        "fit": {
          "type": "object",
          "required": [
            "n_toas",
            "chi2",
            "dof",
            "reduced_chi2",
            "rms_us",
            "wrms_us"
          ],
          "properties": {
            "n_toas": {"type": "integer", "minimum": 0},
            "chi2": {"type": "number", "minimum": 0},
            "dof": {"type": "integer", "minimum": 0},
            "reduced_chi2": {"type": "number", "minimum": 0},
            "rms_us": {"type": "number", "minimum": 0},
            "wrms_us": {"type": "number", "minimum": 0}
          },
          "additionalProperties": false
        },
        "parameters": {
          "type": "array",
          "items": {
            "type": "object",
            "required": ["name", "value", "fitted"],
            "properties": {
              "name": {"type": "string"},
              "value": {"type": "number"},
              "uncertainty": {"type": ["number", "null"]},
              "units": {"type": ["string", "null"]},
              "fitted": {"type": "boolean"}
            },
            "additionalProperties": false
          }
        },
        "residuals_artifact": {"type": ["string", "null"]},
        "plot_artifacts": {
          "type": "array",
          "items": {"type": "string"}
        },
        "warnings": {
          "type": "array",
          "items": {"$ref": "#/$defs/Warning"}
        }
      },
      "additionalProperties": false
    }
  ]
}
```

#### Error Codes

`E_INVALID_INPUT`, `E_NOT_FOUND`, `E_ENV_MISSING`, `E_VERSION_INCOMPATIBLE`, `E_PROCESS_FAILED`, `E_TIMEOUT`, `E_CANCELLED`

---

### 5.24 `timing_diagnose`

**Purpose**: Runs residual and model diagnostics on existing `.par` + `.tim` without refitting. Can check outliers, DM variations, JUMPs, and the residual distribution.

**Backend**: PINT / tempo2  
**Long-running**: no  
**Annotations**: `readOnlyHint=true`, `destructiveHint=false`, `idempotentHint=true`, `openWorldHint=false`

#### Input Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://psrmcp.local/schemas/timing_diagnose.input.json",
  "type": "object",
  "required": ["par", "tim"],
  "properties": {
    "par": {"type": "string"},
    "tim": {"type": "string"},
    "root": {"type": "string"},
    "backend": {
      "enum": ["pint", "tempo2"],
      "default": "pint"
    },
    "checks": {
      "type": "array",
      "items": {
        "enum": [
          "outliers",
          "dm",
          "jumps",
          "reduced_chi2",
          "residual_distribution"
        ]
      },
      "default": [
        "outliers",
        "dm",
        "jumps",
        "reduced_chi2",
        "residual_distribution"
      ],
      "maxItems": 5
    },
    "outlier_sigma": {
      "type": "number",
      "minimum": 1,
      "maximum": 20,
      "default": 5
    },
    "generate_plot": {"type": "boolean", "default": true}
  },
  "additionalProperties": false
}
```

#### Output Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://psrmcp.local/schemas/timing_diagnose.output.json",
  "type": "object",
  "required": [
    "result_type",
    "summary",
    "findings",
    "recommendations",
    "warnings"
  ],
  "properties": {
    "result_type": {"const": "completed"},
    "summary": {
      "type": "object",
      "properties": {
        "n_toas": {"type": "integer", "minimum": 0},
        "reduced_chi2": {"type": ["number", "null"]},
        "rms_us": {"type": ["number", "null"]},
        "n_outliers": {"type": "integer", "minimum": 0}
      },
      "additionalProperties": false
    },
    "findings": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["code", "severity", "message"],
        "properties": {
          "code": {"type": "string"},
          "severity": {"enum": ["info", "warning", "error"]},
          "message": {"type": "string"},
          "details": {"type": "object", "additionalProperties": true}
        },
        "additionalProperties": false
      }
    },
    "plots": {
      "type": "array",
      "items": {"type": "string"}
    },
    "recommendations": {
      "type": "array",
      "items": {"type": "string"}
    },
    "warnings": {
      "type": "array",
      "items": {"$ref": "#/$defs/Warning"}
    }
  },
  "additionalProperties": false
}
```

#### Error Codes

`E_INVALID_INPUT`, `E_NOT_FOUND`, `E_ENV_MISSING`, `E_VERSION_INCOMPATIBLE`, `E_PROCESS_FAILED`

---

### 5.25 `artifact_list`

**Purpose**: Lists artifacts in the current workspace, with paging by kind, time, and cursor.

**Backend**: ArtifactStore  
**Long-running**: no  
**Annotations**: `readOnlyHint=true`, `destructiveHint=false`, `idempotentHint=true`, `openWorldHint=false`

#### Input Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://psrmcp.local/schemas/artifact_list.input.json",
  "type": "object",
  "properties": {
    "kind": {
      "enum": [
        "archive",
        "ephemeris",
        "tim",
        "par",
        "profile",
        "mask",
        "plot",
        "residuals",
        "log",
        "catalog",
        "json",
        "other"
      ]
    },
    "limit": {
      "type": "integer",
      "minimum": 1,
      "maximum": 500,
      "default": 50
    },
    "cursor": {"type": "string"},
    "created_after": {"type": "string", "format": "date-time"}
  },
  "additionalProperties": false
}
```

#### Output Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://psrmcp.local/schemas/artifact_list.output.json",
  "type": "object",
  "required": ["result_type", "artifacts", "count"],
  "properties": {
    "result_type": {"const": "completed"},
    "artifacts": {
      "type": "array",
      "items": {"$ref": "#/$defs/ArtifactRef"}
    },
    "count": {"type": "integer", "minimum": 0},
    "next_cursor": {"type": ["string", "null"]}
  },
  "additionalProperties": false
}
```

#### Error Codes

`E_INVALID_INPUT`, `E_INTERNAL`

---

### 5.26 `job_get`

**Purpose**: Queries the status and result of an asynchronous job. Fallback used only when the client does not support the MCP Tasks extension.

**Backend**: JobManager  
**Long-running**: no  
**Annotations**: `readOnlyHint=true`, `destructiveHint=false`, `idempotentHint=true`, `openWorldHint=false`

#### Input Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://psrmcp.local/schemas/job_get.input.json",
  "type": "object",
  "required": ["job_id"],
  "properties": {
    "job_id": {
      "type": "string",
      "pattern": "^job_[A-Za-z0-9_-]{8,64}$"
    }
  },
  "additionalProperties": false
}
```

#### Output Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://psrmcp.local/schemas/job_get.output.json",
  "type": "object",
  "required": ["result_type", "job"],
  "properties": {
    "result_type": {"const": "completed"},
    "job": {"$ref": "#/$defs/JobRef"}
  },
  "additionalProperties": false
}
```

#### Error Codes

`E_INVALID_INPUT`, `E_TASK_NOT_FOUND`

---

### 5.27 `job_cancel`

**Purpose**: Requests cancellation of an asynchronous job. Cancellation is cooperative: the server sends `SIGTERM` first, and `SIGKILL`s the process group if it is still running after the grace period.

**Backend**: JobManager  
**Long-running**: no  
**Annotations**: `readOnlyHint=false`, `destructiveHint=false`, `idempotentHint=false`, `openWorldHint=false`

#### Input Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://psrmcp.local/schemas/job_cancel.input.json",
  "type": "object",
  "required": ["job_id"],
  "properties": {
    "job_id": {
      "type": "string",
      "pattern": "^job_[A-Za-z0-9_-]{8,64}$"
    },
    "force": {
      "type": "boolean",
      "default": false,
      "description": "Skip the grace period and kill the process group immediately"
    }
  },
  "additionalProperties": false
}
```

#### Output Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://psrmcp.local/schemas/job_cancel.output.json",
  "type": "object",
  "required": ["result_type", "job"],
  "properties": {
    "result_type": {"const": "completed"},
    "job": {"$ref": "#/$defs/JobRef"}
  },
  "additionalProperties": false
}
```

#### Error Codes

`E_INVALID_INPUT`, `E_TASK_NOT_FOUND`, `E_JOB_NOT_RUNNING`

---

## 6. Future Tools

The following tools are out of MVP scope but reserve the interface direction.

### 6.1 `artifact_delete`

- **Purpose**: delete a workspace artifact
- **Annotations**: `readOnlyHint=false`, `destructiveHint=true`, `idempotentHint=false`
- **Input**: `artifact_id`, `confirm: true`
- **Behaviour**: by default moves the artifact to a trash area with a TTL; executes only when `confirm=true`
- **Error codes**: `E_NOT_FOUND`, `E_INVALID_INPUT`, `E_QUOTA_EXCEEDED`

### 6.2 `pipeline_reduce`

- **Purpose**: inspect → dedisperse → RFI → standard profile → measure in a single call
- **Input**: `archive`, `pulsar_name?`, `dm?`, `rfi_strategy?`
- **Output**: the full artifact chain and a summary of each step
- **Note**: composite tools must return intermediate artifacts and keep completed steps on failure, so retries can resume

### 6.3 `compare_timing_models`

- **Purpose**: fit the same par/tim with PINT and with tempo2, then compare parameters and residuals
- **Input**: `par`, `tim`, `fit_params?`
- **Output**: both fit results, parameter differences, residual differences, and warnings

### 6.4 `polarization_calibrate`

- **Purpose**: polarization calibration based on pac
- **Input**: `archive`, `calibrator_archive`, `cal_database?`
- **Output**: the calibrated archive, calibration parameters, and diagnostic plots
- **Note**: strongly dependent on the observing system and the calibrator source; needs a separate design

### 6.5 `search_mode_process`

- **Purpose**: search-mode data processing (PRESTO / SIGPROC / heimdall)
- **Input**: `filterbank`, `dm_range`, `algorithm`
- **Output**: a candidate list and a folded archive
- **Note**: differs substantially from the existing dspsr/psrchive pipeline; recommend a separate module

---

## 7. Versioning and Compatibility

- Server versions follow SemVer
- Renaming tools or parameters is a breaking change and requires a major version
- Adding optional parameters and output fields is a minor version
- Output schemas use `result_type` to distinguish synchronous completion from asynchronous acceptance; a new `result_type` value requires a minor version
- Every artifact's `provenance.tool_version` records the server version at generation time
- Backend version changes do not affect the tool contract, but do affect scientific results; they must be recorded in `provenance.backend_version`
- A tool schema's `$id` can be used by clients for caching and compatibility checks

---

## 8. Implementation Checklist

- [ ] Every input schema sets `additionalProperties: false`
- [ ] Every path parameter passes allowlist and `..` validation
- [ ] Every command uses an argv array, never a shell
- [ ] Every long-running job supports cancellation and progress reporting
- [ ] Every output carries `provenance`
- [ ] Every error uses the unified `ErrorBody`
- [ ] Every tool carries the correct annotations
- [ ] Image results provide both the image content and an artifact ID
- [ ] Large files never go through the MCP resource base64 return path
- [ ] Integration tests cover inspect → dedisperse → RFI → profile → TOA → fit
