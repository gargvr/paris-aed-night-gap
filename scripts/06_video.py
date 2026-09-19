"""Step 6 (presentation): animated map of the Paris AED day-night cycle.

Renders the real OSM pedestrian network of Paris, colouring each street by
whether an available AED is within a 200 m walk, hour by hour; then the night
scenarios, the residents-without-cover map, and the greedy candidate sites.

Every number shown comes from steps 3-5; nothing new is computed here except
the site-by-site coverage curve, which reuses the step-5 sites.

Distinct images are rendered once and held for a chosen duration by ffmpeg
(concat demuxer), so a 75 s film needs ~180 renders rather than 2,250.

Narration is spoken by Kokoro-82M (Apache-2.0) by default, or Piper (MIT)
with --piper; both run locally, nothing is sent to a cloud service. Scene
durations stretch to fit the speech.

Outputs: outputs/video/paris_aed_night_gap.mp4        (with narration)
         outputs/video/paris_aed_night_gap_silent.mp4 (frames only)
         outputs/video/narration.txt                  (the spoken script)
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
TTS = "kokoro"          # "kokoro" (more natural) or "piper" (smaller, faster)
KOKORO_VOICE = "af_heart"   # af_heart/am_michael (US), bf_emma/bm_george (UK)
KOKORO_SPEED = 0.96         # slightly under 1.0 reads as less rushed
PIPER_VOICE = "en_US-lessac-medium"
GAP_AFTER_LINE = 0.45  # seconds of silence after each narrated line

# The spoken script. Keys are frame names; a line plays from that frame on.
# Numbers are written out so the TTS reads them the way a person would.
NARRATION = {
    "title": "Paris has more than four thousand defibrillators on the public register. "
             "The real question is how many of them you can actually reach at three in the morning.",
    "hour00": "This is every Paris street within a two hundred metre walk of a defibrillator that is "
              "available at that hour, across one ordinary Tuesday.",
    "hour08": "Around eight, the city lights up, as offices, shops and pharmacies open.",
    "hour18": "In the evening it goes dark again. Almost all of this supply sits indoors, on daytime hours.",
    "night_declared": "At three in the morning, going by what operators themselves declare, about fifteen "
                      "percent of residents have one within reach.",
    "night_strict": "Count only the devices that are outdoors, or marked freely accessible, and it falls to "
                    "eleven percent.",
    "night_strict_unconditional": "Take out the ones behind a badge, a guard or an intercom, and about ten "
                                  "percent remain.",
    "gap": "That leaves roughly one point eight million residents with no defibrillator within a two hundred "
           "metre walk.",
    "sites001": "So where would new outdoor units, open around the clock, do the most good?",
    "sites025": "Twenty-five of them would lift night coverage from ten percent to sixteen.",
    "sites050": "Fifty take it to twenty-one percent.",
    "sites100": "A hundred reach twenty-nine. Even two hundred would still leave more than half of Paris "
                "uncovered at night.",
    "end": "These are declared opening hours, not checks on the ground, and this measures walking distance, "
           "not survival. The sites are candidates to survey. The data, the code and the limits are all published.",
}

CREDIT = ("Streets © OpenStreetMap contributors · AEDs: Géo'DAE (data.gouv.fr) 2026-09-19 · "
          "Residents: INSEE Filosofi 2021 · Availability is what operators declare, not a site check")


def build_frames() -> list[list]:
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
    plan: list[list] = []
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
        plan.append([p.name, dur, name])
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
    for gi, (dur, note) in enumerate(((2.5, ""), (3.0, "Darker cells: more people with no defibrillator within reach"))):
        fig, ax = new_fig()
        draw_map(ax, None, gap=gap)
        headline(fig, f"{uncovered_people / 1e6:.2f} million residents", note or "Tuesday 03:00 — nobody within a "
                 "200 m walk of a reachable defibrillator", f"{1 - base_share:.0%}",
                 "of residents with none in reach", right_color=INK)
        save(fig, dur, "gap" if gi == 0 else "gap_detail")

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


def make_speaker(engine: str):
    """Return (speak(text) -> float32 mono audio, sample_rate)."""
    if engine == "kokoro":
        import warnings

        warnings.filterwarnings("ignore")
        from kokoro import KPipeline

        pipe = KPipeline(lang_code="b" if KOKORO_VOICE.startswith("b") else "a",
                         repo_id="hexgrad/Kokoro-82M")

        def speak(text: str) -> np.ndarray:
            chunks = [g.audio.numpy() for g in pipe(text, voice=KOKORO_VOICE, speed=KOKORO_SPEED)]
            return np.concatenate(chunks).astype(np.float32)

        return speak, 24000

    import io
    import wave

    from piper import PiperVoice

    voice = PiperVoice.load(RAW / f"voices/{PIPER_VOICE}.onnx")

    def speak(text: str) -> np.ndarray:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            voice.synthesize_wav(text, w)
        buf.seek(0)
        with wave.open(buf) as w:
            return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768

    return speak, 22050


def synthesize(plan: list[list], engine: str = TTS) -> tuple[np.ndarray, int]:
    """Speak each narration line, stretch scenes to fit it, return the audio track."""
    speak, rate = make_speaker(engine)
    clips = []
    for i, (_, _, key) in enumerate(plan):
        if key not in NARRATION:
            continue
        audio = speak(NARRATION[key])
        clips.append((i, audio, len(audio) / rate))

    def starts():
        return np.concatenate([[0.0], np.cumsum([d for _, d, _ in plan])])

    for j, (i, _, length) in enumerate(clips):
        need = starts()[i] + length + GAP_AFTER_LINE
        if j + 1 < len(clips):
            nxt = clips[j + 1][0]
            short = need - starts()[nxt]
            if short > 0:
                plan[nxt - 1][1] += short
        elif need > starts()[-1]:
            plan[-1][1] += need - starts()[-1]

    total = starts()[-1]
    track = np.zeros(int(total * rate) + rate, dtype=np.float32)
    lines = []
    for i, audio, length in clips:
        at = starts()[i]
        track[int(at * rate):int(at * rate) + len(audio)] = audio
        lines.append(f"{int(at) // 60:d}:{at % 60:05.2f}  {NARRATION[plan[i][2]]}")
    peak = float(np.abs(track).max()) or 1.0
    track = (track * (0.89 * 32767 / peak)).astype(np.int16)  # leave ~1 dB of headroom
    (VID / "narration.txt").write_text(
        f"Narration script — {KOKORO_VOICE if engine == 'kokoro' else PIPER_VOICE} ({engine}, local TTS)\n\n" + "\n\n".join(lines) + "\n")
    return track, rate


def encode(plan: list[list], track=None, rate: int = 22050) -> Path:
    concat = VID / "concat.txt"
    with concat.open("w") as f:
        for name, dur, _ in plan:
            f.write(f"file 'frames/{name}'\nduration {dur:.3f}\n")
        f.write(f"file 'frames/{plan[-1][0]}'\n")  # ffmpeg needs the last frame twice
    silent = VID / "paris_aed_night_gap_silent.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(concat),
                    "-vf", f"fps={FPS},format=yuv420p", "-c:v", "libx264", "-crf", "20", "-preset", "medium",
                    "-movflags", "+faststart", str(silent)], check=True)
    if track is None:
        return silent
    import wave
    wav_path = VID / "narration.wav"
    with wave.open(str(wav_path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(track.tobytes())
    out = VID / "paris_aed_night_gap.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(silent), "-i", str(wav_path),
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-shortest",
                    "-movflags", "+faststart", str(out)], check=True)
    return out


if __name__ == "__main__":
    narrate = "--silent" not in sys.argv
    engine = "piper" if "--piper" in sys.argv else TTS
    plan = build_frames()
    track, rate = synthesize(plan, engine) if narrate else (None, 22050)
    out = encode(plan, track, rate)
    total = sum(d for _, d, _ in plan)
    print(f"{len(plan)} frames, {total:.1f}s -> {out} ({out.stat().st_size / 1e6:.1f} MB)")
