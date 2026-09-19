"""Turn Géo'DAE availability fields into a weekly availability grid.

A week is 7 x 1440 minutes, Monday 00:00 first. Hourly availability is the
state at h:30 (midpoint sampling), so "9h/18h" -> hours 9..17.

Three sources, in decreasing order of trust:
  declared  structured fields say 24h/24 (every day, or on the listed days)
  parsed    free text with explicit times ("L au V 8h30/12h30 13h30/17h30")
  assumed   only the category "heures ouvrables" / "heures de nuit": we
            impose a window (BANDS) on the declared days. This is an ESTIMATE.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

import numpy as np

MIN_PER_DAY, DAYS = 1440, 7
WEEKDAYS = frozenset(range(5))
ALL = frozenset(range(7))

# Assumed "heures ouvrables" windows (minutes). Central, narrow, wide.
BANDS = {"central": (9 * 60, 18 * 60), "low": (10 * 60, 17 * 60), "high": (8 * 60, 20 * 60)}
# Assumed "heures de nuit" window when no times are given: 20h -> 8h.
NIGHT_WINDOW = (20 * 60, 8 * 60)

DAY_WORDS = {
    **dict.fromkeys(["l", "lu", "lun", "lundi", "mo"], 0),
    **dict.fromkeys(["ma", "mar", "mardi", "tu"], 1),
    **dict.fromkeys(["me", "mer", "mercredi", "we"], 2),
    **dict.fromkeys(["j", "je", "jeu", "jeudi", "th"], 3),
    **dict.fromkeys(["v", "ve", "ven", "vendredi", "fr"], 4),
    **dict.fromkeys(["s", "sa", "sam", "samedi"], 5),
    **dict.fromkeys(["d", "di", "dim", "dimanche", "su"], 6),
}
FIELD_DAYS = {"lundi": 0, "mardi": 1, "mercredi": 2, "jeudi": 3, "vendredi": 4, "samedi": 5, "dimanche": 6}
TIME_HINT = re.compile(r"\d{1,2}\s*[hH:]")
CONDITIONAL = re.compile(
    r"selon|sur appel|uniquement|residents?|personnel|badge|interphone|gardien|portier|ferme|"
    r"parfois|a recuperer|poste de securite|\bpc\b",
)

# Hand-transcribed readings for strings the grammar gets wrong. Each was read
# by a person; keep this list short and reviewed.
OVERRIDES: dict[str, dict[int, list[tuple[int, int]]] | str] = {
    # grammar would let the resident-only 24h/24 clause win
    "7j/7 9h/18h (7j/7 24h/24 uniquement pour les résidents)": {d: [(540, 1080)] for d in ALL},
    # times before days, then "puis 17h le v et le s" (Fri/Sat close at 17h)
    "07h15 à 11h45 et 13h à 18h l au j puis 17h le v et le s": {
        **{d: [(435, 705), (780, 1080)] for d in range(4)},
        **{d: [(435, 705), (780, 1020)] for d in (4, 5)},
    },
    # "M" read as mardi; "13h3" read as 13h30
    "M au V 13h3/18h30 S D 13h30/19h": {
        **{d: [(810, 1110)] for d in range(1, 5)}, **{d: [(810, 1140)] for d in (5, 6)},
    },
    # "Du L V" read as a range, like "du L au V"
    "Du L V 9h30-17h30": {d: [(570, 1050)] for d in range(5)},
}
# A time directly followed by "du <day>": days come AFTER their times
# ("de 6h30 a 23h du lundi au vendredi et de 8h a 19h du samedi au dimanche").
SUFFIX_DAYS = re.compile(r"\d\s*h\s*\d*\s+du\s+(" + "|".join(sorted(DAY_WORDS, key=len, reverse=True)) + r")\b")


@dataclass
class Parsed:
    ok: bool
    schedule: dict[int, list[tuple[int, int]]] = field(default_factory=dict)  # day -> [(start,end)] minutes
    days_from_field: bool = False  # free text gave times but no days
    note: str = ""


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s.lower().replace("’", "'")).strip()


def normalise(s: str) -> str:
    s = _fold(s)
    s = re.sub(r"24\s*h?\s*(/|sur)\s*24\s*h?|\bh24\b", " FULLDAY ", s)
    s = re.sub(r"7\s*j\s*/\s*7\s*j?|\b7\s*/\s*7\b|tous les jours|toute l'annee", " ALLDAYS ", s)
    s = re.sub(r"\b(l|d|j|s|qu)'", " ", s)  # elisions: l'accueil must not read as Monday
    s = re.sub(r"\blau\b", "l au", s)
    s = re.sub(r"\bminuit\b", "24h", s)
    s = re.sub(r"\bmidi\b", "12h", s)
    s = re.sub(r"(\d{1,2})\s*h\s+(\d{2})\b", r"\1h\2", s)  # "9 h 00"
    s = re.sub(r"\b([a-z]{2,})(\d)", r"\1 \2", s)  # "me15h" -> "me 15h"
    # typos seen in the data
    s = re.sub(r"(\d{1,2})h/(\d{2})/", r"\1h\2/", s)  # 8h/30/12h30
    s = re.sub(r"(\d{1,2})\s*h\s*-\s*(\d{1,2})-(\d{2})\b", r"\1h-\2h\3", s)  # 8h-18-30
    s = re.sub(r"(\d{1,2})h(\d{1,2})h", r"\1h-\2h", s)  # 14h16h
    # bare hours next to a real time: "8-23h", "9h a 13 et"
    s = re.sub(r"\b(\d{1,2})\s*(-|/|a)\s*(\d{1,2})\s*h", r"\1h\2\3h", s)
    s = re.sub(r"(\d{1,2})\s*h(\d{2})?\s*(a|-|/)\s*(\d{1,2})\b(?![h:\d])", r"\1h\2\3\4h", s)
    return s


TOKEN = re.compile(r"FULLDAY|ALLDAYS|(\d{1,2})\s*[h:](\d{2})?|[a-z]+|-")


def tokenize(s: str) -> list[tuple[str, object]]:
    out: list[tuple[str, object]] = []
    for m in TOKEN.finditer(s):
        t = m.group(0)
        if t == "FULLDAY":
            out.append(("FULL", None))
        elif t == "ALLDAYS":
            out.append(("DAYS", ALL))
        elif m.group(1) is not None:
            h, mm = int(m.group(1)), int(m.group(2) or 0)
            if h > 24 or mm > 59:
                return [("BAD", t)]
            out.append(("T", h * 60 + mm))
        elif t in ("au", "a", "-"):
            out.append(("TO", None))
        elif t in DAY_WORDS:
            out.append(("D", DAY_WORDS[t]))
    return out


def _groups_suffix(tokens):
    """Times first, then the days they apply to."""
    groups, times, full, i = [], [], False, 0
    while i < len(tokens):
        kind, val = tokens[i]
        if kind == "T":
            times.append(val)
        elif kind == "FULL":
            full = True
        elif kind in ("D", "DAYS") and (times or full):
            days = set()
            while i < len(tokens) and tokens[i][0] in ("D", "DAYS", "TO"):
                k, v = tokens[i]
                if k == "D" and i + 2 < len(tokens) and tokens[i + 1][0] == "TO" and tokens[i + 2][0] == "D":
                    b = tokens[i + 2][1]
                    days |= {(v + n) % 7 for n in range(((b - v) % 7) + 1)}
                    i += 2
                elif k in ("D", "DAYS"):
                    days |= set(v) if k == "DAYS" else {v}
                i += 1
            groups.append((days, times, full))
            times, full = [], False
            continue
        i += 1
    if times or full:
        groups.append((None, times, full))
    return groups


def _groups(tokens):
    """Split into [(days|None, times, full)] groups; resolve 'L au V' ranges."""
    groups, days, times, full = [], None, [], False
    i = 0
    while i < len(tokens):
        kind, val = tokens[i]
        if kind in ("D", "DAYS"):
            if times or full:
                groups.append((days, times, full))
                days, times, full = None, [], False
            if kind == "D" and i + 2 < len(tokens) and tokens[i + 1][0] == "TO" and tokens[i + 2][0] == "D":
                a, b = val, tokens[i + 2][1]
                rng = {(a + k) % 7 for k in range(((b - a) % 7) + 1)}
                i += 2
            else:
                rng = set(val) if kind == "DAYS" else {val}
            days = (days or set()) | rng
        elif kind == "T":
            times.append(val)
        elif kind == "FULL":
            full = True
        i += 1
    groups.append((days, times, full))
    return groups


def parse_text(text: str, field_days: frozenset[int]) -> Parsed:
    if text.strip() in OVERRIDES:
        return Parsed(True, dict(OVERRIDES[text.strip()]), note="override")
    toks = tokenize(normalise(text))
    if any(k == "BAD" for k, _ in toks):
        return Parsed(False, note="bad time token")
    norm = normalise(text)
    groups = _groups_suffix(toks) if SUFFIX_DAYS.search(norm) else _groups(toks)
    # times-without-days followed by days-without-times -> "8h45 a 18h du L au V"
    merged = []
    for g in groups:
        if merged and merged[-1][0] is None and (merged[-1][1] or merged[-1][2]) and g[0] and not g[1] and not g[2]:
            merged[-1] = (g[0], merged[-1][1], merged[-1][2])
        else:
            merged.append(g)
    sched: dict[int, list[tuple[int, int]]] = {}
    prev_ranges: list[tuple[int, int]] | None = None
    used_field_days = False
    for days, times, full in merged:
        if not times and not full:
            continue  # days with no times, e.g. "+ S D selon evenements"
        if days is None:
            days, used_field_days = set(field_days), True
        if full:
            ranges = [(0, MIN_PER_DAY)]
        elif len(times) == 1 and prev_ranges:
            # "L au J 8h30/12h30 13h30/17h30 et V 16h30": same day, earlier close
            ranges = prev_ranges[:-1] + [(prev_ranges[-1][0], times[0])]
        elif len(times) % 2 == 0:
            ranges = list(zip(times[::2], times[1::2]))
        else:
            return Parsed(False, note=f"odd number of times {times}")
        for d in days:
            sched[d] = ranges  # later groups override earlier ones for the same day
        prev_ranges = ranges
    if not sched:
        return Parsed(False, note="no time ranges")
    if used_field_days and not field_days:
        return Parsed(False, note="times but no days anywhere")
    return Parsed(True, sched, days_from_field=used_field_days)


def field_days(disp_j: frozenset[str]) -> frozenset[int] | None:
    """Days from c_disp_j. None = not stated. Events-only -> empty set."""
    if "7j/7" in disp_j:
        return ALL
    named = frozenset(FIELD_DAYS[t] for t in disp_j if t in FIELD_DAYS)
    if named:
        return named
    if "événements" in disp_j:
        return frozenset()
    return None


def schedule_to_week(sched: dict[int, list[tuple[int, int]]]) -> np.ndarray:
    week = np.zeros(DAYS * MIN_PER_DAY, dtype=bool)
    for d, ranges in sched.items():
        for a, b in ranges:
            start = d * MIN_PER_DAY + a
            end = d * MIN_PER_DAY + (b if b > a else b + MIN_PER_DAY)  # past midnight wraps to next day
            idx = np.arange(start, end) % (DAYS * MIN_PER_DAY)
            week[idx] = True
    return week


def week_to_hours(week: np.ndarray) -> np.ndarray:
    """168 booleans: state at h:30."""
    return week.reshape(DAYS * 24, 60)[:, 30]


def window_schedule(days: frozenset[int], window: tuple[int, int]) -> dict[int, list[tuple[int, int]]]:
    return {d: [window] for d in days}


def pg_array(s: str) -> frozenset[str]:
    """Postgres array literal '{a,"b c"}' -> {'a','b c'}."""
    if not s:
        return frozenset()
    return frozenset(t.strip().strip('"') for t in s.strip("{}").split(",") if t.strip())
