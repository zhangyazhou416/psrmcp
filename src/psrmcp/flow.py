"""Call-flow diagrams (TOOLS.md 1.3): every tool response carries a Mermaid
flow of how it was invoked — inputs -> tool (backend + command) -> outputs —
and `pulsar://calls` renders the accumulated session graph. Diagrams are
best-effort decoration: generation must never break a tool result.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

_SANITIZE = str.maketrans({
    '"': "'", "[": "(", "]": ")", "{": "(", "}": ")",
    "|": "/", "\n": " ", "\r": "",
})
_MAX_NODE = 70
_SESSION_CAP = 50  # session diagram keeps the most recent N calls


def _clean(text: str, limit: int = _MAX_NODE) -> str:
    text = re.sub(r"\s+", " ", str(text)).translate(_SANITIZE).strip()
    return text[: limit - 1] + "…" if len(text) > limit else text


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
    # dedupe by artifact_id, keep first
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


def _short_command(command: list[str] | None) -> str:
    if not command:
        return ""
    return _clean(" ".join(command), 90)


def _backend_of(artifacts: list[dict[str, Any]]) -> tuple[str, list[str]]:
    for a in artifacts:
        prov = a.get("provenance") or {}
        if prov.get("backend"):
            return str(prov["backend"]), prov.get("command") or []
    return "", []


def call_diagram(tool: str, arguments: dict[str, Any] | None,
                 structured: Any, *, ok: bool, error: dict[str, Any] | None = None) -> str:
    """Mermaid flow of ONE tool invocation. On failure the error code and
    message head the terminal node; on success the outputs (or the result
    type for read-only calls) do."""
    args = arguments or {}
    artifacts = collect_artifacts(structured)
    backend, command = _backend_of(artifacts)

    lines = ["```mermaid", "flowchart LR"]
    inputs = _artifact_inputs(args)
    for i, name in enumerate(inputs):
        lines.append(f'  I{i}["📥 {name}"]')
    label = f"⚙️ {tool}"
    if backend:
        label += f"\\n[{backend}]"
    cmd = _short_command(command)
    if cmd:
        label += f"\\n{cmd}"
    if not inputs and args:
        params = _clean(json.dumps(args, ensure_ascii=False), 80)
        label += f"\\n{params}"
    call_id = "T"
    lines.append(f'  {call_id}["{label}"]')

    if ok:
        if artifacts:
            for j, a in enumerate(artifacts[:6]):
                name = _clean((a.get("path") or a["artifact_id"]).rsplit("/", 1)[-1])
                kind = _clean(a.get("kind", ""), 20)
                lines.append(f'  O{j}["📤 {a["artifact_id"]}\\n{name} · {kind}"]')
                lines.append(f"  {call_id} --> O{j}")
        else:
            rtype = structured.get("result_type", "completed") if isinstance(structured, dict) else "completed"
            lines.append(f'  R["📤 {rtype}（只读结果）"]')
            lines.append(f"  {call_id} --> R")
    else:
        code = (error or {}).get("code", "ERROR")
        msg = _clean((error or {}).get("message", ""), 80)
        lines.append(f'  E["❌ {code}\\n{msg}"]')
        lines.append(f"  {call_id} --> E")
    for i in range(len(inputs)):
        lines.append(f"  I{i} --> {call_id}")
    lines.append("```")
    return "\n".join(lines)


def record(tool: str, arguments: dict[str, Any] | None, structured: Any, *,
           ok: bool) -> dict[str, Any]:
    """One journal entry for the session diagram (kept small on purpose)."""
    artifacts = collect_artifacts(structured)
    backend, command = _backend_of(artifacts)
    entry: dict[str, Any] = {
        "tool": tool,
        "ok": ok,
        "inputs": _artifact_inputs(arguments or {}),
        "outputs": [
            {"id": a["artifact_id"],
             "name": _clean((a.get("path") or a["artifact_id"]).rsplit("/", 1)[-1])}
            for a in artifacts[:6]
        ],
        "backend": backend,
        "command": _short_command(command),
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
