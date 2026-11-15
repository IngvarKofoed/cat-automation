# Frontend dev proxy serves local pages against the live backend

<!-- entry 83 -->

Added a frontend dev proxy (`./frontend-dev.sh` → `compute/tools/frontend_dev_proxy.py`): serves the
LOCAL `web/{user,admin}/index.html` (no-store, so edit→refresh) and reverse-proxies every other
request to the real compute PC (`CAT_COMPUTE_URL`, default :8001). Iterate dashboard visuals on the
dev box against live data — no backend change, no CORS (the frontend uses same-origin absolute paths),
no data copy. Dev convenience only; reuses `.venv-compute` (fastapi+uvicorn+requests, already deps).
