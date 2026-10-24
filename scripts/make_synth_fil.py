#!/usr/bin/env python
"""生成合成 SIGPROC filterbank 测试数据(ws/synth.fil)。

1.4 GHz 频带、128 通道、8 bit,内嵌一条按 DM 色散的啁啾脉冲,
供 scripts/live_check.py 的 dedisperse 步骤做真实 dspsr 端到端验证。
psrchive 文档自带的 PSR.ar 是退化的 CPSR2 样本(TBIN='*'),dspsr 无法读取,
不能作为 dedisperse 的测试输入,故用本生成器补位。

用法:uv run python scripts/make_synth_fil.py [输出路径,默认 ws/synth.fil]
"""

from __future__ import annotations

import math
import struct
import sys
from pathlib import Path

TSAMP = 3.28e-5      # s per sample
NCHAN = 128
NSAMP = 4096
FOFF = -0.1953125    # MHz per channel
FCH1 = 1446.484375   # MHz, channel 0 中心频率(最高频)
DM = 2.645
TELESCOPE_ID = 4     # Parkes(dspsr/tempo2 有坐标;id 0 = "Fake" 会被拒绝)


def _name(k: str) -> bytes:
    kb = k.encode()
    return struct.pack("I", len(kb)) + kb


def _item_d(k: str, v: float) -> bytes:
    # 注意:数值项是裸值,没有长度前缀(字符串才有)
    return _name(k) + struct.pack("d", v)


def _item_i(k: str, v: int) -> bytes:
    return _name(k) + struct.pack("i", v)


def _item_s(k: str, v: str) -> bytes:
    vb = v.encode()
    return _name(k) + struct.pack("I", len(vb)) + vb


def build(out_path: Path) -> None:
    buf = bytearray()
    buf += _name("HEADER_START")
    buf += _item_s("source_name", "J0437")
    buf += _item_i("machine_id", 0)
    buf += _item_i("telescope_id", TELESCOPE_ID)
    buf += _item_i("data_type", 1)
    buf += _item_i("ibeam", 1)
    buf += _item_i("nbits", 8)
    buf += _item_d("tstart", 54027.921541)
    buf += _item_d("tsamp", TSAMP)
    buf += _item_i("nifs", 1)
    buf += _item_d("foff", FOFF)
    buf += _item_d("fch1", FCH1)
    buf += _item_i("nchans", NCHAN)
    buf += _name("HEADER_END")

    freqs = [FCH1 + i * FOFF for i in range(NCHAN)]
    data = bytearray(NSAMP * NCHAN)
    f_ref = 1400.0

    def delay_s(f_mhz: float) -> float:
        return 4.148808e6 * DM * (f_ref**-2 - f_mhz**-2) / 1e6

    for t in range(NSAMP):
        for c, f in enumerate(freqs):
            t_arr = 800 + int(delay_s(f) / TSAMP)
            ph = abs(t - t_arr)
            if ph < 40:
                data[t * NCHAN + c] = max(0, min(255, int(120 * math.exp(-0.5 * (ph / 6.0) ** 2))))
            else:
                data[t * NCHAN + c] = t % 7

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("wb") as fh:
        fh.write(bytes(buf))
        fh.write(bytes(data))
    print(f"wrote {out_path}")


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "ws/synth.fil")
    build(target)
