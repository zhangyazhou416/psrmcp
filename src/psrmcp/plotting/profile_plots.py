"""Plot builders for RFI masks and timing residuals (all PNG via matplotlib)."""

from __future__ import annotations

from pathlib import Path

from .style import new_axes


def plot_channel_mask(bad: list[int], nchan: int, png: Path, *, title: str = "RFI mask") -> None:
    """Render flagged channels as a one-row boolean strip."""
    mask = [1 if i in set(bad) else 0 for i in range(nchan)]
    fig, ax = new_axes(figsize=(8.0, 1.8))
    ax.imshow([mask], aspect="auto", cmap="Reds", interpolation="nearest",
              extent=(0, nchan, 0, 1))
    ax.set_yticks([])
    ax.set_xlabel("channel")
    ax.set_title(f"{title} — {len(bad)}/{nchan} channels flagged")
    fig.tight_layout()
    fig.savefig(png)
    fig.canvas.manager  # noqa: B018  (keep figure alive until saved)
    import matplotlib.pyplot as plt

    plt.close(fig)


def plot_residuals(series: dict, png: Path, *, title: str = "Timing residuals") -> None:
    """Postfit/prefit residual scatter: res_us vs MJD with error bars."""
    mjd = list(series["mjd"])
    res = list(series["res_us"])
    err = list(series.get("err_us") or [0.0] * len(mjd))
    freq = list(series.get("freq_mhz") or [])

    fig, ax = new_axes(figsize=(8.5, 4.5))
    if freq and len(set(round(f) for f in freq)) > 1:
        # Colour by frequency band when there is more than one band.
        sc = ax.scatter(mjd, res, c=freq, s=12, cmap="viridis")
        cbar = fig.colorbar(sc, ax=ax)
        cbar.set_label("MHz")
    else:
        ax.errorbar(mjd, res, yerr=err, fmt=".", ms=5, elinewidth=0.6, color="#1f77b4")
    ax.axhline(0.0, color="k", lw=0.6)
    ax.invert_yaxis()  # convention: earlier arrival (more negative) plotted up
    ax.set_xlabel("MJD")
    ax.set_ylabel("residual (us)")
    ax.set_title(title)
    fig.tight_layout()
    fig.savefig(png)
    import matplotlib.pyplot as plt

    plt.close(fig)
