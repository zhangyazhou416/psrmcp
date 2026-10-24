"""MCP prompts (DESIGN.md 5.2): workflow guides, never hardcoded CLI commands."""

from __future__ import annotations

from dataclasses import dataclass

from .errors import invalid_input, not_found

from mcp.types import Prompt, PromptArgument, PromptMessage, TextContent


@dataclass(frozen=True)
class PromptDef:
    description: str
    arguments: tuple[tuple[str, str, bool], ...]  # (name, description, required)
    template: str  # .format(**kwargs) applied to user-supplied values


_DEFS: dict[str, PromptDef] = {
    "reduce_observation": PromptDef(
        description="引导完成“检查 → 消色散 → RFI → 标准轮廓 → 测量”的单次观测约简",
        arguments=(
            ("archive", "输入 archive 的路径或 artifact ID", True),
            ("pulsar_name", "脉冲星名字(J 名称),用于取星表参数", False),
            ("dm", "已知色散量 pc cm^-3;缺省则从数据头/星表推断", False),
            ("rfi_policy", "RFI 策略:conservative|aggressive|skip,默认 conservative", False),
        ),
        template=(
            "请帮我对观测数据 {archive} 完成一次完整的脉冲星约简。\n"
            "{pulsar_name_line}{dm_line}"
            "RFI 策略: {rfi_policy}。\n\n"
            "要求:\n"
            "1. 先用环境与数据发现工具确认后端可用性,再检查数据元信息。\n"
            "2. 消色散前确认 DM 来源(数据头 / 星表 / 用户提供),不确定时先询问。\n"
            "3. RFI 处理先扫描、后清理;conservative 策略下宁可少 zap 并记录 mask。\n"
            "4. 生成标准轮廓并测量 SNR/宽度;所有产物引用 artifact ID。\n"
            "5. 长任务用 execution_mode=auto 并用 job_get 轮询;失败时先读 log artifact。\n"
            "6. 每一步汇报结构化结果的关键数字,不要凭空补全未测量的值。"
        ),
    ),
    "timing_analysis": PromptDef(
        description="引导完成 par+tim 的计时拟合、残差检查与参数解读",
        arguments=(
            ("par", "初始 .par 文件路径或 artifact ID", True),
            ("tim", "TOA .tim 文件路径或 artifact ID", True),
            ("backend", "拟合后端:pint(默认)或 tempo2", False),
        ),
        template=(
            "请对 par={par}、tim={tim} 做计时分析(backend: {backend})。\n\n"
            "要求:\n"
            "1. 先拟合,报告 n_toas、chi2/dof、RMS/WRMS 与被拟合参数及不确定度。\n"
            "2. 再做诊断:离群点、JUMP、DM 信息、残差分布;关注 reduced chi2 异常。\n"
            "3. 按 diagnostics 的 recommendations 清理数据后重拟合,对比前后 RMS。\n"
            "4. 解读参数时给出物理论述(自转/自行/双星),标注哪些参数未被拟合固定。\n"
            "5. 所有中间产物(par/residuals/plot)用 artifact ID 引用。"
        ),
    ),
    "diagnose_rfi": PromptDef(
        description="引导分析坏通道并选择 zap 策略",
        arguments=(("archive", "待分析 archive 的路径或 artifact ID", True),),
        template=(
            "请分析 {archive} 的射频干扰情况。\n\n"
            "要求:\n"
            "1. 先做 RFI 扫描,给出坏通道比例与建议 mask(附图)。\n"
            "2. 结合天文合理性讨论:宽带突发、持续窄带、脉冲相位内/外的差异。\n"
            "3. 给出 zap 建议(哪些通道、为什么),先小范围试 zap 验证影响。\n"
            "4. 清理后重新评估轮廓质量(SNR 变化),并诚实报告统计受限之处。"
        ),
    ),
    "catalog_to_ephemeris": PromptDef(
        description="从星表获取参数并导出 .par 文件",
        arguments=(
            ("name", "脉冲星名字", True),
            ("format", "par 格式:tempo1(默认)或 tempo2", False),
        ),
        template=(
            "请为脉冲星 {name} 导出 {format} 格式的星历文件。\n\n"
            "要求:\n"
            "1. 从星表查询参数,报告 RA/Dec/DM/周期等关键值。\n"
            "2. 导出 .par 并检查包含的参数;双星参数缺失时给出 W_BINARY_PARAMS_INCOMPLETE 警告的处理建议。\n"
            "3. 说明该 par 适用于哪类后续处理(消色散/计时),以及局限(无自行/无 proper motion 等)。"
        ),
    ),
    "compare_timing_models": PromptDef(
        description="对同一 par+tim 分别用 PINT 与 tempo2 拟合并对比结果(后期)",
        arguments=(
            ("par", "计时模型 .par", True),
            ("tim", "TOA .tim", True),
        ),
        template=(
            "请用 PINT 与 tempo2 分别对 par={par}、tim={tim} 拟合,并对比结果。\n\n"
            "要求:\n"
            "1. 两个后端各产出拟合统计与残差;注意 tempo2 输出解析尚属 TODO(contract),异常时如实报告。\n"
            "2. 对比表:RMS/WRMS、reduced chi2、关键参数值与不确定度差异。\n"
            "3. 讨论差异来源(时间历表、太阳系星历、权重处理),并给出推荐后端与理由。"
        ),
    ),
}


def list_prompts() -> list[Prompt]:
    return [
        Prompt(
            name=name,
            description=d.description,
            arguments=[
                PromptArgument(name=n, description=desc, required=req)
                for n, desc, req in d.arguments
            ],
        )
        for name, d in _DEFS.items()
    ]


def get_prompt(name: str, arguments: dict[str, str] | None) -> list[PromptMessage]:
    d = _DEFS.get(name)
    if d is None:
        raise not_found(f"未知 prompt: {name}(可用: {', '.join(sorted(_DEFS))})")
    supplied = arguments or {}
    kwargs: dict[str, str] = {}
    for arg_name, _desc, required in d.arguments:
        val = str(supplied.get(arg_name, "")).strip()
        if not val:
            if required:
                raise invalid_input(f"prompt {name} 缺少必填参数: {arg_name}")
            kwargs[arg_name] = "(未提供)"
        else:
            kwargs[arg_name] = val
    # Optional-context lines for reduce_observation; empty when unsupplied.
    kwargs["pulsar_name_line"] = (
        "" if kwargs.get("pulsar_name") == "(未提供)"
        else f"脉冲星: {kwargs['pulsar_name']}。\n"
    )
    kwargs["dm_line"] = (
        "" if kwargs.get("dm") == "(未提供)" else f"DM: {kwargs['dm']} pc cm^-3。\n"
    )
    text = d.template.format(**kwargs)
    return [PromptMessage(role="user", content=TextContent(type="text", text=text))]


__all__ = ["list_prompts", "get_prompt"]
