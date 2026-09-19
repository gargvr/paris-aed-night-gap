"""Step 6 (presentation): animated map of the Paris AED day-night cycle.

Renders the real OSM pedestrian network of Paris, colouring each street by
whether an available AED is within a 200 m walk, hour by hour; then the night
scenarios, the residents-without-cover map, and the greedy candidate sites.

Every number shown comes from steps 3-5; nothing new is computed here except
the site-by-site coverage curve, which reuses the step-5 sites.

Distinct images are rendered once and held for a chosen duration by ffmpeg
(concat demuxer), so a 75 s film needs ~180 renders rather than 2,250.

Output: outputs/video/paris_aed_night_gap.mp4 (1920x1080, 30 fps, no audio)
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import scipy.sparse as sp
from matplotlib.collections import LineCollection, PatchCollection
from matplotlib.colors import BoundaryNorm, ListedColormap
from matplotlib.patches import Rectangle
from pyproj import Transformer
from scipy.sparse.csgraph import dijkstra
from shapely.geometry import shape
from shapely.ops import transform

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from aednight.network import edge_covered_length, nearest_open  # noqa: E402

RAW, INTERIM, OUT = ROOT / "data/raw", ROOT / "data/interim", ROOT / "outputs"
VID = OUT / "video"
FRAMES = VID / "frames"
T, FPS = 200.0, 30
W, H, DPI = 1920, 1080, 120
COVERED, UNCOVERED, SURFACE, INK, INK2 = "#2a78d6", "#dcdbd5", "#fcfcfb", "#0b0b0b", "#52514e"
SITE, GRID_LINE = "#eb6834", "#e4e3df"
CREDIT = ("Streets © OpenStreetMap contributors · AEDs: Géo'DAE (data.gouv.fr) 2026-09-19 · "
          "Residents: INSEE Filosofi 2021 · Availability is what operators declare, not a site check")


def build_frames() -> list[tuple[str, float]]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    net = np.load(INTERIM / "walk_net.npz")
    xy, eu, ev, elen = net["xy"], net["edge_u"], net["edge_v"], net["edge_len"]
    e_in = net["edge_in_paris"]
    n = len(xy)
    csr_w = sp.coo_matrix((elen, (eu, ev)), shape=(n, n))
    csr = (csr_w + csr_w.T).tocsr()
    pop_e = np.load(INTERIM / "edge_pop.npy")
    total = float((pop_e * e_in).sum())

    reach = sp.load_npz(INTERIM / "reach.npz")
    locs = pd.read_csv(INTERIM / "reach_locations.csv")
    a = pd.read_csv(INTERIM / "availability.csv", dtype=str, keep_default_na=False)
    code = pd.Categorical(a.site_key, categories=locs.site_key).codes
    strict = (a.strict_night_eligible == "True").to_numpy()
    elig = {"declared": np.ones(len(a), bool), "strict": strict,
            "strict_unconditional": strict & (a.conditional_access != "True").to_numpy()}

    class Net:
        edge_u, edge_v, edge_len = eu, ev, elen

    def dist_for(hour_idx: int, scenario: str) -> np.ndarray:
        dev = (a.week_central.str[hour_idx] == "1").to_numpy() & elig[scenario]
        loc = np.zeros(len(locs), bool)
        np.logical_or.at(loc, code, dev)
        return nearest_open(reach, loc)

    def cover(d: np.ndarray) -> tuple[np.ndarray, float]:
        frac = edge_covered_length(Net, d, T) / np.maximum(elen, 1e-9)
        return frac, float((frac * pop_e * e_in).sum() / total)

    # geometry (Paris only, drawn once)
    keep = np.flatnonzero(e_in)
    segs = xy[np.c_[eu[keep], ev[keep]]]
    paris = transform(Transformer.from_crs(4326, 2154, always_xy=True).transform,
                      shape(json.loads((RAW / "paris_commune_75056.geojson").read_text())["geometry"]))

    FRAMES.mkdir(parents=True, exist_ok=True)
    plan: list[tuple[str, float]] = []
    seq = [0]

    def new_fig():
        fig = plt.figure(figsize=(W / DPI, H / DPI), dpi=DPI, facecolor=SURFACE)
        ax = fig.add_axes([0.02, 0.06, 0.96, 0.80])
        ax.set_aspect("equal")
        ax.axis("off")
        fig.text(0.02, 0.022, CREDIT, fontsize=8.5, color=INK2)
        return fig, ax

    def save(fig, dur: float, name: str):
        p = FRAMES / f"{seq[0]:04d}_{name}.png"
        fig.savefig(p, facecolor=SURFACE)
        plt.close(fig)
        plan.append((p.name, dur))
        seq[0] += 1

    def draw_map(ax, frac, sites_xy=None, gap=None):
        if gap is not None:
            ramp = ["#eef3f9", "#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95"]
            bounds = [0, 50, 200, 500, 1000, 2000, 3000, 1e9]
            pc = PatchCollection([Rectangle((x, y), 200, 200) for x, y in zip(gap.x_l93, gap.y_l93)],
                                 cmap=ListedColormap(ramp), norm=BoundaryNorm(bounds, len(ramp)), linewidth=0)
            pc.set_array(gap.uncovered.to_numpy())
            ax.add_collection(pc)
        else:
            covered = frac[keep] >= 0.5
            lc = LineCollection(segs[~covered], colors=UNCOVERED, linewidths=0.5)
            ax.add_collection(lc)
            ax.add_collection(LineCollection(segs[covered], colors=COVERED, linewidths=0.8))
        ax.plot(*paris.exterior.xy, color=INK2, lw=0.9)
        if sites_xy is not None and len(sites_xy):
            ax.scatter(sites_xy[:, 0], sites_xy[:, 1], s=34, color=SITE, edgecolor=SURFACE, linewidth=0.8, zorder=5)
        ax.autoscale()

    def headline(fig, big, small, right_big=None, right_small=None, right_color=COVERED):
        fig.text(0.02, 0.93, big, fontsize=30, fontweight="bold", color=INK, va="top")
        fig.text(0.02, 0.875, small, fontsize=14, color=INK2, va="top")
        if right_big:
            fig.text(0.98, 0.93, right_big, fontsize=40, fontweight="bold", color=right_color, va="top", ha="right")
        if right_small:
            fig.text(0.98, 0.875, right_small, fontsize=13, color=INK2, va="top", ha="right")

    # ---------------- scene 1: title
    fig, ax = new_fig()
    d12, s12 = cover(dist_for(24 + 12, "declared"))
    draw_map(ax, d12)
    fig.text(0.02, 0.93, "Paris at 3am: where is the nearest defibrillator?", fontsize=34, fontweight="bold",
             color=INK, va="top")
    fig.text(0.02, 0.87, "Every street within a 200 m walk of a defibrillator that is available right now.  "
                         "Tuesday, 12h.", fontsize=15, color=INK2, va="top")
    save(fig, 3.5, "title")

    # ---------------- scene 2: the 24-hour sweep (declared)
    for h in range(24):
        frac, share = cover(dist_for(24 + h, "declared"))
        fig, ax = new_fig()
        draw_map(ax, frac)
        headline(fig, f"Tuesday  {h:02d}:00", "Streets within a 200 m walk of an available defibrillator",
                 f"{share:.0%}", "of residents covered")
        save(fig, 1.4 if h in (3, 12) else 0.85, f"hour{h:02d}")

    # ---------------- scene 3: night scenarios at 03h
    labels = {"declared": ("Everything operators declare open at 3am", "declared available"),
              "strict": ("…only those outdoors or marked freely accessible", "outdoor or freely accessible"),
              "strict_unconditional": ("…minus devices behind a badge, a guard or an intercom",
                                       "realistically reachable")}
    for sc in ("declared", "strict", "strict_unconditional"):
        frac, share = cover(dist_for(24 + 3, sc))
        fig, ax = new_fig()
        draw_map(ax, frac)
        headline(fig, "Tuesday  03:00", labels[sc][0], f"{share:.0%}", labels[sc][1])
        save(fig, 3.0, f"night_{sc}")

    # ---------------- scene 4: the gap, in people
    gapc = pd.read_csv(INTERIM / "gap_cells_tue03_strict_uncond.csv")
    to_l93 = Transformer.from_crs(3035, 2154, always_xy=True).transform
    gx, gy = to_l93(gapc.x0.to_numpy(), gapc.y0.to_numpy())
    gap = pd.DataFrame({"x_l93": gx, "y_l93": gy, "uncovered": gapc.uncovered, "pop": gapc["pop"]})
    gap = gap[gap["pop"] > 0]
    d0 = dist_for(24 + 3, "strict_unconditional")
    _, base_share = cover(d0)
    uncovered_people = total * (1 - base_share)
    for dur, note in ((2.5, ""), (3.0, "Darker cells: more people with no defibrillator within reach")):
        fig, ax = new_fig()
        draw_map(ax, None, gap=gap)
        headline(fig, f"{uncovered_people / 1e6:.2f} million residents", note or "Tuesday 03:00 — nobody within a "
                 "200 m walk of a reachable defibrillator", f"{1 - base_share:.0%}",
                 "of residents with none in reach", right_color=INK)
        save(fig, dur, "gap")

    # ---------------- scene 5: adding 24/7 outdoor units
    sites = pd.read_csv(OUT / "05_proposed_sites.csv")
    ids = {v: i for i, v in enumerate(net["node_ids"])}
    idx = np.array([ids[v] for v in sites.osm_node])[:100]
    D = dijkstra(csr, directed=False, indices=idx, limit=T)
    sxy = xy[idx]
    running = d0.copy()
    steps = [1, 2, 3, 5, 8, 12, 16, 20, 25, 30, 35, 40, 50, 60, 70, 80, 90, 100]
    prev = 0
    for k in steps:
        running = np.minimum(running, np.nanmin(np.where(np.isfinite(D[prev:k]), D[prev:k], np.inf), axis=0))
        prev = k
        frac, share = cover(running)
        fig, ax = new_fig()
        draw_map(ax, frac, sites_xy=sxy[:k])
        headline(fig, f"+{k} new 24/7 outdoor units", "Candidate sites to survey, chosen to cover the most residents "
                 "at 3am", f"{share:.0%}", f"of residents covered (from {base_share:.0%})")
        save(fig, 1.6 if k in (25, 50, 100) else 0.5, f"sites{k:03d}")

    # ---------------- scene 6: end card
    fig = plt.figure(figsize=(W / DPI, H / DPI), dpi=DPI, facecolor=SURFACE)
    fig.text(0.06, 0.88, "What the data says", fontsize=34, fontweight="bold", color=INK, va="top")
    lines = [
        ("At 3am, 10–15% of Paris residents have a defibrillator within a 200 m walk.", INK),
        ("At noon it is 76–84% — but that daytime figure rests on an assumed 9h–18h window.", INK),
        ("1.7 million residents are out of reach at night.", INK),
        ("100 well-placed 24/7 outdoor units would take night coverage from 10% to 29%.", INK),
        ("", INK),
        ("Availability is what operators declare in Géo'DAE. Nothing here was checked on the ground,", INK2),
        ("and this says nothing about survival: it measures walking distance, not response.", INK2),
        ("Proposed sites are candidates to survey, not recommendations.", INK2),
        ("", INK),
        ("Method, assumptions and limits are written up in the repository README; every number here is reproducible.", INK2),
    ]
    y = 0.76
    for text, color in lines:
        fig.text(0.06, y, text, fontsize=19 if color == INK else 15, color=color, va="top")
        y -= 0.072 if text else 0.03
    fig.text(0.02, 0.022, CREDIT, fontsize=8.5, color=INK2)
    save(fig, 6.0, "end")
    return plan


def encode(plan: list[tuple[str, float]]) -> Path:
    concat = VID / "concat.txt"
    with concat.open("w") as f:
        for name, dur in plan:
            f.write(f"file 'frames/{name}'\nduration {dur:.3f}\n")
        f.write(f"file 'frames/{plan[-1][0]}'\n")  # ffmpeg needs the last frame twice
    out = VID / "paris_aed_night_gap.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(concat),
                    "-vf", f"fps={FPS},format=yuv420p", "-c:v", "libx264", "-crf", "20", "-preset", "medium",
                    "-movflags", "+faststart", str(out)], check=True)
    return out


if __name__ == "__main__":
    plan = build_frames()
    out = encode(plan)
    total = sum(d for _, d in plan)
    print(f"{len(plan)} frames, {total:.1f}s -> {out} ({out.stat().st_size / 1e6:.1f} MB)")
