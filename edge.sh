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
set -euo pipefail

# This script lives at the repo root; run everything relative to it.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

VENV="$ROOT/.venv"
PY="$VENV/bin/python"

if [ ! -x "$PY" ]; then
  echo "[edge] creating virtualenv at .venv"
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install --upgrade pip >/dev/null
  "$VENV/bin/pip" install -r "$ROOT/edge/requirements.txt"
fi

PORT="${CAT_EDGE_PORT:-8000}"
echo "[edge] starting on http://localhost:${PORT}  (Ctrl-C to stop)"
# exec so Ctrl-C / SIGTERM go straight to the server process.
exec "$PY" -m edge.server.app
