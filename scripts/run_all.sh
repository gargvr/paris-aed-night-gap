#!/usr/bin/env bash
# Runs the whole pipeline in order. Downloads (~600 MB) happen on first run only.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-.venv/bin/python}
$PY -m pytest -q tests
for s in 01_fetch_and_audit 00_fetch_walk_graph 02_availability 03_coverage 04_population 05_greedy_sites; do
  echo "=== $s"
  $PY "scripts/$s.py"
done
echo "=== 06_video (optional, needs ffmpeg)"
command -v ffmpeg >/dev/null && $PY scripts/06_video.py || echo "ffmpeg not found, skipping video"
