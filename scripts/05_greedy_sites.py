"""Step 5: greedy placement of N new 24/7 outdoor AEDs.

Objective: residents within 200 m walk of an AED available at Tuesday 03h,
strict_unconditional scenario (step 4 weighting). New units are assumed
outdoor and 24/7, so they also count under the declared and strict scenarios.

Candidates: every walk-graph node (street intersection / path junction)
inside Paris; a unit sits exactly on the node (snap 0).

The objective sum_e w_e * min(len_e, g(d_u) + g(d_v)), with g(d) = max(0, T - d)
and d = distance to the nearest AED, is monotone submodular in the set of new
sites, so greedy selection is within (1 - 1/e) of the optimum and lazy (CELF)
evaluation is exact.

Robustness: the greedy is rerun with node-allocated residents (step 4 check);
we report how many of the top sites reappear within 100 m.

Output: outputs/05_proposed_sites.csv (ranked, with addresses), 05_*.json/.md, figures.
"""
from __future__ import annotations

import heapq
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd
import scipy.sparse as sp
from pyproj import Transformer
from scipy.sparse.csgraph import dijkstra
from scipy.spatial import cKDTree

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from aednight.network import nearest_open  # noqa: E402

INTERIM, OUT = ROOT / "data/interim", ROOT / "outputs"
T = 200.0
N_MAX = 200
REPORT_N = [25, 50, 100, 200]
TUE_03 = 24 + 3
TO_WGS = Transformer.from_crs(2154, 4326, always_xy=True).transform


def g(d):
    return np.clip(T - d, 0, None)


class Coverage:
    """Weighted street coverage, updated as sites are added."""

    def __init__(self, eu, ev, elen, w, d0):
        self.eu, self.ev, self.elen, self.w = eu, ev, elen, w
        self.d = d0.copy()
        n = len(d0)
        m = len(eu)
        self.inc = sp.csr_matrix((np.ones(2 * m), (np.r_[eu, ev], np.r_[np.arange(m), np.arange(m)])), shape=(n, m))
        self.cov = self._cov(np.arange(len(eu)))

    def _cov(self, e):
        return np.minimum(self.elen[e], g(self.d[self.eu[e]]) + g(self.d[self.ev[e]]))

    def value(self):
        return float((self.w * self.cov).sum())

    def gain(self, nodes, dist):
        old = self.d[nodes].copy()
        self.d[nodes] = np.minimum(old, dist)
        e = np.unique(self.inc[nodes].indices)
        gain = float((self.w[e] * (self._cov(e) - self.cov[e])).sum())
        self.d[nodes] = old
        return gain

    def add(self, nodes, dist):
        self.d[nodes] = np.minimum(self.d[nodes], dist)
        e = np.unique(self.inc[nodes].indices)
        self.cov[e] = self._cov(e)


def candidate_reach(csr, cand, batch=512):
    rows, cols, vals = [], [], []
    for s in range(0, len(cand), batch):
        D = dijkstra(csr, directed=False, indices=cand[s:s + batch], limit=T)
        r, c = np.nonzero(np.isfinite(D))
        rows.append(r + s)
        cols.append(c)
        vals.append(D[r, c] + 1e-9)  # keep zero distances as explicit entries
    return sp.csr_matrix((np.concatenate(vals), (np.concatenate(rows), np.concatenate(cols))),
                         shape=(len(cand), csr.shape[0]))


def lazy_greedy(cov: Coverage, R: sp.csr_matrix, n: int):
    heap = []
    for i in range(R.shape[0]):
        lo, hi = R.indptr[i], R.indptr[i + 1]
        gn = cov.gain(R.indices[lo:hi], R.data[lo:hi])
        if gn > 0:
            heap.append((-gn, i, 0))
    heapq.heapify(heap)
    chosen, gains = [], []
    while heap and len(chosen) < n:
        neg, i, it = heapq.heappop(heap)
        if it == len(chosen):
            lo, hi = R.indptr[i], R.indptr[i + 1]
            cov.add(R.indices[lo:hi], R.data[lo:hi])
            chosen.append(i)
            gains.append(-neg)
        else:
            lo, hi = R.indptr[i], R.indptr[i + 1]
            gn = cov.gain(R.indices[lo:hi], R.data[lo:hi])
            if gn > 0:
                heapq.heappush(heap, (-gn, i, len(chosen)))
    return chosen, gains


def reverse_geocode(lon, lat):
    """French national address base (BAN) reverse geocoder. Sends coordinates only."""
    urls = [f"https://api-adresse.data.gouv.fr/reverse/?lon={lon:.6f}&lat={lat:.6f}&limit=1",
            f"https://data.geopf.fr/geocodage/reverse?lon={lon:.6f}&lat={lat:.6f}&index=address&limit=1"]
    for u in urls:
        try:
            with urllib.request.urlopen(u, timeout=15) as r:
                f = json.load(r)["features"]
            if f:
                p = f[0]["properties"]
                return {"address": p.get("label", ""), "address_type": p.get("type", ""),
                        "address_distance_m": p.get("distance"), "geocoder": urllib.parse.urlparse(u).netloc}
        except Exception as exc:  # noqa: BLE001
            last = str(exc)
    return {"address": "", "address_type": "", "address_distance_m": None, "geocoder": f"failed: {last}"}


def main() -> None:
    net = np.load(INTERIM / "walk_net.npz")
    xy, eu, ev, elen = net["xy"], net["edge_u"], net["edge_v"], net["edge_len"]
    e_in, n_in = net["edge_in_paris"], net["node_in_paris"]
    n = len(xy)
    w_csr = sp.coo_matrix((elen, (eu, ev)), shape=(n, n))
    csr = (w_csr + w_csr.T).tocsr()

    pop_e = np.load(INTERIM / "edge_pop.npy")
    w = pop_e * e_in / np.maximum(elen, 1e-9)  # residents per metre, Paris streets only
    total = float((pop_e * e_in).sum())

    reach = sp.load_npz(INTERIM / "reach.npz")
    locs = pd.read_csv(INTERIM / "reach_locations.csv")
    a = pd.read_csv(INTERIM / "availability.csv", dtype=str, keep_default_na=False)
    code = pd.Categorical(a.site_key, categories=locs.site_key).codes
    strict = (a.strict_night_eligible == "True").to_numpy()
    elig = {"declared": np.ones(len(a), bool), "strict": strict,
            "strict_unconditional": strict & (a.conditional_access != "True").to_numpy()}
    open03 = {}
    for sc, e in elig.items():
        dev = (a.week_central.str[TUE_03] == "1").to_numpy() & e
        loc = np.zeros(len(locs), bool)
        np.logical_or.at(loc, code, dev)
        open03[sc] = loc
    d0 = {sc: nearest_open(reach, m) for sc, m in open03.items()}

    cand = np.flatnonzero(n_in)
    t0 = time.time()
    R = candidate_reach(csr, cand)
    print(f"candidate reach: {len(cand)} candidates, {R.nnz} entries, {time.time() - t0:.0f}s")

    main_cov = Coverage(eu, ev, elen, w, d0["strict_unconditional"])
    base = main_cov.value()
    chosen, gains = lazy_greedy(main_cov, R, N_MAX)
    print(f"greedy: {len(chosen)} sites, {time.time() - t0:.0f}s")

    # coverage curve for all three scenarios with the same chosen sites
    curves = {}
    for sc in elig:
        c = Coverage(eu, ev, elen, w, d0[sc])
        vals = [c.value()]
        for i in chosen:
            lo, hi = R.indptr[i], R.indptr[i + 1]
            c.add(R.indices[lo:hi], R.data[lo:hi])
            vals.append(c.value())
        curves[sc] = np.array(vals) / total

    # robustness: step 4's alternative allocation (cell residents split evenly over the graph nodes in the cell)
    nodes_pop = np.load(INTERIM / "node_pop.npy") * n_in

    class NodeCov(Coverage):
        def __init__(self, d0, pop):
            self.d, self.pop = d0.copy(), pop

        def value(self):
            return float(self.pop[self.d <= T].sum())

        def gain(self, nodes, dist):
            newly = (dist <= T) & (self.d[nodes] > T)
            return float(self.pop[nodes[newly]].sum())

        def add(self, nodes, dist):
            self.d[nodes] = np.minimum(self.d[nodes], dist)

    alt_chosen, _ = lazy_greedy(NodeCov(d0["strict_unconditional"], nodes_pop), R, N_MAX)
    main_xy, alt_xy = xy[cand[chosen]], xy[cand[alt_chosen]]
    overlap = {}
    for k in REPORT_N:
        dd, _ = cKDTree(alt_xy[:k]).query(main_xy[:k])
        overlap[k] = int((dd <= 100).sum())

    # ranked table
    lon, lat = TO_WGS(main_xy[:, 0], main_xy[:, 1])
    loc_xy = np.c_[Transformer.from_crs(4326, 2154, always_xy=True).transform(locs.lon.to_numpy(), locs.lat.to_numpy())]
    dnear, inear = cKDTree(loc_xy).query(main_xy)
    first = a.drop_duplicates("site_key").set_index("site_key")
    cells = pd.read_csv(INTERIM / "gap_cells_tue03_strict_uncond.csv")
    sites = pd.DataFrame({
        "rank": np.arange(1, len(chosen) + 1), "lat": np.round(lat, 6), "lon": np.round(lon, 6),
        "x_l93": np.round(main_xy[:, 0], 1), "y_l93": np.round(main_xy[:, 1], 1),
        "osm_node": net["node_ids"][cand[chosen]],
        "residents_gained_est": np.round(gains).astype(int),
        "cum_share_strict_uncond_est": np.round(curves["strict_unconditional"][1:], 4),
        "also_selected_by_node_allocation_within_100m":
            cKDTree(alt_xy).query(main_xy)[0] <= 100,
        "nearest_existing_aed_m": np.round(dnear).astype(int),
        "nearest_existing_aed_name": first.reindex(locs.site_key.iloc[inear]).c_nom.to_numpy(),
        "nearest_existing_aed_basis": first.reindex(locs.site_key.iloc[inear]).basis.to_numpy(),
    })
    geo_cache_path = INTERIM / "reverse_geocode_cache.json"
    cache = json.loads(geo_cache_path.read_text()) if geo_cache_path.exists() else {}
    rows = []
    for r in sites.itertuples():
        k = f"{r.lon:.6f},{r.lat:.6f}"
        if k not in cache:
            cache[k] = reverse_geocode(r.lon, r.lat)
            time.sleep(0.1)
        rows.append(cache[k])
    geo_cache_path.write_text(json.dumps(cache, indent=1, ensure_ascii=False))
    sites = pd.concat([sites, pd.DataFrame(rows)], axis=1)
    sites["arrondissement"] = sites.address.str.extract(r"(750\d\d)")[0]
    sites.to_csv(OUT / "05_proposed_sites.csv", index=False)
    np.save(INTERIM / "greedy_curves.npy", np.vstack([curves[s] for s in elig]))

    summary = {
        "objective": "residents within 200 m walk, Tue 03h, strict_unconditional (Filosofi 2021 on streets)",
        "candidates": len(cand), "residents_total": round(total),
        "baseline": {sc: round(float(curves[sc][0]), 4) for sc in elig},
        "after_n": {k: {sc: round(float(curves[sc][k]), 4) for sc in elig} for k in REPORT_N},
        "residents_gained": {k: round(float(sum(gains[:k]))) for k in REPORT_N},
        "marginal_gain_at": {k: round(float(gains[k - 1])) for k in REPORT_N},
        "robustness_node_allocation_overlap_within_100m": overlap,
        "geocoder_used": sites.geocoder.value_counts().to_dict(),
        "address_distance_m": {"median": float(np.nanmedian(sites.address_distance_m.astype(float))),
                               "max": float(np.nanmax(sites.address_distance_m.astype(float)))},
        "sites_within_50m_of_existing_aed": int((sites.nearest_existing_aed_m <= 50).sum()),
    }
    (OUT / "05_greedy.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False))
    print(json.dumps(summary, indent=1, ensure_ascii=False))
    print(sites.head(25)[["rank", "residents_gained_est", "cum_share_strict_uncond_est", "address",
                          "address_distance_m", "nearest_existing_aed_m",
                          "also_selected_by_node_allocation_within_100m"]].to_string())

    sys.path.insert(0, str(Path(__file__).parent))
    from fig_05 import make
    make(sites, curves, summary, OUT)


if __name__ == "__main__":
    main()
