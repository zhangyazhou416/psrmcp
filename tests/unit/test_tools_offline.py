"""Graceful degradation + error envelopes with no real backends installed."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

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
    materialises expected output files so handlers can find them. Mirrors
    ProcessRunner.history so the registry's reproduction block is exercised."""

    def __init__(self, responses, produce=None):
        self.responses = responses
        self.calls = []
        self.history = []
        self._produce = produce

    async def run(self, argv, *, cwd=None, env=None, timeout_s=None, cancel=None, stdin_bytes=None):
        from pathlib import Path as _P

        from psrmcp.execution.runner import CommandRecord, RunResult

        self.calls.append(list(argv))
        name = _P(argv[0]).name
        exit_code, out, err = self.responses.get(name, (0, "", ""))
        if self._produce and exit_code == 0:
            self._produce(name, cwd)
        self.history.append(CommandRecord(
            argv=list(argv), cwd=str(cwd) if cwd else None,
            exit_code=exit_code, duration_s=0.01, env=dict(env) if env else None))
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
    assert pam_argv[:7] == ["/fake/pam", "-f", "4", "-t", "2", "-b", "2"]
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


# ---------------------------------------------------------------------------
# polarization_calibrate / calibrator_database (scripted backends)
# ---------------------------------------------------------------------------

_PAC_CAL_SAMPLE = (
    "pac: Generating new calibrator database\n"
    "\n"
    "pac: PolnCalibrator constructed from:\n"
    "\thydra.pcal\n"
    "pac: Mean SEFD = 42.5 Jy\n"
    "pac: FluxCalibrator constructed from:\n"
    "\thydra.fcal\n"
    "pac: Poln calibration complete\n"
    "pac: Flux calibration complete\n"
    "pac: Calibrated archive pac_src.calib unloaded\n"
    "\npac: Finished all files\n"
)
_PAC_MODEL_SAMPLE = (
    "pac: Loading calibrator from sol.pcal\n"
    "pac: Applying specified calibrator\n"
    "pac: Poln calibration complete\n"
    "pac: Not performing flux calibration (no database).\n"
    "pac: Calibrated archive pac_src.calib unloaded\n"
    "\npac: Finished all files\n"
)
_PAC_FAIL_SAMPLE = (
    "pac: Error while handling pac_src.ar:\n"
    "\tError::InvalidState\n"
    "\tno CAL info in archive\n"
    "\npac: Finished all files\n"
)
_PACDB_SAMPLE = (
    "Pulsar::Database::path cals\n"
    "Pulsar::Database # of entries = 2\n"
    "hydra.pcal PolnCal 08:54:37.96 -42:16:39.72 58765.123456789012 "
    "32 1380.5 512 cpsr2n H-OH\n"
    "hydra.fcal FluxCal-On 08:54:37.96 -42:16:39.72 58765.5 "
    "32 1380.5 512 cpsr2n H-OH\n"
)


def _pac_produce(name, cwd):
    if name == "pac" and cwd:
        (cwd / "pac_src.calib").write_bytes(b"fake")


async def test_polarization_calibrate_parses_and_commits(ctx, registry, tmp_path, monkeypatch):
    data = _archive_env(ctx, tmp_path)
    (data / "hydra.pcal").write_bytes(b"fake")
    (data / "hydra.fcal").write_bytes(b"fake")
    monkeypatch.setattr("psrmcp.tools.calibration._psrchive_tool", lambda c, n: f"/fake/{n}")
    runner = _ScriptedRunner({"pac": (0, _PAC_CAL_SAMPLE, "")}, produce=_pac_produce)
    ctx.runner = runner

    out = await registry.dispatch(
        "polarization_calibrate",
        {"archive": "obs.ar", "calibrators": ["hydra.pcal", "hydra.fcal"],
         "execution_mode": "sync"},
    )
    assert out.is_error is False, out.structured_content
    body = out.structured_content
    assert body["calibrated_archive"]["artifact_id"].startswith("art_")
    assert body["poln_calibrated"] is True and body["flux_calibrated"] is True
    assert body["poln_calibrator_source"] == "hydra.pcal"
    assert body["mean_sefd_jy"] == 42.5
    assert body["warnings"] == []
    argv = [c for c in runner.calls if c[0] == "/fake/pac"][0]
    assert argv[argv.index("-p") + 1].endswith("cals")
    # every supplied extension is added to pac's scan set via -u
    assert argv[argv.index("-u") + 1] == "pcal fcal"
    assert "-T" not in argv and "-Z" not in argv  # defaults keep pac's criteria

    # exactly one calibration source is required -> E_INVALID_INPUT
    out = await registry.dispatch(
        "polarization_calibrate",
        {"archive": "obs.ar", "calibrators": ["hydra.pcal"], "model": "sol.pcal",
         "execution_mode": "sync"},
    )
    assert out.is_error is True
    assert out.structured_content["error"]["code"] == ErrorCode.INVALID_INPUT.value
    out = await registry.dispatch(
        "polarization_calibrate", {"archive": "obs.ar", "execution_mode": "sync"}
    )
    assert out.is_error is True


async def test_polarization_calibrate_model_mode_warns_no_fluxdb(ctx, registry, tmp_path, monkeypatch):
    data = _archive_env(ctx, tmp_path)
    (data / "sol.pcal").write_bytes(b"fake")
    monkeypatch.setattr("psrmcp.tools.calibration._psrchive_tool", lambda c, n: f"/fake/{n}")
    runner = _ScriptedRunner({"pac": (0, _PAC_MODEL_SAMPLE, "")}, produce=_pac_produce)
    ctx.runner = runner

    out = await registry.dispatch(
        "polarization_calibrate",
        {"archive": "obs.ar", "model": "sol.pcal", "execution_mode": "sync"},
    )
    assert out.is_error is False, out.structured_content
    body = out.structured_content
    assert body["poln_calibrated"] is True and body["flux_calibrated"] is False
    assert [w["code"] for w in body["warnings"]] == ["W_PAC_NO_FLUXDB"]
    argv = [c for c in runner.calls if c[0] == "/fake/pac"][0]
    assert argv[argv.index("-A") + 1].endswith("model_sol.pcal")
    assert "-p" not in argv


async def test_polarization_calibrate_silent_failure_envelope(ctx, registry, tmp_path, monkeypatch):
    """pac catches per-file errors internally and still exits 0 — no output
    file must surface as E_PROCESS_FAILED with the parsed error text."""
    data = _archive_env(ctx, tmp_path)
    (data / "hydra.pcal").write_bytes(b"fake")
    monkeypatch.setattr("psrmcp.tools.calibration._psrchive_tool", lambda c, n: f"/fake/{n}")
    ctx.runner = _ScriptedRunner({"pac": (0, _PAC_FAIL_SAMPLE, "")})

    out = await registry.dispatch(
        "polarization_calibrate",
        {"archive": "obs.ar", "calibrators": ["hydra.pcal"], "execution_mode": "sync"},
    )
    assert out.is_error is True
    err = out.structured_content["error"]
    assert err["code"] == ErrorCode.PROCESS_FAILED.value
    assert "no CAL info" in err["message"]
    assert err["log_artifact_id"]


async def test_calibrator_database_builds_and_parses(ctx, registry, tmp_path, monkeypatch):
    data = _archive_env(ctx, tmp_path)
    (data / "hydra.pcal").write_bytes(b"fake")
    (data / "calfile").write_bytes(b"fake")  # no extension -> rejected up front
    monkeypatch.setattr("psrmcp.tools.calibration._psrchive_tool", lambda c, n: f"/fake/{n}")

    def _pacdb_produce(name, cwd):
        if name == "pac" and cwd:
            (cwd / "database.txt").write_text(_PACDB_SAMPLE)

    runner = _ScriptedRunner({"pac": (0, "pac: Writing database summary file\n", "")},
                             produce=_pacdb_produce)
    ctx.runner = runner

    out = await registry.dispatch(
        "calibrator_database",
        {"calibrators": ["hydra.pcal"], "execution_mode": "sync"},
    )
    assert out.is_error is False, out.structured_content
    body = out.structured_content
    assert body["database_artifact"]["artifact_id"].startswith("art_")
    assert body["n_calibrators"] == 2
    assert body["entry_types"] == {"FluxCal-On": 1, "PolnCal": 1}
    assert body["entries"][0]["nchan"] == 512
    assert body["warnings"] == []
    argv = [c for c in runner.calls if c[0] == "/fake/pac"][0]
    assert argv[argv.index("-w") + 0] == "-w"
    assert argv[argv.index("-u") + 1] == "pcal"

    # a calibrator without an extension is rejected before pac runs
    out = await registry.dispatch(
        "calibrator_database", {"calibrators": ["calfile"], "execution_mode": "sync"}
    )
    assert out.is_error is True
    assert out.structured_content["error"]["code"] == ErrorCode.INVALID_INPUT.value

    # pac's "no calibrators found" exits non-zero -> E_PROCESS_FAILED
    ctx.runner = _ScriptedRunner({"pac": (-1, "", "pac: No calibrators found in cals\n")})
    out = await registry.dispatch(
        "calibrator_database", {"calibrators": ["hydra.pcal"], "execution_mode": "sync"}
    )
    assert out.is_error is True
    assert out.structured_content["error"]["code"] == ErrorCode.PROCESS_FAILED.value
    assert "No calibrators found" in out.structured_content["error"]["message"]


# --------------------------------------------------------------- batch 4

_PSRSPLIT_ERR_TOO_MANY = (
    "psrsplit: error while processing split_src.ar:\n"
    "Error::stack\nError::InvalidState\n"
    "\tMore sub-integrations per output file requested than available in split_src.ar\n"
)


def _split_produce(name, cwd):
    if name == "psrsplit" and cwd:
        (cwd / "split_src.0000.ar").write_bytes(b"fake")
        (cwd / "split_src.0001.ar").write_bytes(b"fake")


async def test_archive_split_chunks_and_commits(ctx, registry, tmp_path, monkeypatch):
    data = _archive_env(ctx, tmp_path)
    monkeypatch.setattr("psrmcp.tools.archive._tool", lambda c, n: f"/fake/{n}")
    runner = _ScriptedRunner({"psrsplit": (0, "", "psrsplit: writing ...")}, produce=_split_produce)
    ctx.runner = runner

    out = await registry.dispatch(
        "archive_split", {"archive": "obs.ar", "per_subint": 4, "execution_mode": "sync"}
    )
    assert out.is_error is False, out.structured_content
    body = out.structured_content
    assert body["n_files"] == 2
    assert all(a["artifact_id"].startswith("art_") for a in body["archives"])
    assert body["per_subint"] == 4 and body["per_channel"] is None
    argv = [c for c in runner.calls if c[0] == "/fake/psrsplit"][0]
    assert argv == ["/fake/psrsplit", "-n", "4", argv[-1]] and argv[-1].endswith("split_src.ar")

    # channel split and strip_extensions flag mapping
    out = await registry.dispatch(
        "archive_split",
        {"archive": "obs.ar", "per_channel": 32, "strip_extensions": True,
         "execution_mode": "sync"},
    )
    assert out.is_error is False
    argv = [c for c in runner.calls if c[0] == "/fake/psrsplit"][-1]
    assert argv == ["/fake/psrsplit", "-c", "32", "-e", argv[-1]]

    # no chunking dimension given -> E_INVALID_INPUT
    out = await registry.dispatch("archive_split", {"archive": "obs.ar", "execution_mode": "sync"})
    assert out.is_error is True
    assert out.structured_content["error"]["code"] == ErrorCode.INVALID_INPUT.value


async def test_archive_split_silent_failure_envelope(ctx, registry, tmp_path, monkeypatch):
    """psrsplit exits 0 even when -n exceeds the sub-int count; the missing
    output files must surface, with the 'More sub-integrations' case mapped
    to E_INVALID_INPUT."""
    data = _archive_env(ctx, tmp_path)
    monkeypatch.setattr("psrmcp.tools.archive._tool", lambda c, n: f"/fake/{n}")
    ctx.runner = _ScriptedRunner({"psrsplit": (0, "", _PSRSPLIT_ERR_TOO_MANY)})

    out = await registry.dispatch(
        "archive_split", {"archive": "obs.ar", "per_subint": 5, "execution_mode": "sync"}
    )
    assert out.is_error is True
    err = out.structured_content["error"]
    assert err["code"] == ErrorCode.INVALID_INPUT.value
    assert "sub-integrations" in err["message"]


async def test_archive_compare_parses_chisq(ctx, registry, tmp_path, monkeypatch):
    data = _archive_env(ctx, tmp_path)
    (data / "ref.ar").write_bytes(b"fake")
    monkeypatch.setattr("psrmcp.tools.archive._tool", lambda c, n: f"/fake/{n}")
    runner = _ScriptedRunner({"psrdiff": (0, "Weighted reduced chisq: 0.000452555\n", "")})
    ctx.runner = runner

    out = await registry.dispatch(
        "archive_compare", {"archive": "obs.ar", "reference": "ref.ar", "execution_mode": "sync"}
    )
    assert out.is_error is False, out.structured_content
    body = out.structured_content
    assert body["reduced_chisq"] == pytest.approx(0.000452555)
    assert body["weighted"] is True and body["per_polarization"] == {}
    argv = [c for c in runner.calls if c[0] == "/fake/psrdiff"][0]
    assert argv[0:2] == ["/fake/psrdiff", "-W"]
    assert argv[-2].endswith("cmp_ref.ar") and argv[-1].endswith("cmp_data.ar")

    # per-polarization mode
    runner.responses["psrdiff"] = (0, "0 0.000456705\n1 0.000448405\n", "")
    out = await registry.dispatch(
        "archive_compare",
        {"archive": "obs.ar", "reference": "ref.ar", "per_polarization": True,
         "execution_mode": "sync"},
    )
    assert out.is_error is False
    body = out.structured_content
    assert body["per_polarization"]["0"] == pytest.approx(0.000456705)
    assert body["per_polarization"]["1"] == pytest.approx(0.000448405)

    # unweighted mode uses -X
    runner.responses["psrdiff"] = (0, "Reduced chisq: 46686.2\n", "")
    out = await registry.dispatch(
        "archive_compare",
        {"archive": "obs.ar", "reference": "ref.ar", "weighted": False, "execution_mode": "sync"},
    )
    assert out.is_error is False
    assert out.structured_content["reduced_chisq"] == pytest.approx(46686.2)
    argv = [c for c in runner.calls if c[0] == "/fake/psrdiff"][-1]
    assert "-X" in argv


async def test_archive_compare_mismatch_envelope(ctx, registry, tmp_path, monkeypatch):
    """psrdiff prints 'files differ as follows:' + reasons and exits 0 when
    the archives are not mixable — mapped to E_INVALID_INPUT with the reason."""
    data = _archive_env(ctx, tmp_path)
    (data / "ref.ar").write_bytes(b"fake")
    monkeypatch.setattr("psrmcp.tools.archive._tool", lambda c, n: f"/fake/{n}")
    ctx.runner = _ScriptedRunner({"psrdiff": (0,
        "files differ as follows:\n"
        "\tnumbers of channels mismatch: 32 != 128\n", "")})

    out = await registry.dispatch(
        "archive_compare", {"archive": "obs.ar", "reference": "ref.ar", "execution_mode": "sync"}
    )
    assert out.is_error is True
    err = out.structured_content["error"]
    assert err["code"] == ErrorCode.INVALID_INPUT.value
    assert "channels mismatch" in err["message"]


async def test_profile_smooth_commits(ctx, registry, tmp_path, monkeypatch):
    data = _archive_env(ctx, tmp_path)
    monkeypatch.setattr("psrmcp.tools.profile._psrchive_tool", lambda c, n: f"/fake/{n}")

    def _smooth_produce(name, cwd):
        if name == "psrsmooth" and cwd:
            (cwd / "smooth_src.ar.sm").write_bytes(b"fake")

    runner = _ScriptedRunner({"psrsmooth": (0, "", "")}, produce=_smooth_produce)
    ctx.runner = runner

    out = await registry.dispatch(
        "profile_smooth",
        {"archive": "obs.ar", "method": "wavelet", "wavelet_type": "UD8",
         "wavelet_cutoff": 2.0, "normalize": True, "execution_mode": "sync"},
    )
    assert out.is_error is False, out.structured_content
    body = out.structured_content
    assert body["smoothed_archive"]["artifact_id"].startswith("art_")
    assert body["method"] == "wavelet"
    argv = [c for c in runner.calls if c[0] == "/fake/psrsmooth"][0]
    assert argv == ["/fake/psrsmooth", "-W", "-t", "UD8", "-c", "2.0", "-n", argv[-1]]

    # sinc default emits no smoothing flags
    out = await registry.dispatch(
        "profile_smooth", {"archive": "obs.ar", "execution_mode": "sync"}
    )
    assert out.is_error is False
    argv = [c for c in runner.calls if c[0] == "/fake/psrsmooth"][-1]
    assert "-W" not in argv and "-n" not in argv


async def test_profile_smooth_silent_failure_envelope(ctx, registry, tmp_path, monkeypatch):
    data = _archive_env(ctx, tmp_path)
    monkeypatch.setattr("psrmcp.tools.profile._psrchive_tool", lambda c, n: f"/fake/{n}")
    ctx.runner = _ScriptedRunner({"psrsmooth": (0, "", "")})  # no output file

    out = await registry.dispatch(
        "profile_smooth", {"archive": "obs.ar", "execution_mode": "sync"}
    )
    assert out.is_error is True
    err = out.structured_content["error"]
    assert err["code"] == ErrorCode.PROCESS_FAILED.value
    assert err["log_artifact_id"]


_RVM_SAMPLE = (
    "psrmodel: solving with initial guess: \n"
    "psi_0  [fit] = 0 deg\n"
    "beta   [fit] = 5.5 deg\n"
    "alpha  [fit] = 45 deg\n"
    "\nchisq=312.345/nfree=4 = 78.0863\n"
    "psi_0=-12.5 deg\n"
    "phi_0=140.2 deg\n"
    "beta =6.75 deg\n"
    "alpha=50.25 deg\n"
)


async def test_rvm_fit_parses_and_commits_residuals(ctx, registry, tmp_path, monkeypatch):
    data = _archive_env(ctx, tmp_path)
    monkeypatch.setattr("psrmcp.tools.rmfit._psrchive_tool", lambda c, n: f"/fake/{n}")

    def _rvm_produce(name, cwd):
        if name == "psrmodel" and cwd:
            (cwd / "psrmodel_psi_residuals.txt").write_text("0.1 2.0 0.3\n")

    runner = _ScriptedRunner({"psrmodel": (0, "", _RVM_SAMPLE)}, produce=_rvm_produce)
    ctx.runner = runner

    out = await registry.dispatch(
        "rvm_fit",
        {"archive": "obs.ar", "residuals": "psi", "use_beta": True,
         "exclude": ["40:50"], "execution_mode": "sync"},
    )
    assert out.is_error is False, out.structured_content
    body = out.structured_content
    assert body["chisq"] == pytest.approx(312.345)
    assert body["nfree"] == 4 and body["reduced_chisq"] == pytest.approx(78.0863)
    assert body["psi0_deg"] == pytest.approx(-12.5)
    assert body["beta_deg"] == pytest.approx(6.75)
    assert body["alpha_deg"] == pytest.approx(50.25)
    assert body["zeta_deg"] is None
    assert body["residuals_artifact"]["artifact_id"].startswith("art_")
    argv = [c for c in runner.calls if c[0] == "/fake/psrmodel"][0]
    assert argv[argv.index("--exclude") + 1] == "40:50"
    assert "--use_beta" in argv


async def test_rvm_fit_poln_incomplete_envelope(ctx, registry, tmp_path, monkeypatch):
    """npol<4 archives make psrmodel exit 0 with only an InvalidState note on
    stderr — mapped to E_INVALID_INPUT so the client can fix the input."""
    data = _archive_env(ctx, tmp_path)
    monkeypatch.setattr("psrmcp.tools.rmfit._psrchive_tool", lambda c, n: f"/fake/{n}")
    ctx.runner = _ScriptedRunner({"psrmodel": (0, "",
        "Pulsar::Integration::new_PolnProfile\nError::InvalidState\n"
        "\tincomplete polarization information\n")})

    out = await registry.dispatch(
        "rvm_fit", {"archive": "obs.ar", "execution_mode": "sync"}
    )
    assert out.is_error is True
    err = out.structured_content["error"]
    assert err["code"] == ErrorCode.INVALID_INPUT.value
    assert "polarization" in err["message"]

    # bad range syntax rejected by the input model
    out = await registry.dispatch(
        "rvm_fit", {"archive": "obs.ar", "exclude": ["40-50"], "execution_mode": "sync"}
    )
    assert out.is_error is True
    assert out.structured_content["error"]["code"] == ErrorCode.INVALID_INPUT.value


# ------------------------------------------------------- artifact_delete

async def test_artifact_delete_confirm_and_trash(ctx, registry):
    from datetime import datetime, timezone

    from psrmcp.models.common import ArtifactKind, Provenance

    prov = Provenance(tool="t", tool_version="0", backend="b", params={},
                      started_at=datetime.now(timezone.utc), duration_s=0.0)
    parent = ctx.store.commit_text(text="p", filename="p.ar", kind=ArtifactKind.ARCHIVE,
                                   format_name="PSRARCHIVE", provenance=prov)
    child = ctx.store.commit_text(text="c", filename="c.ar", kind=ArtifactKind.ARCHIVE,
                                  format_name="PSRARCHIVE", provenance=prov,
                                  parents=[parent.artifact_id])

    # refusing to run without confirm
    out = await registry.dispatch("artifact_delete", {"artifact_id": parent.artifact_id})
    assert out.is_error is True
    err = out.structured_content["error"]
    assert err["code"] == ErrorCode.INVALID_INPUT.value
    assert "confirm" in err["hint"]

    # a parent with live children is refused
    out = await registry.dispatch("artifact_delete",
                                  {"artifact_id": parent.artifact_id, "confirm": True})
    assert out.is_error is True
    err = out.structured_content["error"]
    assert err["code"] == ErrorCode.OUTPUT_CONFLICT.value
    assert child.artifact_id in err["message"]

    # delete the descendant first, then the parent lands in the trash
    out = await registry.dispatch("artifact_delete",
                                  {"artifact_id": child.artifact_id, "confirm": True})
    assert out.is_error is False, out.structured_content
    assert out.structured_content["trashed"] is True
    assert (ctx.store.trash_dir / child.artifact_id).exists()

    out = await registry.dispatch("artifact_delete",
                                  {"artifact_id": parent.artifact_id, "confirm": True})
    assert out.is_error is False
    assert out.structured_content["artifact_id"] == parent.artifact_id
    assert (ctx.store.trash_dir / parent.artifact_id).exists()

    # gone from the index and NOT_FOUND on repeat (not idempotent)
    out = await registry.dispatch("artifact_list", {})
    assert out.is_error is False
    ids = {a["artifact_id"] for a in out.structured_content["artifacts"]}
    assert child.artifact_id not in ids and parent.artifact_id not in ids
    out = await registry.dispatch("artifact_delete",
                                  {"artifact_id": child.artifact_id, "confirm": True})
    assert out.is_error is True
    assert out.structured_content["error"]["code"] == ErrorCode.NOT_FOUND.value


# -------------------------------------------------------- pipeline_reduce

def _fake_artifact(ctx, name):
    from datetime import datetime, timezone

    from psrmcp.models.common import ArtifactKind, Provenance

    prov = Provenance(tool="pipeline_test", tool_version="0", backend="fake", params={},
                      started_at=datetime.now(timezone.utc), duration_s=0.0)
    return ctx.store.commit_text(text="x", filename=name, kind=ArtifactKind.ARCHIVE,
                                 format_name="PSRARCHIVE", provenance=prov)


def _install_fake_steps(monkeypatch, ctx, *, zap_fails=False, measure_fails=False):
    from types import SimpleNamespace

    from psrmcp.models.results import Dimensions, MeasureMetrics, SuggestedMask

    folded = _fake_artifact(ctx, "folded.ar")
    zapped = _fake_artifact(ctx, "zapped.ar")
    standard = _fake_artifact(ctx, "standard.ar")

    async def fake_inspect(ctx, inp, progress):
        return SimpleNamespace(dimensions=Dimensions(nsubint=1, nchan=128, npol=2, nbin=1024))

    async def fake_fold(ctx, inp, progress):
        return SimpleNamespace(artifact=folded, dm_used=26.76, warnings=[])

    async def fake_scan(ctx, inp, progress):
        return SimpleNamespace(
            suggested_mask=SuggestedMask(channels=[3, 7], n_channels=2, fraction=2 / 128),
            statistics={}, plot_artifact="art_plotfake")

    async def fake_zap(ctx, inp, progress):
        if zap_fails:
            from psrmcp.errors import PsrmcpError, ErrorCode
            raise PsrmcpError(ErrorCode.PROCESS_FAILED, "paz exploded")
        return SimpleNamespace(artifact=zapped, n_channels_zapped=2, zapped_fraction=2 / 128,
                               warnings=[])

    async def fake_standard(ctx, inp, progress):
        return SimpleNamespace(artifact=standard, nbin=1024, snr=50.0, warnings=[])

    async def fake_measure(ctx, inp, progress):
        if measure_fails:
            raise RuntimeError("psrstat lost")
        return SimpleNamespace(metrics=MeasureMetrics(snr=48.2, width=None, flux_mjy=None),
                               plot_artifact="art_measplot", warnings=[])

    monkeypatch.setattr("psrmcp.tools.pipeline._inspect_step", fake_inspect)
    monkeypatch.setattr("psrmcp.tools.pipeline._fold_step", fake_fold)
    monkeypatch.setattr("psrmcp.tools.pipeline._rfi_scan_step", fake_scan)
    monkeypatch.setattr("psrmcp.tools.pipeline._rfi_zap_step", fake_zap)
    monkeypatch.setattr("psrmcp.tools.pipeline._standardize_step", fake_standard)
    monkeypatch.setattr("psrmcp.tools.pipeline._measure_step", fake_measure)
    return {"folded": folded, "zapped": zapped, "standard": standard}


async def test_pipeline_reduce_full_chain(ctx, registry, tmp_path, monkeypatch):
    _archive_env(ctx, tmp_path)
    refs = _install_fake_steps(monkeypatch, ctx)

    out = await registry.dispatch("pipeline_reduce", {
        "archive": "obs.ar", "pulsar_name": "J0332+5454", "execution_mode": "sync"})
    assert out.is_error is False, out.structured_content
    body = out.structured_content
    assert body["succeeded"] is True
    assert body["final_archive"]["artifact_id"] == refs["zapped"].artifact_id
    assert body["standard_profile"]["artifact_id"] == refs["standard"].artifact_id
    assert body["rfi_report_artifact"] == "art_plotfake"
    assert body["measure_metrics"]["snr"] == 48.2
    assert body["measure_plot_artifact"] == "art_measplot"
    statuses = [(s["step"], s["status"]) for s in body["steps"]]
    assert statuses == [
        ("inspect", "ok"), ("fold", "ok"), ("rfi_scan", "ok"),
        ("rfi_zap", "ok"), ("standard", "ok"), ("measure", "ok"),
    ]
    assert body["warnings"] == []


async def test_pipeline_reduce_mid_chain_failure_keeps_steps(ctx, registry, tmp_path, monkeypatch):
    """A failed step stops the chain but keeps completed artifacts; the
    remaining steps are reported as skipped (composite-tool rule)."""
    _archive_env(ctx, tmp_path)
    _install_fake_steps(monkeypatch, ctx, zap_fails=True)

    out = await registry.dispatch("pipeline_reduce", {
        "archive": "obs.ar", "pulsar_name": "J0332+5454", "execution_mode": "sync"})
    assert out.is_error is False, out.structured_content
    body = out.structured_content
    assert body["succeeded"] is False
    statuses = {s["step"]: s["status"] for s in body["steps"]}
    assert statuses == {
        "inspect": "ok", "fold": "ok", "rfi_scan": "ok",
        "rfi_zap": "failed", "standard": "skipped", "measure": "skipped",
    }
    codes = [w["code"] for w in body["warnings"]]
    assert "W_PIPELINE_STEP_FAILED" in codes
    assert "paz exploded" in [w["message"] for w in body["warnings"]][0]


async def test_pipeline_reduce_minimal_plan_and_dm_source(ctx, registry, tmp_path, monkeypatch):
    _archive_env(ctx, tmp_path)
    _install_fake_steps(monkeypatch, ctx)

    # fold=false + no DM source is fine (fold skipped, nothing needed)
    out = await registry.dispatch("pipeline_reduce", {
        "archive": "obs.ar", "fold": False, "rfi_strategy": "none",
        "build_standard": False, "measure": False, "execution_mode": "sync"})
    assert out.is_error is False, out.structured_content
    body = out.structured_content
    assert body["succeeded"] is True
    assert {s["step"]: s["status"] for s in body["steps"]} == {
        "inspect": "ok", "fold": "skipped", "rfi_scan": "skipped",
        "rfi_zap": "skipped", "standard": "skipped", "measure": "skipped",
    }
    assert body["final_archive"] is None

    # fold=true without any DM source is rejected up front
    out = await registry.dispatch("pipeline_reduce", {"archive": "obs.ar", "execution_mode": "sync"})
    assert out.is_error is True
    assert out.structured_content["error"]["code"] == ErrorCode.INVALID_INPUT.value


async def test_dedisperse_dm_only_validation(ctx, registry, tmp_path, monkeypatch):
    """ephemeris became optional: folding needs ephemeris or dm_override."""
    _archive_env(ctx, tmp_path)
    monkeypatch.setattr("psrmcp.tools.archive._tool", lambda c, n: f"/fake/{n}")
    out = await registry.dispatch("dedisperse", {"input": "obs.ar", "execution_mode": "sync"})
    assert out.is_error is True
    assert out.structured_content["error"]["code"] == ErrorCode.INVALID_INPUT.value


async def test_rfi_scan_snr_mad_flagging(ctx, registry, tmp_path, monkeypatch):
    """The backfilled _flag_stats runs psrstat's chan loop and flags S/N
    outliers via MAD; missing/zero-S/N channels are always flagged."""
    data = _archive_env(ctx, tmp_path)
    monkeypatch.setattr("psrmcp.tools.archive._tool", lambda c, n: f"/fake/{n}")
    monkeypatch.setattr("psrmcp.tools.rfi._psrchive_tool", lambda c, n: f"/fake/{n}")

    nchan = 64
    lines = []
    for ch in range(nchan):
        lines.append(f"obs.ar chan={ch} 10.0")
    lines[1] = "obs.ar chan=1 90.0"     # RFI outlier
    lines[2] = "obs.ar chan=2 -"        # unresolvable
    lines[3] = "obs.ar chan=3 0.0"      # dead
    runner = _ScriptedRunner({"vap": (0, _VAP_DIMS, ""), "psrstat": (0, "\n".join(lines) + "\n", "")})
    ctx.runner = runner

    out = await registry.dispatch("rfi_scan", {"archive": "obs.ar", "generate_plot": False})
    assert out.is_error is False, out.structured_content
    body = out.structured_content
    mask = body["suggested_mask"]
    assert mask["channels"][0] == 1          # the outlier
    assert 2 in mask["channels"] and 3 in mask["channels"]
    assert mask["n_channels"] >= 3
    assert body["statistics"]["method"] == "snr_mad"
    assert body["statistics"]["median_snr"] == pytest.approx(10.0)
    argv = [c for c in runner.calls if c[0] == "/fake/psrstat"][0]
    assert "-l" in argv and "chan=0-63" in argv and "-c" in argv and "snr" in argv

    # bandpass method: refused with an explicit hint on this build
    out = await registry.dispatch(
        "rfi_scan", {"archive": "obs.ar", "method": "bandpass", "generate_plot": False})
    assert out.is_error is True
    assert "bandpass" in out.structured_content["error"]["message"]


async def test_toa_include_snr_flag_and_filter(ctx, registry, tmp_path, monkeypatch):
    """include_snr adds pat -C snr; the parser extracts the -snr flag and
    min_snr filters on the measured S/N."""
    data = _archive_env(ctx, tmp_path)
    (data / "std.ar").write_bytes(b"fake")
    monkeypatch.setattr("psrmcp.tools.timing._psrchive_tool", lambda c, n: f"/fake/{n}")

    toa_line = (" 0001.000 0001.000  56000.123456789  0.00012 1400.000  "
                " -snr 33.5 -x 0.02")
    runner = _ScriptedRunner({"pat": (0, "FORMAT 1\n" + toa_line + "\n", "")})
    ctx.runner = runner

    out = await registry.dispatch("toa_extract", {
        "archive": "obs.ar", "standard_profile": "std.ar",
        "include_snr": True, "min_snr": 30.0, "execution_mode": "sync"})
    assert out.is_error is False, out.structured_content
    body = out.structured_content
    assert body["n_toas"] == 1
    assert body["snr"]["min"] == pytest.approx(33.5)
    argv = [c for c in runner.calls if c[0] == "/fake/pat"][0]
    assert argv[argv.index("-C") + 1] == "snr"

    # min_snr above the measured value drops the TOA
    out = await registry.dispatch("toa_extract", {
        "archive": "obs.ar", "standard_profile": "std.ar",
        "include_snr": True, "min_snr": 40.0, "execution_mode": "sync"})
    assert out.is_error is False
    assert out.structured_content["n_toas"] == 0


async def test_profile_measure_psrplot_png(ctx, registry, tmp_path, monkeypatch):
    """profile_measure renders its plot via psrplot's headless PNG device."""
    data = _archive_env(ctx, tmp_path)
    monkeypatch.setattr("psrmcp.tools.profile._psrchive_tool", lambda c, n: f"/fake/{n}")

    def _png_produce(name, cwd):
        if name == "psrplot" and cwd:
            (cwd / "profile.png").write_bytes(b"\x89PNG fake")

    runner = _ScriptedRunner({"psrstat": (0, "obs.ar snr=42.5 w50=5.5", ""),
                              "psrplot": (0, "", "")}, produce=_png_produce)
    ctx.runner = runner

    out = await registry.dispatch("profile_measure", {
        "archive": "obs.ar", "metrics": ["snr", "width"], "generate_plot": True})
    assert out.is_error is False, out.structured_content
    body = out.structured_content
    assert body["plot_artifact"].startswith("art_")
    argv = [c for c in runner.calls if c[0] == "/fake/psrplot"][0]
    assert argv[argv.index("-p") + 1] == "flux"
    assert argv[argv.index("-D") + 1].endswith("/PNG")

    # psrplot failing degrades to a warning, not an error
    runner.responses["psrplot"] = (1, "", "boom")
    out = await registry.dispatch("profile_measure", {
        "archive": "obs.ar", "metrics": ["snr"], "generate_plot": True})
    assert out.is_error is False
    codes = [w["code"] for w in out.structured_content["warnings"]]
    assert "W_PLOT_UNAVAILABLE" in codes


async def test_archive_inspect_integrations_rows(ctx, registry, tmp_path, monkeypatch):
    """include_integrations now returns real per-subint rows via psrstat's
    subint loop."""
    data = _archive_env(ctx, tmp_path)
    monkeypatch.setattr("psrmcp.tools.archive._tool", lambda c, n: f"/fake/{n}")
    sub_rows = "".join(
        f"obs.ar subint={i} {10.0 + i} 1024 128 1412\n" for i in range(16)
    )
    runner = _ScriptedRunner({"vap": (0, _VAP_DIMS, ""), "psrstat": (0, sub_rows, "")})
    ctx.runner = runner

    out = await registry.dispatch("archive_inspect", {
        "archive": "obs.ar", "include_integrations": True})
    assert out.is_error is False, out.structured_content
    body = out.structured_content
    rows = body["integrations"]
    assert rows and rows[0]["index"] == 0 and rows[0]["snr"] == pytest.approx(10.0)
    assert rows[-1]["index"] == 15 and rows[-1]["freq_mhz"] == 1412
    argv = [c for c in runner.calls if c[0] == "/fake/psrstat"][0]
    assert "subint=0-15" in argv


async def test_profile_stability_emits_expected_profiles(ctx, registry, tmp_path, monkeypatch):
    data = _archive_env(ctx, tmp_path)
    for i in range(4):
        (data / f"c{i}.ar").write_bytes(b"fake")
    monkeypatch.setattr("psrmcp.tools.profile._psrchive_tool", lambda c, n: f"/fake/{n}")

    def _stab_produce(name, cwd):
        if name == "psrstability" and cwd:
            out = Path(cwd) / "out"
            out.mkdir(exist_ok=True)
            (out / "profile_0.ar").write_bytes(b"fake")
            (out / "profile_1.ar").write_bytes(b"fake")

    runner = _ScriptedRunner({"psrstability": (0, "", "")}, produce=_stab_produce)
    ctx.runner = runner

    out = await registry.dispatch("profile_stability", {
        "archives": ["c0.ar", "c1.ar", "c2.ar", "c3.ar"], "turns": 2,
        "execution_mode": "sync"})
    assert out.is_error is False, out.structured_content
    body = out.structured_content
    assert body["n_profiles"] == 2
    assert all(a["artifact_id"].startswith("art_") for a in body["profiles"])
    argv = [c for c in runner.calls if c[0] == "/fake/psrstability"][0]
    assert argv[:3] == ["/fake/psrstability", "-t", "2"]
    assert "-O" in argv and sum("chunk" in a for a in argv) == 4

    # turns >= n_archives -> no outputs -> E_PROCESS_FAILED
    ctx.runner = _ScriptedRunner({"psrstability": (0, "", "")})  # produce nothing
    out = await registry.dispatch("profile_stability", {
        "archives": ["c0.ar", "c1.ar"], "turns": 5, "execution_mode": "sync"})
    assert out.is_error is True
    assert out.structured_content["error"]["code"] == ErrorCode.PROCESS_FAILED.value


async def test_profile_pca_commits_products(ctx, registry, tmp_path, monkeypatch):
    data = _archive_env(ctx, tmp_path)
    (data / "std.ar").write_bytes(b"fake")
    monkeypatch.setattr("psrmcp.tools.profile._psrchive_tool", lambda c, n: f"/fake/{n}")

    def _pca_produce(name, cwd):
        if name == "psrpca" and cwd:
            (Path(cwd) / "pca_diffs.ar").write_bytes(b"fake")
            (Path(cwd) / "pca_evecs.ar").write_bytes(b"fake")
            (Path(cwd) / "pca_evals.dat").write_text("1.0\n0.5\n")

    runner = _ScriptedRunner({"psrpca": (0, "", "")}, produce=_pca_produce)
    ctx.runner = runner

    out = await registry.dispatch("profile_pca", {
        "archive": "obs.ar", "standard_profile": "std.ar", "execution_mode": "sync"})
    assert out.is_error is False, out.structured_content
    body = out.structured_content
    assert body["diffs_archive"]["artifact_id"].startswith("art_")
    assert body["evecs_archive"]["artifact_id"].startswith("art_")
    assert body["eigenvalues_artifact"].startswith("art_")
    argv = [c for c in runner.calls if c[0] == "/fake/psrpca"][0]
    assert argv[argv.index("-s") + 1].endswith("pca_std.ar")
    assert "-p" in argv and argv[argv.index("-p") + 1] == "pca"


async def test_dynamic_spectra_commits_table(ctx, registry, tmp_path, monkeypatch):
    data = _archive_env(ctx, tmp_path)
    (data / "std.ar").write_bytes(b"fake")
    monkeypatch.setattr("psrmcp.tools.singlepulse._psrchive_tool", lambda c, n: f"/fake/{n}")

    def _ds_produce(name, cwd):
        if name == "psrflux" and cwd:
            (Path(cwd) / "ds_src.ar.ds").write_text(
                "# Dynamic spectrum computed by psrflux\n"
                "# MJD0: 60190.5\n# Flux method: StandardFlux\n"
                "   0    0   0.24 1475.5 +2.6e2 +1.7e0\n"
                "   1    0   0.48 1475.5 +2.5e2 +1.8e0\n")

    runner = _ScriptedRunner({"psrflux": (0, "", "")}, produce=_ds_produce)
    ctx.runner = runner

    out = await registry.dispatch("dynamic_spectra", {
        "archive": "obs.ar", "standard_profile": "std.ar",
        "align": "per_profile", "execution_mode": "sync"})
    assert out.is_error is False, out.structured_content
    body = out.structured_content
    assert body["ds_artifact"]["artifact_id"].startswith("art_")
    assert body["n_rows"] == 2 and body["n_subints"] == 2
    assert body["flux_method"] == "StandardFlux"
    argv = [c for c in runner.calls if c[0] == "/fake/psrflux"][0]
    assert "-a" in argv

    # missing .ds output -> E_PROCESS_FAILED
    ctx.runner = _ScriptedRunner({"psrflux": (0, "", "")})
    out = await registry.dispatch("dynamic_spectra", {
        "archive": "obs.ar", "standard_profile": "std.ar", "execution_mode": "sync"})
    assert out.is_error is True
    assert out.structured_content["error"]["code"] == ErrorCode.PROCESS_FAILED.value


# ------------------------------------------------------------ call flow

async def test_flow_diagram_appended_to_success(ctx, registry):
    out = await registry.dispatch("pulsar_environment", {})
    text = out.content[0].text
    assert "```mermaid" in text and "调用流程" in text
    assert "pulsar_environment" in text and "flowchart" in text
    assert len(ctx.flow_log) == 1 and ctx.flow_log[0]["tool"] == "pulsar_environment"


async def test_flow_diagram_on_error(ctx, registry):
    out = await registry.dispatch("artifact_list", {"kind": "bogus_kind"})  # enum reject
    assert out.is_error is True
    text = out.content[0].text
    assert "❌" in text and "E_INVALID_INPUT" in text
    assert ctx.flow_log[-1]["ok"] is False


async def test_flow_diagram_chains_artifacts(ctx, registry):
    """Committed artifacts become output nodes; their artifact ids feed the
    session diagram as reusable input nodes."""
    from datetime import datetime, timezone

    from psrmcp.models.common import ArtifactKind, Provenance

    prov = Provenance(tool="t", tool_version="0", backend="b", params={},
                      started_at=datetime.now(timezone.utc), duration_s=0.0)
    ctx.store.commit_text(text="x", filename="a.tim", kind=ArtifactKind.TIM,
                          format_name="tempo2", provenance=prov)
    out = await registry.dispatch("artifact_list", {})
    assert "📤" in out.content[0].text

    from psrmcp.resources import read_resource
    blocks = await read_resource(ctx, "pulsar://calls")
    md = blocks[0].text
    assert "flowchart TD" in md and "artifact_list" in md and "a.tim" in md or \
        "📥" in md
    assert "本会话调用流程" in md


async def test_response_carries_reproduction_commands(ctx, registry, tmp_path, monkeypatch):
    """The exact executed argv reaches the user: profile_measure's response
    includes the psrstat command line with cd + exit code."""
    data = _archive_env(ctx, tmp_path)
    monkeypatch.setattr("psrmcp.tools.profile._psrchive_tool", lambda c, n: f"/fake/{n}")
    runner = _ScriptedRunner({"psrstat": (0, "obs.ar snr=42.5 w50=5.5", "")})
    ctx.runner = runner

    out = await registry.dispatch("profile_measure", {
        "archive": "obs.ar", "metrics": ["snr"], "generate_plot": False})
    assert out.is_error is False
    text = out.content[0].text
    assert "**复现命令**" in text
    assert "/fake/psrstat -c snr,w50" in text or "/fake/psrstat" in text
    assert "exit 0" in text
    # the command runs on the absolute resolved path, no cd needed
    assert f"/fake/psrstat -c snr {data / 'obs.ar'}" in text
    # the command text also appears as a 💻 node in the diagram
    assert "💻 /fake/psrstat" in text


async def test_error_response_carries_reproduction_commands(ctx, registry, tmp_path, monkeypatch):
    """A failed step still reports the commands that ran before the failure —
    that is what makes the failure reproducible."""
    data = _archive_env(ctx, tmp_path)
    monkeypatch.setattr("psrmcp.tools.rmfit._psrchive_tool", lambda c, n: f"/fake/{n}")
    ctx.runner = _ScriptedRunner({"psrmodel": (0, "", "Error::InvalidState\nincomplete polarization information\n")})

    out = await registry.dispatch("rvm_fit", {"archive": "obs.ar", "execution_mode": "sync"})
    assert out.is_error is True
    text = out.content[0].text
    assert "**复现命令**" in text and "/fake/psrmodel" in text
    assert "💻 /fake/psrmodel" in text


async def test_job_get_reports_job_commands(ctx, registry, tmp_path, monkeypatch):
    """Async long tasks: the commands are captured while the job runs and
    surface on job_get once it has finished."""
    data = _archive_env(ctx, tmp_path)
    monkeypatch.setattr("psrmcp.tools.profile._psrchive_tool", lambda c, n: f"/fake/{n}")

    def _smooth_produce(name, cwd):
        if name == "psrsmooth" and cwd:
            (Path(cwd) / "smooth_src.ar.sm").write_bytes(b"fake")

    ctx.runner = _ScriptedRunner({"psrsmooth": (0, "", "")}, produce=_smooth_produce)
    out = await registry.dispatch("profile_smooth", {"archive": "obs.ar"})  # auto -> async
    assert out.is_error is False
    job_id = out.structured_content["job"]["job_id"]
    for _ in range(50):  # let the job task run to completion
        await asyncio.sleep(0.01)
        if ctx.jobs.get(job_id).status.value in ("completed", "failed"):
            break
    assert ctx.jobs.get(job_id).status.value == "completed"
    assert job_id in ctx.job_commands and ctx.job_commands[job_id]

    out = await registry.dispatch("job_get", {"job_id": job_id})
    assert out.is_error is False
    text = out.content[0].text
    assert "**复现命令**" in text and "/fake/psrsmooth" in text


async def test_steps_diagram_describes_each_step(ctx, registry, tmp_path, monkeypatch):
    """处理流程 is a step-by-step chart: ordinal, tool, what it did (summary)
    and the key parameters — cumulative across the session."""
    data = _archive_env(ctx, tmp_path)
    monkeypatch.setattr("psrmcp.tools.profile._psrchive_tool", lambda c, n: f"/fake/{n}")
    ctx.runner = _ScriptedRunner({"psrstat": (0, "obs.ar snr=42.5 w50=5.5", "")})

    out = await registry.dispatch("pulsar_environment", {})
    text = out.content[0].text
    assert "**处理流程**" in text and "① pulsar_environment" in text

    out = await registry.dispatch("profile_measure", {
        "archive": "obs.ar", "metrics": ["snr"], "generate_plot": False})
    text = out.content[0].text
    assert "② profile_measure" in text
    # what the step did: the outcome summary from the response
    assert "S/N = 42.5" in text
    assert "S0 --> S1" in text          # steps chained in order
    assert "archive=obs.ar" in text     # key parameter recorded


async def test_steps_diagram_marks_failed_step(ctx, registry):
    """A failed step appears in the sequence with ❌, so the chart shows
    exactly where processing stopped."""
    await registry.dispatch("pulsar_environment", {})
    out = await registry.dispatch("artifact_list", {"kind": "bogus_kind"})
    text = out.content[0].text
    assert "**处理流程**" in text
    assert "② ❌ artifact_list" in text
