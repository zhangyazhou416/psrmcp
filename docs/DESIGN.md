# 脉冲星数据处理 MCP Server 设计方案

- 文档版本：v0.1
- 日期：2026-10-17
- 状态：设计草案
- 范围：只包含架构与接口设计，不包含实现代码

---

## 1. 定位与边界

### 1.1 定位

一个 MCP Server，把 `dspsr`、`psrchive`、`PINT`、`tempo2`、`psrcat` 等天文工具封装成**面向任务、参数受控、结果结构化**的 MCP Tools，同时提供 Resources 和 Prompts，支持本地 stdio 与远程 Streamable HTTP 两种部署方式。

### 1.2 核心目标

- LLM 能完成典型流程：查星表 → 检查数据 → 消色散 → RFI → 标准轮廓 → TOA → 计时拟合 → 残差诊断
- 所有操作可追溯：记录工具版本、输入哈希、命令参数、输出产物
- 长任务可异步执行、查询进度、取消
- 默认不修改原始数据，所有结果落成新的不可变产物
- 对路径、命令、资源使用有严格限制

### 1.3 非目标（第一阶段不做）

- 不提供任意 shell / 任意 CLI 参数透传
- 不做实时观测控制
- 暂不做搜索模式单脉冲 pipeline（PRESTO、heimdall 等，后续可扩展）
- 暂不做偏振校准、流量定标（依赖具体观测系统，放到后期）
- 不替代专家的科学判断

---

## 2. 关键设计决策

| 决策 | 选择 | 理由 |
|---|---|---|
| 开发语言 | Python 3.11+ | PINT 是 Python 原生；psrchive 有 Python 绑定；astropy/numpy/matplotlib 生态成熟 |
| MCP SDK | 官方 Python SDK / FastMCP | tools/resources/prompts、进度、取消、结构化输出支持完整 |
| 工具封装方式 | CLI 适配器为主，Python 绑定为辅 | CLI 行为稳定、可复现；Python 绑定用于读取、统计和绘图 |
| PINT 集成 | 独立子进程 worker | 避免阻塞事件循环、隔离崩溃和内存问题，便于取消 |
| 工具粒度 | 任务级工具，不暴露全部 CLI flag | 降低 LLM 选错工具/参数的概率，便于校验和审计 |
| 数据产物 | 不可变 artifact | 原始数据只读，结果可追溯、可缓存、可复现 |
| 长任务 | MCP Tasks 扩展优先，job 轮询兜底 | 规范支持异步任务；客户端不支持时仍可用 |
| 工具链 | 固定版本的容器镜像 | dspsr/psrchive/tempo2 依赖复杂，容器最可控 |
| 安全模型 | 路径白名单 + argv 数组 + 资源限额 | 防止路径穿越、命令注入和资源耗尽 |

当前机器检测结果（2026-10-17）：项目目录为空，未安装 `dspsr`、`psrchive`、`PINT`、`tempo2`、`psrcat`，仅有 `astropy/numpy/matplotlib`。因此 Phase 0 必须先把工具链容器化或通过 conda-forge / Spack / 源码安装固定下来。

---

## 3. 总体架构

```
┌──────────────────────────────────────────────┐
│ MCP Client（Claude Code / Desktop / Web）      │
│  tools/call · resources/read · prompts/get    │
└──────────────────────┬───────────────────────┘
                       │ stdio / Streamable HTTP
┌──────────────────────▼───────────────────────┐
│ MCP Server（FastMCP）                         │
│  · 参数校验（Pydantic）                        │
│  · Tool annotations                          │
│  · 进度 / 取消 / Tasks 扩展                    │
└──────────────────────┬───────────────────────┘
                       │
┌──────────────────────▼───────────────────────┐
│ Domain Services                              │
│  Catalog · Archive · Dedispersion · RFI       │
│  Profile · Timing · Artifact · Job            │
└──────────────────────┬───────────────────────┘
                       │
┌──────────────────────▼───────────────────────┐
│ Adapters                                     │
│  dspsr │ psrchive │ PINT worker │ tempo2 │ psrcat │
└──────────────────────┬───────────────────────┘
                       │
┌──────────────────────▼───────────────────────┐
│ Execution Layer                              │
│  ProcessRunner（argv、超时、进程组、限额）      │
│  JobManager · Workspace · ArtifactStore       │
│  SQLite（任务/产物/缓存索引）                   │
└──────────────────────────────────────────────┘
```

### 3.1 推荐目录结构

```
psrmcp/
├── pyproject.toml
├── src/psrmcp/
│   ├── server.py                 # MCP 入口
│   ├── config.py                 # 配置加载与校验
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
│   ├── models/                   # Pydantic 输出模型
│   ├── plotting/
│   └── workers/pint_worker.py
├── tests/
├── docker/
└── docs/
```

---

## 4. MCP 接口设计

### 4.1 Tools 总览

| 分组 | 工具 | 作用 | 后端 | 类型 | 长任务 |
|---|---|---|---|---|---|
| 系统 | `pulsar_environment` | 返回工具版本、可用后端、配置和限额 | 全部 | 只读 | 否 |
| 数据 | `data_find` | 在白名单目录中查找数据文件 | 文件系统 | 只读 | 否 |
| 星表 | `catalog_search` | 按名称、位置、周期、DM、双星等查询 | psrcat / psrqpy | 只读 | 否 |
| 星表 | `catalog_get` | 获取单个脉冲星记录和别名 | psrcat / psrqpy | 只读 | 否 |
| 星表 | `ephemeris_export` | 导出 TEMPO/TEMPO2 格式的 `.par` | psrcat / PINT | 生成产物 | 否 |
| 归档 | `archive_inspect` | 读取 PSRFITS/PSRARCHIVE 元数据与统计 | psrchive | 只读 | 否 |
| 归档 | `archive_convert` | 格式转换 | psrconv | 生成产物 | 否 |
| 归档 | `archive_merge` | 合并多个 archive | psradd | 生成产物 | 视大小 |
| 消色散 | `dedisperse` | 相干/非相干消色散并折叠 | dspsr | 生成产物 | 是 |
| RFI | `rfi_scan` | 分析坏通道并给出建议掩膜 | psrchive / clfd | 只读 | 否 |
| RFI | `rfi_zap` | 应用通道掩膜 | psrchive / clfd | 生成产物 | 否 |
| 轮廓 | `profile_standardize` | 生成标准轮廓 | pas | 生成产物 | 否 |
| 轮廓 | `profile_measure` | 测量 S/N、宽度、流量等 | psrstat / vap | 只读 | 否 |
| 计时 | `toa_extract` | 从 archive 提取 TOA | pat | 生成产物 | 视数据量 |
| 计时 | `timing_fit` | PINT/tempo2 计时拟合 | PINT / tempo2 | 生成产物 | 是 |
| 计时 | `timing_diagnose` | 残差、离群点、DM、JUMP 诊断 | PINT | 只读 | 否 |
| 产物 | `artifact_list` | 列出工作区产物 | ArtifactStore | 只读 | 否 |
| 任务 | `job_get` | 查询异步任务状态（兜底） | JobManager | 只读 | 否 |
| 任务 | `job_cancel` | 取消异步任务（兜底） | JobManager | 写 | 否 |

> 工具名建议保持简洁；如果客户端会聚合多个 MCP Server，可统一加 `pulsar_` 前缀避免冲突。

### 4.2 代表性工具契约

#### `archive_inspect`

```json
{
  "name": "archive_inspect",
  "description": "读取 PSRFITS/PSRARCHIVE 的元数据和基本统计，不修改文件。处理前应先调用此工具确认数据内容与格式。",
  "inputSchema": {
    "type": "object",
    "properties": {
      "archive": {
        "type": "string",
        "description": "artifact ID，或配置目录下的相对路径"
      },
      "include_stats": {"type": "boolean", "default": true}
    },
    "required": ["archive"],
    "additionalProperties": false
  }
}
```

输出结构：

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

核心参数：

- `input`：原始数据或 archive
- `ephemeris`：`.par` artifact 或 `pulsar_name`
- `mode`：`coherent` / `incoherent`
- `dm_override`：可选，显式覆盖 DM，并记录到 provenance
- `output_format`：`psrfits` / `psrarchive`
- `nsubint`、`nchan`、`nbin`：可选输出维度
- `threads`：受配置上限约束
- `output_name`：可选

输出：

```json
{
  "artifact": {"artifact_id": "art_...", "kind": "archive", "format": "PSRFITS"},
  "dm_used": 2.644,
  "dimensions": {"nsubint": 64, "nchan": 512, "nbin": 1024},
  "warnings": [],
  "log_artifact": "art_..."
}
```

长任务：客户端支持 Tasks 扩展时返回 task handle；否则返回 `job_id`，通过 `job_get` 轮询。

#### `timing_fit`

核心参数：

- `par`：`.par` artifact
- `tim`：`.tim` artifact
- `backend`：`pint` / `tempo2`
- `fit_params`：可选，缺省使用 par 文件中的 fit flags
- `weighting`：`uniform` / `1/sigma^2`
- `generate_plot`：是否生成残差图
- `max_iterations`：迭代上限

输出：

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

### 4.3 工具设计规范

每个工具必须满足：

- **描述明确**：说明何时使用、前置条件、副作用、是否长任务
- **参数受控**：枚举、范围、单位明确，不接受任意 CLI flag
- **输出结构化**：`structuredContent` + 简短文本摘要
- **结果有界**：列表分页，图像降采样，JSON 结果默认不超过 256 KB
- **不覆盖输入**：所有操作生成新 artifact
- **annotations 准确**：
  - 只读工具：`readOnlyHint: true`
  - 仅删除产物：`destructiveHint: true`
  - 同类输入输出一致：`idempotentHint: true`
  - 仅在线星表查询：`openWorldHint: true`

不建议提供 `run_raw_command` 之类的“万能工具”。如果确实需要高级参数，使用白名单化的 `advanced` 对象，而不是原始字符串。

---

## 5. Resources 与 Prompts

### 5.1 Resources

| URI | 内容 |
|---|---|
| `pulsar://environment` | 环境、版本、可用后端、限额 |
| `pulsar://catalog/{name}` | 星表记录 JSON |
| `pulsar://ephemeris/{name}` | TEMPO/TEMPO2 `.par` 文本 |
| `pulsar://artifact/{artifact_id}` | 产物元数据 JSON |
| `pulsar://artifact/{artifact_id}/log` | 任务日志文本 |
| `pulsar://artifact/{artifact_id}/preview` | 图像/预览 blob |
| `pulsar://job/{job_id}` | 任务状态 JSON |
| `pulsar://docs/{topic}` | 使用指南，如 reduction、timing、rfi |

注意：**不要把大型 archive 通过 resource 以 base64 返回**。大文件只返回 artifact ID、路径和元数据，后续工具直接引用 artifact ID。

### 5.2 Prompts

| Prompt | 参数 | 作用 |
|---|---|---|
| `reduce_observation` | `archive`, `pulsar_name?`, `dm?`, `rfi_policy?` | 引导完成“检查 → 消色散 → RFI → 标准轮廓 → 测量” |
| `timing_analysis` | `par`, `tim`, `backend?` | 引导完成拟合、残差检查和参数解读 |
| `diagnose_rfi` | `archive` | 引导分析坏通道并选择 zap 策略 |
| `catalog_to_ephemeris` | `name`, `format?` | 从星表获取参数并导出 `.par` |
| `compare_timing_models` | `par`, `tim` | 对比 PINT 与 tempo2 结果（后期） |

Prompt 只负责给出工作流和安全约束，具体执行仍由模型选择 Tools，避免把 CLI 命令硬编码进 Prompt。

---

## 6. 数据、产物与任务模型

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

### 6.2 工作区布局

```
$WORKSPACE/
├── sessions/<session_id>/
│   ├── artifacts/<artifact_id>/<filename>
│   ├── logs/<job_id>.log
│   └── manifest.jsonl
├── cache/<cache_key>/
└── tmp/<job_id>/
```

- 原子写入：先写临时目录，成功后 rename
- 缓存键：输入文件 SHA-256 + 工具版本 + 规范化参数 + ephemeris 哈希
- 配额与 TTL：按会话/用户限制磁盘占用，过期产物可清理
- 原始数据目录只读挂载

### 6.3 Job / Task 模型

- 短任务同步返回
- 超过阈值（默认 20 秒）转异步
- 支持 MCP Tasks 扩展时：
  - `taskId`
  - 状态：`working` / `input_required` / `completed` / `failed` / `cancelled`
  - `tasks/get` 轮询，`tasks/cancel` 取消
  - 结果在 `completed` 后返回
- 不支持 Tasks 时：
  - 工具返回 `job_id`
  - 客户端调用 `job_get` / `job_cancel`
- 进度：
  - 优先解析 dspsr/psrchive 的 stderr
  - 否则按阶段上报：`validating → dedispersing → folding → writing → complete`
- 取消：
  - 先 `SIGTERM`，超时后 `SIGKILL`
  - 杀掉进程组，清理临时产物
- 重启：
  - 任务元数据持久化到 SQLite
  - 本地正在运行的进程在服务重启后标记为 `interrupted`
  - HPC/Slurm 场景可通过外部 job ID 重新挂接

---

## 7. 安全与科研正确性

### 7.1 安全

- **路径白名单**：只允许配置的 `read_roots` 和 `workspace_root`
- 使用 `Path.resolve()` 并检查是否逃逸；拒绝符号链接逃逸
- **不调用 shell**：所有命令使用 argv 数组
- 校验文件名，拒绝以 `-` 开头和含控制字符的输入
- 资源限制：超时、最大输入/输出大小、最大线程数、磁盘配额
- 进程级限制：`setrlimit` 或 cgroups
- HTTP 部署：TLS + OAuth 2.1 / Bearer token，按用户隔离 workspace
- 审计日志：记录调用者、工具、参数、命令、输入哈希、退出码、耗时
- 数据按不可信输入处理：PSRFITS header、par 文件、日志文本都要安全解析和转义
- 删除产物需要显式确认，默认移入回收站并设置保留期

### 7.2 科研正确性

- 校验 ephemeris 与观测是否匹配：源名、RA/DEC、DM、时间范围
- DM 覆盖必须显式传入，并写入 provenance
- 明确时间系统：MJD UTC、TT(TAI)、太阳系质心修正，记录所用时钟文件
- TOA 提取前检查标准轮廓和 bin 数兼容性
- 拟合前检查 TOA 数量、参数约束和 fit flags，避免无约束拟合
- 报告 reduced chi2、RMS、WRMS、离群点比例，但不把 reduced chi2 当作唯一判据
- 不静默丢数据：RFI zap、加权、裁剪都要报告比例和掩膜
- 记录所有工具版本、输入哈希和命令行，保证可复现

---

## 8. 配置与部署

### 8.1 配置示例

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

### 8.2 部署方式

**本地 stdio**

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

**容器**

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

**远程服务**

- 使用 Streamable HTTP
- TLS 终止、OAuth 2.1、按用户分配 workspace
- 限制并发、请求大小和任务队列长度
- 只读数据卷，禁止容器内访问宿主机其他路径

### 8.3 工具链镜像

镜像中固定：

- dspsr / psrchive
- tempo2 及所需插件
- psrcat 及星表数据库
- PINT、astropy、numpy、matplotlib
- 可选：psrqpy、clfd / coastguard、libstempo、psrchive Python 绑定

启动时运行环境自检，结果通过 `pulsar_environment` 暴露；版本不匹配时给出明确错误，而不是运行时才失败。

---

## 9. 测试与验收

### 9.1 测试层次

- **单元测试**：路径策略、参数校验、argv 构建、输出解析、错误映射
- **契约测试**：在固定容器中用小型测试数据调用真实 dspsr/psrchive/PINT/tempo2
- **集成测试**：完整流程 J0437-4715 或 B1937+21：inspect → dedisperse → RFI → profile → TOA → fit
- **安全测试**：路径穿越、符号链接逃逸、命令注入字符串、超大文件、磁盘耗尽
- **失败测试**：缺少二进制、损坏 archive、超时、取消、版本不兼容
- **回归测试**：关键数值（DM、周期、TOA 数、拟合参数）与 golden output 对比

### 9.2 MVP 验收标准

1. `archive_inspect` 能正确读取 PSRFITS 元数据
2. `dedisperse` 能对 filterbank/archive 完成相干消色散并生成 PSRFITS
3. `rfi_scan` 能给出坏通道建议，`rfi_zap` 能应用掩膜
4. `profile_standardize` + `profile_measure` 能生成标准轮廓并测量 S/N
5. `toa_extract` 能生成 `.tim`
6. `timing_fit` 能完成 PINT 拟合并返回参数、残差和残差图
7. 长任务可查询进度、可取消、取消后无残留临时文件
8. 无法访问白名单外路径，无法执行任意命令
9. 所有输出带 provenance，可复现

---

## 10. 实施路线

| 阶段 | 内容 | 预计 |
|---|---|---|
| Phase 0 | 工具链容器、项目骨架、配置、`pulsar_environment`、`data_find`、`archive_inspect`、星表查询 | 1–2 周 |
| Phase 1 | `archive_convert/merge`、`dedisperse`、`rfi_scan/zap`、`profile_*`、artifact/job 基础设施 | 2–3 周 |
| Phase 2 | `toa_extract`、PINT worker、`timing_fit`、`timing_diagnose`、残差图 | 2–3 周 |
| Phase 3 | tempo2/libstempo 后端、偏振校准、复合 pipeline 工具、HTTP/HPC 部署 | 3–4 周 |
| Phase 4 | 搜索模式单脉冲、批量处理、多用户配额、可观测性 | 按需 |

> 工期按“熟悉脉冲星工具链的 1 名开发者”估算，容器构建和真实数据调试通常是最大不确定项。

---

## 11. 主要风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| dspsr/psrchive/tempo2 安装与版本兼容 | 环境不可用 | 固定容器镜像，启动自检，记录版本 |
| PINT API 变化 | 拟合工具失效 | 锁定版本，独立 worker，契约测试 |
| 数据文件过大 | 内存/磁盘/客户端超限 | 流式处理、artifact 引用、结果分页 |
| 星表/ephemeris 不匹配 | 科学结果错误 | 强制校验源名、位置、DM，显式覆盖并记录 |
| LLM 选错工具或参数 | 流程失败 | 任务级工具、枚举参数、清晰描述、Prompt 引导 |
| Tasks 扩展客户端支持不一 | 长任务体验差 | Tasks + job 轮询双路径 |
| 路径/命令注入 | 安全问题 | 白名单、argv 数组、资源限制、审计 |
| 可复现性不足 | 结果无法复核 | 输入哈希、工具版本、命令行、参数全部落盘 |

---

## 12. 需要后续确认的决策

1. 部署形态：单机本地使用，还是多人共享 / HPC？
2. 主要数据来源和格式：FAST、Parkes、GBT、SKA 先导？PSRFITS、PSRARCHIVE、SIGPROC 哪些优先？
3. 计时后端以 PINT 还是 tempo2 为准？是否需要两者结果对比？
4. 星表查询要求离线还是允许联网？
5. 是否接受 psrchive Python 绑定，还是坚持全部走 CLI？
6. 远程部署是否需要 OAuth / 多用户隔离？

---

## 参考

- [MCP 规范 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28)
- [MCP Tasks 扩展](https://modelcontextprotocol.io/extensions/tasks/overview)
- [DSPSR 用户手册](https://dspsr.sourceforge.net/manuals/dspsr/)
- [psrchive 工具手册](https://psrchive.sourceforge.net/manuals/)
- [PINT 文档](https://nanograv-pint.readthedocs.io/en/latest/)
- [TEMPO2 官方文档](http://www.atnf.csiro.au/research/pulsar/tempo2/)
- [ATNF Pulsar Catalogue](https://www.atnf.csiro.au/research/pulsar/psrcat/)
