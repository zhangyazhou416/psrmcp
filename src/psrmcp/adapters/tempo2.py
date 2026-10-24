"""tempo2 adapter (secondary timing backend).

tempo2's non-interactive fit writes `tempo2.par` and residual files into its
working directory. TODO(contract) marks the exact output layout to verify on
first run; parsers are written against recorded samples.
"""

from __future__ import annotations

import re
from typing import Any


def tempo2_fit_cmd(binary: str, par: str, tim: str) -> list[str]:
    # Runs in a job tmp cwd; tempo2 drops tempo2.par / resid* there.
    return [binary, "-f", par, tim, "-resid", "2"]


def parse_tempo2_output(text: str) -> dict[str, Any]:
    """Extract fit statistics from tempo2 stdout (best-effort, sample-driven)."""
    stats: dict[str, Any] = {}
    for line in text.splitlines():
        low = line.lower()
        m = re.search(r"rms[^\d\n]{0,30}([0-9]+\.?[0-9]*(?:[eE][+-]?\d+)?)", low)
        if m:
            stats.setdefault("rms_us", float(m.group(1)))
        m = re.search(r"chi(?:2|-squared)[^\d\n]{0,30}([0-9]+\.?[0-9]*(?:[eE][+-]?\d+)?)", low)
        if m:
            stats.setdefault("chi2", float(m.group(1)))
        m = re.search(r"n(?:umber)?\s*(?:of\s*)?toas?\s*[:=]\s*(\d+)", low)
        if m:
            stats.setdefault("n_toas", int(m.group(1)))
    return stats
