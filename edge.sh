#!/usr/bin/env bash
#
# Start the edge (Raspberry Pi camera node) server.
#
# On first run this bootstraps a virtualenv at .venv from edge/requirements.txt;
# on later runs it just launches. Works from any directory. Override the port
# with CAT_EDGE_PORT (default 8000):
#
#   ./edge.sh                 # http://localhost:8000
#   CAT_EDGE_PORT=9000 ./edge.sh
#
# To rebuild the venv (e.g. after changing requirements), delete .venv and re-run.
#
# Pi CSI camera: Picamera2 is apt-installed (`sudo apt install python3-picamera2`)
# and a normal venv can't see it. On Linux this script builds the venv with
# --system-site-packages automatically so it can import picamera2 (off elsewhere,
# e.g. Mac dev), and rebuilds the venv if that setting changed. Override with
# EDGE_VENV_SYSTEM_SITE_PACKAGES=1 (force on) or =0 (force off).
set -euo pipefail

# This script lives at the repo root; run everything relative to it.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

VENV="$ROOT/.venv"
PY="$VENV/bin/python"

# Should the venv see system site-packages? Needed on the Pi so it can import the
# apt-installed python3-picamera2. Default on for Linux (the Pi), off elsewhere.
if [ "$(uname -s)" = "Linux" ]; then default_ssp=1; else default_ssp=0; fi
if [ "${EDGE_VENV_SYSTEM_SITE_PACKAGES:-$default_ssp}" = "1" ]; then want=true; else want=false; fi

# Rebuild if the venv is missing or its system-site-packages setting has changed
# (this is what previously required a manual `rm -rf .venv`).
if [ -x "$PY" ] && ! grep -q "include-system-site-packages = $want" "$VENV/pyvenv.cfg" 2>/dev/null; then
  echo "[edge] rebuilding .venv (system-site-packages -> $want)"
  rm -rf "$VENV"
fi

if [ ! -x "$PY" ]; then
  echo "[edge] creating virtualenv at .venv (system-site-packages=$want)"
  if [ "$want" = true ]; then
    python3 -m venv --system-site-packages "$VENV"
  else
    python3 -m venv "$VENV"
  fi
  "$VENV/bin/pip" install --upgrade pip >/dev/null
  "$VENV/bin/pip" install -r "$ROOT/edge/requirements.txt"
fi

PORT="${CAT_EDGE_PORT:-8000}"
echo "[edge] starting on http://localhost:${PORT}  (Ctrl-C to stop)"
# exec so Ctrl-C / SIGTERM go straight to the server process.
exec "$PY" -m edge.server.app
