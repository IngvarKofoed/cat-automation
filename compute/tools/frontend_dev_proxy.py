"""Local frontend dev server: serve the LOCAL web/ HTML, proxy everything else to the real compute PC.

Iterate on ``compute/api/web/{user,admin}/index.html`` on your dev box against the
REAL backend's data (cats, events, media, models) with a plain browser reload — no
backend changes, no data copy, no CORS. Because the frontend uses same-origin
absolute paths (``/api/...``, ``/media/...``), a same-origin reverse proxy is all it
takes: this server answers ``/`` and ``/admin`` from your local files (served
no-store, so an edit shows on refresh) and forwards every other request to
``CAT_COMPUTE_URL`` unchanged.

**Fully async + streaming**, and that is load-bearing, not incidental. The user
dashboard opens a long-lived **SSE** stream (``/api/events/stream``, a keepalive every
~21 s) and fires a burst of ``/media`` image requests when a visit's playback opens.
An earlier version proxied with a *blocking* client (``requests``) inside an ``async``
handler: reading the never-ending SSE body parked the single event loop forever, which
froze every other request and made Ctrl-C unresponsive (the loop was stuck in a C-level
socket read, so the interrupt couldn't be serviced). ``httpx.AsyncClient`` streams
responses without blocking the loop and pools connections, so the media burst reuses
sockets instead of a fresh TCP handshake per image (which over a high-latency link — a
Tailscale address, say — exhausts sockets/FDs and wedges the proxy).

Aimed at frontend-visual work that needs NO backend change. (Do not point real
automation at this — it's a dev convenience, not the app.)

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
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

_WEB = Path(__file__).resolve().parents[1] / "api" / "web"
_USER_HTML = _WEB / "user" / "index.html"
_ADMIN_HTML = _WEB / "admin" / "index.html"

_BACKEND = os.environ.get("CAT_COMPUTE_URL", "http://localhost:8001").rstrip("/")
_PORT = int(os.environ.get("CAT_DEV_PORT", "8080"))

# Request headers we must not forward verbatim: Host (httpx sets it for the upstream
# from the URL), Content-Length (recomputed from the body), Connection (hop-by-hop),
# and Accept-Encoding (we force `identity` below so the upstream replies uncompressed
# and its raw bytes stream straight through without any Content-Encoding juggling).
_DROP_REQ = {"host", "content-length", "connection", "accept-encoding"}
# Response headers we must not copy back: the framework sets its own length/framing,
# so a stale Content-Encoding/Length/Transfer-Encoding would corrupt the streamed body.
_DROP_RESP = {"content-encoding", "content-length", "transfer-encoding", "connection", "keep-alive"}

# One shared, pooled, ASYNC client for the whole proxy (see the module docstring for
# why async+streaming is essential). connect=5 s fails fast on a wrong/unreachable
# backend instead of hanging; the 65 s read timeout comfortably outlasts the SSE feed's
# ~21 s keepalive (so a live stream never trips it) yet still bounds a genuinely stalled
# upstream. Pool sizes cover the playback media burst.
_TIMEOUT = httpx.Timeout(65.0, connect=5.0)
_LIMITS = httpx.Limits(max_connections=100, max_keepalive_connections=40)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Open the pooled client for the server's lifetime and close it (and every pooled
    # connection) on shutdown.
    async with httpx.AsyncClient(timeout=_TIMEOUT, limits=_LIMITS, follow_redirects=False) as client:
        app.state.client = client
        yield


app = FastAPI(lifespan=_lifespan)


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
    client: httpx.AsyncClient = request.app.state.client
    url = f"{_BACKEND}/{full_path}"
    body = await request.body()
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _DROP_REQ}
    headers["accept-encoding"] = "identity"  # ask for uncompressed; aiter_bytes() decodes anyway if not honoured
    upstream_req = client.build_request(
        request.method,
        url,
        params=list(request.query_params.multi_items()),
        content=body or None,
        headers=headers,
    )
    try:
        # stream=True: don't buffer the body — hand back the open response and stream it
        # (media, and the endless SSE feed) as bytes arrive, never parking the event loop.
        upstream = await client.send(upstream_req, stream=True)
    except httpx.HTTPError as exc:
        # A clear message beats an opaque 500 when the compute URL is wrong/unreachable.
        return JSONResponse(
            {"detail": f"dev proxy: cannot reach compute backend at {_BACKEND} ({exc.__class__.__name__}: {exc})"},
            status_code=502,
        )
    resp_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in _DROP_RESP}
    # aiter_bytes() yields DECODED bytes, so even if an upstream compresses despite the
    # forced `Accept-Encoding: identity` we hand the client plain bytes to match the
    # stripped Content-Encoding header (aiter_raw would leak still-compressed bytes).
    # Close the upstream connection when the response finishes or the client disconnects
    # (e.g. the browser tears down its EventSource) — otherwise a stream would leak.
    return StreamingResponse(
        upstream.aiter_bytes(),
        status_code=upstream.status_code,
        headers=resp_headers,
        media_type=upstream.headers.get("content-type"),
        background=BackgroundTask(upstream.aclose),
    )


if __name__ == "__main__":
    import uvicorn

    print(f"[frontend-dev] serving local web/ at http://localhost:{_PORT}  (/ user, /admin workbench)")
    print(f"[frontend-dev] proxying everything else -> {_BACKEND}")
    # timeout_graceful_shutdown bounds how long Ctrl-C waits for in-flight requests.
    # Without it (uvicorn's default is wait-forever), the proxied SSE stream — which
    # never ends on its own — would keep a single Ctrl-C hanging; 5 s force-closes it.
    uvicorn.run(app, host="127.0.0.1", port=_PORT, log_level="warning", timeout_graceful_shutdown=5)
