"""Tests for the user dashboard's live-freshness backend (compute/api/app.py,
compute/collection/store.py):

- ``Store.activity_signal`` — the cheap motion-scoped change fingerprint the SSE
  endpoint samples.
- ``GET /api/events/stream`` — the SSE change-push (connected preamble + a push
  when a new motion frame lands).
- shell ``Cache-Control: no-cache`` and the ``/apple-touch-icon*`` PNG routes.

Frames are built directly as ``StreamFrame`` (no cv2/model/network), matching
``test_events.py``.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from compute.collection.store import Store
from compute.ingest import StreamFrame
from shared.wire import StreamFrameMeta

_JPEG_BODY = b"\xff\xd8\xff\xe0" + b"fake-jpeg-body" + b"\xff\xd9"


def _frame(frame_id=1, ts=1_000, motion=False, area=0.0) -> StreamFrame:
    meta = StreamFrameMeta(frame_id=frame_id, ts=ts, motion=motion, bbox=None, area=area)
    return StreamFrame(meta, _JPEG_BODY)


def _store(tmp_path) -> Store:
    return Store(
        db_path=str(tmp_path / "index.db"),
        media_root=str(tmp_path / "media"),
        max_bytes=10_000_000,
    )


# --- Store.activity_signal --------------------------------------------------


def test_activity_signal_empty_is_all_zero(tmp_path):
    assert _store(tmp_path).activity_signal() == {"motion_id": 0, "ident_rev": 0, "model_id": 0}


def test_activity_signal_tracks_motion_frames_only(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    # A non-motion frame must NOT move motion_id (continuous capture would otherwise
    # fire the signal every tick).
    store.add(_frame(frame_id=1, motion=False), recv_ts_ms=base)
    assert store.activity_signal()["motion_id"] == 0

    # A motion frame sets motion_id to its row id.
    mid = store.add(_frame(frame_id=2, motion=True, area=0.2), recv_ts_ms=base + 100)
    assert store.activity_signal()["motion_id"] == mid

    # A later non-motion frame leaves it put; a later motion frame advances it.
    store.add(_frame(frame_id=3, motion=False), recv_ts_ms=base + 200)
    assert store.activity_signal()["motion_id"] == mid
    mid2 = store.add(_frame(frame_id=4, motion=True, area=0.3), recv_ts_ms=base + 300)
    assert store.activity_signal()["motion_id"] == mid2 > mid


# --- routes -----------------------------------------------------------------


@pytest.fixture
def api_client(tmp_path):
    from compute.api.app import create_app

    store = _store(tmp_path)
    app = create_app(store=store, start_collector=False)
    return TestClient(app), store


def test_index_shell_is_no_cache(api_client):
    client, _store = api_client
    resp = client.get("/")
    # The shell exists in the repo, so this is 200 with the revalidation header.
    assert resp.status_code == 200
    assert resp.headers.get("cache-control") == "no-cache"


@pytest.mark.parametrize("path", ["/apple-touch-icon.png", "/apple-touch-icon-precomposed.png"])
def test_apple_touch_icon_served(api_client, path):
    client, _store = api_client
    resp = client.get(path)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert "max-age" in resp.headers.get("cache-control", "")
    assert resp.content[:8] == b"\x89PNG\r\n\x1a\n"  # real PNG magic


def test_events_stream_connects_and_pushes_on_new_motion(api_client, monkeypatch):
    """The SSE stream sends the connected preamble and pushes 'activity' on a change.

    Driven directly against the ASGI app (not TestClient.stream) because the endpoint
    is an INFINITE generator: this harness owns the receive channel and cancels the
    task in ``finally``, so the test can never hang on teardown the way a buffered
    streaming client would.
    """
    import compute.api.app as appmod

    monkeypatch.setattr(appmod, "_SSE_POLL_SECONDS", 0.01)
    client, store = api_client
    app = client.app
    base = 1_700_000_000_000

    async def drive():
        scope = {
            "type": "http", "http_version": "1.1", "method": "GET",
            "path": "/api/events/stream", "raw_path": b"/api/events/stream",
            "query_string": b"", "headers": [], "scheme": "http",
            "client": ("test", 1), "server": ("test", 80),
        }
        disconnect = asyncio.Event()
        first = True
        start_status = {}
        start_headers = {}
        chunks: "list[bytes]" = []
        got_activity = asyncio.Event()

        async def receive():
            nonlocal first
            if first:
                first = False
                return {"type": "http.request", "body": b"", "more_body": False}
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.start":
                start_status["code"] = message["status"]
                start_headers.update({k.decode(): v.decode() for k, v in message["headers"]})
            elif message["type"] == "http.response.body":
                body = message.get("body", b"")
                if body:
                    chunks.append(body)
                    if b"event: activity" in body:
                        got_activity.set()

        task = asyncio.ensure_future(app(scope, receive, send))
        try:
            await asyncio.sleep(0.1)  # let the baseline sample run first
            store.add(_frame(frame_id=1, motion=True, area=0.2), recv_ts_ms=base)
            await asyncio.wait_for(got_activity.wait(), timeout=5.0)
        finally:
            # Guaranteed teardown: signal disconnect, then cancel and reap the task so
            # the infinite generator can't outlive the test.
            disconnect.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        assert start_status["code"] == 200
        assert start_headers["content-type"].startswith("text/event-stream")
        assert start_headers.get("cache-control") == "no-cache"
        joined = b"".join(chunks)
        assert joined.startswith(b": connected")
        assert b"event: activity" in joined

    asyncio.run(drive())
