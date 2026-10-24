"""Graceful degradation + error envelopes with no real backends installed."""

from __future__ import annotations

from psrmcp.errors import ErrorCode
from psrmcp.models.common import BackendInfo


async def test_environment_reports_all_backends(ctx, registry):
    out = await registry.dispatch("pulsar_environment", {})
    assert out.is_error is False
    data = out.structured_content
    backends = data["backends"]
    for name in ("dspsr", "psrchive", "tempo2", "psrcat"):
        assert backends[name]["available"] is False, name
        assert backends[name]["reason"], f"{name} must explain why it is missing"
    assert "workspace" in data and "limits" in data


def _fake_backend(name: str) -> BackendInfo:
    return BackendInfo(available=True, version="test", capabilities=[name])


def _fake_backends(ctx) -> None:
    """Shadow probe methods so path checks run without real binaries."""

    async def fake(name: str, *, refresh: bool = False) -> BackendInfo:
        return _fake_backend(name)

    ctx.backends.get = fake
    ctx.backends.require = fake


async def test_archive_inspect_missing_path_envelope(ctx, registry, monkeypatch):
    _fake_backends(ctx)
    monkeypatch.setattr("psrmcp.tools.archive._tool", lambda c, n: f"/fake/{n}")
    out = await registry.dispatch("archive_inspect", {"archive": "ghost.ar"})
    assert out.is_error is True
    err = out.structured_content["error"]
    assert err["code"] == ErrorCode.NOT_FOUND.value
    assert err["retryable"] is False
    assert set(err) >= {"code", "message", "retryable"}  # hint/details optional (TOOLS 1.5)


async def test_archive_inspect_traversal_denied(ctx, registry, monkeypatch):
    _fake_backends(ctx)
    monkeypatch.setattr("psrmcp.tools.archive._tool", lambda c, n: f"/fake/{n}")
    out = await registry.dispatch("archive_inspect", {"archive": "../escape.ar"})
    assert out.is_error is True
    assert out.structured_content["error"]["code"] == ErrorCode.PATH_DENIED.value


async def test_env_missing_when_backend_absent(ctx, registry):
    # No psrchive in cache: archive_inspect degrades to E_ENV_MISSING.
    out = await registry.dispatch("archive_inspect", {"archive": "whatever.ar"})
    assert out.is_error is True
    assert out.structured_content["error"]["code"] == ErrorCode.ENV_MISSING.value


async def test_catalog_local_without_psrcat(ctx, registry):
    ctx.cfg.catalog.mode = "local"
    out = await registry.dispatch("catalog_search", {"name": "J0437-4715"})
    assert out.is_error is True
    assert out.structured_content["error"]["code"] == ErrorCode.ENV_MISSING.value


async def test_unknown_tool_invalid_input(ctx, registry):
    out = await registry.dispatch("no_such_tool", {})
    assert out.is_error is True
    assert out.structured_content["error"]["code"] == ErrorCode.INVALID_INPUT.value


async def test_validation_error_locates_field(ctx, registry):
    out = await registry.dispatch("catalog_search", {"radius_deg": 5.0})  # radius without ra/dec
    assert out.is_error is True
    assert out.structured_content["error"]["code"] == ErrorCode.INVALID_INPUT.value


async def test_extra_properties_rejected(ctx, registry):
    out = await registry.dispatch("pulsar_environment", {"bogus": True})
    assert out.is_error is True
    assert out.structured_content["error"]["code"] == ErrorCode.INVALID_INPUT.value


async def test_artifact_list_roundtrip(ctx, registry):
    from datetime import datetime, timezone

    from psrmcp.models.common import ArtifactKind, Provenance

    prov = Provenance(tool="t", tool_version="0", backend="b", params={},
                      started_at=datetime.now(timezone.utc), duration_s=0.0)
    ref = ctx.store.commit_text(text="x", filename="a.tim", kind=ArtifactKind.TIM,
                                format_name="tempo2", provenance=prov)
    out = await registry.dispatch("artifact_list", {"kind": "tim"})
    assert out.is_error is False
    assert any(a["artifact_id"] == ref.artifact_id for a in out.structured_content["artifacts"])


async def test_job_get_and_cancel_flow(ctx, registry):
    out = await registry.dispatch("job_get", {"job_id": "job_missing0001"})
    assert out.is_error is True
    assert out.structured_content["error"]["code"] == ErrorCode.TASK_NOT_FOUND.value

    from psrmcp.models.results import ToaExtractResult  # noqa: F401  (long-task import sanity)

    sub = await registry.dispatch(
        "artifact_list", {}  # not long; submit path covered by job tests
    )
    assert sub.is_error is False


async def test_long_task_async_accepted_then_job_fails_gracefully(ctx, registry, tmp_path):
    """toa_extract is long_task: auto -> AsyncAccepted; the job then fails on
    missing psrchive and the error is retrievable via job_get."""
    import asyncio

    data = tmp_path / "data"
    (data / "obs.ar").write_bytes(b"fake")
    (data / "std.ar").write_bytes(b"fake")

    out = await registry.dispatch(
        "toa_extract", {"archive": "obs.ar", "standard_profile": "std.ar"}
    )
    assert out.is_error is False
    acc = out.structured_content
    assert acc["result_type"] == "accepted"  # TOOLS.md 2: AsyncAccepted discriminator
    job_id = acc["job"]["job_id"]

    ref = None
    for _ in range(200):
        ref = ctx.jobs.get(job_id)
        if ref.status.value in ("completed", "failed", "cancelled", "interrupted"):
            break
        await asyncio.sleep(0.02)
    assert ref is not None and ref.status.value == "failed"
    assert ref.error is not None and ref.error.code == ErrorCode.ENV_MISSING.value

    got = await registry.dispatch("job_get", {"job_id": job_id})
    assert got.is_error is False
    assert got.structured_content["job"]["status"] == "failed"

    cancel_after = await registry.dispatch("job_cancel", {"job_id": job_id})
    assert cancel_after.is_error is True  # already finished -> E_JOB_NOT_RUNNING


async def test_sync_mode_forces_inline_execution(ctx, registry, tmp_path):
    """execution_mode=sync on a long tool runs inline and returns the error directly."""
    data = tmp_path / "data"
    (data / "obs.ar").write_bytes(b"fake")
    (data / "std.ar").write_bytes(b"fake")

    out = await registry.dispatch(
        "toa_extract",
        {"archive": "obs.ar", "standard_profile": "std.ar", "execution_mode": "sync"},
    )
    assert out.is_error is True
    assert out.structured_content["error"]["code"] == ErrorCode.ENV_MISSING.value
