"""Download the OSM pedestrian network for Paris + 400 m buffer (osmnx, network_type="walk").

The buffer lets paths just outside the boundary carry walks, and lets AEDs in
neighbouring communes cover Paris streets near the périphérique.

OSM is a live database: re-running this later gives a slightly different graph.
The download date is recorded in data/raw/walk_graph_meta.json.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import osmnx as ox
from shapely.geometry import shape
from shapely.ops import transform
from pyproj import Transformer

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data/raw"
BUFFER_M = 400

to_l93 = Transformer.from_crs(4326, 2154, always_xy=True).transform
to_wgs = Transformer.from_crs(2154, 4326, always_xy=True).transform


def main(refresh: bool = False) -> None:
    if (RAW / "walk_graph.graphml").exists() and not refresh:
        print("walk_graph.graphml already present; pass --refresh to download again")
        return
    paris = shape(json.loads((RAW / "paris_commune_75056.geojson").read_text())["geometry"])
    area = transform(to_wgs, transform(to_l93, paris).buffer(BUFFER_M))
    ox.settings.use_cache = True
    ox.settings.cache_folder = str(ROOT / "data/raw/osmnx_cache")
    G = ox.graph_from_polygon(area, network_type="walk", simplify=True, retain_all=False)
    ox.save_graphml(G, RAW / "walk_graph.graphml")
    meta = {"downloaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "osmnx": ox.__version__, "network_type": "walk", "buffer_m": BUFFER_M,
            "nodes": G.number_of_nodes(), "edges": G.number_of_edges()}
    (RAW / "walk_graph_meta.json").write_text(json.dumps(meta, indent=1))
    print(meta)


if __name__ == "__main__":
    import sys
    main(refresh="--refresh" in sys.argv)
