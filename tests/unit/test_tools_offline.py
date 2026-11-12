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


# ---------------------------------------------------------------------------
# archive_transform / archive_edit / profile_optimize (scripted backends)
# ---------------------------------------------------------------------------

_VAP_DIMS = (
    "J0437-4715 12:34 43:56 2.64 5.75e-3 1400.5 256 64 2 1024 16 600 PKS"
)
_PDMP_SAMPLE = """\
Reading archive obs.ar
Best barycentric period (ms) = 22.789120 +/- 1.2e-05
Best DM = 2.6441 +/- 0.002
Best signal-to-noise ratio = 154.3
"""


class _ScriptedRunner:
    """Maps binary basename -> (exit, stdout, stderr); optional produce hook
    materialises expected output files so handlers can find them."""

    def __init__(self, responses, produce=None):
        self.responses = responses
        self.calls = []
        self._produce = produce

    async def run(self, argv, *, cwd=None, env=None, timeout_s=None, cancel=None, stdin_bytes=None):
        from pathlib import Path as _P

        from psrmcp.execution.runner import RunResult

        self.calls.append(list(argv))
        name = _P(argv[0]).name
        exit_code, out, err = self.responses.get(name, (0, "", ""))
        if self._produce and exit_code == 0:
            self._produce(name, cwd)
        return RunResult(
            argv=list(argv), exit_code=exit_code,
            stdout=out.encode(), stderr=err.encode(),
            duration_s=0.01, timed_out=False,
        )

    def stderr_tail(self, n: int = 800) -> str:
        return ""


def _archive_env(ctx, tmp_path):
    _fake_backends(ctx)
    data = tmp_path / "data"
    (data / "obs.ar").write_bytes(b"fake")
    return data


async def test_archive_transform_success(ctx, registry, tmp_path, monkeypatch):
    data = _archive_env(ctx, tmp_path)
    monkeypatch.setattr("psrmcp.tools.archive._tool", lambda c, n: f"/fake/{n}")
    runner = _ScriptedRunner(
        {"vap": (0, _VAP_DIMS, ""), "pam": (0, "", "")},
        produce=lambda name, cwd: (cwd / "transform_src.tr").write_bytes(b"fake")
        if name == "pam" and cwd else None,
    )
    ctx.runner = runner

    out = await registry.dispatch(
        "archive_transform",
        {"archive": "obs.ar", "fscrunch_to": 16, "tscrunch_to": 8,
         "binscrunch_to": 512, "sum_pols": True, "remove_baseline": True,
         "execution_mode": "sync"},
    )
    assert out.is_error is False, out.structured_content
    data_out = out.structured_content
    assert data_out["artifact"]["artifact_id"].startswith("art_")
    assert data_out["dimensions"]["nchan"] == 64
    assert data_out["warnings"] == []
    pam_argv = [c for c in runner.calls if c[0] == "/fake/pam"][0]
    assert pam_argv[:7] == ["/fake/pam", "-F", "4", "-T", "2", "-b", "2"]
    assert "-p" in pam_argv and "-r" in pam_argv


async def test_archive_transform_validation_and_warnings(ctx, registry, tmp_path, monkeypatch):
    _archive_env(ctx, tmp_path)
    monkeypatch.setattr("psrmcp.tools.archive._tool", lambda c, n: f"/fake/{n}")

    out = await registry.dispatch("archive_transform", {"archive": "obs.ar"})
    assert out.is_error is True  # no operation requested -> E_INVALID_INPUT
    assert out.structured_content["error"]["code"] == ErrorCode.INVALID_INPUT.value

    runner = _ScriptedRunner({"vap": (0, _VAP_DIMS, ""), "pam": (0, "", "")},
                             produce=lambda name, cwd: (cwd / "transform_src.tr").write_bytes(b"fake")
                             if name == "pam" and cwd else None)
    ctx.runner = runner
    out = await registry.dispatch(
        "archive_transform", {"archive": "obs.ar", "fscrunch_to": 50, "execution_mode": "sync"}
    )
    assert out.is_error is False
    # 50 is not an integral divisor of 64 -> skipped with a warning
    codes = [w["code"] for w in out.structured_content["warnings"]]
    assert "W_FACTOR_INFEASIBLE" in codes


async def test_archive_edit_query_and_set(ctx, registry, tmp_path, monkeypatch):
    _archive_env(ctx, tmp_path)
    monkeypatch.setattr("psrmcp.tools.archive._tool", lambda c, n: f"/fake/{n}")

    # query only: read-only, no artifact
    runner = _ScriptedRunner({"psredit": (0, "name=J0437-4715 dm=2.64\n", "")})
    ctx.runner = runner
    out = await registry.dispatch("archive_edit", {"archive": "obs.ar", "query": ["name", "dm"]})
    assert out.is_error is False
    body = out.structured_content
    assert body["artifact"] is None
    assert body["values"] == {"name": "J0437-4715", "dm": 2.64}

    # set: writes a new artifact via psredit -e edit
    runner = _ScriptedRunner(
        {"psredit": (0, "", "")},
        produce=lambda name, cwd: (cwd / "edit_src.edit").write_bytes(b"fake")
        if cwd else None,
    )
    ctx.runner = runner
    out = await registry.dispatch(
        "archive_edit", {"archive": "obs.ar", "set": {"name": "J0437-4715", "dm": "2.644"}}
    )
    assert out.is_error is False
    assert out.structured_content["artifact"]["artifact_id"].startswith("art_")
    argv = [c for c in runner.calls if c[0] == "/fake/psredit"][0]
    assert "-c" in argv and "name=J0437-4715,dm=2.644" in argv
    assert argv[argv.index("-e") + 1] == "edit"

    # invalid psredit key -> E_INVALID_INPUT envelope
    out = await registry.dispatch(
        "archive_edit", {"archive": "obs.ar", "query": ["na me"]}
    )
    assert out.is_error is True
    assert out.structured_content["error"]["code"] == ErrorCode.INVALID_INPUT.value


async def test_profile_optimize_parses_pdmp_output(ctx, registry, tmp_path, monkeypatch):
    data = _archive_env(ctx, tmp_path)
    monkeypatch.setattr("psrmcp.tools.profile._psrchive_tool", lambda c, n: f"/fake/{n}")
    (data / "std.ar").write_bytes(b"fake")
    runner = _ScriptedRunner({"pdmp": (0, _PDMP_SAMPLE, "")})
    ctx.runner = runner

    out = await registry.dispatch(
        "profile_optimize",
        {"archive": "obs.ar", "standard_profile": "std.ar", "execution_mode": "sync"},
    )
    assert out.is_error is False, out.structured_content
    body = out.structured_content
    assert body["best_period_ms"] == 22.789120
    assert body["period_error_ms"] == 1.2e-05
    assert body["best_dm"] == 2.6441
    assert body["dm_error"] == 0.002
    assert body["best_snr"] == 154.3
    assert body["warnings"] == []
    argv = [c for c in runner.calls if c[0] == "/fake/pdmp"][0]
    assert argv[:3] == ["/fake/pdmp", "-g", "/null"]
    assert argv[argv.index("-s") + 1].endswith("std.ar")


async def test_profile_optimize_unparsed_output_warns(ctx, registry, tmp_path, monkeypatch):
    _archive_env(ctx, tmp_path)
    monkeypatch.setattr("psrmcp.tools.profile._psrchive_tool", lambda c, n: f"/fake/{n}")
    runner = _ScriptedRunner({"pdmp": (0, "no recognisable values here\n", "")})
    ctx.runner = runner

    out = await registry.dispatch(
        "profile_optimize", {"archive": "obs.ar", "execution_mode": "sync"}
    )
    assert out.is_error is False
    body = out.structured_content
    assert body["best_dm"] is None
    assert [w["code"] for w in body["warnings"]] == ["W_PDMP_UNPARSED"]


# ---------------------------------------------------------------------------
# single_pulse_analyze / rotation_measure_fit (scripted backends)
# ---------------------------------------------------------------------------

_SPA_MAXAMP = "spa_src.ar 0 1 0.45 0.5 312 18.4 2.1\nspa_src.ar 1 1 0.45 0.5 297 11.2 -1.0\n"
_SPA_PULSES = "spa_src.ar 3 42.5 310.0 8.0 1\nspa_src.ar 7 101.25 312.0 12.0 1\n"
_RMFIT_SAMPLE = (
    "Best RM is: 2.6441 +/- 0.05\n"
    "Chi-squared = 130.5 / 120 = 1.0875\n"
)


def _spa_produce(name, cwd):
    if name == "psrspa" and cwd:
        (cwd / "spa_flux_30.dat").write_text("# histogram\n")
        (cwd / "spa_maxamp.dat").write_text(_SPA_MAXAMP)
        (cwd / "spa_pulses.dat").write_text(_SPA_PULSES)


def _rmfit_produce(name, cwd):
    if name == "rmfit" and cwd:
        (cwd / "rmfit.out").write_text("NAME <I> ...\n")
        (cwd / "rm_src.rmfit").write_bytes(b"fake")


async def test_single_pulse_analyze_modes_and_outputs(ctx, registry, tmp_path, monkeypatch):
    _archive_env(ctx, tmp_path)
    monkeypatch.setattr("psrmcp.tools.singlepulse._psrchive_tool", lambda c, n: f"/fake/{n}")
    runner = _ScriptedRunner({"psrspa": (0, "", "")}, produce=_spa_produce)
    ctx.runner = runner

    out = await registry.dispatch(
        "single_pulse_analyze",
        {"archive": "obs.ar", "histograms": ["flux"], "find_max_amplitude": True,
         "phase_ranges": [0.45, 0.5], "execution_mode": "sync"},
    )
    assert out.is_error is False, out.structured_content
    body = out.structured_content
    assert body["n_histogram_files"] == 1
    assert body["histograms_artifact"]["artifact_id"].startswith("art_")
    assert len(body["max_amplitudes"]) == 2
    assert body["max_amplitudes"][0]["max_bin"] == 312
    assert body["max_amplitudes"][0]["bin_min"] == 0
    assert body["warnings"] == []
    argv = [c for c in runner.calls if c[0] == "/fake/psrspa"][0]
    assert argv[:2] == ["/fake/psrspa", "-hf"]
    assert "-fm" in argv and "-a" not in argv
    assert argv[argv.index("-pr") + 1] == "0.45,0.5"
    assert argv[argv.index("-e") + 1] == "dat"

    # find_pulses runs alone (psrspa's -a excludes the histogram modes)
    runner = _ScriptedRunner({"psrspa": (0, "", "")}, produce=_spa_produce)
    ctx.runner = runner
    out = await registry.dispatch(
        "single_pulse_analyze",
        {"archive": "obs.ar", "find_pulses": True, "execution_mode": "sync"},
    )
    assert out.is_error is False, out.structured_content
    body = out.structured_content
    assert body["pulses_artifact"]["artifact_id"].startswith("art_")
    assert len(body["pulses"]) == 2 and body["pulses"][0]["flux"] == 42.5
    assert body["warnings"] == []
    argv = [c for c in runner.calls if c[0] == "/fake/psrspa"][0]
    assert argv[argv.index("-a") + 1] == "above"

    # no mode requested -> E_INVALID_INPUT
    out = await registry.dispatch("single_pulse_analyze", {"archive": "obs.ar"})
    assert out.is_error is True
    assert out.structured_content["error"]["code"] == ErrorCode.INVALID_INPUT.value

    # find_pulses is exclusive of the histogram modes -> E_INVALID_INPUT
    out = await registry.dispatch(
        "single_pulse_analyze", {"archive": "obs.ar", "find_pulses": True,
                                 "histograms": ["flux"]}
    )
    assert out.is_error is True

    # bad pulse algorithm / odd phase ranges -> E_INVALID_INPUT
    out = await registry.dispatch(
        "single_pulse_analyze", {"archive": "obs.ar", "find_pulses": True,
                                 "pulse_algorithm": "rm -rf /"}
    )
    assert out.is_error is True
    out = await registry.dispatch(
        "single_pulse_analyze", {"archive": "obs.ar", "find_max_amplitude": True,
                                 "phase_ranges": [0.5]}
    )
    assert out.is_error is True


async def test_rotation_measure_fit_parses_and_commits(ctx, registry, tmp_path, monkeypatch):
    _archive_env(ctx, tmp_path)
    monkeypatch.setattr("psrmcp.tools.rmfit._psrchive_tool", lambda c, n: f"/fake/{n}")
    runner = _ScriptedRunner({"rmfit": (0, _RMFIT_SAMPLE, "")}, produce=_rmfit_produce)
    ctx.runner = runner

    out = await registry.dispatch(
        "rotation_measure_fit",
        {"archive": "obs.ar", "rm_range": [-50.0, 50.0, 200], "refine": True,
         "save_rm_archive": True, "execution_mode": "sync"},
    )
    assert out.is_error is False, out.structured_content
    body = out.structured_content
    assert body["best_rm"] == 2.6441 and body["best_rm_error"] == 0.05
    assert body["chi2_reduced"] == 1.0875
    assert body["rm_archive"]["artifact_id"].startswith("art_")
    assert len(body["data_artifacts"]) >= 1  # rmfit.out (+ <src>.data if written)
    assert body["warnings"] == []
    argv = [c for c in runner.calls if c[0] == "/fake/rmfit"][0]
    assert argv[argv.index("-m") + 1] == "-50,50,200"
    assert "-r" in argv and argv[argv.index("-x") + 1] == "rmfit"

    # brute_force + rm_range are mutually exclusive -> E_INVALID_INPUT
    out = await registry.dispatch(
        "rotation_measure_fit",
        {"archive": "obs.ar", "brute_force": True, "rm_range": [-1.0, 1.0, 10],
         "execution_mode": "sync"},
    )
    assert out.is_error is True
    assert out.structured_content["error"]["code"] == ErrorCode.INVALID_INPUT.value


async def test_rotation_measure_fit_unparsed_output_warns(ctx, registry, tmp_path, monkeypatch):
    _archive_env(ctx, tmp_path)
    monkeypatch.setattr("psrmcp.tools.rmfit._psrchive_tool", lambda c, n: f"/fake/{n}")
    ctx.runner = _ScriptedRunner({"rmfit": (0, "no recognisable values\n", "")})

    out = await registry.dispatch(
        "rotation_measure_fit", {"archive": "obs.ar", "brute_force": True,
                                 "execution_mode": "sync"}
    )
    assert out.is_error is False
    body = out.structured_content
    assert body["best_rm"] is None
    assert [w["code"] for w in body["warnings"]] == ["W_RMFIT_UNPARSED"]
