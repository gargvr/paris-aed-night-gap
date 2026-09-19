# Step 1: Géo'DAE Paris data quality

Source: Géo'DAE national AED database, data.gouv.fr resource `edb6a9e1-…794c`,
resolved to `static.data.gouv.fr/.../20260919-190012/geodae.csv` (Last-Modified
2026-09-19 19:00 UTC, sha256 `a54eac3a…d978d`, 187,172 rows).
Paris boundary: geo.api.gouv.fr commune 75056. Script: `scripts/01_fetch_and_audit.py`.

**Every number below is VERIFIED as a count from the file.** None of them verifies
that a device exists or is reachable on the ground; they describe what operators
declared.

## Paris filter

| Step | Rows |
|---|---|
| Points inside the Paris commune polygon | 5,834 |
| of which admin INSEE code is a Paris arrondissement | 4,072 |
| of which INSEE is blank or other (mostly blank INSEE, Paris postcode) | 1,762 |
| INSEE says Paris but point outside polygon (excluded) | 15 |

The filter uses the point, not the admin fields. `the_geom` (WKB) is used as the
position because `c_lat_coor1`/`c_long_coor1` are rounded and contain garbage values.

## Exclusions

| Check | Rows flagged | Treatment |
|---|---|---|
| Not "validées" (pending 350, doubtful 1, blank 1) | 352 | excluded |
| Not "En fonctionnement" (temporarily absent 12, out of service 10) | 22 | excluded |
| `c_doublon = t` (operator-flagged duplicate) | 275 | excluded |
| `c_dae_mobile = t` (mobile unit, no fixed location) | 140 | excluded |
| **Placeholder coordinates** (≥10 distinct addresses on one exact point) | 392 | excluded |
| Postcode far from Paris (not 75/92/93/94), e.g. Tulle, Brive | 37 | excluded |
| Postcode in a neighbouring département (border) | 22 | kept, flagged |
| Coordinates with ≤3 decimals (~100 m error) | 9 | kept, flagged |

**Placeholder finding.** Three points are geocoding fallbacks, not real sites:
`48.87718, 2.335965` (9th arr., 268 rows from about 180 operators across France),
`48.856614, 2.352222` (the generic "Paris" geocode, 101 rows), and `48.856, 2.349` (23 rows).
Only 3 of the 347 status-valid rows on these points have a Paris postcode.
**Without the location checks, the status-valid Paris set would be about 7% larger, and
outdoor declared-24/7 devices would number 65 instead of 19.**

## Clean set

**4,703 devices at 4,319 distinct locations.** 226 locations hold 2 or more devices,
with at most 13 at one location (a hospital). For coverage, what matters is locations, not devices.
Freshness: 3,335 of 4,703 devices (71%) were updated in 2025–2026, and 139 have no update date.

## Availability (clean set)

The file has **no machine-readable opening hours**. It has three fields:
- `c_disp_j`: day list (`lundi`…`dimanche`, `7j/7`, `jours fériés`, `événements`, `non renseigné`)
- `c_disp_h`: a **category**, not hours: `heures ouvrables`, `24h/24`, `heures de nuit`, `non renseigné`
- `c_disp_complt`: optional free text, e.g. `L au V 8h30/12h30 13h30/17h30`

| Tier | Meaning | Devices | Locations | Hour-level? |
|---|---|---|---|---|
| A | `24h/24` and every day (`7j/7` or all 7 days listed) | 340 | 306 | yes (trivially) |
| B | free text with explicit times | 414 | 382 | yes, after parsing (step 2) |
| C | category only, mostly "heures ouvrables" | 3,861 | 3,563 | **no** |
| D | not stated / events only | 88 | 81 | no |

- **Usable hours: 754 devices (16%)** = A + B. 82% have only "business hours" with no times.
  In tier C, 2,518 are Monday–Friday, 645 have unstated days, and 422 say `7j/7` +
  "heures ouvrables", which is ambiguous.
- **24/7: 340 devices / 306 locations declared.** Only **19 are outdoors (18 locations)**.
  321 are indoors: hotel receptions, shelters (Emmaüs 29), SNCF stations (34), RATP (16),
  security posts, and office kitchenettes. 122 of the 340 are marked `c_acc_lib = f`
  (not freely accessible). "Declared 24/7" is therefore an **upper bound** on 3am
  public access, not a measurement.
- No declared-24/7 device has free text that contradicts it (heuristic check, 0 hits).

## Comparison with the OSM prototype (context, not a validation)

Géo'DAE has about 5.2× more Paris locations than OSM (4,319 vs 825). OSM had 73 devices tagged
"outdoor or 24/7". Géo'DAE has 306 locations that declare 24/7, but only 18 of them are outdoors.
The two definitions differ, so these counts are not directly comparable.

## Implications for steps 2–5 (decisions needed)

1. Hour-by-hour availability can be **parsed** for 16% of devices only. The other 82%
   need an **assumed** schedule for "heures ouvrables" (for example 9h–18h on the declared days).
   Any curve built this way is an estimate and needs a sensitivity band.
2. Night coverage needs at least two scenarios: *declared 24/7* (upper bound, 306 locations)
   and *declared 24/7 AND (outdoor OR freely accessible)* (stricter: 219 devices / 209 locations).
