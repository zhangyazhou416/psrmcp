"""Adapter parsers: pure functions fed recorded backend output shapes."""

from __future__ import annotations

import math


from psrmcp.adapters import dspsr as dsp
from psrmcp.adapters import psrcat as pc
from psrmcp.adapters import psrchive as psa
from psrmcp.adapters import tempo2 as t2a


# 实测样本(PSRCHIVE 2026-10-16):头列名是 filename,关键字是 nsub,
# 缺失值打印 INVALID
VAP_SAMPLE = """\
filename  name ra dec dm period freq bw nchan npol nbin nsub duration telescope
tap.ar J0437-4715 04:37:15.8 -47:15.09 2.645 0.0057575 1367.5 256 8 2 1024 512 600 Parkes
"""


def test_parse_vap():
    fields = psa.parse_vap(VAP_SAMPLE)
    assert fields["name"] == "J0437-4715"
    assert math.isclose(fields["archive_dm"], 2.645, rel_tol=1e-9)
    assert fields["ra_str"] == "04:37:15.8"
    assert fields["nsubint"] == 512  # vap 关键字 nsub 映射到 nsubint 字段
    assert fields["telescope"] == "Parkes"


def test_parse_vap_invalid_and_legacy_header():
    # 列数不足时按 VAP_KEYS 顺序保留原始值;INVALID 归一为 None
    legacy = psa.parse_vap(
        "File  name ra dec dm period freq bw nchan npol nbin nsub duration telescope\n"
        "x.ar J0437-4715 INVALID -47:15.09 2.645 0.0057575 1367.5 256 8 2 1024 512 600 Parkes\n"
    )
    assert legacy["name"] == "J0437-4715"
    assert legacy["ra_str"] is None  # INVALID -> None
    assert legacy["nsubint"] == 512


def test_psrstat_parse():
    # 实测格式:`psrstat -c snr,wt,w50 file` → `file snr=... wt=- w50=-`
    out = psa.parse_psrstat("/data/PSR.ar snr=133.858 wt=- w50=-\n")
    assert math.isclose(out["snr"], 133.858, rel_tol=1e-9)
    assert out["wt"] is None  # "-" -> None
    assert out["w50"] is None
    assert out["_filename"] == "/data/PSR.ar"
    # -Q 变体:裸值列表
    q = psa.parse_psrstat("/data/PSR.ar 133.858 - -\n")
    assert q["_positional"][0] == 133.858 and q["_positional"][1] is None


def test_psrcat_row_and_units():
    # 实测 9 列(name raj decj p0 f0 p1 dm binary assoc),-o short 行首带行号;
    # binary 列是 BINARY 模型(T2/...;孤立脉冲星为 "*",解析为 None)
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
    assert "-E" not in argv  # 无 ephemeris 时不传 -E
    assert not any(";" in a or "|" in a for a in argv)

    argv_e = dsp.dedisperse_cmd("dspsr", "obs.fil", dm=2.645, ephemeris="/tmp/j0437.par")
    # 实测 -E file 让 dspsr 用 par 生成相位预测器(filterbank 等无内嵌 predictor 的输入必需)
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
