"""Step 3: walking coverage of Paris for every hour of the week.

Supply : clean Géo'DAE locations in Paris + 400 m ring (step 2 availability).
Demand : the walk network INSIDE Paris (edges by midpoint, nodes by position).
Metric : share of Paris walkable street length within T metres walking of an
         AED that is available that hour (primary); share of graph nodes
         (secondary, comparable with the OSM prototype's "intersections").
         Population weighting comes in step 4.

Access scenarios (applied at every hour):
  declared              device available at that hour per step 2
  strict                ... AND (outdoor OR marked freely accessible)
  strict_unconditional  ... AND no conditional-access text (badge/staff/intercom...)
  reference_all         every clean device, ignoring hours (upper bound)
Bands central/low/high only move the ASSUMED business-hours devices.

Outputs: outputs/03_coverage_hourly.csv, 03_coverage.json/.md, fig_03_*.png,
         data/interim/reach.npz (+ reach_locations.csv) for steps 4-5.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import scipy.sparse as sp

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from aednight.network import MAX_D, WalkNetwork, edge_covered_length, nearest_open, paris_l93  # noqa: E402

RAW, INTERIM, OUT = ROOT / "data/raw", ROOT / "data/interim", ROOT / "outputs"
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
BANDS = ["central", "low", "high"]
SCENARIOS = ["declared", "strict", "strict_unconditional"]
THRESHOLDS = [100, 200, 300, 400]
MAIN_T = 200


def load_locations() -> tuple[pd.DataFrame, dict]:
    a = pd.read_csv(INTERIM / "availability.csv", dtype=str, keep_default_na=False)
    a["lat"], a["lon"] = a.lat.astype(float), a.lon.astype(float)
    strict = a.strict_night_eligible == "True"
    uncond = strict & (a.conditional_access != "True")
    elig = {"declared": np.ones(len(a), bool), "strict": strict.to_numpy(), "strict_unconditional": uncond.to_numpy()}
    locs = a.groupby("site_key", sort=True).agg(lat=("lat", "first"), lon=("lon", "first"),
                                                in_paris=("in_paris", "first"), devices=("gid", "size"))
    # open[(scenario, band)] -> bool array (locations x 168)
    code = pd.Categorical(a.site_key, categories=locs.index).codes
    open_ = {}
    for band in BANDS:
        m = np.array([[ch == "1" for ch in w] for w in a[f"week_{band}"]], dtype=bool)
        for sc in SCENARIOS:
            dev = m & elig[sc][:, None]
            loc = np.zeros((len(locs), 168), bool)
            np.logical_or.at(loc, code, dev)
            open_[(sc, band)] = loc
    return locs, open_


LABELS = {"declared": "Declared available", "strict": "Strict: outdoor or freely accessible",
          "strict_unconditional": "Strict, minus badge/staff/intercom"}


def fig_hourly(hourly: pd.DataFrame, summary: dict) -> None:
    from aednight import style
    import matplotlib.pyplot as plt
    style.apply()
    h = hourly[hourly.threshold_m == MAIN_T]
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.8), sharey=True)
    ref = summary["reference_all"][MAIN_T]["street"]
    for ax, day in zip(axes, ("Tue", "Sun")):
        for sc, color in zip(SCENARIOS, style.SERIES):
            g = {b: h[(h.scenario == sc) & (h.band == b) & (h.day == day)].sort_values("hour") for b in BANDS}
            x = g["central"].hour.to_numpy()
            if sc == "declared":  # one ribbon only; the strict lines move the same way
                ax.fill_between(x, g["low"].street_share * 100, g["high"].street_share * 100, color=color,
                                alpha=0.12, linewidth=0, step="mid")
            ax.step(x, g["central"].street_share * 100, where="mid", color=color, lw=2, label=LABELS[sc])
        ax.axhline(ref * 100, color=style.INK2, lw=1)
        ax.text(23.4, ref * 100 + 1.2, f"All devices, ignoring hours: {ref:.0%}", ha="right", fontsize=8.5,
                color=style.INK2)
        d = h[(h.scenario == "declared") & (h.band == "central") & (h.day == day) & (h.hour == 3)].street_share.iloc[0]
        ax.annotate(f"{d:.0%} at 03h", (3, d * 100), xytext=(3, d * 100 + 12), ha="center", fontsize=9,
                    arrowprops=dict(arrowstyle="-", color=style.INK2, lw=0.8))
        ax.set_title({"Tue": "Tuesday (typical weekday)", "Sun": "Sunday"}[day], loc="left")
        ax.set_xticks(range(0, 24, 3), [f"{x:02d}h" for x in range(0, 24, 3)])
        ax.set_xlim(-0.5, 23.5)
        ax.set_ylim(0, 100)
        ax.grid(axis="x", visible=False)
    axes[0].set_ylabel(f"% of Paris street length within {MAIN_T} m walk")
    handles, labels = axes[0].get_legend_handles_labels()
    from matplotlib.patches import Patch
    handles.append(Patch(color=style.SERIES[0], alpha=0.15))
    labels.append("Declared, if business hours are 10-17h … 8-20h")
    fig.legend(handles, labels, frameon=False, loc="upper left", bbox_to_anchor=(0.01, 0.93), ncol=4, fontsize=8.5)
    fig.suptitle(f"Share of Paris within {MAIN_T} m walk of an available AED, by hour", x=0.01, ha="left",
                 fontsize=12, fontweight="bold")
    fig.text(0.01, 0.01, "Walking distance on the OSM pedestrian network. AEDs: Géo'DAE 2026-09-19, Paris + 400 m ring. "
             "Availability = operator declarations, not site checks.\nDaytime levels depend on the assumed "
             "business-hours window. Street length is a proxy for where people are (population weighting: step 4).",
             fontsize=8, color=style.INK2)
    fig.tight_layout(rect=(0, 0.06, 1, 0.9))
    fig.savefig(OUT / "fig_03_coverage_by_hour.png", dpi=160)
    plt.close(fig)


def fig_maps(net, reach, open_, locs) -> None:
    from aednight import style
    import matplotlib.pyplot as plt
    from matplotlib.collections import LineCollection
    style.apply()
    segs = []
    for u, v, g in zip(net.edge_u, net.edge_v, net.edge_geom):
        segs.append(np.asarray(g.coords) if g is not None else net.xy[[u, v]])
    inside = net.edge_in_paris
    panels = [("Tuesday 12h · declared", open_[("declared", "central")][:, 24 + 12]),
              ("Tuesday 03h · declared", open_[("declared", "central")][:, 24 + 3]),
              ("Tuesday 03h · strict, minus badge/staff", open_[("strict_unconditional", "central")][:, 24 + 3])]
    fig, axes = plt.subplots(1, 3, figsize=(15, 4.4))
    for ax, (title, rows) in zip(axes, panels):
        d = nearest_open(reach, rows)
        frac = edge_covered_length(net, d, MAIN_T) / np.maximum(net.edge_len, 1e-9)
        cov = inside & (frac >= 0.5)
        unc = inside & ~cov
        ax.add_collection(LineCollection([segs[i] for i in np.flatnonzero(unc)], colors="#d4d3cd", linewidths=0.35))
        ax.add_collection(LineCollection([segs[i] for i in np.flatnonzero(cov)], colors=style.SERIES[0], linewidths=0.5))
        share = (edge_covered_length(net, d, MAIN_T) * inside).sum() / (net.edge_len * inside).sum()
        ax.set_title(f"{title}: {share:.0%} of streets", loc="left", fontsize=10.5)
        ax.set_aspect("equal")
        ax.autoscale()
        ax.axis("off")
    fig.suptitle(f"Paris streets within {MAIN_T} m walk of an available AED", x=0.01, ha="left", fontsize=12,
                 fontweight="bold")
    fig.text(0.01, 0.015, "Blue: at least half of the street segment is within reach. Grey: not. Géo'DAE 2026-09-19, "
             "OSM walk network. Declared availability, not verified on site.", fontsize=8, color=style.INK2)
    fig.tight_layout(rect=(0, 0.05, 1, 0.93))
    fig.savefig(OUT / "fig_03_map_noon_vs_3am.png", dpi=170)
    plt.close(fig)


def main() -> None:
    paris = paris_l93(RAW)
    net = WalkNetwork(RAW / "walk_graph.graphml", paris)
    locs, open_ = load_locations()
    src, snap = net.snap(locs.lon.to_numpy(), locs.lat.to_numpy())
    locs["node"], locs["snap_m"] = net.node_ids[src], snap
    reach = net.reach(src, snap)

    L = net.edge_len * net.edge_in_paris
    total_len, n_nodes = L.sum(), net.node_in_paris.sum()

    def metrics(open_rows):
        d = nearest_open(reach, open_rows)
        out = {}
        for t in THRESHOLDS:
            out[t] = (float((edge_covered_length(net, d, t) * net.edge_in_paris).sum() / total_len),
                      float(((d <= t) & net.node_in_paris).sum() / n_nodes))
        return out, d

    cache, rows = {}, []
    for (sc, band), M in open_.items():
        for h in range(168):
            key = M[:, h].tobytes()
            if key not in cache:
                cache[key] = metrics(M[:, h])[0]
            for t, (sl, sn) in cache[key].items():
                rows.append(dict(scenario=sc, band=band, day=DAY_NAMES[h // 24], hour=h % 24, threshold_m=t,
                                 street_share=sl, node_share=sn,
                                 open_locations=int(M[:, h].sum()), open_locations_paris=int((M[:, h] & (locs.in_paris == "True").to_numpy()).sum())))
    ref, d_ref = metrics(np.ones(len(locs), bool))
    for t, (sl, sn) in ref.items():
        for h in range(168):
            rows.append(dict(scenario="reference_all", band="-", day=DAY_NAMES[h // 24], hour=h % 24, threshold_m=t,
                             street_share=sl, node_share=sn, open_locations=len(locs),
                             open_locations_paris=int((locs.in_paris == "True").sum())))
    hourly = pd.DataFrame(rows)
    hourly.to_csv(OUT / "03_coverage_hourly.csv", index=False)

    # Ring-effect check: Paris-only supply, Tue 03h declared, 200 m.
    paris_rows = (locs.in_paris == "True").to_numpy()
    no_ring = metrics(open_[("declared", "central")][:, 24 + 3] & paris_rows)[0][MAIN_T]
    no_ring_ref = metrics(paris_rows)[0][MAIN_T]

    sp.save_npz(INTERIM / "reach.npz", reach)
    locs.reset_index().to_csv(INTERIM / "reach_locations.csv", index=False)
    np.savez_compressed(INTERIM / "walk_net.npz", node_ids=net.node_ids, xy=net.xy, node_in_paris=net.node_in_paris,
                        edge_u=net.edge_u, edge_v=net.edge_v, edge_len=net.edge_len, edge_in_paris=net.edge_in_paris)

    def pick(sc, band, day, hour, t=MAIN_T):
        r = hourly[(hourly.scenario == sc) & (hourly.band == band) & (hourly.day == day) &
                   (hourly.hour == hour) & (hourly.threshold_m == t)].iloc[0]
        return {"street": round(r.street_share, 4), "nodes": round(r.node_share, 4), "open_locations": int(r.open_locations)}

    summary = {
        "graph": {"nodes_total": len(net.node_ids), "nodes_in_paris": int(n_nodes),
                  "edges_total": len(net.edge_len), "street_km_in_paris": round(total_len / 1000, 1)},
        "snap_m": {q: round(float(np.quantile(snap, v)), 1) for q, v in
                   [("median", .5), ("p90", .9), ("p99", .99), ("max", 1)]},
        "snap_over_50m": int((snap > 50).sum()),
        "locations": {"total": len(locs), "in_paris": int(paris_rows.sum()), "ring": int((~paris_rows).sum())},
        "reference_all": {t: {"street": round(v[0], 4), "nodes": round(v[1], 4)} for t, v in ref.items()},
        "reference_all_paris_supply_only_200m": {"street": round(no_ring_ref[0], 4), "nodes": round(no_ring_ref[1], 4)},
        "tue_03h_declared_paris_supply_only_200m": {"street": round(no_ring[0], 4), "nodes": round(no_ring[1], 4)},
        "points": {f"{sc}|{band}|{day}{h:02d}": pick(sc, band, day, h)
                   for sc in SCENARIOS for band in BANDS for day in ("Tue", "Sun") for h in (3, 9, 12, 18, 21)},
        "thresholds_tue": {f"{sc}|{h:02d}|{t}m": pick(sc, "central", "Tue", h, t)["street"]
                           for sc in SCENARIOS for h in (3, 12) for t in THRESHOLDS},
    }
    (OUT / "03_coverage.json").write_text(json.dumps(summary, indent=1))
    fig_hourly(hourly, summary)
    fig_maps(net, reach, open_, locs)
    print(json.dumps({k: summary[k] for k in ("graph", "snap_m", "snap_over_50m", "locations", "reference_all",
                                              "reference_all_paris_supply_only_200m",
                                              "tue_03h_declared_paris_supply_only_200m")}, indent=1))
    for k, v in summary["points"].items():
        if "|central|" in k or "|low|Tue" in k or "|high|Tue" in k:
            print(k, v)
    print(summary["thresholds_tue"])


if __name__ == "__main__":
    main()
