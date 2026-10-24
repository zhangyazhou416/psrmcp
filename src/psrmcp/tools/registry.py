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
        if spec is None:
            return _error_result(invalid_input(f"未知工具: {name}"))
        try:
            try:
                tool_input = spec.input_model.model_validate(arguments or {})
            except ValidationError as exc:
                raise _validation_error(spec.name, exc) from exc

            mode = getattr(tool_input, "execution_mode", None)
            # Long-capable tools go async unless execution_mode=sync is forced
            # (TOOLS.md 1.4: auto prefers async for declared long tasks).
            if spec.long_task and str(mode) != "sync":
                return await self._submit_job(spec, tool_input)

            result = await spec.handler(self.ctx, tool_input, DEFAULT_PROGRESS)
            return _result_envelope(result)
        except PsrmcpError as exc:
            return _error_result(exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("tool %s crashed", name)
            return _error_result(
                PsrmcpError(
                    ErrorCode.INTERNAL,
                    f"{type(exc).__name__}: {exc}",
                    details={"traceback": traceback.format_exc()[-2000:]},
                )
            )

    async def _submit_job(self, spec: ToolSpec, tool_input: Any) -> CallToolResult:
        params = json.loads(tool_input.model_dump_json(exclude={"execution_mode"}))

        async def run(progress: ProgressFn) -> dict[str, Any]:
            result = await spec.handler(self.ctx, tool_input, progress)
            return result.model_dump(mode="json")

        ref = self.ctx.jobs.submit(spec.name, run, params)
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
    return invalid_input(f"{tool}: 参数无效 {loc}: {msg}", hint="检查参数类型与取值范围")


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
        text += f"\n提示: {body['hint']}"
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
