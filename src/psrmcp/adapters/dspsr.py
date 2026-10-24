"""dspsr adapter: dedisperse/fold command builder.

dspsr applies coherent dedispersion automatically to raw voltage backends when a
DM is available; filterbank inputs are incoherently dedispersed. The MCP `mode`
parameter is therefore advisory: it is validated, recorded in provenance, and a
warning is emitted when it cannot be honoured. TODO(contract) marks flags to
verify on the installed version.
"""

from __future__ import annotations



def dedisperse_cmd(
    binary: str,
    source: str,
    *,
    dm: float | None = None,
    nbin: int | None = None,
    nchan: int | None = None,
    nsubint: int | None = None,
    out_prefix: str = "out",
    out_ext: str = "ar",
    threads: int | None = None,
    ephemeris: str | None = None,
) -> list[str]:
    args = [binary]
    if dm is not None:
        args += ["-D", f"{dm:.12g}"]
    if ephemeris is not None:
        # -E: 用 par 生成相位预测器(输入头内嵌 predictor 缺失时必需,
        # 如 filterbank/无 POLYCO 的 PSRFITS)
        args += ["-E", str(ephemeris)]
    if nbin is not None:
        args += ["-b", str(nbin)]
    if nchan is not None:
        # TODO(contract): "-F" configures output channels (e.g. -F 512:D).
        args += ["-F", str(nchan)]
    if nsubint is not None:
        # TODO(contract): "-L" sets sub-integration length in seconds; deriving
        # it from nsubint requires the observation duration, so pass through as
        # a best-effort "-L" only when the caller computed it.
        args += ["-L", str(nsubint)]
    if threads is not None:
        args += ["-t", str(threads)]  # TODO(contract): thread flag spelling.
    # -O 是输出文件名前缀(不是目录):产物 = <prefix>.<out_ext>
    args += ["-O", out_prefix]
    args += ["-e", out_ext]
    args.append(source)
    return args
