"""FastAPI app for the compute tier: browse collected frames, serve media, clear.

The integration hub for the frame-collection browser (the compute analogue of
the edge's Flask app + background grabber). It wires three things together behind
one HTTP server: the bounded ``Store``, the background collector thread that
fills it off the edge stream, and a small JSON+media API a vanilla-JS page browses
with. See ``docs/specs/2026-07-09-frame-collection-browser.md``.

``create_app`` is the injection seam, mirroring the edge's
``create_app(source_factory, start_grabber)``: tests pass an explicit ``store``
and ``start_collector=False`` to exercise the routes with no edge and no thread.
There is deliberately NO module-level app instance that would start a collector
thread on import — ``compute.sh`` launches ``uvicorn --factory ...:create_app``.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse

from compute.collection.collector import run_collector
from compute.collection.store import Store

_WEB_DIR = Path(__file__).resolve().parent / "web"
_INDEX_HTML = _WEB_DIR / "index.html"

# Config via environment variables (the edge's style; the compute tier has no
# config store yet). CAT_PI_URL is read by EdgeClient itself, not here.
_ENV_DIR = "CAT_COLLECT_DIR"
_ENV_MAX_BYTES = "CAT_COLLECT_MAX_BYTES"
_DEFAULT_DIR = "./data/collection"
_DEFAULT_MAX_BYTES = 5368709120  # 5 GiB — ~2 h at 10 fps, a testing window

# Browse-page limit: default 200 rows, hard-capped so one request can't ask the
# server to marshal an unbounded page. The cap is generous because the browse
# grid lazy-loads images off-screen — the per-row JSON is tiny and only visible
# thumbnails actually fetch, so a big page is cheap to serve.
_DEFAULT_LIMIT = 200
_MAX_LIMIT = 1000


def _store_from_env() -> Store:
    """Build a ``Store`` under ``CAT_COLLECT_DIR`` with the ``index.db`` + media/ split.

    The DB lives beside the media dir but NOT inside it, so ``Store.clear`` (which
    only touches media files) can never race the DB file.
    """
    root = os.environ.get(_ENV_DIR, _DEFAULT_DIR)
    try:
        max_bytes = int(os.environ.get(_ENV_MAX_BYTES, _DEFAULT_MAX_BYTES))
    except ValueError:
        max_bytes = _DEFAULT_MAX_BYTES
    return Store(
        db_path=os.path.join(root, "index.db"),
        media_root=os.path.join(root, "media"),
        max_bytes=max_bytes,
    )


def create_app(*, store=None, client=None, start_collector: bool = True) -> FastAPI:
    """Build the FastAPI app.

    ``store`` defaults to a ``Store`` built from the environment. When
    ``start_collector`` is true, ``client`` defaults to an ``EdgeClient()`` built
    from ``CAT_PI_URL`` and a daemon thread is started to pump its reconnecting
    stream into the store. Tests pass an explicit ``store`` and
    ``start_collector=False`` so no edge connection and no thread are created.
    """
    store = store if store is not None else _store_from_env()
    app = FastAPI()
    app.state.store = store

    if start_collector:
        if client is None:
            # Import lazily so the module (and tests) load without the ingest
            # client's transitive deps (requests) when the collector is off.
            from compute.ingest import EdgeClient

            client = EdgeClient()
        stop_event = threading.Event()
        app.state.collector_stop = stop_event
        thread = threading.Thread(
            target=run_collector,
            args=(client, store, stop_event),
            name="collector",
            daemon=True,
        )
        app.state.collector_thread = thread
        thread.start()

        @app.on_event("shutdown")
        def _stop_collector() -> None:
            # Signal the loop to stop between frames; the thread is a daemon so it
            # won't block process exit even mid-reconnect-backoff.
            stop_event.set()

    @app.get("/")
    def index():
        # Served by path (the frontend agent owns web/index.html); 404 until it
        # exists so a missing frontend is an obvious not-found, not a crash.
        if not _INDEX_HTML.is_file():
            raise HTTPException(status_code=404, detail="browse UI not built")
        return FileResponse(_INDEX_HTML, media_type="text/html")

    @app.get("/api/frames")
    def api_frames(
        cursor: "str | None" = Query(default=None),
        limit: int = Query(default=_DEFAULT_LIMIT),
        motion: str = Query(default="all"),
        order: str = Query(default="time"),
    ):
        # cursor is an OPAQUE keyset token from a prior page's next_cursor (the
        # store parses it per order; a malformed one → 400 via the ValueError
        # path below). Clamp the limit rather than reject it — a client asking
        # for more just gets the cap.
        limit = max(1, min(limit, _MAX_LIMIT))
        try:
            rows, next_cursor = store.query(cursor, limit, motion, order)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"frames": rows, "next_cursor": next_cursor}

    @app.get("/api/stats")
    def api_stats():
        return store.stats()

    @app.get("/media/{frame_id}")
    def media(frame_id: int):
        path = store.path_for(frame_id)
        # Unknown row OR an evicted/missing file → 404. path_for resolves a stale
        # row to where its file would be, so existence is checked here.
        if path is None or not os.path.isfile(path):
            raise HTTPException(status_code=404, detail="frame not found")
        return FileResponse(path, media_type="image/jpeg")

    @app.post("/api/clear")
    def api_clear():
        deleted = store.clear()
        return JSONResponse({"ok": True, "deleted": deleted})

    return app
