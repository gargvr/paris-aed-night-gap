"""Figures for step 4 (called from 04_population.py)."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
from pyproj import Transformer
from shapely.geometry import shape
from shapely.ops import transform

ROOT = Path(__file__).resolve().parents[1]
SCEN = ["declared", "strict", "strict_unconditional"]
LABELS = {"declared": "Declared available", "strict": "Strict: outdoor or freely accessible",
          "strict_unconditional": "Strict, minus badge/staff/intercom"}
BANDS = ["central", "low", "high"]


def make(hourly: pd.DataFrame, arr_df: pd.DataFrame, gap: pd.DataFrame, summary: dict, out: Path) -> None:
    from aednight import style
    import matplotlib.pyplot as plt
    style.apply()
    hourly_fig(plt, style, hourly, summary, out)
    arr_fig(plt, style, arr_df, out)
    gap_map(plt, style, gap, out)


def hourly_fig(plt, style, hourly, summary, out):
    h = hourly[hourly.threshold_m == 200]
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.8), sharey=True)
    ref = summary["reference_all"][200]
    for ax, day in zip(axes, ("Tue", "Sun")):
        for sc, color in zip(SCEN, style.SERIES):
            g = {b: h[(h.scenario == sc) & (h.band == b) & (h.day == day)].sort_values("hour") for b in BANDS}
            x = g["central"].hour.to_numpy()
            if sc == "declared":
                ax.fill_between(x, g["low"].pop_share * 100, g["high"].pop_share * 100, color=color, alpha=0.12,
                                linewidth=0, step="mid")
            ax.step(x, g["central"].pop_share * 100, where="mid", color=color, lw=2, label=LABELS[sc])
        ax.axhline(ref * 100, color=style.INK2, lw=1)
        ax.text(23.4, ref * 100 + 1.2, f"All devices, ignoring hours: {ref:.0%}", ha="right", fontsize=8.5,
                color=style.INK2)
        for sc, dy in (("declared", 14), ("strict_unconditional", -9)):
            v = h[(h.scenario == sc) & (h.band == "central") & (h.day == day) & (h.hour == 3)].pop_share.iloc[0]
            ax.annotate(f"{v:.0%}", (3, v * 100), xytext=(3.8, v * 100 + dy), fontsize=9,
                        arrowprops=dict(arrowstyle="-", color=style.INK2, lw=0.8))
        ax.set_title({"Tue": "Tuesday (typical weekday)", "Sun": "Sunday"}[day], loc="left")
        ax.set_xticks(range(0, 24, 3), [f"{x:02d}h" for x in range(0, 24, 3)])
        ax.set_xlim(-0.5, 23.5)
        ax.set_ylim(0, 100)
        ax.grid(axis="x", visible=False)
    axes[0].set_ylabel("% of Paris residents within 200 m walk")
    from matplotlib.patches import Patch
    handles, labels = axes[0].get_legend_handles_labels()
    handles.append(Patch(color=style.SERIES[0], alpha=0.15))
    labels.append("Declared, if business hours are 10-17h … 8-20h")
    fig.legend(handles, labels, frameon=False, loc="upper left", bbox_to_anchor=(0.01, 0.93), ncol=4, fontsize=8.5)
    fig.suptitle("Share of Paris residents within 200 m walk of an available AED, by hour", x=0.01, ha="left",
                 fontsize=12, fontweight="bold")
    fig.text(0.01, 0.01, "Residents: INSEE Filosofi 2021 200 m grid (fiscal households; excludes collective housing "
             "and homeless people). AEDs: Géo'DAE 2026-09-19, declared availability.\nResidents are the right "
             "denominator at night; by day they under-weight workplaces. Daytime levels depend on the assumed "
             "business-hours window.", fontsize=8, color=style.INK2)
    fig.tight_layout(rect=(0, 0.06, 1, 0.9))
    fig.savefig(out / "fig_04_population_coverage_by_hour.png", dpi=160)
    plt.close(fig)


def arr_fig(plt, style, arr_df, out):
    d = arr_df[(arr_df.day == "Tue") & (arr_df.hour == 3)].pivot(index="arr", columns="scenario", values="pop_share")
    pop = arr_df.drop_duplicates("arr").set_index("arr").arr_pop
    d = d.sort_values("strict_unconditional")
    names = [f"{int(a[-2:])}{'er' if a.endswith('01') else 'e'}" for a in d.index]
    fig, ax = plt.subplots(figsize=(8, 6.2))
    y = np.arange(len(d))
    ax.hlines(y, d.strict_unconditional * 100, d.declared * 100, color=style.GRID, lw=3)
    ax.scatter(d.declared * 100, y, s=46, color=style.SERIES[0], zorder=3, label=LABELS["declared"],
               edgecolor=style.SURFACE, linewidth=1.5)
    ax.scatter(d.strict_unconditional * 100, y, s=46, color=style.SERIES[2], zorder=3,
               label=LABELS["strict_unconditional"], edgecolor=style.SURFACE, linewidth=1.5)
    ax.set_yticks(y, [f"{n}  ({pop[a] / 1000:.0f}k)" for n, a in zip(names, d.index)])
    ax.set_xlabel("% of residents within 200 m walk of an AED available at 03h (Tuesday)")
    ax.xaxis.set_major_formatter(lambda v, _: f"{v:.0f}%")
    ax.grid(axis="y", visible=False)
    ax.set_xlim(0, max(70, d.declared.max() * 100 + 5))
    ax.legend(frameon=False, loc="lower right", fontsize=8.5)
    fig.suptitle("Night coverage by arrondissement", x=0.01, ha="left", fontsize=12, fontweight="bold")
    fig.text(0.01, 0.01, "Residents in brackets (Filosofi 2021, allocated to streets). Géo'DAE declared availability, "
             "not site checks.", fontsize=8, color=style.INK2)
    fig.tight_layout(rect=(0, 0.03, 1, 0.96))
    fig.savefig(out / "fig_04_arrondissements_3am.png", dpi=160)
    plt.close(fig)


def gap_map(plt, style, gap, out):
    from matplotlib.collections import PatchCollection
    from matplotlib.colors import BoundaryNorm, ListedColormap
    from matplotlib.patches import Rectangle
    g = gap[gap["pop"] > 0].reset_index()
    ramp = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]  # sequential blue
    bounds = [0, 50, 200, 500, 1000, 2000, 3000, 1e9]
    cmap, norm = ListedColormap(ramp), BoundaryNorm(bounds, len(ramp))
    fig, ax = plt.subplots(figsize=(9, 6.4))
    rects = [Rectangle((x, y), 200, 200) for x, y in zip(g.x0, g.y0)]
    pc = PatchCollection(rects, cmap=cmap, norm=norm, linewidth=0)
    pc.set_array(g.uncovered.to_numpy())
    ax.add_collection(pc)
    paris = shape(json.loads((ROOT / "data/raw/paris_commune_75056.geojson").read_text())["geometry"])
    paris = transform(Transformer.from_crs(4326, 3035, always_xy=True).transform, paris)
    ax.plot(*paris.exterior.xy, color=style.INK2, lw=0.8)
    ax.set_aspect("equal")
    ax.autoscale()
    ax.axis("off")
    cb = fig.colorbar(pc, ax=ax, shrink=0.6, pad=0.01, ticks=bounds[:-1])
    cb.set_ticklabels(["0", "50", "200", "500", "1,000", "2,000", "3,000+"])
    cb.set_label("Residents per 200 m cell without an AED within 200 m walk", color=style.INK2)
    cb.outline.set_visible(False)
    tot = g.uncovered.sum()
    fig.suptitle(f"The 3am gap: {tot / 1e6:.2f} million residents with no AED within 200 m walk (strict scenario)", x=0.01, ha="left",
                 fontsize=12, fontweight="bold")
    fig.text(0.01, 0.01, "Tuesday 03h. Strict = outdoor or freely accessible, minus badge/staff/intercom devices.\n"
             "INSEE Filosofi 2021 residents (200 m cells); Géo'DAE 2026-09-19 declared availability; OSM walk network.",
             fontsize=8, color=style.INK2)
    fig.tight_layout(rect=(0, 0.05, 1, 0.95))
    fig.savefig(out / "fig_04_gap_map_3am.png", dpi=170)
    plt.close(fig)
