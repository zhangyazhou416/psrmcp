"""Common types (TOOLS.md §2 $defs) as Pydantic models.

Field names, patterns and enums mirror the contract 1:1 so generated JSON
schemas can be snapshot-tested against the document.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

_EXTRA_FORBID = ConfigDict(extra="forbid")


class ArtifactKind(StrEnum):
    ARCHIVE = "archive"
    EPHEMERIS = "ephemeris"
    TIM = "tim"
    PAR = "par"
    PROFILE = "profile"
    MASK = "mask"
    PLOT = "plot"
    RESIDUALS = "residuals"
    LOG = "log"
    CATALOG = "catalog"
    JSON = "json"
    OTHER = "other"


class Provenance(BaseModel):
    model_config = _EXTRA_FORBID

    tool: str
    tool_version: str
    backend: str
    backend_version: str | None = None
    params: dict[str, Any]
    command: list[str] = Field(default_factory=list)
    input_hashes: dict[str, str] = Field(default_factory=dict)
    started_at: datetime
    duration_s: float = Field(ge=0)


class WarningInfo(BaseModel):
    """TOOLS.md §2 `Warning` (renamed to avoid shadowing the builtin)."""

    model_config = _EXTRA_FORBID

    code: str = Field(pattern=r"^W_[A-Z0-9_]+$")
    message: str
    severity: Literal["info", "warning", "error"] = "info"
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorBody(BaseModel):
    model_config = _EXTRA_FORBID

    code: str
    message: str
    hint: str | None = None
    retryable: bool
    details: dict[str, Any] = Field(default_factory=dict)
    log_artifact_id: str | None = None


class ArtifactRef(BaseModel):
    model_config = _EXTRA_FORBID

    artifact_id: str = Field(pattern=r"^art_[A-Za-z0-9_-]{8,64}$")
    kind: ArtifactKind
    format: str = Field(min_length=1)
    path: str | None = None
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    created_at: datetime
    parents: list[str] = Field(default_factory=list)
    provenance: Provenance


class JobStatus(StrEnum):
    QUEUED = "queued"
    WORKING = "working"
    INPUT_REQUIRED = "input_required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class JobRef(BaseModel):
    model_config = _EXTRA_FORBID

    job_id: str = Field(pattern=r"^job_[A-Za-z0-9_-]{8,64}$")
    status: JobStatus
    phase: str
    progress: float = Field(ge=0, le=1)
    message: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result: dict[str, Any] | None = None
    error: ErrorBody | None = None


class BackendInfo(BaseModel):
    model_config = _EXTRA_FORBID

    available: bool
    path: str | None = None
    version: str | None = None
    reason: str | None = None
    capabilities: list[str] = Field(default_factory=list)


class CatalogRecord(BaseModel):
    model_config = _EXTRA_FORBID

    name: str
    aliases: list[str] = Field(default_factory=list)
    ra_deg: float = Field(ge=0, le=360)
    dec_deg: float = Field(ge=-90, le=90)
    p0_s: float = Field(gt=0)
    f0_hz: float | None = None
    p1: float | None = None
    dm: float = Field(ge=0)
    binary: bool
    binary_model: str | None = None
    observatory: str | None = None
    catalog_version: str


class CatalogMode(StrEnum):
    LOCAL = "local"
    ONLINE = "online"
    CACHE = "cache"


class CatalogMeta(BaseModel):
    model_config = _EXTRA_FORBID

    source: str
    version: str
    retrieved_at: datetime
    mode: CatalogMode


class AsyncAccepted(BaseModel):
    """Envelope returned when a long job is accepted (TOOLS.md §1.4)."""

    model_config = _EXTRA_FORBID

    result_type: Literal["accepted"] = "accepted"
    job: JobRef


COMPLETED = "completed"
