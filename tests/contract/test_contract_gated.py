"""Contract tests: run only after the real stack is installed (scripts/install_pulsar_stack.sh).

    source scripts/dev_env.sh
    PSRMCP_CONTRACT=1 uv run pytest tests/contract

Assertions encode the CLI contract verified on PSRCHIVE 2026-10-16 (d4b4e2d9c),
psrcat 2.6.1 and tempo2 (github ipta, fda6ad4) on 2026-10-24; they pin the
behaviour that adapters/* rely on.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("PSRMCP_CONTRACT") != "1",
    reason="requires the installed pulsar software stack: PSRMCP_CONTRACT=1 (see docs/INSTALL.md)",
)


def _which(name: str) -> str | None:
    return shutil.which(name)


def _archive() -> str | None:
    return os.environ.get("PSRMCP_CONTRACT_ARCHIVE")


def test_backends_on_path():
    for binary in ("vap", "psrstat", "pat", "psrconv", "psradd", "paz", "pam", "dspsr", "tempo2", "psrcat"):
        assert _which(binary), f"{binary} not on PATH; run source scripts/dev_env.sh first"


def test_psrchive_probe_version_format():
    """psrstat --version prints 'PSRCHIVE <date> <commit>' — the base.py probe."""
    from psrmcp.adapters.base import ALL_SPECS

    psrstat = _which("psrstat")
    assert psrstat
    out = subprocess.run([psrstat, "--version"], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr[-300:]
    import re

    spec = ALL_SPECS["psrchive"]
    regex = spec.probes[0][2]
    assert re.search(regex, out.stdout), f"probe regex {regex!r} did not match: {out.stdout!r}"


def test_vap_on_real_archive():
    """vap: header column is 'filename', keyword is nsub, missing -> INVALID."""
    from psrmcp.adapters import psrchive as psa

    vap = _which("vap")
    archive = _archive()
    if not (vap and archive):
        pytest.skip("set PSRMCP_CONTRACT_ARCHIVE=<archive path> to run")
    out = subprocess.run(psa.vap_cmd(vap, archive), capture_output=True, text=True, check=True)
    meta = psa.parse_vap(out.stdout)
    assert "name" in meta and "W_vap_column_mismatch" not in meta
    assert meta["name"], "name column parsed empty"


def test_psrstat_kv_format():
    """psrstat -c prints '<file> key=value ...'; missing values are '-'."""
    from psrmcp.adapters import psrchive as psa

    psrstat = _which("psrstat")
    archive = _archive()
    if not (psrstat and archive):
        pytest.skip("set PSRMCP_CONTRACT_ARCHIVE=<archive path> to run")
    out = subprocess.run(psa.psrstat_cmd(psrstat, archive, ["snr", "wt", "w50"]),
                         capture_output=True, text=True, check=True)
    parsed = psa.parse_psrstat(out.stdout)
    assert "snr" in parsed, f"missing k=v output: {out.stdout!r}"
    assert isinstance(parsed["snr"], float)


def test_pat_toa_format(tmp_path):
    """pat -f tempo2 -A PGS output parses into TOAs."""
    from psrmcp.adapters import psrchive as psa
    from psrmcp.tools.timing import _parse_toa_lines

    pat = _which("pat")
    archive = _archive()
    standard = os.environ.get("PSRMCP_CONTRACT_STANDARD")
    if not (pat and archive and standard):
        pytest.skip("set PSRMCP_CONTRACT_ARCHIVE / _STANDARD to run")
    out = subprocess.run(psa.pat_cmd(pat, archive, standard), capture_output=True, text=True)
    assert out.returncode == 0, out.stderr[-500:]
    toas = _parse_toa_lines(out.stdout)
    assert toas, "pat output yielded no TOAs; check _parse_toa_lines token heuristics"


def test_pam_standard_profile_roundtrip(tmp_path):
    """pam -T -e std builds a standard profile; pat accepts it back."""
    from psrmcp.adapters import psrchive as psa
    from psrmcp.tools.timing import _parse_toa_lines

    pam, pat = _which("pam"), _which("pat")
    archive = _archive()
    if not (pam and pat and archive):
        pytest.skip("set PSRMCP_CONTRACT_ARCHIVE=<archive path> to run")
    src = tmp_path / "src.ar"
    shutil.copy(archive, src)
    argv = psa.pam_cmd(pam, str(src), "std")
    out = subprocess.run(argv, capture_output=True, text=True, cwd=tmp_path)
    assert out.returncode == 0, out.stderr[-500:]
    std = tmp_path / "src.std"
    assert std.exists(), f"pam did not produce src.std: {sorted(p.name for p in tmp_path.iterdir())}"
    toa = subprocess.run(psa.pat_cmd(pat, str(src), str(std)), capture_output=True, text=True)
    assert toa.returncode == 0, toa.stderr[-500:]
    assert _parse_toa_lines(toa.stdout), "self-matched TOA failed to parse"


def test_psradd_dash_o(tmp_path):
    """psradd -o fname merges archives (-N does not exist)."""
    from psrmcp.adapters import psrchive as psa

    psradd = _which("psradd")
    archive = _archive()
    if not (psradd and archive):
        pytest.skip("set PSRMCP_CONTRACT_ARCHIVE=<archive path> to run")
    src = tmp_path / "a.ar"
    shutil.copy(archive, src)
    argv = psa.psradd_cmd(psradd, [str(src)], str(tmp_path / "merged.ar"))
    out = subprocess.run(argv, capture_output=True, text=True, cwd=tmp_path)
    assert out.returncode == 0, out.stderr[-500:]
    assert (tmp_path / "merged.ar").exists()


def test_paz_writes_paz_in_cwd(tmp_path):
    """paz needs an output flag: -e paz writes <basename>.paz into cwd."""
    from psrmcp.adapters import psrchive as psa

    paz = _which("paz")
    archive = _archive()
    if not (paz and archive):
        pytest.skip("set PSRMCP_CONTRACT_ARCHIVE=<archive path> to run")
    src = tmp_path / "src.ar"
    shutil.copy(archive, src)
    argv = psa.paz_cmd(paz, str(src), "0-2", str(tmp_path))
    out = subprocess.run(argv, capture_output=True, text=True, cwd=tmp_path)
    assert out.returncode == 0, out.stderr[-500:]
    assert (tmp_path / "src.paz").exists(), f"paz did not produce src.paz: {sorted(p.name for p in tmp_path.iterdir())}"
    assert src.exists(), "paz must not modify the input (no -m flag)"


def test_psrcat_positional_name_and_row_numbers(tmp_path):
    """psrcat: no -db/-name flags; -o short rows carry a leading line number."""
    from psrmcp.adapters import psrcat as pc

    psrcat = _which("psrcat")
    db = os.environ.get("PSRMCP_PSRCAT_DB")
    if not (psrcat and db):
        pytest.skip("set PSRMCP_PSRCAT_DB=<psrcat.db path> to run")
    env = {**os.environ, "PSRCAT_FILE": db}
    argv = pc.catalog_get_cmd(psrcat, "J0437-4715")
    out = subprocess.run(argv, capture_output=True, text=True, env=env)
    assert out.returncode == 0, out.stderr[-500:]
    lines = [ln for ln in out.stdout.splitlines() if ln.strip()]
    assert lines, "psrcat query returned no results"
    row = pc.parse_psrcat_row(lines[0])
    assert row["name"] == "J0437-4715"
    assert row["dec_str"].startswith("-"), f"decj should carry a leading minus: {row['dec_str']!r}"


def test_psrcat_conditions_use_env_db():
    from psrmcp.adapters import psrcat as pc

    psrcat = _which("psrcat")
    db = os.environ.get("PSRMCP_PSRCAT_DB")
    if not (psrcat and db):
        pytest.skip("set PSRMCP_PSRCAT_DB=<psrcat.db path> to run")
    env = {**os.environ, "PSRCAT_FILE": db}
    argv = pc.catalog_search_cmd(psrcat, conditions=["p0 < 0.1"])
    out = subprocess.run(argv, capture_output=True, text=True, env=env)
    assert out.returncode == 0, out.stderr[-500:]
    assert out.stdout.strip(), "-l condition syntax mismatch with the implementation; update adapters/psrcat.py"
    rows = [pc.parse_psrcat_row(ln) for ln in out.stdout.splitlines() if ln.strip()]
    assert rows and all(float(r["p0_s"]) < 0.1 for r in rows)


def test_psredit_query_separate_flag_form():
    """psredit: -q and -c must be separate argv tokens — the merged '-qc -c'
    form reports the query keys 'not found' (exit 0), which archive_edit's
    query path relies on parsing. Regression guard for psredit_get_cmd."""
    from psrmcp.adapters import psrchive as psa

    psredit = _which("psredit")
    archive = _archive()
    if not (psredit and archive):
        pytest.skip("set PSRMCP_CONTRACT_ARCHIVE=<archive path> to run")

    argv = psa.psredit_get_cmd(psredit, archive, ["type", "polc"])
    assert "-q" in argv and "-c" in argv and "-qc" not in argv, argv
    out = subprocess.run(argv, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr[-500:]
    assert "not found" not in (out.stdout + out.stderr), \
        f"query keys rejected — merged -qc flag form regression: {out.stdout!r} {out.stderr!r}"
    parsed = psa.parse_psrstat(out.stdout)
    assert "type" in parsed and parsed["type"], f"psredit query yielded no keys: {out.stdout!r}"


def test_pac_calibrate_writes_output(tmp_path):
    """pac on an observation with a PolnCal-typed copy exits 0 and writes
    <input>.calib[.P] — even when flux calibration fails, pac still exits 0,
    so the tools must check the output file, not the exit code."""
    from psrmcp.adapters import psrchive as psa

    pac, psredit = _which("pac"), _which("psredit")
    archive = _archive()
    if not (pac and psredit and archive):
        pytest.skip("set PSRMCP_CONTRACT_ARCHIVE=<archive path> to run")

    # pac matches calibrators by extension (-u): stage a PolnCal-typed copy
    cal = tmp_path / "cals" / "cal.pcal"
    cal.parent.mkdir()
    shutil.copy(archive, tmp_path / "obs.ar")
    shutil.copy(archive, cal)
    flip = subprocess.run(psa.psredit_set_cmd(psredit, str(cal), {"type": "PolnCal"}),
                          capture_output=True, text=True, cwd=tmp_path)
    assert flip.returncode == 0, flip.stderr[-500:]
    edited = cal.parent / "cal.edit"
    assert edited.exists(), f"psredit -c type= wrote nothing: {sorted(p.name for p in tmp_path.rglob('*'))}"
    shutil.move(edited, cal)

    argv = psa.pac_calibrate_cmd(pac, str(tmp_path / "obs.ar"),
                                 cals_dir=str(cal.parent), extensions=["pcal"])
    out = subprocess.run(argv, capture_output=True, text=True, cwd=tmp_path)
    assert out.returncode == 0, out.stderr[-500:]
    produced = sorted(tmp_path.glob("obs.calib*"))
    assert produced, (
        f"pac exited 0 but wrote no obs.calib* (silent per-file failure): "
        f"stdout/stderr={out.stdout[-300:]!r} {out.stderr[-300:]!r}"
    )
    assert produced[0].name.endswith("P"), (
        "no FluxCal present, so pac must append 'P' (poln-only) to the output name"
    )
