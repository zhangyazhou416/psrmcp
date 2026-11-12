"""Adapter parsers: pure functions fed recorded backend output shapes."""

from __future__ import annotations

import math

import pytest

from psrmcp.adapters import dspsr as dsp
from psrmcp.adapters import psrcat as pc
from psrmcp.adapters import psrchive as psa
from psrmcp.adapters import tempo2 as t2a
from psrmcp.errors import PsrmcpError


# Real sample (PSRCHIVE 2026-10-16): header column is filename, keyword is nsub,
# missing values print INVALID
VAP_SAMPLE = """\
filename  name ra dec dm period freq bw nchan npol nbin nsub duration telescope
tap.ar J0437-4715 04:37:15.8 -47:15.09 2.645 0.0057575 1367.5 256 8 2 1024 512 600 Parkes
"""


def test_parse_vap():
    fields = psa.parse_vap(VAP_SAMPLE)
    assert fields["name"] == "J0437-4715"
    assert math.isclose(fields["archive_dm"], 2.645, rel_tol=1e-9)
    assert fields["ra_str"] == "04:37:15.8"
    assert fields["nsubint"] == 512  # vap keyword nsub maps to the nsubint field
    assert fields["telescope"] == "Parkes"


def test_parse_vap_invalid_and_legacy_header():
    # with fewer columns, raw values are kept in VAP_KEYS order; INVALID normalizes to None
    legacy = psa.parse_vap(
        "File  name ra dec dm period freq bw nchan npol nbin nsub duration telescope\n"
        "x.ar J0437-4715 INVALID -47:15.09 2.645 0.0057575 1367.5 256 8 2 1024 512 600 Parkes\n"
    )
    assert legacy["name"] == "J0437-4715"
    assert legacy["ra_str"] is None  # INVALID -> None
    assert legacy["nsubint"] == 512


def test_psrstat_parse():
    # observed format: `psrstat -c snr,wt,w50 file` → `file snr=... wt=- w50=-`
    out = psa.parse_psrstat("/data/PSR.ar snr=133.858 wt=- w50=-\n")
    assert math.isclose(out["snr"], 133.858, rel_tol=1e-9)
    assert out["wt"] is None  # "-" -> None
    assert out["w50"] is None
    assert out["_filename"] == "/data/PSR.ar"
    # -Q variant: bare value list
    q = psa.parse_psrstat("/data/PSR.ar 133.858 - -\n")
    assert q["_positional"][0] == 133.858 and q["_positional"][1] is None


def test_psrcat_row_and_units():
    # observed 9 columns (name raj decj p0 f0 p1 dm binary assoc); -o short rows
    # carry a leading line number; the binary column holds a BINARY model
    # (T2/...; isolated pulsars print "*", parsed as None)
    row = pc.parse_psrcat_row(
        "1     J0437-4715    04:37:15.8  -47:15.09  0.0057575  173.687946 "
        "5.73e-20  2.65  T2  GRS:4FGL[aab+22]"
    )
    assert row[pc.PSRCAT_FIELD["name"]] == "J0437-4715"
    assert math.isclose(row[pc.PSRCAT_FIELD["dm"]], 2.65, rel_tol=1e-9)
    assert row["dec_str"] == "-47:15.09"
    assert row["binary_model"] == "T2"
    assert row["observatory"] == "GRS:4FGL[aab+22]"

    iso = pc.parse_psrcat_row(
        "2     J0030+0451    00:30:27.4  +04:51:39.7  0.004865  205.530696 "
        "1.02e-20  4.33  *  GRS:4FGL_J0030.4+0451[aab+22]"
    )
    assert iso["binary_model"] is None


def test_hms_to_deg():
    assert math.isclose(pc.hms_to_deg("04:37:15.8"), (4 + 37 / 60 + 15.8 / 3600) * 15, rel_tol=1e-6)
    assert math.isclose(pc.dms_to_deg("-47:15.09"), -(47 + 15.09 / 60), rel_tol=1e-6)


def test_hms_numeric_passthrough():
    assert math.isclose(pc.hms_to_deg_or_num("70.9"), 70.9, rel_tol=1e-9)


def test_build_par_text_tempo1_vs_2():
    kw = dict(name="J0437-4715", ra_deg=69.316, dec_deg=-47.252, p0_s=0.0057575, dm=2.645)
    par1 = pc.build_par_text(**kw)
    assert "PSRJ            J0437-4715" in par1 and "RAJ" in par1
    assert "CLK             UTC(NIST)" in par1 and "UNITS           TCB" in par1
    par2 = pc.build_par_text(**kw, tempo2=True)
    assert "CLK             TT(TAI)" in par2 and "UNITS           TDB" in par2
    assert "TIMEEPH         T2 Barycentric" in par2


def test_tempo2_output_parse():
    stats = t2a.parse_tempo2_output(
        "Number of TOAs: 120\nWeighted RMS residual: 1.234 us\nPost-fit chi-squared: 130.5\n"
    )
    assert stats.get("n_toas") == 120
    assert math.isclose(stats["rms_us"], 1.234, rel_tol=1e-6)
    assert math.isclose(stats["chi2"], 130.5, rel_tol=1e-6)


def test_dedisperse_cmd_shape():
    argv = dsp.dedisperse_cmd("dspsr", "obs.fil", dm=2.645, nbin=1024, out_ext="ar")
    assert argv[0] == "dspsr"
    assert "-D" in argv and "2.645" in argv
    assert "-E" not in argv  # no ephemeris -> no -E flag
    assert not any(";" in a or "|" in a for a in argv)

    argv_e = dsp.dedisperse_cmd("dspsr", "obs.fil", dm=2.645, ephemeris="/tmp/j0437.par")
    # observed: -E file makes dspsr build a phase predictor from the par
    # (required for filterbank and other inputs without an embedded predictor)
    assert argv_e[argv_e.index("-E") + 1] == "/tmp/j0437.par"


def test_channels_to_ranges():
    from psrmcp.tools.rfi import _channels_to_ranges

    assert _channels_to_ranges([0, 1, 2, 7]) == "0-2,7"
    assert _channels_to_ranges([5]) == "5"


def test_psradd_cmd_contract():
    argv = psa.psradd_cmd("psradd", ["/a/1.ar", "/a/2.ar"], "/tmp/m.ar")
    assert argv == ["psradd", "-o", "/tmp/m.ar", "/a/1.ar", "/a/2.ar"]


def test_pat_cmd_contract():
    argv = psa.pat_cmd("pat", "obs.ar", "std.ar")
    assert argv == ["pat", "-f", "tempo2", "-A", "PGS", "-s", "std.ar", "obs.ar"]
    try:
        psa.pat_cmd("pat", "obs.ar", "std.ar", algorithm="PACE")
        raise AssertionError("PACE should be rejected")
    except ValueError:
        pass


def test_paz_cmd_contract():
    argv = psa.paz_cmd("paz", "/in/src.ar", "0-2,7", "/tmp/out")
    assert argv == ["paz", "-z", "0-2,7", "-e", "paz", "/in/src.ar"]


def test_pam_cmd_contract():
    argv = psa.pam_cmd("pam", "src.ar", "std", bin_factor=4)
    assert argv == ["pam", "-T", "-b", "4", "-e", "std", "src.ar"]


def test_psrcat_cmd_contract():
    argv = pc.catalog_get_cmd("psrcat", "J0437-4715")
    assert "J0437-4715" in argv and "-name" not in argv
    search = pc.catalog_search_cmd("psrcat", conditions=["dm > 1"], )
    assert "-l" in search and "dm > 1" in search
    assert "-ra" not in search and "-binary" not in search and "-db" not in search


def test_resolve_columns_whitelist_and_dedup():
    # defaults stay first, requested extras are appended, duplicates collapse
    cols = pc.resolve_columns(["pb", "gb", "name", "PB"])
    assert cols[:9] == pc.PSRCAT_COLUMNS
    assert cols[9:] == ["pb", "gb"]
    with pytest.raises(PsrmcpError) as ei:
        pc.resolve_columns(["nope"])
    assert "unknown psrcat column" in str(ei.value)


def test_validate_expression_accepts_known_columns():
    out = pc.validate_expression("p0 < 0.005 && s1400 > 10")
    assert out == "p0 < 0.005 && s1400 > 10"
    # scientific notation: 'e3' after a digit is part of the number, not an identifier
    pc.validate_expression("edot > 1e3")
    # string literals are exempt
    pc.validate_expression('assoc == "GRS:4FGL[aab+22]"')
    # identifier followed by "(" passes through as a function call
    pc.validate_expression("sqrt(edot) > 2")


def test_validate_expression_rejects_bad_input():
    # unknown column: psrcat exits 0 with no output on those, so we reject up front
    with pytest.raises(PsrmcpError) as ei:
        pc.validate_expression("bogus > 1")
    assert "bogus" in str(ei.value)
    with pytest.raises(PsrmcpError):
        pc.validate_expression("p0 < 0.005\x01")  # control character
    with pytest.raises(PsrmcpError):
        pc.validate_expression("p0")  # below 3-char floor
    with pytest.raises(PsrmcpError):
        pc.validate_expression("p0 < " + "1" * 600)  # above 500-char ceiling


def test_parse_psrcat_row_columns_generic():
    cols = pc.resolve_columns(["s1400", "gb"])
    # shape mirrors a real `-o short` row with two extras (J0034-0534-like)
    row = pc.parse_psrcat_row_columns(
        "7     B1937+21    19:39:38.5  -21:25:27  0.001558  641.928250 "
        "1.05e-19  50.71  *  GRS:4FGL_J1937.4-2123[aab+22]  13.90  -21.26",
        cols,
    )
    assert row["name"] == "B1937+21"
    assert math.isclose(row["s1400"], 13.90, rel_tol=1e-9)
    assert math.isclose(row["gb"], -21.26, rel_tol=1e-9)


def test_catalog_query_cmd_contract():
    argv = pc.catalog_query_cmd("psrcat", expression="p0 < 0.005 && s1400 > 10",
                                columns=["name", "p0", "s1400"])
    assert argv[:4] == ["psrcat", "-nohead", "-o", "short"]
    i = argv.index("-l")
    assert argv[i + 1] == "p0 < 0.005 && s1400 > 10"
    assert "-db" not in argv


def test_pam_scrunch_cmd_contract():
    argv = psa.pam_scrunch_cmd("pam", "src.ar", "tr", freq_factor=4, time_factor=2,
                               sum_pols=True, bin_factor=2, remove_baseline=True, centre=True)
    assert argv[:8] == ["pam", "-F", "4", "-T", "2", "-b", "2", "-p"]
    assert argv[8:] == ["-r", "-c", "-e", "tr", "src.ar"]
    # factor 1 / None are no-ops
    argv = psa.pam_scrunch_cmd("pam", "src.ar", "tr", freq_factor=1, time_factor=None)
    assert argv == ["pam", "-e", "tr", "src.ar"]


def test_pdmp_cmd_contract():
    argv = psa.pdmp_cmd("pdmp", "obs.ar", dm_offset=1.0, dm_half_range=5.0,
                        period_offset_us=6.0, max_channels=128, std_profile="std.ar")
    assert argv[:3] == ["pdmp", "-g", "/null"]
    for flag, val in (("-po", "6.0"), ("-do", "1.0"),
                      ("-dr", "5.0"), ("-mc", "128"), ("-s", "std.ar")):
        assert argv[argv.index(flag) + 1] == val
    assert argv[-1] == "obs.ar"
    # bare minimum
    assert psa.pdmp_cmd("pdmp", "obs.ar") == ["pdmp", "-g", "/null", "obs.ar"]


def test_parse_pdmp_labels_and_errors():
    sample = """\
Reading archive
Best barycentric period (ms) = 22.789120 +/- 1.2e-05
Best DM = 2.6441 +/- 0.002
Best signal-to-noise ratio = 154.3
"""
    out = psa.parse_pdmp(sample)
    assert out["best_period_ms"] == 22.789120
    assert out["best_period_ms_error"] == 1.2e-05
    assert out["best_dm"] == 2.6441
    assert out["best_dm_error"] == 0.002
    assert out["best_snr"] == 154.3
    assert psa.parse_pdmp("nothing useful\n") == {}


def test_psredit_cmd_contracts():
    assert psa.psredit_get_cmd("psredit", "obs.ar", ["name", "dm"]) == \
        ["psredit", "-qc", "-c", "name,dm", "obs.ar"]
    argv = psa.psredit_set_cmd("psredit", "obs.ar", {"name": "J0437-4715", "dm": "2.644"})
    assert argv == ["psredit", "-c", "name=J0437-4715,dm=2.644", "-e", "edit", "obs.ar"]


def test_psredit_key_ok():
    assert psa.psredit_key_ok("name")
    assert psa.psredit_key_ok("dm")
    assert psa.psredit_key_ok("int:mjd")
    assert psa.psredit_key_ok("int[0]:wt")
    assert psa.psredit_key_ok("int[2,5-]:duration")
    assert psa.psredit_key_ok("aux:rmc")
    assert not psa.psredit_key_ok("na me")
    assert not psa.psredit_key_ok("a;b")
    assert not psa.psredit_key_ok("")
    assert not psa.psredit_key_ok("key=value")
    assert not psa.psredit_key_ok("9start")


def test_psrconv_cmd_contract():
    # -o must be passed explicitly: without it cfitsio builds silently fall
    # back to PSRFITS even for Timer/ASCII outputs
    argv = psa.psrconv_cmd("psrconv", "src.ar", "ar", "psrarchive")
    assert argv == ["psrconv", "-o", "Timer", "-e", "ar", "src.ar"]
    assert psa.psrconv_cmd("psrconv", "src.ar", "fits", "psrfits")[2] == "PSRFITS"
    assert psa.psrconv_cmd("psrconv", "src.ar", "txt", "ascii")[2] == "ASCII"


def test_psrspa_cmd_contract():
    argv = psa.psrspa_cmd(
        "psrspa", "obs.ar", histograms=("flux", "poldeg"), find_max_amplitude=True,
        find_pulses=True, pulse_algorithm="consecutive:threshold=4", min_pulse_bins=128,
        phase_ranges=(0.45, 0.5, 0.6, 0.7), hist_bins=64, log_scale=True,
    )
    assert argv[:3] == ["psrspa", "-hf", "-hd"]
    assert "-fm" in argv and "-sla" not in argv
    assert argv[argv.index("-a") + 1] == "consecutive:threshold=4"
    assert argv[argv.index("-N") + 1] == "128"
    assert argv[argv.index("-pr") + 1] == "0.45,0.5,0.6,0.7"
    assert argv[argv.index("-b") + 1] == "64"
    assert "-l" in argv
    assert argv[argv.index("-pfix") + 1] == "spa"
    assert argv[argv.index("-e") + 1] == "dat"
    assert argv[-1] == "obs.ar"
    # bare minimum
    assert psa.psrspa_cmd("psrspa", "obs.ar", histograms=("flux",)) == \
        ["psrspa", "-hf", "-pfix", "spa", "-e", "dat", "obs.ar"]


def test_psrspa_algorithm_ok():
    for name in psa.SPA_ALGORITHMS:
        assert psa.psrspa_algorithm_ok(name), name
    assert psa.psrspa_algorithm_ok("consecutive:threshold=4")
    assert psa.psrspa_algorithm_ok("normal:baseline=off:threshold=3")
    assert not psa.psrspa_algorithm_ok("bogus")
    assert not psa.psrspa_algorithm_ok("consecutive:threshold=4;rm -rf")
    assert not psa.psrspa_algorithm_ok("")


def test_parse_spa_outputs():
    maxamp = psa.parse_spa_maxamp(
        "obs.ar 0 1 307 717 312 18.4 2.1\n"
        "obs.ar 1 1 307 717 297 11.2 -1.0\n"
    )
    assert len(maxamp) == 2
    assert maxamp[0]["subint"] == 0 and maxamp[0]["max_bin"] == 312
    assert maxamp[0]["bin_min"] == 307 and maxamp[0]["bin_max"] == 717
    assert maxamp[0]["max_amplitude"] == 18.4 and maxamp[0]["b_sigma"] == 2.1
    assert maxamp[1]["subint"] == 1 and maxamp[1]["max_amplitude"] == 11.2
    # multiple ranges repeat the 5-field group per range on one line
    two = psa.parse_spa_maxamp("obs.ar 2 1 200 300 100 5.0 1.0 301 400 150 7.5 0.5\n")
    assert len(two) == 2
    assert two[1]["bin_min"] == 301 and two[1]["max_amplitude"] == 7.5

    pulses = psa.parse_spa_pulses(
        "obs.ar 3 42.5 310.0 8.0 1\nobs.ar 7 101.25 312.0 12.0 1\n"
    )
    assert pulses[0]["subint"] == 3 and pulses[0]["flux"] == 42.5
    assert pulses[1]["phase"] == 312.0 and pulses[1]["width_bins"] == 12.0
    assert psa.parse_spa_maxamp("") == [] and psa.parse_spa_pulses("junk line\n") == []


def test_rmfit_cmd_contract():
    argv = psa.rmfit_cmd(
        "rmfit", "obs.ar", rm_range=(-50.0, 50.0, 200), refine=True,
        quadratic_window=(0.4, 0.6), threshold_sigma=4.0, rm_max_upper=2000,
        rm_per_dm=6.0, rm_initial=2.6, save_rm_ext="rmfit",
    )
    assert argv[argv.index("-m") + 1] == "-50,50,200"
    assert "-r" in argv
    assert argv[argv.index("-w") + 1] == "0.4,0.6"
    assert argv[argv.index("-T") + 1] == "4.0"
    assert argv[argv.index("-u") + 1] == "2000"
    assert argv[argv.index("-U") + 1] == "6.0"
    assert argv[argv.index("-R") + 1] == "2.6"
    assert argv[argv.index("-x") + 1] == "rmfit"
    assert argv[-1] == "obs.ar"
    # brute force with auto bounds
    assert psa.rmfit_cmd("rmfit", "obs.ar", brute_force=True) == ["rmfit", "-t", "obs.ar"]
    # rm_range takes precedence over brute_force (mutually exclusive upstream)
    argv = psa.rmfit_cmd("rmfit", "obs.ar", brute_force=True, rm_range=(0, 100, 10))
    assert "-t" not in argv and argv[argv.index("-m") + 1] == "0,100,10"


def test_parse_rmfit_labels():
    sample = """\
rmfit: do_refine set threshold = 3
Chi-squared = 130.5 / 120 = 1.0875
rmfit: estimated RM = 2.6441 +/- 0.031
rmfit: converged in 4 iterations
       final rotation measure = 2.6438
Best RM is: 2.6441 +/- 0.05
"""
    out = psa.parse_rmfit(sample)
    assert out["best_rm"] == 2.6441 and out["best_rm_error"] == 0.05
    assert out["search_rm"] == 2.6441 and out["search_rm_error"] == 0.031
    assert out["refined_rm"] == 2.6438
    assert out["converged_iterations"] == 4
    assert out["chi2_reduced"] == 1.0875
    # "Best RM is:" without an error survives the Gaussian fit failure path
    out = psa.parse_rmfit("rmfit: failed to fit for best RM.\nBest RM is: 2.6\n")
    assert out["best_rm"] == 2.6 and "best_rm_error" not in out
    assert psa.parse_rmfit("nothing useful here\n") == {}
