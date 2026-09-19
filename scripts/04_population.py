"""Step 4: weight walking coverage by resident population.

Population: INSEE Filosofi 2021, 200 m grid (published 2026-02-16),
  https://www.insee.fr/fr/statistiques/8735162  (Filosofi2021_carreaux_200m_csv.zip)
  `ind` = individuals in fiscal households. Not imputed even in imputed cells
  (the only variable released as-is). Excludes collective housing (care
  homes, shelters, student halls, prisons) and homeless people.

Allocation (dasymetric): each cell's residents are spread over the walkable
street length whose midpoint lies in the cell. Coverage of a street segment
is the fraction of its length within T m of an open AED (step 3), so the
covered population is sum(pop_e * covered_len_e / len_e). Streets with no
residents (Bois, rail yards) get zero weight.
Check: a second allocation (cell residents split evenly over graph nodes in
the cell) is reported alongside.

Residents are the right denominator at night (people are home). By day they
are not: central business districts are under-weighted. Daytime population
figures are therefore residents-within-reach, not people-within-reach.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import scipy.sparse as sp
from pyproj import Transformer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from aednight.network import edge_covered_length, nearest_open  # noqa: E402

RAW, INTERIM, OUT = ROOT / "data/raw", ROOT / "data/interim", ROOT / "outputs"
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
BANDS = ["central", "low", "high"]
SCENARIOS = ["declared", "strict", "strict_unconditional"]
THRESHOLDS = [100, 200, 300, 400]
MAIN_T = 200
L93_TO_LAEA = Transformer.from_crs(2154, 3035, always_xy=True).transform


def ensure_filosofi() -> None:
    """Download + unzip the INSEE Filosofi 2021 200 m grid if it is not there yet."""
    csv = RAW / "filosofi2021/carreaux_200m_met.csv"
    if csv.exists():
        return
    import hashlib
    import urllib.request
    import zipfile
    zp = RAW / "filosofi2021_200m_csv.zip"
    if not zp.exists():
        urllib.request.urlretrieve(
            "https://www.insee.fr/fr/statistiques/fichier/8735162/Filosofi2021_carreaux_200m_csv.zip", zp)
    print("filosofi zip sha256", hashlib.sha256(zp.read_bytes()).hexdigest())
    with zipfile.ZipFile(zp) as z:
        z.extract("carreaux_200m_met.csv", RAW / "filosofi2021")


def load_cells(xmin, ymin, xmax, ymax) -> pd.DataFrame:
    cols = ["idcar_200m", "i_est_200", "lcog_geo", "ind"]
    df = pd.read_csv(RAW / "filosofi2021/carreaux_200m_met.csv", usecols=cols, dtype=str)
    xy = df.idcar_200m.str.extract(r"N(\d+)E(\d+)").astype(int)
    df["y0"], df["x0"] = xy[0], xy[1]
    df = df[(df.x0 >= xmin - 200) & (df.x0 <= xmax) & (df.y0 >= ymin - 200) & (df.y0 <= ymax)].copy()
    df["ind"] = df.ind.astype(float)
    df["arr"] = df.lcog_geo.str.extract(r"(751\d\d)")[0]
    return df.set_index(["x0", "y0"])


def main() -> None:
    ensure_filosofi()
    net = np.load(INTERIM / "walk_net.npz")
    xy, eu, ev, elen, e_in = net["xy"], net["edge_u"], net["edge_v"], net["edge_len"], net["edge_in_paris"]
    n_in = net["node_in_paris"]
    mid = (xy[eu] + xy[ev]) / 2
    mx, my = L93_TO_LAEA(mid[:, 0], mid[:, 1])
    nx_, ny_ = L93_TO_LAEA(xy[:, 0], xy[:, 1])
    cells = load_cells(nx_.min(), ny_.min(), nx_.max(), ny_.max())

    # --- allocation A: street length (primary)
    ekey = pd.MultiIndex.from_arrays([(mx // 200 * 200).astype(int), (my // 200 * 200).astype(int)])
    e_df = pd.DataFrame({"len": elen}, index=ekey)
    len_in_cell = e_df.groupby(level=[0, 1]).len.sum()
    has_street = cells.index.isin(len_in_cell.index)
    orphan = cells[~has_street & (cells.ind > 0)]
    # residents in cells with no street midpoint: give them to the nearest street midpoint
    from scipy.spatial import cKDTree
    tree = cKDTree(np.c_[mx, my])
    od, oi = tree.query(np.c_[orphan.index.get_level_values(0) + 100, orphan.index.get_level_values(1) + 100])
    # cells beyond the downloaded network (suburbs in the bounding box) are dropped, not moved
    near = od <= 200
    orphan_far = orphan[~near]
    orphan, od, oi = orphan[near], od[near], oi[near]
    density = (cells.ind.reindex(ekey).to_numpy() / len_in_cell.reindex(ekey).to_numpy())
    pop_e = np.nan_to_num(density) * elen
    np.add.at(pop_e, oi, orphan.ind.to_numpy())
    arr_e = cells.arr.reindex(ekey).to_numpy()

    # --- allocation B: nodes (check)
    nkey = pd.MultiIndex.from_arrays([(nx_ // 200 * 200).astype(int), (ny_ // 200 * 200).astype(int)])
    n_per_cell = pd.Series(1, index=nkey).groupby(level=[0, 1]).sum()
    pop_n = np.nan_to_num(cells.ind.reindex(nkey).to_numpy() / n_per_cell.reindex(nkey).to_numpy())

    W = pop_e * e_in
    WN = pop_n * n_in
    np.save(INTERIM / "edge_pop.npy", pop_e)
    np.save(INTERIM / "node_pop.npy", pop_n)

    reach = sp.load_npz(INTERIM / "reach.npz")
    locs = pd.read_csv(INTERIM / "reach_locations.csv")
    a = pd.read_csv(INTERIM / "availability.csv", dtype=str, keep_default_na=False)
    code = pd.Categorical(a.site_key, categories=locs.site_key).codes
    strict = (a.strict_night_eligible == "True").to_numpy()
    elig = {"declared": np.ones(len(a), bool), "strict": strict,
            "strict_unconditional": strict & (a.conditional_access != "True").to_numpy()}

    class Net:  # minimal view for edge_covered_length
        edge_u, edge_v, edge_len = eu, ev, elen

    arrs = sorted(pd.Series(arr_e[e_in]).dropna().unique())
    arr_mask = {r: (arr_e == r) & e_in for r in arrs}

    def metrics(rows):
        d = nearest_open(reach, rows)
        out = {}
        for t in THRESHOLDS:
            frac = edge_covered_length(Net, d, t) / np.maximum(elen, 1e-9)
            out[t] = {"pop": float((frac * W).sum() / W.sum()), "pop_nodes": float(((d <= t) * WN).sum() / WN.sum()),
                      "pop_covered": float((frac * W).sum())}
            if t == MAIN_T:
                out["arr"] = {r: float((frac * pop_e * m).sum() / (pop_e * m).sum()) for r, m in arr_mask.items()}
                out["frac"] = frac
        return out

    cache, rows, arr_rows, maps = {}, [], [], {}
    for band in BANDS:
        m = np.array([[ch == "1" for ch in w] for w in a[f"week_{band}"]], dtype=bool)
        for sc in SCENARIOS:
            loc = np.zeros((len(locs), 168), bool)
            np.logical_or.at(loc, code, m & elig[sc][:, None])
            for h in range(168):
                k = loc[:, h].tobytes()
                if k not in cache:
                    cache[k] = metrics(loc[:, h])
                r = cache[k]
                for t in THRESHOLDS:
                    rows.append(dict(scenario=sc, band=band, day=DAY_NAMES[h // 24], hour=h % 24, threshold_m=t,
                                     pop_share=r[t]["pop"], pop_share_node_alloc=r[t]["pop_nodes"],
                                     residents_covered=round(r[t]["pop_covered"])))
                if band == "central" and DAY_NAMES[h // 24] in ("Tue", "Sun") and h % 24 in (3, 12):
                    for arr, v in r["arr"].items():
                        arr_rows.append(dict(scenario=sc, day=DAY_NAMES[h // 24], hour=h % 24, arr=arr, pop_share=v))
                    maps[(sc, DAY_NAMES[h // 24], h % 24)] = r["frac"]
    ref = metrics(np.ones(len(locs), bool))
    hourly = pd.DataFrame(rows)
    hourly.to_csv(OUT / "04_population_coverage_hourly.csv", index=False)
    arr_df = pd.DataFrame(arr_rows)
    arr_df["arr_pop"] = arr_df.arr.map({r: float((pop_e * m).sum()) for r, m in arr_mask.items()})
    arr_df.to_csv(OUT / "04_arrondissement_coverage.csv", index=False)

    # per-cell residents NOT covered at Tue 03h (strict_unconditional) for the gap map
    frac = maps[("strict_unconditional", "Tue", 3)]
    gap = pd.DataFrame({"x0": ekey.get_level_values(0), "y0": ekey.get_level_values(1),
                        "uncovered": pop_e * (1 - frac) * e_in, "pop": pop_e * e_in}).groupby(["x0", "y0"]).sum()
    gap.to_csv(INTERIM / "gap_cells_tue03_strict_uncond.csv")

    def pick(sc, band, day, h, t=MAIN_T):
        r = hourly[(hourly.scenario == sc) & (hourly.band == band) & (hourly.day == day) & (hourly.hour == h)
                   & (hourly.threshold_m == t)].iloc[0]
        return {"pop_share": round(r.pop_share, 4), "node_alloc": round(r.pop_share_node_alloc, 4),
                "residents_covered": int(r.residents_covered)}

    summary = {
        "population_source": {
            "dataset": "INSEE Filosofi 2021, données carroyées 200 m (y compris imputées)",
            "page": "https://www.insee.fr/fr/statistiques/8735162",
            "file": "https://www.insee.fr/fr/statistiques/fichier/8735162/Filosofi2021_carreaux_200m_csv.zip",
            "published": "2026-02-16", "reference_year": 2021},
        "cells_loaded": len(cells),
        "residents_on_paris_streets": round(float(W.sum())),
        "residents_paris_cells_majority_751xx": round(float(cells[cells.lcog_geo.str[:3] == "751"].ind.sum())),
        "residents_in_imputed_cells_share": round(float(cells[cells.i_est_200 == "1"].ind.sum() / cells.ind.sum()), 4),
        "orphan_cells_moved_to_nearest_street": {"n": len(orphan), "residents": round(float(orphan.ind.sum())),
                                                 "max_move_m": round(float(od.max()), 1) if len(od) else 0},
        "cells_outside_network_dropped": {"n": len(orphan_far), "residents": round(float(orphan_far.ind.sum())),
                                          "with_751xx_code": int(orphan_far.arr.notna().sum())},
        "paris_street_km_with_zero_residents_share": round(float(((pop_e == 0) & e_in).dot(elen) / (e_in * elen).sum()), 4),
        "reference_all": {t: round(ref[t]["pop"], 4) for t in THRESHOLDS},
        "points": {f"{sc}|{b}|{d}{h:02d}": pick(sc, b, d, h) for sc in SCENARIOS for b in BANDS
                   for d in ("Tue", "Sun") for h in (3, 9, 12, 18)},
        "thresholds_tue03": {f"{sc}|{t}m": pick(sc, "central", "Tue", 3, t)["pop_share"]
                             for sc in SCENARIOS for t in THRESHOLDS},
    }
    (OUT / "04_population.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps({k: v for k, v in summary.items() if k != "points"}, indent=1))
    for k, v in summary["points"].items():
        if "|central|" in k or "|low|Tue09" in k:
            print(k, v)
    print(arr_df[(arr_df.day == "Tue") & (arr_df.hour == 3)].pivot(index="arr", columns="scenario", values="pop_share")
          .round(3).to_string())

    from importlib import import_module
    import_module("fig_04").make(hourly, arr_df, gap, summary, OUT)


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent))
    main()
