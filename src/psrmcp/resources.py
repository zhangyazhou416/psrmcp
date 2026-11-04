"""MCP resources (DESIGN.md 5.1): environment, catalog, ephemeris, artifact, job, docs.

Large archives are never returned through resources — only metadata and text
byproducts; tools reference artifacts by ID (DESIGN.md 5.1 note).
"""

from __future__ import annotations

import base64

from mcp.types import BlobResourceContents, Resource, TextResourceContents

from .errors import ErrorCode, PsrmcpError, not_found
from .models.inputs import CatalogGetInput, EphemerisExportInput, PulsarEnvironmentInput
from .tools._common import DEFAULT_PROGRESS, AppContext
from .tools.catalog import _export, _get
from .tools.system import run as run_environment

MIME_JSON = "application/json"
MIME_TEXT = "text/plain"
MIME_MD = "text/markdown"
MIME_PNG = "image/png"

_DOCS: dict[str, str] = {
    "reduction": (
        "# Single-observation pulsar reduction workflow\n\n"
        "1. `data_find` locates raw data; `archive_inspect` inspects metadata.\n"
        "2. `dedisperse` dedisperses (DM required: from the archive header, an ephemeris, or dm_override).\n"
        "3. `rfi_scan` assesses bad channels; `rfi_zap` flags them.\n"
        "4. `profile_standardize` produces the standardized profile; `profile_measure` measures S/N and width.\n"
        "5. Reference all products by artifact ID; for long tasks use execution_mode=auto and poll with job_get.\n\n"
        "Safety constraint: inputs may only be workspace whitelisted paths or artifact IDs; do not pass absolute paths to files outside read_roots."
    ),
    "timing": (
        "# Timing analysis workflow\n\n"
        "1. `ephemeris_export` exports a .par from the catalog (or use an existing par artifact).\n"
        "2. `toa_extract` extracts TOAs from each archive against the standard profile and merges them into a .tim.\n"
        "3. `timing_fit` (backend=pint recommended) fits; inspect parameters and reduced chi2.\n"
        "4. `timing_diagnose` checks outliers/JUMPs/DM/residual distribution; clean per recommendations and refit.\n\n"
        "uniform weighting changes the statistics (W_UNIFORM_WEIGHTING warning); the default is 1/sigma^2."
    ),
    "rfi": (
        "# RFI handling guide\n\n"
        "- `rfi_scan` (method=spectral_kurtosis requires the clfd backend) returns a suggested mask and plot.\n"
        "- `rfi_zap` accepts explicit channels (e.g. an index list expanded from [0,1,240-255]) or auto.\n"
        "- zap is an irreversible flag write (produces a .paz); validate on a small range before a full run; the product provenance records the mask."
    ),
}


def list_resources(ctx: AppContext) -> list[Resource]:
    """Static entries plus artifacts from the current session."""
    items: list[Resource] = [
        Resource(
            uri="pulsar://environment",
            name="Environment and backend status",
            description="Backend availability, versions, workspace, and limits",
            mimeType=MIME_JSON,
        ),
    ]
    for topic, text in _DOCS.items():
        items.append(Resource(
            uri=f"pulsar://docs/{topic}",
            name=f"Guide: {topic}",
            description=text.splitlines()[0].lstrip("# "),
            mimeType=MIME_MD,
        ))
    for ref in ctx.store.iter_index():
        items.append(Resource(
            uri=f"pulsar://artifact/{ref.artifact_id}",
            name=ref.path.rsplit("/", 1)[-1],
            description=f"{ref.kind.value} / {ref.format}",
            mimeType=MIME_PNG if ref.format == "png" else MIME_JSON,
        ))
    return items


async def read_resource(ctx: AppContext, uri: str) -> list[TextResourceContents | BlobResourceContents]:
    scheme, sep, rest = uri.partition("://")
    if scheme != "pulsar" or not sep:
        raise PsrmcpError(
            ErrorCode.INVALID_INPUT, f"unsupported resource scheme: {scheme or uri}",
            hint="use pulsar://environment, pulsar://artifact/{id}, etc.",
        )
    parts = [p for p in rest.split("/") if p]

    if uri == "pulsar://environment":
        result = await run_environment(ctx, PulsarEnvironmentInput(), DEFAULT_PROGRESS)
        return [TextResourceContents(uri=uri, mimeType=MIME_JSON, text=result.model_dump_json(indent=2))]

    if parts and parts[0] == "docs":
        topic = parts[1] if len(parts) > 1 else ""
        if topic not in _DOCS:
            raise not_found(f"unknown docs topic: {topic} (available: {', '.join(_DOCS)})")
        return [TextResourceContents(uri=uri, mimeType=MIME_MD, text=_DOCS[topic])]

    if parts and parts[0] == "catalog":
        if len(parts) != 2:
            raise PsrmcpError(ErrorCode.INVALID_INPUT, "usage: pulsar://catalog/{name}")
        res = await _get(ctx, CatalogGetInput(name=parts[1]), DEFAULT_PROGRESS)
        return [TextResourceContents(uri=uri, mimeType=MIME_JSON, text=res.record.model_dump_json(indent=2))]

    if parts and parts[0] == "ephemeris":
        if len(parts) != 2:
            raise PsrmcpError(ErrorCode.INVALID_INPUT, "usage: pulsar://ephemeris/{name}")
        res = await _export(
            ctx, EphemerisExportInput(pulsar=parts[1]), DEFAULT_PROGRESS
        )
        path = ctx.ws.root / res.artifact.path
        return [TextResourceContents(uri=uri, mimeType=MIME_TEXT, text=path.read_text(encoding="utf-8"))]

    if parts and parts[0] == "artifact":
        if len(parts) not in (2, 3):
            raise PsrmcpError(ErrorCode.INVALID_INPUT, "usage: pulsar://artifact/{id}[/{log|preview}]")
        ref = ctx.store.get(parts[1])
        if len(parts) == 2:
            return [TextResourceContents(uri=uri, mimeType=MIME_JSON, text=ref.model_dump_json(indent=2))]
        sub = parts[2]
        path = ctx.ws.root / ref.path
        if sub == "log":
            if ref.kind != "log":
                raise PsrmcpError(
                    ErrorCode.NOT_FOUND, f"artifact {ref.artifact_id} is not a log ({ref.kind.value})"
                )
            return [TextResourceContents(uri=uri, mimeType=MIME_TEXT, text=path.read_text(errors="replace"))]
        if sub == "preview":
            if ref.format != "png":
                # DESIGN 5.1: never base64 large binaries; only previews blob out.
                raise PsrmcpError(
                    ErrorCode.FORMAT_UNSUPPORTED,
                    f"artifact {ref.artifact_id} has no preview ({ref.format}); reference the artifact ID and process it with tools",
                )
            return [BlobResourceContents(
                uri=uri, mimeType=MIME_PNG,
                blob=base64.b64encode(path.read_bytes()).decode("ascii"),
            )]
        raise PsrmcpError(ErrorCode.INVALID_INPUT, f"unknown artifact sub-resource: {sub}")

    if parts and parts[0] == "job":
        if len(parts) != 2:
            raise PsrmcpError(ErrorCode.INVALID_INPUT, "usage: pulsar://job/{id}")
        return [TextResourceContents(
            uri=uri, mimeType=MIME_JSON, text=ctx.jobs.get(parts[1]).model_dump_json(indent=2),
        )]

    raise not_found(f"unknown resource: {uri}")


def docs_topics() -> list[str]:
    return sorted(_DOCS)
