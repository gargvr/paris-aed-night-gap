"""Step 1: download Géo'DAE, filter to Paris, audit data quality.

Every number printed here is VERIFIED in the sense of "counted directly from
the file". It says nothing about whether the declared availability is true on
the ground.

Outputs:
  data/raw/geodae.csv                  national extract (+ sha256 in manifest)
  data/raw/paris_commune_75056.geojson Paris boundary (geo.api.gouv.fr)
  data/interim/geodae_paris.csv        Paris rows with derived columns
  outputs/01_data_quality.json         machine-readable counts
  outputs/01_data_quality.md           human-readable report
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import shapely
import shapely.wkb
from pyproj import Transformer
from shapely.geometry import shape
from shapely.ops import transform

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aednight.hours import pg_array as tokens  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
RAW, INTERIM, OUT = ROOT / "data/raw", ROOT / "data/interim", ROOT / "outputs"
GEODAE_URL = "https://www.data.gouv.fr/api/1/datasets/r/edb6a9e1-2f16-4bbf-99e7-c3eb6b90794c"
PARIS_URL = "https://geo.api.gouv.fr/communes/75056?format=geojson&geometry=contour&fields=nom,code,population"
ALL_DAYS = {"lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"}
TIME_RE = re.compile(r"\d{1,2}\s*[hH:]")
# An exact coordinate shared by >= this many distinct street addresses is a
# geocoding fallback, not a real location (found: one point in Paris 9e with
# devices from all over France, and the generic "Paris centre" geocode).
PLACEHOLDER_MIN_ADDRESSES = 10
NEAR_PARIS_DEPTS = ("75", "92", "93", "94")
RING_M = 400
TO_L93 = Transformer.from_crs(4326, 2154, always_xy=True).transform
TO_WGS = Transformer.from_crs(2154, 4326, always_xy=True).transform


def fetch(url: str, dest: Path, refresh: bool) -> dict:
    dest.parent.mkdir(parents=True, exist_ok=True)
    meta = {"url": url, "path": str(dest.relative_to(ROOT))}
    if refresh or not dest.exists():
        with urllib.request.urlopen(url) as r:
            meta["resolved_url"] = r.geturl()
            meta["last_modified"] = r.headers.get("Last-Modified")
            dest.write_bytes(r.read())
        meta["downloaded_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    meta["sha256"] = hashlib.sha256(dest.read_bytes()).hexdigest()
    meta["bytes"] = dest.stat().st_size
    return meta


def availability_tier(row) -> str:
    h, j, txt = row.H, row.J, row.c_disp_complt.strip()
    if "24h/24" in h and ("7j/7" in j or ALL_DAYS <= j):
        return "A_declared_24_7"
    if TIME_RE.search(txt):
        return "B_freetext_hours"
    if "heures ouvrables" in h or "heures de nuit" in h or "24h/24" in h:
        return "C_category_only"
    return "D_unknown"


def main(refresh: bool = False) -> None:
    manifest = {
        "geodae": fetch(GEODAE_URL, RAW / "geodae.csv", refresh),
        "paris_boundary": fetch(PARIS_URL, RAW / "paris_commune_75056.geojson", refresh),
    }
    df = pd.read_csv(RAW / "geodae.csv", sep=";", dtype=str, keep_default_na=False)
    n_national = len(df)

    # the_geom (WKB, EPSG:4326) is the canonical position: c_lat/c_long are
    # rounded strings and contain some garbage values.
    pts = df.the_geom.map(lambda h: shapely.wkb.loads(bytes.fromhex(h)).geoms[0])
    df["lon"], df["lat"] = pts.map(lambda p: p.x), pts.map(lambda p: p.y)

    paris = shape(json.loads((RAW / "paris_commune_75056.geojson").read_text())["geometry"])
    inside = shapely.contains_xy(paris, df.lon.values, df.lat.values)
    # Devices within RING_M outside Paris can still be the nearest AED for Paris streets.
    ring_poly = transform(TO_WGS, transform(TO_L93, paris).buffer(RING_M))
    ring = shapely.contains_xy(ring_poly, df.lon.values, df.lat.values) & ~inside
    insee_paris = df.c_com_insee.str.match(r"^751(0[1-9]|1\d|20)$")
    p = df[inside | ring].copy()
    p["in_paris"] = inside[inside | ring]

    p["J"], p["H"] = p.c_disp_j.map(tokens), p.c_disp_h.map(tokens)
    p["tier"] = p.apply(availability_tier, axis=1)
    p["site_key"] = p.lon.round(6).astype(str) + "," + p.lat.round(6).astype(str)
    adr = (p.c_adr_num + p.c_adr_voie).str.lower().str.replace(r"[^a-z0-9]", "", regex=True)
    n_adr = adr.groupby(p.site_key).transform("nunique")
    p["loc_placeholder"] = n_adr >= PLACEHOLDER_MIN_ADDRESSES
    on_grid = lambda v: (v * 1000).round(9).eq((v * 1000).round())
    p["loc_low_precision"] = on_grid(p.lat) & on_grid(p.lon) & ~p.loc_placeholder  # <=3 dp, ~100 m
    cp = p.c_com_cp.str.strip()
    admin_paris = cp.str.match(r"^75\d{3}$") | p.c_com_insee.str.match(r"^751(0[1-9]|1\d|20)$")
    admin_blank = (cp == "") & (p.c_com_insee == "")
    p["loc_admin_far"] = ~admin_paris & ~admin_blank & ~cp.str[:2].isin(NEAR_PARIS_DEPTS)
    p["loc_admin_border"] = ~admin_paris & ~admin_blank & cp.str[:2].isin(NEAR_PARIS_DEPTS)
    p["usable_location"] = ~p.loc_placeholder & ~p.loc_admin_far
    p["usable_status"] = (
        (p.c_etat_valid == "validées")
        & (p.c_etat_fonct == "En fonctionnement")
        & (p.c_doublon != "t")
        & (p.c_dae_mobile != "t")
    )
    txt = p.c_disp_complt.str.strip()
    p["freetext_contradicts_24_7"] = (p.tier == "A_declared_24_7") & txt.str.contains(
        r"non accessible|L au V|lundi au vendredi|\d{1,2}\s*h\s*[/-]\s*\d", case=False, regex=True
    ) & ~txt.str.contains(r"24\s*h", case=False)

    p["usable"] = p.usable_status & p.usable_location
    full = p
    p = full[full.in_paris]  # every count below is Paris-only
    clean = p[p.usable]
    q = {
        "national_rows": n_national,
        "paris_filter": {
            "inside_polygon": int(inside.sum()),
            "inside_polygon_and_insee_751xx": int((inside & insee_paris).sum()),
            "inside_polygon_insee_blank_or_other": int((inside & ~insee_paris).sum()),
            "insee_751xx_but_outside_polygon": int((~inside & insee_paris).sum()),
        },
        "status_all_paris": {
            "c_etat_valid": p.c_etat_valid.replace("", "(blank)").value_counts().to_dict(),
            "c_etat_fonct": p.c_etat_fonct.value_counts().to_dict(),
            "c_doublon_true": int((p.c_doublon == "t").sum()),
            "c_dae_mobile_true": int((p.c_dae_mobile == "t").sum()),
            "c_acc": p.c_acc.value_counts().to_dict(),
        },
        "location_checks_all_paris": {
            "placeholder_sites": sorted(p[p.loc_placeholder].site_key.unique().tolist()),
            "placeholder_rows": int(p.loc_placeholder.sum()),
            "admin_far_rows (postcode outside 75/92/93/94)": int(p.loc_admin_far.sum()),
            "admin_border_rows (postcode 92/93/94, kept, flagged)": int(p.loc_admin_border.sum()),
            "low_precision_rows (<=3 decimals, kept, flagged)": int(p.loc_low_precision.sum()),
        },
        "ring_outside_paris": {
            "ring_m": RING_M,
            "rows": int((~full.in_paris).sum()),
            "clean_devices": int((~full.in_paris & full.usable).sum()),
            "clean_locations": int(full[~full.in_paris & full.usable].site_key.nunique()),
        },
        "status_fail_rows": int((~p.usable_status).sum()),
        "location_fail_rows": int((~p.usable_location).sum()),
        "clean_devices": {
            "definition": "validated AND 'En fonctionnement' AND not flagged duplicate AND not mobile "
                          "AND not at a placeholder coordinate AND postcode not far from Paris",
            "devices": len(clean),
            "distinct_locations": int(clean.site_key.nunique()),
            "locations_with_2plus_devices": int((clean.site_key.value_counts() >= 2).sum()),
            "max_devices_at_one_location": int(clean.site_key.value_counts().max()),
        },
        "availability_clean": {
            "tier_counts": clean.tier.value_counts().sort_index().to_dict(),
            "tier_distinct_locations": clean.groupby("tier").site_key.nunique().to_dict(),
            "declared_24_7_by_access": pd.crosstab(clean.tier == "A_declared_24_7", clean.c_acc)
            .loc[True].to_dict(),
            "declared_24_7_freetext_contradiction": int(clean.freetext_contradicts_24_7.sum()),
            "c_disp_h_raw": clean.c_disp_h.value_counts().to_dict(),
            "c_disp_j_top10": clean.c_disp_j.value_counts().head(10).to_dict(),
            "freetext_nonempty": int((clean.c_disp_complt.str.strip() != "").sum()),
        },
        "freshness_clean": {
            "c_maj_don_year": clean.c_maj_don.str[:4].replace("", "(blank)").value_counts().sort_index().to_dict(),
        },
    }
    manifest["run_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    q["manifest"] = manifest

    INTERIM.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    cols = ["gid", "c_gid", "c_nom", "lat", "lon", "c_adr_num", "c_adr_voie", "c_com_cp", "c_com_insee",
            "c_acc", "c_acc_lib", "c_acc_etg", "c_acc_complt", "c_disp_j", "c_disp_h", "c_disp_complt",
            "c_etat_valid", "c_etat_fonct", "c_doublon", "c_dae_mobile", "c_maj_don", "c_expt_rais",
            "tier", "site_key", "in_paris", "usable_status", "usable_location", "usable", "loc_placeholder",
            "loc_low_precision", "loc_admin_far", "loc_admin_border", "freetext_contradicts_24_7"]
    full[cols].to_csv(INTERIM / "geodae_paris.csv", index=False)
    (OUT / "01_data_quality.json").write_text(json.dumps(q, indent=2, ensure_ascii=False, default=str))
    print(json.dumps(q, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main(refresh="--refresh" in sys.argv)
