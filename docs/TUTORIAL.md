# psrmcp 使用教程

本教程带你从零开始:安装 → 配置 → 接入 AI 客户端 → 完成一次真实的脉冲星数据处理。

两种用法贯穿全文:

1. **自然语言驱动(推荐)**:接入 Claude Code / Claude Desktop 后,直接用中文描述需求,AI 自动选择并串联工具;
2. **直接调用**:文中同时给出每个工具的参数示例,方便在其他 MCP 客户端或脚本中精确调用。

前置阅读:[FEATURES.md](FEATURES.md)(能做什么、19 个工具一览);逐字段 Schema 见 [TOOLS.md](TOOLS.md)。

## 1. 安装

### 1.1 脉冲星软件栈(可选但推荐)

```bash
bash scripts/install_pulsar_stack.sh            # 全源码编译进 ~/.pulsar
# 常用旗标: --prefix ~/Pulsar 自定义安装位置;--with-presto 连 PRESTO;
#           --jobs N 并行度;断点续作,重复执行安全
```

装完后 `source ~/.pulsar/env.sh` 可在交互终端获得 PATH/TEMPO2/PSRCAT_FILE。
**没有栈也能跑**:服务器优雅降级,`pulsar_environment` 会逐后端报告缺失原因。

### 1.2 服务器本体

```bash
git clone <本仓库> psrmcp && cd psrmcp
uv sync                                         # Python 3.12+,依赖装进 .venv
uv run psrmcp --init-config ~/.psrmcp.toml      # 生成带注释的默认配置
```

### 1.3 配置文件

配置是单个 TOML(`PSRMCP_CONFIG` 环境变量指定路径,不设则用 `~/.psrmcp.toml`)。装好栈后建议显式写绝对路径——stdio 冷启动没有 shell 环境,不依赖 env.sh:

```toml
[server]
workspace = "~/.psrmcp/workspace"   # 产物/任务库落点;job_timeout_s 控制单任务超时

[data]
read_roots = [                      # ★ 安全边界:服务器只能读这些目录里的数据
  "~/psrdata",
]
allow_absolute_paths = false        # 只许 read_roots 内的相对路径

[backends]
# 不写则从 PATH 探测;写了绝对路径则不依赖 env.sh
dspsr = "~/.pulsar/bin/dspsr"
psrchive_bin = "~/.pulsar/bin"      # vap/psrstat/pam/pat/psradd/paz 所在目录
tempo2 = "~/.pulsar/bin/tempo2"
psrcat = "~/.pulsar/bin/psrcat"
pint_enabled = true

[catalog]
mode = "local"                      # 本地 psrcat.db(离线可用)
database = "~/.pulsar/psrcat/psrcat.db"
```

要点:

- **read_roots 是唯一的数据库入口**,所有工具的路径参数都解析到这些目录之内,`..`、绝对路径、symlink 逃逸一律被拒(`E_PATH_DENIED`);
- workspace 是唯一可写区,产物以 `art_` ID 管理,不污染数据目录。

## 2. 接入 AI 客户端

### Claude Code(命令行)

```bash
claude mcp add psrmcp \
  -e PSRMCP_CONFIG=~/.psrmcp.toml \
  -- uv --directory /path/to/psrmcp run psrmcp
```

新开会话后用 `/mcp` 确认 `psrmcp` 已连接。

### Claude Desktop / 其他客户端(JSON)

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

### 验证连接

对 AI 说:

> 检查一下脉冲星处理环境

AI 会调用 `pulsar_environment`,返回类似:

```
**服务器** psrmcp v0.1.0 (MCP 2026-07-28)
**可用后端**: dspsr, pint, psrcat, psrchive, tempo2
**数据目录**: ~/psrdata
```

缺后端时会看到每个缺失项的 reason(如 `dspsr: not found in PATH`),按提示补装即可。

## 3. 实战一:星表查询(不需要观测数据)

适合先熟悉工具。对 AI 说:

> 查一下毫秒脉冲星 J0437-4715 的基本参数,再给我 5 颗周期小于 5ms 的脉冲星

对应的工具调用:

```json
→ catalog_get  {"name": "J0437-4715"}
→ catalog_search {"p0_max_s": 0.005, "limit": 5,
                  "fields": ["name", "p0_s", "dm", "binary"]}
```

导出计时用的星历(par 文件,成为可复用产物):

```json
→ ephemeris_export {"pulsar": "J0437-4715", "format": "tempo2"}
← {"artifact": {"artifact_id": "art_...", ...}}
```

- `ephemeris` 参数在后续工具里**三种写法都认**:这个 artifact ID、read_roots 下的 `.par` 相对路径、或直接写脉冲星名(自动查询并落成 par);
- 观测位置与星表不符时用 `overrides` 覆盖 RA/Dec/DM/PEPOCH 等字段,覆盖行为会记入 provenance。

## 4. 实战二:一条观测数据的完整处理链

这是核心工作流。以下每步都给出「你说的话 → AI 调的工具 → 你得到什么」。数值取自本项目真实链路验证(synth.fil:Parkes 1.4 GHz、128 通道、DM 2.645)。

> **没有观测数据?** `uv run python scripts/make_synth_fil.py` 可生成一条合成 filterbank(`ws/synth.fil`,内嵌 DM 2.645 的色散脉冲)用于练手全流程。

### 第 1 步:发现数据

> 看看数据目录里有什么

```json
→ data_find {"pattern": "**/*.fil"}
← 匹配列表(路径、大小、类型识别)
```

### 第 2 步:消色散折叠

> 把 synth.fil 消色散折叠成 PSRFITS,目标是 J0437-4715

```json
→ dedisperse {"input": "synth.fil", "ephemeris": "J0437-4715",
              "mode": "incoherent", "output_format": "psrfits"}
← {"artifact": {"artifact_id": "art_..."}, "dm_used": 2.645, "dimensions": {...}}
```

- DM 自动从星历的 `DM` 行读取;也可 `dm_override` 显式指定(会记 warning 与 provenance);
- filterbank 输入只能非相干消色散,误设 `mode: coherent` 会自动降级并给 warning;
- 这是长任务,超过阈值自动转异步(见第 7 节),AI 自己会等。

### 第 3 步:检查折叠结果

> 看看这个 archive 的基本信息

```json
→ archive_inspect {"archive": "art_..."}
← nbin/nchan/npol/nsubint、频带、MJD 等头信息
```

### 第 4 步:去 RFI

> 先扫描一下有没有干扰,有的话清掉

```json
→ rfi_scan  {"archive": "art_..."}                     # 通道统计,给出可疑清单
→ rfi_zap   {"archive": "art_...", "mask": {"channels": [0, 1, 2]}}
```

不想手动挑通道?用自动标志:

```json
→ rfi_zap {"archive": "art_...",
           "mask": {"auto": {"method": "psrchive_median", "threshold_sigma": 3.0}}}
```

### 第 5 步:标准轮廓与测量

> 建立标准轮廓,然后测一下 SNR 和脉宽

```json
→ profile_standardize {"archive": "art_..."}           # → 标准轮廓 artifact + 轮廓图
→ profile_measure  {"archive": "art_...", "metrics": ["snr", "width", "flux"]}
← {"snr": 133.9, "w50": ..., "plot": {"artifact_id": "art_..."}}
```

标准轮廓 artifact ID 记住——下一步要用(AI 会自动衔接)。

### 第 6 步:提取 TOA

> 用这个标准轮廓提取 TOA

```json
→ toa_extract {"archive": "art_...", "standard_profile": "art_...",
               "algorithm": "PGS", "min_snr": 8}
← {"n_toas": 128, "tim_artifact": {"artifact_id": "art_..."}}
```

产物是一份 `.tim` 文件(algorithm 可选 GIS/PIS/ZPS/SIS/FDM;`mjd_range`/`freq_range_mhz` 可筛子集)。

### 第 7 步:PINT 计时拟合

> 用 J0437-4715 的星历拟合这批 TOA

```json
→ timing_fit {"par": "art_<星历>", "tim": "art_<tim>",
              "backend": "pint", "weighting": "1/sigma^2"}
← {"fit": {"reduced_chi2": ..., "wrms_us": ...},
   "parameters": [...], "plot": {...}, "fitted_par": {...}}
```

- 默认按 par 里的 fit flag 决定自由参数,也可 `fit_params: ["F0", "F1", "DM"]` 显式指定;
- 交付:参数表(值/不确定度/是否拟合)、残差图 PNG、拟合后 par 文件。

### 第 8 步:残差诊断

> 残差有没有问题?

```json
→ timing_diagnose {"par": "art_<拟合后par>", "tim": "art_<tim>"}
← {"summary": {"n_outliers": 3, "reduced_chi2": ...},
   "findings": [...], "recommendations": [...]}
```

输出结构化结论:离群 TOA 清单(>5σ,MAD 基)、reduced χ² 过高/过低判读、是否需要 JUMP/DMX、残差偏斜提醒——每条附建议处置。

## 5. 实战三:archive 日常加工

```json
→ archive_convert {"archive": "synth.fil", "output_format": "psrfits"}
→ archive_merge   {"archives": ["art_a", "art_b"], "weighting": "time"}
```

- merge 默认 `require_same_source: true`,来源不同直接拒(`E_SOURCE_MISMATCH`),防止错合并;
- 输入是退化/异构格式时,转换或后续 dspsr 失败会返回 `E_PROCESS_FAILED` 并附 log 产物(见第 8 节)。

## 6. 产物与任务管理

- **一切皆产物**:每步输出都有 `art_` ID,跨工具引用 ID 即可,不必管文件落点。`artifact_list {"kind": "archive"}` 随时盘点;
- **谱系可溯**:产物 provenance 记录完整 argv、参数、后端版本、父产物 ID——三个月后仍能回答"这张图是怎么来的";
- **长任务**:超过阈值自动转异步,返回 `job_id`;AI 通常自动轮询,也可手动 `job_get {"job_id": "..."}` 查进度、`job_cancel` 中止(进程组级,不留僵尸进程);服务器重启后未完成任务标记 interrupted,不会假报成功。

## 7. 提示词技巧

AI 面对的是 19 个带严格 Schema 的工具,所以**描述目标而非步骤**通常效果最好:

| 你说 | AI 会做 |
|---|---|
| "处理一下 ~/psrdata 里昨晚的观测,目标是 J0437-4715" | 找数据 → 查星历 → 折叠 → 去 RFI → 出测量,全程自动串联 |
| "只测 SNR,先别动数据" | 只调 profile_measure,不产生副作用 |
| "把刚才那个 archive 转成 PSRARCHIVE 格式" | 用上文产物 ID 调 archive_convert |
| "拟合结果看起来偏大,帮我诊断一下" | timing_diagnose 全套检查并解读 findings |

- 引用上文产物用自然语言即可("刚才的标准轮廓"),AI 会带上正确 ID;
- 拿不准就先让 AI 跑只读工具(`data_find` / `archive_inspect` / `rfi_scan`)看一眼再决定;
- 所有有副作用的操作(写文件、覆盖参数)都会在结果里以 warning/provenance 明示,不会静默发生。

## 8. 出错怎么办

错误是结构化信封,不抛裸 traceback:

```json
{"code": "E_PATH_DENIED", "message": "路径越界: ../secret",
 "hint": "只允许 read_roots 内的相对路径", "retryable": false}
```

| code | 含义 | 处置 |
|---|---|---|
| `E_PATH_DENIED` | 路径越出 read_roots | 把数据目录加进配置的 read_roots |
| `E_NOT_FOUND` | 文件/产物/脉冲星不存在 | 核对名字;产物 ID 可用 artifact_list 查 |
| `E_ENV_MISSING` | 后端未安装 | 按 reason 补装或 source env.sh 后 `refresh: true` 重探 |
| `E_PROCESS_FAILED` | 后端命令失败 | 看 `log_artifact_id` 指向的 log 产物(含 stderr 尾部与完整 argv) |
| `E_INVALID_INPUT` | 参数不合法 | 按 message 修正;Schema 见 TOOLS.md |
| `E_QUOTA_EXCEEDED` | workspace 超配额 | 清理旧产物或调大 limits |
| `EPHEMERIS_MISMATCH` | 星历与数据对不上(如无 DM) | 提供 .par 或用 dm_override |

警告(warnings,不阻断):`W_DM_OVERRIDE`(DM 被显式覆盖)、`W_COHERENT_UNAVAILABLE`(filterbank 自动降级为非相干)等,都会原样带在结果里。

## 9. 自检与回归

```bash
uv run pytest -q                                   # 单元测试,不需要后端
PSRMCP_CONTRACT=1 uv run pytest tests/contract     # 契约测试,需要已装栈
uv run python examples/client_stdio.py             # stdio 冒烟(19 工具握手)
PSRMCP_CONFIG=$PWD/config.toml uv run python scripts/live_check.py
                                                   # 真实栈 9 步端到端(需数据+栈)
```

## 相关文档

- [FEATURES.md](FEATURES.md) — 功能总览
- [DESIGN.md](DESIGN.md) — 架构与执行模型
- [TOOLS.md](TOOLS.md) — 19 个工具的完整 JSON Schema 契约
- [INSTALL.md](INSTALL.md) — 软件栈一键安装详解
