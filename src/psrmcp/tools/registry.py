"""Tool registry: contract-driven registration, dispatch, error envelope (TOOLS.md 1.3/1.5)."""

from __future__ import annotations

import asyncio
import json
import logging
import traceback
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from mcp.types import CallToolResult, TextContent, Tool, ToolAnnotations
from pydantic import BaseModel, ValidationError

from .. import flow
from ..errors import ErrorCode, PsrmcpError, invalid_input
from ..models.common import AsyncAccepted, JobStatus
from ._common import AppContext, DEFAULT_PROGRESS, ProgressFn

log = logging.getLogger(__name__)

Handler = Callable[[AppContext, Any, ProgressFn], Awaitable[Any]]


@dataclass
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    annotations: ToolAnnotations
    handler: Handler
    long_task: bool = False


class ToolRegistry:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self._specs: dict[str, ToolSpec] = {}

    def add(self, spec: ToolSpec) -> None:
        if spec.name in self._specs:
            raise ValueError(f"duplicate tool: {spec.name}")
        self._specs[spec.name] = spec

    def spec(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def names(self) -> list[str]:
        return sorted(self._specs)

    # -- MCP list_tools -------------------------------------------------------

    def mcp_tools(self) -> list[Tool]:
        tools = []
        for spec in self._specs.values():
            tools.append(
                Tool(
                    name=spec.name,
                    description=spec.description,
                    inputSchema=_input_schema(spec.input_model),
                    annotations=spec.annotations,
                )
            )
        return tools

    # -- MCP call_tool --------------------------------------------------------

    async def dispatch(self, name: str, arguments: dict[str, Any] | None) -> CallToolResult:
        spec = self._specs.get(name)
        n0 = self._history_len()
        if spec is None:
            return self._with_flow(name, arguments,
                                   _error_result(invalid_input(f"unknown tool: {name}")),
                                   commands=self._commands_since(n0))
        try:
            try:
                tool_input = spec.input_model.model_validate(arguments or {})
            except ValidationError as exc:
                raise _validation_error(spec.name, exc) from exc

            mode = getattr(tool_input, "execution_mode", None)
            # Long-capable tools go async unless execution_mode=sync is forced
            # (TOOLS.md 1.4: auto prefers async for declared long tasks).
            if spec.long_task and str(mode) != "sync":
                out = await self._submit_job(spec, tool_input)
                return self._with_flow(spec.name, arguments, out,
                                       commands=self._commands_since(n0))

            result = await spec.handler(self.ctx, tool_input, DEFAULT_PROGRESS)
            out = _result_envelope(result)
            return self._with_flow(name, arguments, out,
                                   commands=self._commands_since(n0))
        except PsrmcpError as exc:
            # commands run before the failure are still reported — they are
            # what the user needs to reproduce the failing step
            return self._with_flow(name, arguments, _error_result(exc),
                                   commands=self._commands_since(n0))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("tool %s crashed", name)
            return self._with_flow(
                name, arguments,
                _error_result(
                    PsrmcpError(
                        ErrorCode.INTERNAL,
                        f"{type(exc).__name__}: {exc}",
                        details={"traceback": traceback.format_exc()[-2000:]},
                    )
                ),
                commands=self._commands_since(n0),
            )

    # -- command capture (reproducibility) --------------------------------

    def _history_len(self) -> int:
        history = getattr(self.ctx.runner, "history", None) or []
        return len(history)

    def _commands_since(self, n0: int) -> list[dict[str, Any]]:
        history = getattr(self.ctx.runner, "history", None) or []
        out: list[dict[str, Any]] = []
        for h in history[n0:]:
            argv = list(getattr(h, "argv", []) or [])
            if "--version" in argv:
                continue  # backend version probes are not part of the workflow
            out.append(h.as_dict())
        return out

    def _with_flow(self, name: str, arguments: dict[str, Any] | None,
                   out: CallToolResult, *, commands: list[dict[str, Any]] | None = None) -> CallToolResult:
        """Append the call-flow diagram and the reproduction commands to the
        response text and record the call in the session journal. Best-effort
        decoration: a reporting bug must never turn a success into an error."""
        try:
            structured = out.structured_content
            cmds = list(commands or [])
            # job_get: attach the commands captured when the job ran
            if name == "job_get" and not cmds:
                job = structured.get("job") or {}
                cmds = self.ctx.job_commands.get(job.get("job_id") or "", [])
            diagram = flow.call_diagram(name, arguments, structured,
                                        ok=not out.is_error,
                                        error=structured.get("error"),
                                        commands=cmds)
            text = out.content[0].text if out.content else ""
            self.ctx.flow_log.append(flow.record(
                name, arguments, structured, ok=not out.is_error, commands=cmds,
                summary=flow.first_line(text),
            ))
            if text:
                text = text + "\n\n**调用流程**\n\n" + diagram
                repro = flow.reproduction_block(cmds, self.ctx.ws.root)
                if repro:
                    text += "\n\n" + repro
                # 处理流程: step-by-step view — what each step did, in order
                steps = flow.steps_diagram(self.ctx.flow_log)
                if steps:
                    text += "\n\n**处理流程**\n\n" + steps
                out.content[0].text = text
        except Exception:  # noqa: BLE001
            log.debug("flow diagram failed for %s", name, exc_info=True)
        return out

    async def _submit_job(self, spec: ToolSpec, tool_input: Any) -> CallToolResult:
        params = json.loads(tool_input.model_dump_json(exclude={"execution_mode"}))
        holder: dict[str, str] = {}

        async def run(progress: ProgressFn) -> dict[str, Any]:
            n0 = self._history_len()
            try:
                result = await spec.handler(self.ctx, tool_input, progress)
                return result.model_dump(mode="json")
            finally:
                # job_get picks these up once the job has run (TOOLS.md 1.3)
                job_id = holder.get("job_id")
                if job_id:
                    self.ctx.job_commands[job_id] = self._commands_since(n0)

        ref = self.ctx.jobs.submit(spec.name, run, params)
        holder["job_id"] = ref.job_id
        if ref.status == JobStatus.FAILED:
            # Submit-time validation failure: surface the error directly.
            err = ref.error
            exc = PsrmcpError(
                ErrorCode.INVALID_INPUT if err and err.code == "E_INVALID_INPUT" else ErrorCode.INTERNAL,
                err.message if err else "job failed",
            )
            return _error_result(exc)
        return _result_envelope(AsyncAccepted(job=ref))


def _validation_error(tool: str, exc: ValidationError) -> PsrmcpError:
    first = exc.errors()[0]
    loc = ".".join(str(x) for x in first.get("loc", ()))
    msg = first.get("msg", "invalid input")
    return invalid_input(f"{tool}: invalid argument {loc}: {msg}", hint="check argument types and value ranges")


def _result_envelope(result: Any) -> CallToolResult:
    structured = result.model_dump(mode="json")
    summary = result.summarize() if hasattr(result, "summarize") else json.dumps(structured, ensure_ascii=False)
    if not summary:
        summary = f"result_type={structured.get('result_type', 'completed')}"
    return CallToolResult(
        content=[TextContent(type="text", text=summary)],
        structuredContent=structured,
        isError=False,
    )


def _error_result(exc: PsrmcpError) -> CallToolResult:
    body = exc.as_dict()
    text = f"**[{body['code']}]** {body['message']}"
    if body.get("hint"):
        text += f"\nHint: {body['hint']}"
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structuredContent={"result_type": "failed", "error": body},
        isError=True,
    )


def _input_schema(model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema(ref_template="#/$defs/{model}")
    defs = schema.pop("$defs", {})
    # Inline $defs so clients receive a self-contained schema.
    _inline_defs(schema, defs)
    schema.pop("$schema", None)
    schema.pop("title", None)
    return schema


def _inline_defs(node: Any, defs: dict[str, Any]) -> None:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            name = ref.split("/")[-1]
            target = defs.get(name, {})
            node.clear()
            node.update(json.loads(json.dumps(target)))  # deep copy
            _inline_defs(node, defs)
            return
        for key, value in node.items():
            _inline_defs(value, defs)
    elif isinstance(node, list):
        for item in node:
            _inline_defs(item, defs)
