# 脉冲星数据处理 MCP Server 工具契约

- 文档版本：v0.1
- 日期：2026-10-17
- 配套文档：[DESIGN.md](./DESIGN.md)
- MCP 协议：2026-07-28
- Schema 方言：JSON Schema Draft 2020-12
- 状态：设计草案，尚未实现

---

## 1. 通用约定

### 1.1 命名与单位

- 工具名、参数名统一使用 `snake_case`
- 所有输入对象设置 `additionalProperties: false`
- 数值字段在名称中携带单位：
  - 时间：`_s`、`_ms`、`_us`
  - 频率：`_mhz`
  - 角度：`_deg`
  - 时间坐标：`_mjd`
  - 大小：`_bytes`
- 未注明时间系统的 MJD 默认是 UTC
- 时间字符串使用 RFC 3339 / ISO 8601
- 文本字段设置合理的 `minLength` / `maxLength`，拒绝控制字符

### 1.2 路径与 ID

- `artifact_id`：`art_` 前缀，工作区内的不可变产物
- `job_id`：`job_` 前缀，异步任务
- `archive`、`par`、`tim` 等参数可以接受：
  - artifact ID
  - 配置的 `read_roots` 下的相对路径
- 当相对路径可能匹配多个 root 时，用可选的 `root` 参数指定
- 绝对路径仅在 `allow_absolute_paths = true` 时接受，且仍必须位于白名单内
- `output_name` 只是人类可读标签，服务器生成唯一路径，不覆盖已有产物
- 路径参数禁止包含 `..`、控制字符、NUL

### 1.3 Tool 结果封装

MCP `tools/call` 返回：

```json
{
  "content": [
    {"type": "text", "text": "简短的人类可读摘要"}
  ],
  "structuredContent": {
    "result_type": "completed"
  },
  "isError": false
}
```

约定：

- `content`：Markdown 摘要，便于直接展示
- `structuredContent`：严格符合本文件的 `outputSchema`
- 图像通过 `content` 中的 `image` block 或 `resource_link` 返回
- 错误时 `isError: true`，`structuredContent.error` 为 `ErrorBody`
- 不在错误信息中返回完整 stderr；完整日志放入 `log_artifact_id`

### 1.4 长任务

可能耗时较长的工具支持 `execution_mode`：

- `auto`（默认）：预计超过 `long_job_threshold_s` 时转异步
- `sync`：同步等待，超过 `job_timeout_s` 返回 `E_TIMEOUT`
- `async`：立即返回 `result_type: "accepted"` 和 `JobRef`

异步结果有两种承载方式：

1. **MCP Tasks 扩展可用**：协议层返回 `CreateTaskResult`，客户端通过 `tasks/get`、`tasks/cancel` 操作；最终 `result` 使用本文件的 completed schema
2. **Tasks 扩展不可用**：工具返回 `accepted` + `JobRef`，客户端用 `job_get` / `job_cancel` 轮询和取消

`JobRef.status`：

- `queued`
- `working`
- `input_required`
- `completed`
- `failed`
- `cancelled`
- `interrupted`（服务重启后本地进程丢失）

### 1.5 错误处理

错误结果：

```json
{
  "isError": true,
  "structuredContent": {
    "result_type": "failed",
    "error": {
      "code": "E_EPHEMERIS_MISMATCH",
      "message": "Ephemeris source J0437-4715 does not match archive source J1744-1134",
      "hint": "请提供与观测匹配的 .par 文件，或显式设置 dm_override",
      "retryable": false,
      "details": {},
      "log_artifact_id": "art_..."
    }
  }
}
```

原则：

- 错误码稳定，不随实现细节变化
- `retryable` 明确告诉客户端是否值得重试
- 用户可修复的问题必须给出 `hint`
- 科学数据不匹配属于错误，不静默继续

---

## 2. 公共类型

以下 `$defs` 由所有工具 schema 引用。实现时可用 Pydantic 模型生成并展开，文档中用 `$ref` 表示。

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
          "description": "工作区相对路径；客户端不应假设该路径可直接访问"
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
          "description": "不可用时的原因"
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
        "catalog_version": {"type": "string"}
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

## 3. 错误码

| 错误码 | 含义 | 可重试 | 典型处理 |
|---|---|---|---|
| `E_CONFIG` | 配置无效或缺失 | 否 | 修正配置文件并重启 |
| `E_ENV_MISSING` | 后端二进制/Python 包不可用 | 否 | 安装或启用对应后端 |
| `E_INVALID_INPUT` | Schema 或语义校验失败 | 否 | 修正参数 |
| `E_NOT_FOUND` | artifact、文件、脉冲星不存在 | 否 | 检查 ID、路径或名称 |
| `E_PATH_DENIED` | 路径不在白名单内 | 否 | 使用允许的 data root |
| `E_FORMAT_UNSUPPORTED` | 数据格式或版本不支持 | 否 | 转换格式或启用对应后端 |
| `E_SOURCE_MISMATCH` | 输入数据源名不一致 | 否 | 确认输入是否属于同一脉冲星 |
| `E_EPHEMERIS_MISMATCH` | ephemeris 与观测不匹配 | 否 | 提供正确的 `.par` |
| `E_OUTPUT_CONFLICT` | 输出命名冲突且不允许覆盖 | 视情况 | 换一个 `output_name` |
| `E_PROCESS_FAILED` | 后端命令执行失败 | 视情况 | 查看 `log_artifact_id` |
| `E_TIMEOUT` | 超过任务超时 | 是 | 改用 async 或减少数据量 |
| `E_CANCELLED` | 任务被取消 | 是 | 确认后重跑 |
| `E_QUOTA_EXCEEDED` | 磁盘/内存/文件大小超限 | 否 | 清理工作区或调整限额 |
| `E_TASK_NOT_FOUND` | job/task ID 不存在或已过期 | 否 | 重新提交任务 |
| `E_JOB_NOT_RUNNING` | 对已结束任务执行取消 | 否 | 查询状态即可 |
| `E_VERSION_INCOMPATIBLE` | 工具/数据版本不兼容 | 否 | 统一工具链版本 |
| `E_INTERNAL` | 未预期的服务器错误 | 视情况 | 携带日志报告问题 |

---

## 4. 工具索引与 Annotations

| 工具 | `readOnlyHint` | `destructiveHint` | `idempotentHint` | `openWorldHint` | 长任务 |
|---|---|---|---|---|---|
| `pulsar_environment` | true | false | true | false | 否 |
| `data_find` | true | false | true | false | 否 |
| `catalog_search` | true | false | true | true | 否 |
| `catalog_get` | true | false | true | true | 否 |
| `ephemeris_export` | false | false | true | true | 否 |
| `archive_inspect` | true | false | true | false | 否 |
| `archive_convert` | false | false | true | false | 否 |
| `archive_merge` | false | false | true | false | 是 |
| `dedisperse` | false | false | true | false | 是 |
| `rfi_scan` | true | false | true | false | 否 |
| `rfi_zap` | false | false | true | false | 否 |
| `profile_standardize` | false | false | true | false | 否 |
| `profile_measure` | true | false | true | false | 否 |
| `toa_extract` | false | false | true | false | 是 |
| `timing_fit` | false | false | true | false | 是 |
| `timing_diagnose` | true | false | true | false | 否 |
| `artifact_list` | true | false | true | false | 否 |
| `job_get` | true | false | true | false | 否 |
| `job_cancel` | false | false | false | false | 否 |

说明：

- `idempotentHint` 表示相同输入和参数会得到等价产物；产物 ID 可能不同，但内容可缓存复用
- 所有工具都**不会覆盖输入文件**
- `catalog_search` / `catalog_get` / `ephemeris_export` 在 online 模式下会访问外部星表，因此标记 `openWorldHint: true`
- 未来 `artifact_delete` 将标记 `destructiveHint: true`

---

## 5. 工具详细契约

### 5.1 `pulsar_environment`

**用途**：返回服务器版本、MCP 协议版本、后端可用性、数据根目录、工作区和资源限额。建议作为每次会话的第一个调用。

**后端**：环境探测  
**长任务**：否  
**Annotations**：`readOnlyHint=true`, `destructiveHint=false`, `idempotentHint=true`, `openWorldHint=false`

#### 输入 Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://psrmcp.local/schemas/pulsar_environment.input.json",
  "type": "object",
  "properties": {
    "refresh": {
      "type": "boolean",
      "default": false,
      "description": "重新探测后端，忽略启动时的缓存结果"
    }
  },
  "additionalProperties": false
}
```

#### 输出 Schema

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

#### 错误码

`E_CONFIG`, `E_INTERNAL`

---

### 5.2 `data_find`

**用途**：在配置的 `read_roots` 内按 glob 查找数据文件，并给出格式初判。不会读取或修改文件内容。

**后端**：文件系统  
**长任务**：否  
**Annotations**：`readOnlyHint=true`, `destructiveHint=false`, `idempotentHint=true`, `openWorldHint=false`

#### 输入 Schema

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
      "description": "glob 模式，例如 '*.ar'、'J0437*'、'**/*.fits'"
    },
    "root": {
      "type": "string",
      "description": "read_roots 下的子目录；省略时搜索所有 root"
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

#### 输出 Schema

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

#### 错误码

`E_INVALID_INPUT`, `E_PATH_DENIED`, `E_NOT_FOUND`

---

### 5.3 `catalog_search`

**用途**：查询 ATNF/psrcat 星表。支持名称、位置锥、周期、DM 和双星筛选。至少提供一个筛选条件。

**后端**：psrcat / psrqpy / 本地缓存  
**长任务**：否  
**Annotations**：`readOnlyHint=true`, `destructiveHint=false`, `idempotentHint=true`, `openWorldHint=true`

#### 输入 Schema

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
    "binary_only": {"type": "boolean", "default": false},
    "fields": {
      "type": "array",
      "items": {
        "enum": [
          "name",
          "aliases",
          "ra_deg",
          "dec_deg",
          "p0_s",
          "f0_hz",
          "p1",
          "dm",
          "binary",
          "binary_model",
          "observatory",
          "catalog_version"
        ]
      },
      "maxItems": 12
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

语义校验（在 JSON Schema 之外）：

- `radius_deg` 必须与 `ra_deg`、`dec_deg` 同时出现
- `p0_min_s <= p0_max_s`
- `dm_min <= dm_max`
- 至少提供 `name`、位置条件、周期条件、DM 条件或 `binary_only=true` 之一

#### 输出 Schema

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

#### 错误码

`E_INVALID_INPUT`, `E_ENV_MISSING`, `E_NOT_FOUND`

---

### 5.4 `catalog_get`

**用途**：按名称精确获取单个脉冲星记录，并判断是否可以生成 ephemeris。

**后端**：psrcat / psrqpy / 本地缓存  
**长任务**：否  
**Annotations**：`readOnlyHint=true`, `destructiveHint=false`, `idempotentHint=true`, `openWorldHint=true`

#### 输入 Schema

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
      "description": "例如 'J0437-4715' 或 'B1937+21'"
    }
  },
  "additionalProperties": false
}
```

#### 输出 Schema

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

#### 错误码

`E_INVALID_INPUT`, `E_NOT_FOUND`, `E_ENV_MISSING`

---

### 5.5 `ephemeris_export`

**用途**：从星表记录或已有 `.par` 产物生成 TEMPO/TEMPO2 格式 ephemeris。转换可能丢失信息，必须返回 warnings。

**后端**：psrcat / PINT  
**长任务**：否  
**Annotations**：`readOnlyHint=false`, `destructiveHint=false`, `idempotentHint=true`, `openWorldHint=true`

#### 输入 Schema

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
      "description": "已有 .par artifact ID"
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

#### 输出 Schema

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

#### 错误码

`E_INVALID_INPUT`, `E_NOT_FOUND`, `E_ENV_MISSING`, `E_VERSION_INCOMPATIBLE`

---

### 5.6 `archive_inspect`

**用途**：读取 PSRFITS/PSRARCHIVE 的元数据、维度和基本统计，不修改文件。处理任何数据前应先调用。

**后端**：psrchive  
**长任务**：否  
**Annotations**：`readOnlyHint=true`, `destructiveHint=false`, `idempotentHint=true`, `openWorldHint=false`

#### 输入 Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://psrmcp.local/schemas/archive_inspect.input.json",
  "type": "object",
  "required": ["archive"],
  "properties": {
    "archive": {
      "type": "string",
      "description": "artifact ID 或 read root 下的相对路径"
    },
    "root": {
      "type": "string",
      "description": "当 archive 是相对路径时指定 data root"
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

#### 输出 Schema

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

#### 错误码

`E_INVALID_INPUT`, `E_NOT_FOUND`, `E_PATH_DENIED`, `E_FORMAT_UNSUPPORTED`, `E_PROCESS_FAILED`

---

### 5.7 `archive_convert`

**用途**：在 PSRFITS、PSRARCHIVE、SIGPROC、ASCII 之间转换。输入保持不变，输出为新 artifact。

**后端**：psrconv  
**长任务**：否（超大文件可转异步）  
**Annotations**：`readOnlyHint=false`, `destructiveHint=false`, `idempotentHint=true`, `openWorldHint=false`

#### 输入 Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://psrmcp.local/schemas/archive_convert.input.json",
  "type": "object",
  "required": ["archive", "output_format"],
  "properties": {
    "archive": {
      "type": "string",
      "description": "artifact ID 或 read root 下的相对路径"
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

#### 输出 Schema

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

#### 错误码

`E_INVALID_INPUT`, `E_NOT_FOUND`, `E_FORMAT_UNSUPPORTED`, `E_PROCESS_FAILED`, `E_QUOTA_EXCEEDED`

---

### 5.8 `archive_merge`

**用途**：合并多个 archive。默认要求源名一致；不一致时返回 `E_SOURCE_MISMATCH`，除非显式关闭检查。

**后端**：psradd  
**长任务**：是  
**Annotations**：`readOnlyHint=false`, `destructiveHint=false`, `idempotentHint=true`, `openWorldHint=false`

#### 输入 Schema

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

#### 输出 Schema

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

#### 错误码

`E_INVALID_INPUT`, `E_NOT_FOUND`, `E_SOURCE_MISMATCH`, `E_PROCESS_FAILED`, `E_TIMEOUT`, `E_CANCELLED`, `E_QUOTA_EXCEEDED`

---

### 5.9 `dedisperse`

**用途**：使用 dspsr 对数据进行相干或非相干消色散并折叠。相干消色散需要匹配的 ephemeris。

**后端**：dspsr  
**长任务**：是  
**Annotations**：`readOnlyHint=false`, `destructiveHint=false`, `idempotentHint=true`, `openWorldHint=false`

#### 输入 Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://psrmcp.local/schemas/dedisperse.input.json",
  "type": "object",
  "required": ["input", "ephemeris"],
  "properties": {
    "input": {
      "type": "string",
      "description": "原始数据或 archive 的 artifact ID / 相对路径"
    },
    "root": {"type": "string"},
    "ephemeris": {
      "type": "string",
      "description": "artifact ID、read root 下的 .par 路径，或脉冲星名"
    },
    "mode": {
      "enum": ["coherent", "incoherent"],
      "default": "coherent"
    },
    "dm_override": {
      "type": "number",
      "exclusiveMinimum": 0,
      "description": "显式覆盖 DM；会写入 provenance"
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

#### 输出 Schema

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

#### 错误码

`E_ENV_MISSING`, `E_INVALID_INPUT`, `E_NOT_FOUND`, `E_EPHEMERIS_MISMATCH`, `E_FORMAT_UNSUPPORTED`, `E_PROCESS_FAILED`, `E_TIMEOUT`, `E_CANCELLED`, `E_QUOTA_EXCEEDED`

---

### 5.10 `rfi_scan`

**用途**：分析 archive 的通道统计，给出建议的坏通道掩膜。只读，不修改数据。

**后端**：psrchive / clfd  
**长任务**：否  
**Annotations**：`readOnlyHint=true`, `destructiveHint=false`, `idempotentHint=true`, `openWorldHint=false`

#### 输入 Schema

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

#### 输出 Schema

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
      "description": "不同 method 返回的统计量不同，至少包含 flagged_count 和 threshold"
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

#### 错误码

`E_INVALID_INPUT`, `E_NOT_FOUND`, `E_ENV_MISSING`, `E_FORMAT_UNSUPPORTED`, `E_PROCESS_FAILED`

---

### 5.11 `rfi_zap`

**用途**：对 archive 应用通道掩膜，生成新的 archive。支持显式通道列表或自动方法。

**后端**：psrchive / clfd  
**长任务**：否  
**Annotations**：`readOnlyHint=false`, `destructiveHint=false`, `idempotentHint=true`, `openWorldHint=false`

#### 输入 Schema

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

#### 输出 Schema

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

#### 错误码

`E_INVALID_INPUT`, `E_NOT_FOUND`, `E_ENV_MISSING`, `E_PROCESS_FAILED`, `E_QUOTA_EXCEEDED`

---

### 5.12 `profile_standardize`

**用途**：从 archive 生成标准轮廓，供 TOA 提取使用。

**后端**：pas  
**长任务**：否  
**Annotations**：`readOnlyHint=false`, `destructiveHint=false`, `idempotentHint=true`, `openWorldHint=false`

#### 输入 Schema

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

#### 输出 Schema

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

#### 错误码

`E_INVALID_INPUT`, `E_NOT_FOUND`, `E_ENV_MISSING`, `E_PROCESS_FAILED`, `E_QUOTA_EXCEEDED`

---

### 5.13 `profile_measure`

**用途**：测量 archive 的轮廓指标，例如 S/N、FWHM、流量和偏振参数。只读。

**后端**：psrstat / vap / psrchive Python 绑定  
**长任务**：否  
**Annotations**：`readOnlyHint=true`, `destructiveHint=false`, `idempotentHint=true`, `openWorldHint=false`

#### 输入 Schema

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
      "description": "可选，标准轮廓 artifact ID"
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

#### 输出 Schema

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

#### 错误码

`E_INVALID_INPUT`, `E_NOT_FOUND`, `E_FORMAT_UNSUPPORTED`, `E_PROCESS_FAILED`

---

### 5.14 `toa_extract`

**用途**：使用标准轮廓从 archive 提取 TOA，生成 TEMPO `.tim` 文件。

**后端**：pat  
**长任务**：是  
**Annotations**：`readOnlyHint=false`, `destructiveHint=false`, `idempotentHint=true`, `openWorldHint=false`

#### 输入 Schema

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

语义校验：

- `mjd_range[0] <= mjd_range[1]`
- `freq_range_mhz[0] <= freq_range_mhz[1]`

#### 输出 Schema

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

#### 错误码

`E_INVALID_INPUT`, `E_NOT_FOUND`, `E_ENV_MISSING`, `E_PROCESS_FAILED`, `E_TIMEOUT`, `E_CANCELLED`, `E_QUOTA_EXCEEDED`

---

### 5.15 `timing_fit`

**用途**：使用 PINT 或 tempo2 对 `.par` + `.tim` 做计时拟合，返回更新后的 `.par`、拟合统计、残差和残差图。

**后端**：PINT worker / tempo2  
**长任务**：是  
**Annotations**：`readOnlyHint=false`, `destructiveHint=false`, `idempotentHint=true`, `openWorldHint=false`

#### 输入 Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://psrmcp.local/schemas/timing_fit.input.json",
  "type": "object",
  "required": ["par", "tim"],
  "properties": {
    "par": {
      "type": "string",
      "description": "artifact ID 或 read root 下的 .par 相对路径"
    },
    "tim": {
      "type": "string",
      "description": "artifact ID 或 read root 下的 .tim 相对路径"
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
      "description": "缺省使用 par 文件中的 fit flags；显式提供时必须能在 par 中解析"
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

#### 输出 Schema

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

#### 错误码

`E_INVALID_INPUT`, `E_NOT_FOUND`, `E_ENV_MISSING`, `E_VERSION_INCOMPATIBLE`, `E_PROCESS_FAILED`, `E_TIMEOUT`, `E_CANCELLED`

---

### 5.16 `timing_diagnose`

**用途**：对已有 `.par` + `.tim` 做残差和模型诊断，不重新拟合。可检查离群点、DM 变化、JUMP 和残差分布。

**后端**：PINT / tempo2  
**长任务**：否  
**Annotations**：`readOnlyHint=true`, `destructiveHint=false`, `idempotentHint=true`, `openWorldHint=false`

#### 输入 Schema

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

#### 输出 Schema

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

#### 错误码

`E_INVALID_INPUT`, `E_NOT_FOUND`, `E_ENV_MISSING`, `E_VERSION_INCOMPATIBLE`, `E_PROCESS_FAILED`

---

### 5.17 `artifact_list`

**用途**：列出当前工作区的 artifact，支持按类型、时间和游标分页。

**后端**：ArtifactStore  
**长任务**：否  
**Annotations**：`readOnlyHint=true`, `destructiveHint=false`, `idempotentHint=true`, `openWorldHint=false`

#### 输入 Schema

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

#### 输出 Schema

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

#### 错误码

`E_INVALID_INPUT`, `E_INTERNAL`

---

### 5.18 `job_get`

**用途**：查询异步任务状态和结果。仅在客户端不支持 MCP Tasks 扩展时作为兜底使用。

**后端**：JobManager  
**长任务**：否  
**Annotations**：`readOnlyHint=true`, `destructiveHint=false`, `idempotentHint=true`, `openWorldHint=false`

#### 输入 Schema

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

#### 输出 Schema

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

#### 错误码

`E_INVALID_INPUT`, `E_TASK_NOT_FOUND`

---

### 5.19 `job_cancel`

**用途**：请求取消异步任务。取消是协作式的：服务器先发送 `SIGTERM`，宽限期后如仍运行则 `SIGKILL` 进程组。

**后端**：JobManager  
**长任务**：否  
**Annotations**：`readOnlyHint=false`, `destructiveHint=false`, `idempotentHint=false`, `openWorldHint=false`

#### 输入 Schema

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
      "description": "跳过宽限期，直接终止进程组"
    }
  },
  "additionalProperties": false
}
```

#### 输出 Schema

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

#### 错误码

`E_INVALID_INPUT`, `E_TASK_NOT_FOUND`, `E_JOB_NOT_RUNNING`

---

## 6. 未来工具

以下工具不进入 MVP，但预留接口方向。

### 6.1 `artifact_delete`

- **用途**：删除工作区 artifact
- **Annotations**：`readOnlyHint=false`, `destructiveHint=true`, `idempotentHint=false`
- **输入**：`artifact_id`、`confirm: true`
- **行为**：默认移入回收站并设置 TTL；只有 `confirm=true` 才执行
- **错误码**：`E_NOT_FOUND`, `E_INVALID_INPUT`, `E_QUOTA_EXCEEDED`

### 6.2 `pipeline_reduce`

- **用途**：一次完成 inspect → dedisperse → RFI → standard profile → measure
- **输入**：`archive`、`pulsar_name?`、`dm?`、`rfi_strategy?`
- **输出**：完整 artifact 链和每一步摘要
- **注意**：复合工具必须返回中间产物，失败时保留已完成步骤，便于重试

### 6.3 `compare_timing_models`

- **用途**：同一组 par/tim 分别用 PINT 和 tempo2 拟合，比较参数和残差
- **输入**：`par`、`tim`、`fit_params?`
- **输出**：两套拟合结果、参数差异、残差差异和 warnings

### 6.4 `polarization_calibrate`

- **用途**：基于 pac 的偏振校准
- **输入**：`archive`、`calibrator_archive`、`cal_database?`
- **输出**：校准后的 archive、校准参数和诊断图
- **注意**：强依赖观测系统和校准源，需单独设计

### 6.5 `search_mode_process`

- **用途**：搜索模式数据处理（PRESTO / SIGPROC / heimdall）
- **输入**：`filterbank`、`dm_range`、`algorithm`
- **输出**：候选列表、折叠 archive
- **注意**：与现有 dspsr/psrchive 流程差异较大，建议独立模块

---

## 7. 版本与兼容性

- 服务器版本遵循 SemVer
- 工具名、参数名变更属于破坏性变更，需要 major 版本
- 新增可选参数和输出字段属于 minor 版本
- 输出 schema 使用 `result_type` 区分同步完成和异步接受；新增 `result_type` 值需要 minor 版本
- 所有 artifact 的 `provenance.tool_version` 记录生成时的服务器版本
- 后端版本变化不影响工具契约，但会影响科学结果；必须记录在 `provenance.backend_version`
- 工具 schema 的 `$id` 可用于客户端缓存和兼容性检查

---

## 8. 实现检查清单

- [ ] 所有输入 schema 设置 `additionalProperties: false`
- [ ] 所有路径参数经过白名单和 `..` 校验
- [ ] 所有命令使用 argv 数组，不使用 shell
- [ ] 所有长任务支持取消和进度上报
- [ ] 所有输出带 `provenance`
- [ ] 所有错误使用统一 `ErrorBody`
- [ ] 所有工具标注正确的 annotations
- [ ] 图像结果同时提供 image content 和 artifact ID
- [ ] 大文件不进入 MCP resource 的 base64 返回路径
- [ ] 集成测试覆盖 inspect → dedisperse → RFI → profile → TOA → fit
