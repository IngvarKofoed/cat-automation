"""Local frontend dev server: serve the LOCAL web/ HTML, proxy everything else to the real compute PC.

Iterate on ``compute/api/web/{user,admin}/index.html`` on your dev box against the
REAL backend's data (cats, events, media, models) with a plain browser reload — no
backend changes, no data copy, no CORS. Because the frontend uses same-origin
absolute paths (``/api/...``, ``/media/...``), a same-origin reverse proxy is all it
takes: this server answers ``/`` and ``/admin`` from your local files (served
no-store, so an edit shows on refresh) and forwards every other request to
``CAT_COMPUTE_URL`` unchanged.

Aimed at frontend-visual work that needs NO backend change. The user page uses only
plain request/response endpoints (events, cats, media, avatar upload), which forward
cleanly; there is no SSE/streaming on this path, so a buffering ``requests`` proxy is
enough. (Do not point real automation at this — it's a dev convenience, not the app.)

Usage (from the repo root, after ``./compute.sh`` has built ``.venv-compute`` once):

    ./frontend-dev.sh http://<compute-pc-ip>:8001      # backend as an arg
    CAT_COMPUTE_URL=http://<compute-pc-ip>:8001 .venv-compute/bin/python compute/tools/frontend_dev_proxy.py

Then open http://localhost:8080/ (user) or http://localhost:8080/admin (workbench).

Env:
    CAT_COMPUTE_URL   real compute backend base URL (default http://localhost:8001)
    CAT_DEV_PORT      local port to serve on          (default 8080)
"""
from __future__ import annotations

import os
from pathlib import Path

import requests
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response

_WEB = Path(__file__).resolve().parents[1] / "api" / "web"
_USER_HTML = _WEB / "user" / "index.html"
_ADMIN_HTML = _WEB / "admin" / "index.html"

_BACKEND = os.environ.get("CAT_COMPUTE_URL", "http://localhost:8001").rstrip("/")
_PORT = int(os.environ.get("CAT_DEV_PORT", "8080"))

# Request headers we must not forward verbatim: Host (requests sets it for the
# upstream), Content-Length (recomputed from the body), Connection (hop-by-hop),
# and Accept-Encoding (drop it so the backend replies uncompressed — requests then
# hands us plain bytes and we needn't juggle Content-Encoding).
_DROP_REQ = {"host", "content-length", "connection", "accept-encoding"}
# Response headers we must not copy back: the body is already decoded and the
# framework sets its own length, so a stale Content-Encoding/Length would corrupt it.
_DROP_RESP = {"content-encoding", "content-length", "transfer-encoding", "connection", "keep-alive"}

app = FastAPI()


def _serve_local(path: Path, what: str) -> Response:
    if not path.is_file():
        return JSONResponse({"detail": f"{what} not found at {path}"}, status_code=404)
    # no-store: a local edit shows on a plain refresh, without cache-busting.
    return FileResponse(path, media_type="text/html", headers={"Cache-Control": "no-store"})


@app.get("/")
def user_index() -> Response:
    return _serve_local(_USER_HTML, "user page")


@app.get("/admin")
def admin_index() -> Response:
    return _serve_local(_ADMIN_HTML, "admin page")


@app.api_route(
    "/{full_path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
)
async def proxy(full_path: str, request: Request) -> Response:
    # Everything that isn't a locally-served page (/, /admin) is reverse-proxied to
    # the real backend: same method, query string, headers, and body.
    url = f"{_BACKEND}/{full_path}"
    body = await request.body()
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _DROP_REQ}
    try:
        upstream = requests.request(
            request.method,
            url,
            params=list(request.query_params.multi_items()),
            data=body if body else None,
            headers=headers,
            timeout=30,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        # A clear message beats an opaque 500 when the compute URL is wrong/unreachable.
        return JSONResponse(
            {"detail": f"dev proxy: cannot reach compute backend at {_BACKEND} ({exc.__class__.__name__}: {exc})"},
            status_code=502,
        )
    resp_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in _DROP_RESP}
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=resp_headers,
        media_type=upstream.headers.get("content-type"),
    )


if __name__ == "__main__":
    import uvicorn

    print(f"[frontend-dev] serving local web/ at http://localhost:{_PORT}  (/ user, /admin workbench)")
    print(f"[frontend-dev] proxying everything else -> {_BACKEND}")
    uvicorn.run(app, host="127.0.0.1", port=_PORT, log_level="warning")
