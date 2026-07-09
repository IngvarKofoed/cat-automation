#!/usr/bin/env bash
#
# Start the compute (NVIDIA PC "brain") frame-collection browser.
#
# On first run this bootstraps a virtualenv at .venv-compute from
# compute/requirements.txt; on later runs it just launches. Works from any
# directory. It always-on collects every frame off the edge stream into a bounded
# local store and serves a web UI to browse them.
#
#   ./compute.sh                           # edge defaults to localhost:8000
#   ./compute.sh catpi.local:8000          # edge as an argument (http:// added if omitted)
#   ./compute.sh http://catpi.local:8000   # ...or a full URL
#   CAT_COLLECT_PORT=9001 ./compute.sh     # different web port
#
# A DISTINCT venv dir (.venv-compute) is used so it never clobbers the edge's
# .venv when both tiers are checked out on one dev box (in production they run on
# different hosts). To rebuild it, delete .venv-compute and re-run.
#
# Env:
#   CAT_PI_URL             edge base URL (default http://localhost:8000; the optional 1st arg wins over it)
#   CAT_COLLECT_DIR        store root      (default ./data/collection)
#   CAT_COLLECT_MAX_BYTES  retention cap   (default 5368709120 = 5 GiB)
#   CAT_COLLECT_PORT       web port        (default 8001; the edge uses 8000)
set -euo pipefail

# This script lives at the repo root; run everything relative to it.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

VENV="$ROOT/.venv-compute"
PY="$VENV/bin/python"

if [ ! -x "$PY" ]; then
  echo "[compute] creating virtualenv at .venv-compute"
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install --upgrade pip >/dev/null
  "$VENV/bin/pip" install -r "$ROOT/compute/requirements.txt"
fi

PORT="${CAT_COLLECT_PORT:-8001}"
# The edge the collector connects to, in precedence order: the optional 1st
# positional arg, then the CAT_PI_URL env var, then the edge on THIS host (handy
# for tuning straight on the Pi). Guard $1 under `set -u` via $#. A bare
# host[:port] with no scheme gets http:// prepended so `./compute.sh catpi.local:8000`
# just works (EdgeClient needs a scheme).
if [ "$#" -ge 1 ]; then
  CAT_PI_URL="$1"
fi
CAT_PI_URL="${CAT_PI_URL:-http://localhost:8000}"
case "$CAT_PI_URL" in
  *://*) ;;
  *) CAT_PI_URL="http://$CAT_PI_URL" ;;
esac
export CAT_PI_URL
echo "[compute] edge stream: ${CAT_PI_URL}"
echo "[compute] store:       ${CAT_COLLECT_DIR:-./data/collection}  (cap ${CAT_COLLECT_MAX_BYTES:-5368709120} bytes)"
echo "[compute] browse UI:   http://localhost:${PORT}   (Ctrl-C to stop)"
# --factory: create_app() builds the store and starts the collector thread; there
# is no module-level app that would start a thread on import. exec so Ctrl-C /
# SIGTERM go straight to uvicorn.
exec "$PY" -m uvicorn --factory compute.api.app:create_app --host 0.0.0.0 --port "$PORT"
