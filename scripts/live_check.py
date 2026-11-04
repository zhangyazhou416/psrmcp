#!/usr/bin/env python
"""End-to-end check against the real stack (run on a machine with the pulsar stack installed).

Dispatches 9 real tool calls in sequence: pulsar_environment → archive_inspect →
profile_standardize → toa_extract → rfi_zap → catalog_search →
ephemeris_export → timing_fit → dedisperse; long-running calls wait for their job to finish.

Prerequisites:
  1. scripts/install_pulsar_stack.sh has been run and the shell has sourced
     <prefix>/env.sh (or config.toml [backends] holds absolute paths)
  2. PSRMCP_CONFIG points at a config with the real stack (config.toml is a ready template)
  3. config.toml [data].read_roots includes psrchive's docs/_static (the PSR.ar sample)
     and this repo's ws/ (where synth.fil lives); generate synth.fil with scripts/make_synth_fil.py

Usage: uv run python scripts/live_check.py
Exit code: 0 = all green; 1 = at least one step failed.
"""

from __future__ import annotations

import asyncio
import os
import sys

from psrmcp._context import build_context
from psrmcp.config import load_config
from psrmcp.server import build_registry


async def main() -> int:
    ctx = build_context(load_config(None))
    reg = build_registry(ctx)
    ok = fail = 0

    async def call(name: str, args: dict, wait_job: bool = False):
        nonlocal ok, fail
        out = await reg.dispatch(name, args)
        data = out.structured_content
        if out.is_error:
            fail += 1
            print(f"FAIL {name}: {data.get('error', {}).get('message', '?')[:160]}")
            return None
        if wait_job and data.get("result_type") == "accepted":
            jid = data["job"]["job_id"]
            for _ in range(600):
                ref = ctx.jobs.get(jid)
                if ref.status.value in ("completed", "failed", "cancelled", "interrupted"):
                    break
                await asyncio.sleep(0.2)
            if ref.status.value != "completed":
                fail += 1
                msg = ref.error and ref.error.message
                print(f"FAIL {name}: job {ref.status.value} err={msg and msg[:160]}")
                return None
            data = ref.result
        ok += 1
        print(f"ok   {name}")
        return data

    env = await call("pulsar_environment", {})
    print("     backends:", {k: v["available"] for k, v in env["backends"].items()})

    insp = await call("archive_inspect", {"archive": "PSR.ar"})
    print("     nbin:", insp["dimensions"]["nbin"], "nsub:", insp["dimensions"]["nsubint"])

    std = await call("profile_standardize", {"archive": "PSR.ar"})
    std_id = std["artifact"]["artifact_id"]
    print("     std nbin:", std["nbin"], "snr:", std["snr"] and round(std["snr"], 1))

    toa = await call("toa_extract", {"archive": "PSR.ar", "standard_profile": std_id}, wait_job=True)
    tim_id = toa and toa["tim_artifact"]["artifact_id"]
    print("     toas:", toa and toa["n_toas"])

    zap = await call("rfi_zap", {"archive": "PSR.ar", "mask": {"channels": [0, 1, 2]}})
    print("     zapped:", zap["n_channels_zapped"])

    cat = await call("catalog_search", {"name": "J0437-4715", "limit": 3})
    print("     catalog hits:", cat["count"], cat["records"][0]["name"] if cat["records"] else None)

    eph = await call("ephemeris_export", {"pulsar": "J0437-4715", "format": "tempo2"})
    par_id = eph["artifact"]["artifact_id"]

    if tim_id and par_id:
        fit = await call("timing_fit", {"tim": tim_id, "par": par_id, "backend": "pint"}, wait_job=True)
        if fit:
            print("     fit reduced chi2:", fit["fit"]["reduced_chi2"])

    dd = await call(
        "dedisperse",
        {"input": "synth.fil", "ephemeris": par_id, "mode": "incoherent", "dm_override": 2.645},
        wait_job=True,
    )
    print("     dedisperse artifact:", dd and dd["artifact"]["artifact_id"][:12])

    print(f"\nLIVE CHECK: ok={ok} fail={fail}", flush=True)
    sys.stdout.flush()
    os._exit(1 if fail else 0)  # don't wait for background jobs; avoids asyncio.run teardown hanging


if __name__ == "__main__":
    asyncio.run(main())
