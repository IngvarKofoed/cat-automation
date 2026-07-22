#!/usr/bin/env bash
# Local frontend dev proxy — edit the web/ HTML on your dev box, see it against the
# REAL compute PC's data. Serves compute/api/web/{user,admin}/index.html locally
# (no-store, so a refresh shows edits) and reverse-proxies /api + /media to the real
# backend. Frontend-only; no backend changes. See compute/tools/frontend_dev_proxy.py.
#
# Usage:
#   ./frontend-dev.sh http://<compute-pc-ip>:8001    # backend as an arg (scheme optional)
#   CAT_COMPUTE_URL=http://host:8001 ./frontend-dev.sh
#   CAT_DEV_PORT=9090 ./frontend-dev.sh              # different local port (default 8080)
#
# Then open http://localhost:8080/ (user) or http://localhost:8080/admin (workbench).
set -euo pipefail
cd "$(dirname "$0")"

# Reuse the compute venv (run ./compute.sh once to create it — it already has
# fastapi + uvicorn + requests, which is all the proxy needs).
PY=".venv-compute/bin/python"
[ -x "$PY" ] || { echo "[frontend-dev] $PY missing — run ./compute.sh once to build .venv-compute"; exit 1; }

# Backend URL: positional arg > CAT_COMPUTE_URL > localhost:8001. A bare host[:port]
# gets http:// prepended, mirroring compute.sh's CAT_PI_URL handling.
if [ "$#" -ge 1 ]; then CAT_COMPUTE_URL="$1"; fi
CAT_COMPUTE_URL="${CAT_COMPUTE_URL:-http://localhost:8001}"
case "$CAT_COMPUTE_URL" in
  http://*|https://*) ;;
  *) CAT_COMPUTE_URL="http://$CAT_COMPUTE_URL" ;;
esac
export CAT_COMPUTE_URL
export CAT_DEV_PORT="${CAT_DEV_PORT:-8080}"

echo "[frontend-dev] backend:      $CAT_COMPUTE_URL"
echo "[frontend-dev] local server: http://localhost:${CAT_DEV_PORT}   (/ user, /admin workbench; Ctrl-C to stop)"
exec "$PY" compute/tools/frontend_dev_proxy.py
