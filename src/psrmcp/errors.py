"""Stable error codes (TOOLS.md §3) and the single exception type tools may raise."""

from __future__ import annotations

from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    CONFIG = "E_CONFIG"
    ENV_MISSING = "E_ENV_MISSING"
    INVALID_INPUT = "E_INVALID_INPUT"
    NOT_FOUND = "E_NOT_FOUND"
    PATH_DENIED = "E_PATH_DENIED"
    FORMAT_UNSUPPORTED = "E_FORMAT_UNSUPPORTED"
    SOURCE_MISMATCH = "E_SOURCE_MISMATCH"
    EPHEMERIS_MISMATCH = "E_EPHEMERIS_MISMATCH"
    OUTPUT_CONFLICT = "E_OUTPUT_CONFLICT"
    PROCESS_FAILED = "E_PROCESS_FAILED"
    TIMEOUT = "E_TIMEOUT"
    CANCELLED = "E_CANCELLED"
    QUOTA_EXCEEDED = "E_QUOTA_EXCEEDED"
    TASK_NOT_FOUND = "E_TASK_NOT_FOUND"
    JOB_NOT_RUNNING = "E_JOB_NOT_RUNNING"
    VERSION_INCOMPATIBLE = "E_VERSION_INCOMPATIBLE"
    INTERNAL = "E_INTERNAL"


class PsrmcpError(Exception):
    """Carries an ErrorBody payload (TOOLS.md §1.5) up to the tool registry."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        hint: str | None = None,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
        log_artifact_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.retryable = retryable
        self.details = details or {}
        self.log_artifact_id = log_artifact_id

    def as_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "code": self.code.value,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.hint is not None:
            body["hint"] = self.hint
        if self.details:
            body["details"] = self.details
        if self.log_artifact_id is not None:
            body["log_artifact_id"] = self.log_artifact_id
        return body


def config_error(message: str, **kw: Any) -> PsrmcpError:
    return PsrmcpError(ErrorCode.CONFIG, message, **kw)


def invalid_input(message: str, hint: str | None = None, **kw: Any) -> PsrmcpError:
    return PsrmcpError(ErrorCode.INVALID_INPUT, message, hint=hint, **kw)


def not_found(message: str, hint: str | None = None, **kw: Any) -> PsrmcpError:
    return PsrmcpError(ErrorCode.NOT_FOUND, message, hint=hint, **kw)


def path_denied(message: str, hint: str | None = None, **kw: Any) -> PsrmcpError:
    return PsrmcpError(
        ErrorCode.PATH_DENIED,
        message,
        hint=hint or "使用配置的 read_roots 下的相对路径或 artifact ID",
        **kw,
    )


def env_missing(backend: str, reason: str, hint: str | None = None) -> PsrmcpError:
    return PsrmcpError(
        ErrorCode.ENV_MISSING,
        f"backend '{backend}' unavailable: {reason}",
        hint=hint or "运行 scripts/install_pulsar_stack.sh 安装对应后端,或在配置中修正其路径",
        details={"backend": backend},
    )


def format_unsupported(message: str, **kw: Any) -> PsrmcpError:
    return PsrmcpError(ErrorCode.FORMAT_UNSUPPORTED, message, **kw)


def process_failed(message: str, *, log_artifact_id: str | None = None, **kw: Any) -> PsrmcpError:
    return PsrmcpError(
        ErrorCode.PROCESS_FAILED,
        message,
        retryable=kw.pop("retryable", True),
        log_artifact_id=log_artifact_id,
        **kw,
    )
