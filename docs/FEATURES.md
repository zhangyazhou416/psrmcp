# psrmcp 功能总览

psrmcp 把脉冲星射电天文的专业处理软件栈(**dspsr / PSRCHIVE / tempo2 / psrcat / PINT**)封装成 MCP 服务器,让 AI 助手(Claude Code、Claude Desktop 等)能直接、安全地驱动它们,完成从原始观测数据到计时分析报告的完整流程。AI 不需要记忆各软件的命令行 flag,而是调用 19 个面向任务的高层工具;所有结果结构化、可溯源。

设计原则:**面向任务、参数受控、结果结构化、全程可追溯**(架构见 [DESIGN.md](DESIGN.md),逐工具 Schema 见 [TOOLS.md](TOOLS.md))。

## 五个后端

| 后端 | 作用 |
|---|---|
| PSRCHIVE | archive 检查/加工(vap / psrstat / pam / pat / psradd / paz) |
| dspsr | 原始数据(filterbank / PSRFITS)消色散折叠 |
| tempo2 | 脉冲星计时(星历、相位预测) |
| psrcat | ATNF 脉冲星星表本地查询与星历导出 |
| PINT | Python 计时拟合与残差分析 |

后端缺失时服务器不崩溃:`pulsar_environment` 逐个报告 available/reason,依赖缺失后端的工具返回 `E_ENV_MISSING` 与提示。

## 19 个工具(按处理链路分组)

**环境与数据发现**

| 工具 | 作用 |
|---|---|
| `pulsar_environment` | 五后端版本探测与可用性自检,报告 workspace/limits |
| `data_find` | 在 read_roots 白名单目录中扫描识别观测数据(filterbank / PSRFITS / archive) |

**星表与星历**

| 工具 | 作用 |
|---|---|
| `catalog_search` | 按名字/条件(p0、DM 等)/位置半径查 ATNF 星表 |
| `catalog_get` | 取单颗脉冲星的完整参数行 |
| `ephemeris_export` | 导出 TEMPO/TEMPO2 格式 par 星历(本地 psrcat `-e` 完整双星模型,支持字段覆盖) |

**Archive 加工**

| 工具 | 作用 |
|---|---|
| `archive_inspect` | 头信息与维度(nbin/nchan/npol/nsubint/频带) |
| `archive_convert` | PSRFITS ↔ PSRARCHIVE ↔ filterbank 格式转换 |
| `archive_merge` | 多 archive 合并(psradd) |

**消色散**

| 工具 | 作用 |
|---|---|
| `dedisperse` ⏱ | dspsr 折叠;DM 取自星历文件或 `dm_override`,星历可由 artifact ID 或脉冲星名自动解析 |

**射频干扰(RFI)**

| 工具 | 作用 |
|---|---|
| `rfi_scan` | 通道统计扫描,定位可疑通道/频段 |
| `rfi_zap` | 按 mask(通道/频段)剔除 RFI(paz) |

**轮廓分析**

| 工具 | 作用 |
|---|---|
| `profile_standardize` | 归一化/对齐到标准轮廓,输出轮廓图 |
| `profile_measure` | SNR、W50/W10 脉宽、峰值流量等测量,附 matplotlib 轮廓图 |

**计时**

| 工具 | 作用 |
|---|---|
| `toa_extract` ⏱ | pat 批量提取 TOA(自动生成标准轮廓),输出 .tim 与 .par 产物 |
| `timing_fit` ⏱ | PINT 加权拟合(1/σ² 或 uniform),输出参数表、reduced χ²、WRMS、残差图与拟合后 par |
| `timing_diagnose` | 残差诊断:离群 TOA(基于 MAD)、reduced χ² 高/低、JUMP 参数存在性、DM 信息、分布偏度/峰度,并给出处理建议 |

**产物与任务管理**

| 工具 | 作用 |
|---|---|
| `artifact_list` | 列出 workspace 产物(支持按 kind 过滤) |
| `job_get` | 查询异步任务状态/结果 |
| `job_cancel` | 取消运行中的任务(进程组级) |

⏱ = 长任务:超过阈值自动转异步(返回 `job_id` 轮询),支持取消,状态 SQLite 持久化,服务器重启后 working → interrupted。

## 工程特性

- **产物溯源**:每个结果都是 `art_xxx` 产物,记录 sha256、完整 provenance(命令 argv、参数、后端版本、父子谱系),工具间用 artifact ID 串流水线。
- **安全沙盒**:路径白名单(拒 `..`/控制字符/symlink 逃逸)、命令只走 argv 数组不进 shell、rlimits、超时先 SIGTERM 后 SIGKILL 进程组、stderr 尾部落 log 产物。
- **结构化输出**:每个工具返回 Markdown 摘要(content)+ 结构化 JSON(structuredContent);错误统一信封 `code/message/hint/retryable/log_artifact_id`,不暴露裸 traceback。
- **绘图**:所有图走 Python matplotlib,以 PNG plot artifact 交付(轮廓、残差等),无 PGPLOT 依赖。
- **资源与提示**:`pulsar://environment`(与工具一致的运行环境快照)、`pulsar://docs/reduction`(标准处理流程);5 个预设 prompt:观测流程、计时分析、RFI 诊断、星表转星历、计时模型对比。
- **配置**:`--init-config` 生成 TOML 模板(五段:server/data/backends/catalog/limits),环境变量 `PSRMCP_CONFIG` 指定路径;stdio 冷启动无 shell 环境也可用绝对路径直接配置后端。

## 一次完整处理链

对 AI 说"处理这批观测数据",它可以自主完成:

```
pulsar_environment   确认后端可用
data_find            找到 ws/synth.fil
ephemeris_export     查 psrcat 取 J0437-4715 的完整 par 星历
dedisperse           dspsr 消色散折叠(-E 星历生成相位预测器)
rfi_zap              剔除干扰通道
profile_standardize  建立标准轮廓(SNR 133.9)
toa_extract          提取 128 个 TOA
timing_fit           PINT 拟合,得 reduced χ² 与残差图
timing_diagnose      诊断:离群 TOA、是否需要 DMX/JUMP
```

每一步的产物(archive、.tim/.par、图、日志)都带 provenance,可回溯、可复现。

## 相关文档

- [TUTORIAL.md](TUTORIAL.md) — 使用教程(从安装到完整处理链实战)
- [DESIGN.md](DESIGN.md) — 架构与执行模型
- [TOOLS.md](TOOLS.md) — 19 个工具的完整 JSON Schema 契约
- [INSTALL.md](INSTALL.md) — 软件栈一键安装
