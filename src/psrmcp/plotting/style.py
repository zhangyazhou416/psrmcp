"""Shared matplotlib setup: headless Agg + consistent rcParams."""

from __future__ import annotations

import matplotlib

matplotlib.use("Agg")  # must run before pyplot import; never opens a window

import matplotlib.pyplot as plt  # noqa: E402

_RC = {
    "figure.dpi": 120,
    "savefig.dpi": 120,
    "font.size": 9,
    "axes.grid": True,
    "grid.alpha": 0.3,
    "axes.titlesize": 10,
    "legend.fontsize": 8,
}


def apply_style() -> None:
    matplotlib.rcParams.update(_RC)


def new_axes(figsize: tuple[float, float] = (8.0, 4.5)):
    apply_style()
    return plt.subplots(figsize=figsize)
