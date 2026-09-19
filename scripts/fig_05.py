"""Figures for step 5 (called from 05_greedy_sites.py)."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
from pyproj import Transformer
from shapely.geometry import shape
from shapely.ops import transform

ROOT = Path(__file__).resolve().parents[1]
LABELS = {"declared": "Declared available", "strict": "Strict: outdoor or freely accessible",
          "strict_unconditional": "Strict, minus badge/staff/intercom"}


def make(sites: pd.DataFrame, curves: dict, summary: dict, out: Path) -> None:
    from aednight import style
    import matplotlib.pyplot as plt
    style.apply()
    curve_fig(plt, style, curves, summary, out)
    site_map(plt, style, sites, out)


def curve_fig(plt, style, curves, summary, out):
    fig, ax = plt.subplots(figsize=(8.5, 5))
    x = np.arange(len(curves["strict_unconditional"]))
    for sc, color, dy in zip(["declared", "strict", "strict_unconditional"], style.SERIES, (4, 1, -7)):
        y = curves[sc] * 100
        ax.plot(x, y, color=color, lw=2, label=LABELS[sc])
        ax.annotate(f"{y[-1]:.0f}%", (x[-1], y[-1]), xytext=(7, dy), textcoords="offset points",
                    color=style.INK, fontsize=9)
        ax.annotate(f"{y[0]:.0f}%", (0, y[0]), xytext=(-30, dy), textcoords="offset points", color=style.INK,
                    fontsize=9)
    for k in (50, 100):
        ax.axvline(k, color=style.GRID, lw=1, zorder=0)
    ax.set_xlabel("New 24/7 outdoor AEDs added (greedy order)")
    ax.set_ylabel("% of Paris residents within 200 m at 03h")
    ax.set_xlim(-12, 214)
    ax.set_ylim(0, 50)
    ax.legend(frameon=False, loc="upper left", fontsize=9)
    fig.suptitle("Closing the 3am gap: coverage gained per new unit", x=0.01, ha="left", fontsize=12,
                 fontweight="bold")
    fig.text(0.01, 0.01, "Sites chosen greedily for the strict, non-conditional scenario at Tuesday 03h, then scored "
             "under all three scenarios.\nResidents: Filosofi 2021. Existing AEDs: Géo'DAE declared availability. "
             "Greedy is within 1-1/e of the optimum here. New units assumed outdoor and always available.",
             fontsize=8, color=style.INK2)
    fig.tight_layout(rect=(0, 0.05, 1, 0.95))
    fig.savefig(out / "fig_05_coverage_curve.png", dpi=160)
    plt.close(fig)


def site_map(plt, style, sites, out):
    from matplotlib.collections import PatchCollection
    from matplotlib.colors import BoundaryNorm, ListedColormap
    from matplotlib.patches import Rectangle
    gap = pd.read_csv(ROOT / "data/interim/gap_cells_tue03_strict_uncond.csv")
    g = gap[gap["pop"] > 0]
    ramp = ["#e8eef6", "#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95"]
    bounds = [0, 50, 200, 500, 1000, 2000, 3000, 1e9]
    fig, ax = plt.subplots(figsize=(10, 6.6))
    pc = PatchCollection([Rectangle((x, y), 200, 200) for x, y in zip(g.x0, g.y0)],
                         cmap=ListedColormap(ramp), norm=BoundaryNorm(bounds, len(ramp)), linewidth=0)
    pc.set_array(g.uncovered.to_numpy())
    ax.add_collection(pc)
    paris = shape(json.loads((ROOT / "data/raw/paris_commune_75056.geojson").read_text())["geometry"])
    paris = transform(Transformer.from_crs(4326, 3035, always_xy=True).transform, paris)
    ax.plot(*paris.exterior.xy, color=style.INK2, lw=0.8)
    sx, sy = Transformer.from_crs(2154, 3035, always_xy=True).transform(sites.x_l93.to_numpy(),
                                                                       sites.y_l93.to_numpy())
    ax.scatter(sx[100:], sy[100:], s=14, facecolor="none", edgecolor=style.SERIES[1], linewidth=0.9,
               label="sites 101-200", zorder=3)
    ax.scatter(sx[:100], sy[:100], s=26, color=style.SERIES[1], edgecolor=style.SURFACE, linewidth=0.8,
               label="first 100 sites", zorder=4)
    ax.set_aspect("equal")
    ax.autoscale()
    ax.axis("off")
    ax.legend(frameon=False, loc="lower left", fontsize=9)
    cb = fig.colorbar(pc, ax=ax, shrink=0.55, pad=0.01, ticks=bounds[:-1])
    cb.set_ticklabels(["0", "50", "200", "500", "1,000", "2,000", "3,000+"])
    cb.set_label("Residents per 200 m cell with no AED within 200 m at 03h", color=style.INK2)
    cb.outline.set_visible(False)
    fig.suptitle("Candidate sites for 24/7 outdoor AEDs, over the 3am gap", x=0.01, ha="left", fontsize=12,
                 fontweight="bold")
    fig.text(0.01, 0.01, "Candidate locations to survey, not recommendations: each is a street intersection chosen by "
             "the model, and siting, power, vandalism and maintenance are not modelled.\nBackground: residents "
             "without a reachable AED at Tuesday 03h (strict, non-conditional). Filosofi 2021 · Géo'DAE 2026-09-19 · "
             "OSM walk network.", fontsize=8, color=style.INK2)
    fig.tight_layout(rect=(0, 0.05, 1, 0.95))
    fig.savefig(out / "fig_05_proposed_sites_map.png", dpi=170)
    plt.close(fig)
