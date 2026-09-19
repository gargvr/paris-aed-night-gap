"""Walking-network reach of AED locations.

Distances are metres along the OSM walk graph (EPSG:2154). An AED is snapped
to its nearest graph node, and the straight-line snap distance is ADDED to
every walk from it (conservative: it never makes an AED look closer).

`reach` is a sparse (locations x nodes) matrix holding SLACK = MAX_D + 1 - d
for every node within MAX_D metres of a location. Storing slack instead of
distance makes "nearest open AED" a column-wise max over the open rows, where
an implicit zero means "none within MAX_D".
"""
from __future__ import annotations

import json
from pathlib import Path

import networkx as nx
import numpy as np
import osmnx as ox
import scipy.sparse as sp
from pyproj import Transformer
from scipy.sparse.csgraph import dijkstra
from scipy.spatial import cKDTree
from shapely.geometry import shape
from shapely.ops import transform

MAX_D = 400.0  # largest threshold we evaluate (m)
TO_L93 = Transformer.from_crs(4326, 2154, always_xy=True).transform


def paris_l93(raw: Path):
    geom = shape(json.loads((raw / "paris_commune_75056.geojson").read_text())["geometry"])
    return transform(TO_L93, geom)


class WalkNetwork:
    def __init__(self, graphml: Path, paris):
        G = ox.project_graph(ox.load_graphml(graphml), to_crs="EPSG:2154")
        self.node_ids = np.array(list(G.nodes))
        idx = {n: i for i, n in enumerate(self.node_ids)}
        self.xy = np.array([(G.nodes[n]["x"], G.nodes[n]["y"]) for n in self.node_ids])
        # undirected, shortest parallel edge
        best: dict[tuple[int, int], float] = {}
        geoms = {}
        for u, v, d in G.edges(data=True):
            a, b = sorted((idx[u], idx[v]))
            if a == b:
                continue
            if d["length"] < best.get((a, b), np.inf):
                best[(a, b)] = d["length"]
                geoms[(a, b)] = d.get("geometry")
        pairs = np.array(list(best.keys()))
        self.edge_u, self.edge_v = pairs[:, 0], pairs[:, 1]
        self.edge_len = np.array(list(best.values()))
        self.edge_geom = [geoms[k] for k in best.keys()]
        n = len(self.node_ids)
        w = sp.coo_matrix((self.edge_len, (self.edge_u, self.edge_v)), shape=(n, n))
        self.csr = (w + w.T).tocsr()
        import shapely
        self.node_in_paris = shapely.contains_xy(paris, self.xy[:, 0], self.xy[:, 1])
        mid = (self.xy[self.edge_u] + self.xy[self.edge_v]) / 2
        self.edge_in_paris = shapely.contains_xy(paris, mid[:, 0], mid[:, 1])
        self.tree = cKDTree(self.xy)

    def snap(self, lon, lat):
        x, y = TO_L93(np.asarray(lon), np.asarray(lat))
        d, i = self.tree.query(np.c_[x, y])
        return i, d

    def reach(self, src_nodes: np.ndarray, snap_d: np.ndarray, batch: int = 256) -> sp.csr_matrix:
        rows, cols, vals = [], [], []
        for s in range(0, len(src_nodes), batch):
            D = dijkstra(self.csr, directed=False, indices=src_nodes[s:s + batch], limit=MAX_D)
            D += snap_d[s:s + batch, None]
            r, c = np.nonzero(D <= MAX_D)
            rows.append(r + s)
            cols.append(c)
            vals.append(MAX_D + 1 - D[r, c])
        return sp.csr_matrix((np.concatenate(vals), (np.concatenate(rows), np.concatenate(cols))),
                             shape=(len(src_nodes), len(self.node_ids)))


def nearest_open(reach: sp.csr_matrix, open_rows: np.ndarray) -> np.ndarray:
    """Distance (m) from every node to the nearest open location; inf beyond MAX_D."""
    if not open_rows.any():
        return np.full(reach.shape[1], np.inf)
    slack = np.asarray(reach[open_rows].max(axis=0).todense()).ravel()
    return np.where(slack > 0, MAX_D + 1 - slack, np.inf)


def edge_covered_length(net: WalkNetwork, dist: np.ndarray, t: float) -> np.ndarray:
    """Metres of each edge within t of an open AED, walking in from either end."""
    cu = np.clip(t - dist[net.edge_u], 0, None)
    cv = np.clip(t - dist[net.edge_v], 0, None)
    return np.minimum(net.edge_len, np.nan_to_num(cu) + np.nan_to_num(cv))
