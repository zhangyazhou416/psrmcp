"""Call-flow + reproducibility reporting (TOOLS.md 1.3).

Every tool response carries:
  - `**调用流程**`: a Mermaid flowchart of the invocation (inputs -> tool ->
    executed commands -> outputs / error), and
  - `**复现命令**`: the exact shell command lines the call executed (absolute
    argv, working directory, env overrides, exit code, duration), so the user
    can reproduce the step by hand.

`pulsar://calls` renders the accumulated session graph. All of this is
best-effort decoration: generation must never break a tool result.
"""

from __future__ import annotations

import re
import shlex
from datetime import datetime, timezone
from typing import Any

_SANITIZE = str.maketrans({
    '"': "'", "[": "(", "]": ")", "{": "(", "}": ")",
    "|": "/", "\n": " ", "\r": "",
})
_MAX_NODE = 70
_SESSION_CAP = 50  # session diagram keeps the most recent N calls
_TASK = {"<": "‹", ">": "›"}  # keep mermaid node text safe


def _clean(text: str, limit: int = _MAX_NODE) -> str:
    text = re.sub(r"\s+", " ", str(text)).translate(_SANITIZE).strip()
    return text[: limit - 1] + "…" if len(text) > limit else text


def _clean_cmdtext(text: str, limit: int = 150) -> str:
    """Command text for a mermaid node: sanitize, keep it readable."""
    text = re.sub(r"\s+", " ", str(text)).strip().translate(_TASK)
    return text[: limit - 1] + "…" if len(text) > limit else text


def command_text(cmd: dict[str, Any]) -> str:
    argv = cmd.get("argv") or []
    return " ".join(str(a) for a in argv)


def collect_artifacts(structured: Any) -> list[dict[str, Any]]:
    """Every dict in the result tree that looks like an ArtifactRef."""
    found: list[dict[str, Any]] = []

    def walk(node: Any, seen: set[int]) -> None:
        if isinstance(node, dict):
            if "artifact_id" in node and isinstance(node.get("artifact_id"), str):
                found.append(node)
            if id(node) not in seen:
                seen = seen | {id(node)}
                for value in node.values():
                    walk(value, seen)
        elif isinstance(node, list):
            for item in node:
                walk(item, seen)

    walk(structured, set())
    out: dict[str, dict[str, Any]] = {}
    for a in found:
        out.setdefault(a["artifact_id"], a)
    return list(out.values())


def _artifact_inputs(arguments: dict[str, Any]) -> list[str]:
    """Argument values that reference artifacts (art_ ids) or files."""
    inputs: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, str):
            if node.startswith("art_") or ("." in node and "/" not in node and len(node) < 120):
                if node not in inputs:
                    inputs.append(node)

    walk(arguments)
    return inputs[:6]


def _backend_of(artifacts: list[dict[str, Any]]) -> str:
    for a in artifacts:
        prov = a.get("provenance") or {}
        if prov.get("backend"):
            return str(prov["backend"])
    return ""


def call_diagram(tool: str, arguments: dict[str, Any] | None,
                 structured: Any, *, ok: bool, error: dict[str, Any] | None = None,
                 commands: list[dict[str, Any]] | None = None) -> str:
    """Mermaid flow of ONE tool invocation, including every executed command."""
    args = arguments or {}
    artifacts = collect_artifacts(structured)
    backend = _backend_of(artifacts)

    lines = ["```mermaid", "flowchart LR"]
    inputs = _artifact_inputs(args)
    for i, name in enumerate(inputs):
        lines.append(f'  I{i}["📥 {name}"]')
    label = f"⚙️ {tool}"
    if backend:
        label += f"\\n[{backend}]"
    if not inputs and args:
        params = _clean(str(args), 80)
        label += f"\\n{params}"
    lines.append(f'  T["{label}"]')

    cmds = commands or []
    for k, cmd in enumerate(cmds[:4]):
        text = _clean_cmdtext(command_text(cmd), 100)
        if text:
            lines.append(f'  C{k}["💻 {text}"]')
            lines.append(f"  T --> C{k}")

    if ok:
        if artifacts:
            for j, a in enumerate(artifacts[:6]):
                name = _clean((a.get("path") or a["artifact_id"]).rsplit("/", 1)[-1])
                kind = _clean(a.get("kind", ""), 20)
                lines.append(f'  O{j}["📤 {a["artifact_id"]}\\n{name} · {kind}"]')
                lines.append(f"  T --> O{j}")
        else:
            rtype = structured.get("result_type", "completed") if isinstance(structured, dict) else "completed"
            lines.append(f'  R["📤 {rtype}（只读结果）"]')
            lines.append("  T --> R")
    else:
        code = (error or {}).get("code", "ERROR")
        msg = _clean((error or {}).get("message", ""), 80)
        lines.append(f'  E["❌ {code}\\n{msg}"]')
        lines.append("  T --> E")
    for i in range(len(inputs)):
        lines.append(f"  I{i} --> T")
    lines.append("```")
    return "\n".join(lines)


def reproduction_block(commands: list[dict[str, Any]] | None,
                       ws_root: Any = None) -> str:
    """Copy-pasteable shell block of every command the call executed."""
    cmds = commands or []
    if not cmds:
        return ""
    ws = str(ws_root) if ws_root is not None else ""
    out: list[str] = ["**复现命令**（可直接复制执行）", "", "```bash"]
    staged = False
    for cmd in cmds[:8]:
        argv = cmd.get("argv") or []
        if not argv:
            continue
        env = cmd.get("env") or {}
        prefix = " ".join(f"{k}={shlex.quote(str(v))}" for k, v in env.items())
        line = shlex.join([str(a) for a in argv])
        if prefix:
            line = f"{prefix} {line}"
        bits = []
        if cmd.get("exit_code") is not None:
            bits.append(f"exit {cmd['exit_code']}")
        if cmd.get("duration_s") is not None:
            bits.append(f"{cmd['duration_s']:.2f} s")
        if cmd.get("cancelled"):
            bits.append("cancelled")
        hint = " · ".join(bits)
        out.append(f"# {hint}" if hint else "# command")
        cwd = cmd.get("cwd")
        if cwd:
            out.append(f"cd {shlex.quote(str(cwd))} && {line}")
        else:
            out.append(line)
        out.append("")
        cwd_s = str(cwd) if cwd else ""
        if ws and (any(ws in str(a) for a in argv) or cwd_s.startswith(ws)):
            staged = True
    out.append("```")
    if staged:
        out.append("")
        out.append(f"> 注：`{ws}` 下的文件是本次调用复制的输入副本；复现时把该路径换成你的原文件，"
                   f"或先把它复制到该路径。")
    return "\n".join(out)


def record(tool: str, arguments: dict[str, Any] | None, structured: Any, *,
           ok: bool, commands: list[dict[str, Any]] | None = None,
           summary: str | None = None) -> dict[str, Any]:
    """One journal entry for the session diagrams (kept small on purpose)."""
    artifacts = collect_artifacts(structured)
    backend = _backend_of(artifacts)
    first_cmd = command_text(commands[0]) if commands else ""
    entry: dict[str, Any] = {
        "tool": tool,
        "ok": ok,
        "summary": summary or "",
        "params": param_summary(arguments),
        "inputs": _artifact_inputs(arguments or {}),
        "outputs": [
            {"id": a["artifact_id"],
             "name": _clean((a.get("path") or a["artifact_id"]).rsplit("/", 1)[-1])}
            for a in artifacts[:6]
        ],
        "backend": backend,
        "command": _clean(first_cmd, 90) if first_cmd else "",
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if not ok and isinstance(structured, dict):
        err = structured.get("error") or {}
        entry["error"] = err.get("code")
    return entry


def session_diagram(records: list[dict[str, Any]]) -> str:
    """Combined Mermaid graph of the session's tool calls (most recent
    _SESSION_CAP entries), edges chaining parent artifacts into later calls."""
    recent = records[-_SESSION_CAP:]
    lines = ["```mermaid", "flowchart TD"]
    if not recent:
        lines.append('  EMPTY["本会话还没有工具调用"]')
        lines.append("```")
        return "\n".join(lines)
    known_outputs: set[str] = set()
    for i, rec in enumerate(recent):
        call = f"T{i}"
        label = f"⚙️ {rec['tool']}"
        if rec.get("backend"):
            label += f"\\n[{rec['backend']}]"
        if rec.get("command"):
            label += f"\\n{_clean(rec['command'], 60)}"
        if not rec.get("ok"):
            label += f"\\n❌ {rec.get('error') or 'failed'}"
        lines.append(f'  {call}["{_clean(label, 110)}"]')
        for name in rec.get("inputs", []):
            node = f"A_{re.sub(r'[^A-Za-z0-9]', '_', name)}"[:40]
            if name not in known_outputs:
                lines.append(f'  {node}["📥 {name}"]')
            lines.append(f"  {node} --> {call}")
            known_outputs.add(name)
        for out in rec.get("outputs", []):
            out_id = out["id"] if isinstance(out, dict) else out
            out_name = out.get("name") if isinstance(out, dict) else out_id
            node = f"A_{re.sub(r'[^A-Za-z0-9]', '_', out_id)}"[:40]
            if out_id not in known_outputs:
                lines.append(f'  {node}["📤 {out_id}\\n{_clean(out_name, 40)}"]')
            lines.append(f"  {call} --> {node}")
            known_outputs.add(out_id)
    lines.append("```")
    header = f"本会话调用流程（最近 {len(recent)} 次调用，完整 provenance 见各 artifact）：\n\n"
    return header + "\n".join(lines)


# ---------------------------------------------------------------------------
# 处理流程: step-by-step view — what each step DID (action + params + result)
# ---------------------------------------------------------------------------

_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
_STEPS_CAP = 30


def _step_no(i: int) -> str:
    return _CIRCLED[i] if i < len(_CIRCLED) else f"({i + 1})"


def first_line(text: str, limit: int = 90) -> str:
    """First non-empty line of a response summary — the step's outcome."""
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped:
            return _clean(stripped, limit)
    return ""


def param_summary(arguments: dict[str, Any] | None, limit: int = 3) -> str:
    """Compact "k=v, k=v" of the scalar call parameters (what the step was
    told to do). Lists/objects and plumbing args are skipped."""
    parts: list[str] = []
    for key, value in (arguments or {}).items():
        if key in ("execution_mode", "root", "output_name"):
            continue
        if isinstance(value, bool):
            if value:
                parts.append(key)
        elif isinstance(value, (int, float)):
            parts.append(f"{key}={value}")
        elif isinstance(value, str) and len(value) <= 40 and not value.startswith("art_"):
            parts.append(f"{key}={value}")
        if len(parts) >= limit:
            break
    return _clean(", ".join(parts), 70)


def steps_diagram(records: list[dict[str, Any]]) -> str:
    """Step-by-step processing flowchart: one node per executed step, in
    order, each labelled with WHAT the step did (tool action), the key
    parameters it was given, and the outcome summary."""
    recent = records[-_STEPS_CAP:]
    if not recent:
        return ""
    lines = ["```mermaid", "flowchart TD"]
    for i, rec in enumerate(recent):
        mark = "" if rec.get("ok", True) else "❌ "
        summary = rec.get("summary") or (rec.get("error") or "")
        parts = [f"{_step_no(i)} {mark}{rec['tool']}"]
        if summary:
            parts.append(_clean(summary, 80))
        params = rec.get("params")
        if params and params not in summary:
            parts.append(_clean(params, 60))
        label = "<br/>".join(parts)
        lines.append(f'  S{i}["{label}"]')
        if i:
            lines.append(f"  S{i - 1} --> S{i}")
    lines.append("```")
    header = f"处理流程（{len(recent)} 步，按执行顺序）：\n\n"
    return header + "\n".join(lines)
