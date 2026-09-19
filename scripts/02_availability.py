"""Step 2: availability per hour of the week for every clean Paris device.

Input : data/interim/geodae_paris.csv (from step 1)
Output: data/interim/availability.csv   one row per clean device, three 168-char
                                        '0'/'1' strings (Mon 00h .. Sun 23h):
                                        week_central / week_low / week_high
        outputs/02_parse_audit.csv      every distinct free-text string -> reading
        outputs/02_availability.json/.md
        outputs/fig_02_locations_by_hour.png

`basis` records where each device's hours come from:
  declared_24_7        structured fields: 24h/24 every day            (VERIFIED as declared)
  declared_24h_days    structured fields: 24h/24 on listed days        (VERIFIED as declared)
  parsed_text          explicit times in free text                     (VERIFIED as declared, via parser)
  assumed_business     "heures ouvrables" only -> BANDS window         (ESTIMATE)
  assumed_night        "heures de nuit" only -> 20h-8h                 (ESTIMATE)
  none                 events only / nothing stated -> never available (conservative)
Only assumed_* rows differ between the central/low/high bands.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from aednight.hours import (  # noqa: E402
    ALL, BANDS, CONDITIONAL, NIGHT_WINDOW, TIME_HINT, WEEKDAYS, _fold, field_days, parse_text,
    pg_array, schedule_to_week, week_to_hours, window_schedule,
)

INTERIM, OUT = ROOT / "data/interim", ROOT / "outputs"
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def hours_of(sched) -> np.ndarray:
    return week_to_hours(schedule_to_week(sched))


def classify(row) -> dict:
    J, H = pg_array(row.c_disp_j), pg_array(row.c_disp_h)
    fd = field_days(J)
    days = fd if fd is not None else WEEKDAYS  # unstated days -> Mon-Fri (assumption)
    rec = {"days_assumed": fd is None, "parse_failed": False, "text_source": "", "text": ""}
    zeros = np.zeros(168, dtype=bool)

    def same(h):
        return dict(rec, central=h, low=h, high=h)

    if row.tier == "A_declared_24_7":
        return same(np.ones(168, dtype=bool)) | {"basis": "declared_24_7"}

    for src in ("c_disp_complt", "c_acc_complt"):
        txt = getattr(row, src).strip()
        if txt and TIME_HINT.search(txt):
            rec |= {"text_source": src, "text": txt}
            p = parse_text(txt, days)
            if p.ok:
                rec["days_assumed"] = fd is None and p.days_from_field
                return same(hours_of(p.schedule)) | {"basis": "parsed_text"}
            rec["parse_failed"] = True
            break

    if "24h/24" in H and days:
        return same(hours_of(window_schedule(days, (0, 1440)))) | {"basis": "declared_24h_days"}
    if not days:  # events only
        return same(zeros) | {"basis": "none"}
    business, night = "heures ouvrables" in H, "heures de nuit" in H
    if not business and not night:
        return same(zeros) | {"basis": "none"}
    out = {"basis": "assumed_business" if business else "assumed_night"}
    for band, window in BANDS.items():
        h = hours_of(window_schedule(days, window)) if business else zeros.copy()
        if night:
            h |= hours_of(window_schedule(days, NIGHT_WINDOW))
        out[band] = h
    return rec | out


PRIORITY = [("Declared 24h", ("declared_24_7", "declared_24h_days")),
            ("Parsed opening times", ("parsed_text",)),
            ("Assumed business hours", ("assumed_business", "assumed_night"))]


def _matrix(df, band="central"):
    return np.array([[ch == "1" for ch in w] for w in df[f"week_{band}"]], dtype=bool)


def priority_counts(c) -> dict:
    """Locations open per hour, each counted once under its most trusted open basis."""
    out, taken = {}, pd.DataFrame(np.zeros((c.site_key.nunique(), 168), dtype=bool),
                                  index=sorted(c.site_key.unique()))
    for label, bases in PRIORITY:
        g = c[c.basis.isin(bases)]
        per_band = {}
        for band in BANDS:
            open_ = pd.DataFrame(_matrix(g, band)).groupby(g.site_key.values).any()
            open_ = open_.reindex(taken.index, fill_value=False)
            per_band[band] = (open_ & ~taken).sum(axis=0).tolist()
            if band == "central":
                new_taken = taken | open_
        taken = new_taken
        out[label] = per_band
    return out


def strict_count(c, day, hour) -> int:
    i = DAY_NAMES.index(day) * 24 + hour
    ok = c[c.strict_night_eligible & (c.week_central.str[i] == "1")]
    return int(ok.site_key.nunique())


def figure(summary) -> None:
    from aednight import style
    import matplotlib.pyplot as plt
    style.apply()
    pr = summary["priority_locations_by_hour"]
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.8), sharey=True)
    x = np.arange(24)
    for ax, day in zip(axes, ("Tue", "Sun")):
        d = DAY_NAMES.index(day)
        sl = slice(d * 24, (d + 1) * 24)
        bottom = np.zeros(24)
        for (label, _), color in zip(PRIORITY, style.SERIES):
            v = np.array(pr[label]["central"][sl])
            ax.bar(x, v, bottom=bottom, width=0.8, color=color, label=label,
                   edgecolor=style.SURFACE, linewidth=1)
            bottom += v
        # low/high band for the assumed layer only (declared and parsed are fixed)
        base = bottom - np.array(pr["Assumed business hours"]["central"][sl])
        lo = base + np.array(pr["Assumed business hours"]["low"][sl])
        hi = base + np.array(pr["Assumed business hours"]["high"][sl])
        ax.vlines(x, lo, hi, color=style.INK2, linewidth=1, alpha=0.8)
        ax.set_title({"Tue": "Tuesday (typical weekday)", "Sun": "Sunday"}[day], loc="left")
        ax.set_xticks(range(0, 24, 3), [f"{h:02d}h" for h in range(0, 24, 3)])
        ax.grid(axis="x", visible=False)
        n3 = int(bottom[3])
        ax.annotate(f"{n3} at 03h", (3, n3), xytext=(3, n3 + 450), ha="center", fontsize=9,
                    color=style.INK, arrowprops=dict(arrowstyle="-", color=style.INK2, lw=0.8))
    axes[0].set_ylabel("Locations with an available AED")
    from matplotlib.lines import Line2D
    handles, labels = axes[0].get_legend_handles_labels()
    handles.append(Line2D([], [], color=style.INK2, lw=1))
    labels.append("Range if business hours are 10-17h … 8-20h")
    fig.legend(handles, labels, frameon=False, loc="upper left", bbox_to_anchor=(0.01, 0.93), ncol=4, fontsize=9)
    fig.suptitle(f"Paris AED locations available by hour ({summary['locations']:,} locations, Géo'DAE 2026-09-19)",
                 x=0.01, ha="left", fontsize=12, fontweight="bold")
    fig.text(0.01, 0.01, "Each location is counted once, under its most trusted open source. Bars use the central "
             "assumption (9-18h on the declared days; Mon-Fri when days are not stated).\nAll layers are operator "
             "declarations in Géo'DAE, not site checks. Hour h counts as open if the device is open at h:30.",
             fontsize=8, color=style.INK2)
    fig.tight_layout(rect=(0, 0.06, 1, 0.9))
    fig.savefig(OUT / "fig_02_locations_by_hour.png", dpi=160)


def bits(a: np.ndarray) -> str:
    return "".join("1" if x else "0" for x in a)


def main() -> None:
    p = pd.read_csv(INTERIM / "geodae_paris.csv", dtype=str, keep_default_na=False)
    c = p[p.usable == "True"].copy()
    recs = [classify(r) for r in c.itertuples()]
    for k in ("basis", "days_assumed", "parse_failed", "text_source", "text"):
        c[k] = [r[k] for r in recs]
    for band in BANDS:
        c[f"week_{band}"] = [bits(r[band]) for r in recs]
    cond_text = (c.c_disp_complt + " " + c.c_acc_complt).map(_fold)
    c["conditional_access"] = cond_text.str.contains(CONDITIONAL)
    c["strict_night_eligible"] = (c.c_acc == "Extérieur") | (c.c_acc_lib == "t")
    c["lat"], c["lon"] = c.lat.astype(float), c.lon.astype(float)

    keep = ["gid", "site_key", "in_paris", "lat", "lon", "c_nom", "c_adr_num", "c_adr_voie", "c_com_cp", "c_acc",
            "c_acc_lib", "tier", "basis", "days_assumed", "parse_failed", "conditional_access",
            "strict_night_eligible", "text_source", "text", "week_central", "week_low", "week_high"]
    c[keep].to_csv(INTERIM / "availability.csv", index=False)

    # Parse audit: every distinct string and how it was read (for human review).
    audit = c[c.text != ""].groupby(["text", "c_disp_j"], as_index=False).agg(
        n=("gid", "size"), basis=("basis", "first"), week=("week_central", "first"))

    def render(week: str) -> str:
        parts = []
        for d in range(7):
            day, runs, h = week[d * 24:(d + 1) * 24], [], 0
            while h < 24:
                if day[h] == "1":
                    e = h
                    while e + 1 < 24 and day[e + 1] == "1":
                        e += 1
                    runs.append(f"{h}-{e + 1}h")
                    h = e
                h += 1
            if runs:
                parts.append(f"{DAY_NAMES[d]} {' '.join(runs)}")
        return " | ".join(parts) or "never"
    audit["hours_at_h30"] = audit.week.map(render)
    audit.drop(columns="week").sort_values("n", ascending=False).to_csv(OUT / "02_parse_audit.csv", index=False)

    audit_all = audit
    c = c[c.in_paris == "True"]  # summaries and figure: Paris only (ring devices kept in the CSV for step 3)

    # Summary: devices and distinct locations available per hour.
    def per_hour(df, band):
        m = np.array([[ch == "1" for ch in w] for w in df[f"week_{band}"]], dtype=bool)
        dev = m.sum(0)
        loc = pd.DataFrame(m).groupby(df.site_key.values).any().sum(axis=0).to_numpy()
        return dev, loc

    summary = {"devices": len(c), "locations": int(c.site_key.nunique()),
               "basis_devices": c.basis.value_counts().to_dict(),
               "basis_locations": c.groupby("basis").site_key.nunique().to_dict(),
               "parse": {
                   "rows_with_time_text": int((c.text != "").sum()),
                   "parsed_ok": int((c.basis == "parsed_text").sum()),
                   "parse_failed_fell_back_to_assumed": int(c.parse_failed.sum()),
                   "text_from_c_acc_complt": int(((c.text_source == "c_acc_complt") & (c.basis == "parsed_text")).sum()),
                   "distinct_strings": int(c[c.text != ""].text.nunique()),
               },
               "days_assumed_mon_fri": int(c.days_assumed.sum()),
               "conditional_access_flag": int(c.conditional_access.sum()),
               "hourly": {}}
    for band in BANDS:
        dev, loc = per_hour(c, band)
        summary["hourly"][band] = {
            day: {"devices": dev[i * 24:(i + 1) * 24].tolist(), "locations": loc[i * 24:(i + 1) * 24].tolist()}
            for i, day in enumerate(DAY_NAMES)}
    # By basis, central band, for the figure.
    by_basis = {}
    for b, g in c.groupby("basis"):
        _, loc = per_hour(g, "central")
        by_basis[b] = loc.tolist()
    summary["hourly_locations_by_basis_central"] = by_basis
    (OUT / "02_availability.json").write_text(json.dumps(summary, indent=1, default=int))

    summary["priority_locations_by_hour"] = priority_counts(c)
    summary["strict_tue_03h_locations"] = strict_count(c, "Tue", 3)
    summary["strict_sun_03h_locations"] = strict_count(c, "Sun", 3)
    (OUT / "02_availability.json").write_text(json.dumps(summary, indent=1, default=int))
    figure(summary)

    t = summary["hourly"]
    print(json.dumps({k: v for k, v in summary.items() if k not in ("hourly", "hourly_locations_by_basis_central")},
                     indent=1, default=int))
    for band in BANDS:
        for day in ("Tue", "Sun"):
            print(band, day, "locations 03h:", t[band][day]["locations"][3], "12h:", t[band][day]["locations"][12],
                  "min/max:", min(t[band][day]["locations"]), max(t[band][day]["locations"]))


if __name__ == "__main__":
    main()
