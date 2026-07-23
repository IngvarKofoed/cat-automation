"""Tests for the compute ingest client (compute/ingest/client.py).

Two layers, both runnable without a camera or a GPU:

- **Unit.** Feed ``iter_stream`` a CANNED multipart byte stream built with
  ``shared.wire.format_part_headers`` — so the producer and the consumer agree by
  construction — through a fake ``requests`` response, and assert the yielded
  ``StreamFrame.meta``/``.jpeg`` for both a motion-active part (with bbox) and an
  inactive one. Plus the constructor's config-error contract, ``get_status``
  parsing, and ``EdgeUnavailable`` on a connection error.
- **Integration.** Run the REAL edge Flask app in-process over
  ``FakeCaptureSource`` on an ephemeral port and point a real ``EdgeClient`` at
  it, exercising the whole HTTP path end to end (no camera, no GPU).

``cv2``/``numpy`` are only needed for the integration test's ``.image`` decode
(``numpy`` is imported inside that test, not at module top); the unit tests
deliberately never touch ``.image``, matching the client's lazy-decode design, so
they collect and run with only ``requests`` installed.
"""
from __future__ import annotations

import io

import pytest
import requests

from compute.ingest import EdgeClient, EdgeUnavailable, StreamFrame
from shared import wire
from shared.wire import StreamFrameMeta


# --- Test doubles for the requests layer ------------------------------------


class _FakeRaw(io.RawIOBase):
    """A minimal RawIOBase over fixed bytes, standing in for ``resp.raw``.

    ``io.BufferedReader`` (which the client wraps ``resp.raw`` in) needs a
    readable raw stream exposing ``readinto``; this is the smallest thing that
    satisfies that so we can drive the parser off canned bytes.
    """

    def __init__(self, data: bytes) -> None:
        super().__init__()
        self._buf = io.BytesIO(data)

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        chunk = self._buf.read(len(b))
        b[: len(chunk)] = chunk
        return len(chunk)


class _FakeResponse:
    """A stand-in for a streamed ``requests`` response."""

    def __init__(self, data: bytes = b"", status_code: int = 200, json_obj=None) -> None:
        self.raw = _FakeRaw(data)
        self.status_code = status_code
        self._json_obj = json_obj

    def json(self):
        if self._json_obj is None:
            raise ValueError("no JSON body")
        return self._json_obj

    def close(self) -> None:
        pass


def _canned_part(meta: StreamFrameMeta, jpeg: bytes) -> bytes:
    """Build one multipart part exactly as the edge does (headers + body + CRLF).

    Uses the shared serializer so this fixture and the client's parser bind to the
    same wire definition — the point of ``shared/wire.py``.
    """
    return wire.format_part_headers(meta, len(jpeg)) + jpeg + b"\r\n"


# --- Unit: stream parsing ---------------------------------------------------


def test_iter_stream_parses_motion_active_and_inactive_parts(monkeypatch):
    active = StreamFrameMeta(
        frame_id=7, ts=1234, motion=True, bbox=(0.1, 0.2, 0.3, 0.4), area=0.05
    )
    inactive = StreamFrameMeta(
        frame_id=8, ts=1235, motion=False, bbox=None, area=0.0
    )
    body_a = b"\xff\xd8active-jpeg\xff\xd9"
    body_b = b"\xff\xd8inactive-jpeg\xff\xd9"
    stream_bytes = _canned_part(active, body_a) + _canned_part(inactive, body_b)

    def fake_get(url, **kwargs):
        assert url.endswith("/stream")
        assert kwargs.get("stream") is True
        return _FakeResponse(data=stream_bytes)

    monkeypatch.setattr(requests, "get", fake_get)

    client = EdgeClient(base_url="http://pi.test:8000")
    # A real /stream never ends cleanly, so the client raises EdgeUnavailable when
    # the canned bytes run out (EOF == the edge closed the connection). Collect the
    # frames delivered before that drop — both parts must come through first.
    frames = []
    with pytest.raises(EdgeUnavailable):
        for frame in client.iter_stream():
            frames.append(frame)

    assert len(frames) == 2

    assert isinstance(frames[0], StreamFrame)
    assert frames[0].meta == active
    assert frames[0].jpeg == body_a
    # bbox survives the float round-trip through the wire exactly.
    assert frames[0].meta.bbox == (0.1, 0.2, 0.3, 0.4)

    assert frames[1].meta == inactive
    assert frames[1].jpeg == body_b
    assert frames[1].meta.bbox is None


def test_iter_stream_non_200_raises_unavailable(monkeypatch):
    monkeypatch.setattr(
        requests, "get", lambda url, **kw: _FakeResponse(status_code=503)
    )
    client = EdgeClient(base_url="http://pi.test:8000")
    with pytest.raises(EdgeUnavailable):
        list(client.iter_stream())


def test_iter_stream_connection_error_raises_unavailable(monkeypatch):
    def boom(url, **kwargs):
        raise requests.exceptions.ConnectionError("refused")

    monkeypatch.setattr(requests, "get", boom)
    client = EdgeClient(base_url="http://pi.test:8000")
    with pytest.raises(EdgeUnavailable):
        list(client.iter_stream())


def test_iter_stream_corrupt_header_raises_unavailable(monkeypatch):
    # A part whose X-Frame-Id is non-integer: parse_part_headers raises
    # WireParseError, which the client treats like a stall → EdgeUnavailable.
    corrupt = (
        b"--frame\r\n"
        b"Content-Type: image/jpeg\r\n"
        b"Content-Length: 3\r\n"
        b"X-Frame-Id: not-a-number\r\n"
        b"X-Timestamp: 1\r\n"
        b"X-Motion: 0\r\n"
        b"X-Area: 0.0\r\n"
        b"\r\n"
        b"abc\r\n"
    )
    monkeypatch.setattr(requests, "get", lambda url, **kw: _FakeResponse(data=corrupt))
    client = EdgeClient(base_url="http://pi.test:8000")
    with pytest.raises(EdgeUnavailable):
        list(client.iter_stream())


# --- Unit: construction -----------------------------------------------------


def test_missing_base_url_raises_config_error(monkeypatch):
    monkeypatch.delenv("CAT_PI_URL", raising=False)
    with pytest.raises(ValueError):
        EdgeClient(base_url=None)


def test_base_url_from_env(monkeypatch):
    monkeypatch.setenv("CAT_PI_URL", "http://from-env:9000/")
    client = EdgeClient()
    # Trailing slash normalized away so route joins don't double up.
    assert client.base_url == "http://from-env:9000"


def test_explicit_base_url_beats_env(monkeypatch):
    monkeypatch.setenv("CAT_PI_URL", "http://from-env:9000")
    client = EdgeClient(base_url="http://explicit:1234")
    assert client.base_url == "http://explicit:1234"


# --- Unit: status -----------------------------------------------------------


def test_get_status_parses_json(monkeypatch):
    payload = {
        wire.FIELD_FRAME_ID: 42,
        wire.FIELD_TS: 99999,
        wire.FIELD_MOTION: True,
        wire.FIELD_BBOX: [0.1, 0.2, 0.3, 0.4],
        wire.FIELD_AREA: 0.07,
        wire.FIELD_CAMERA_OK: True,
        wire.FIELD_LAST_ERROR: None,
        wire.FIELD_VERSION: "v0.1.0",
        wire.FIELD_SYSTEM: {"cpu_percent": 12.5, "mem_percent": 40.0},
    }

    def fake_get(url, **kwargs):
        assert url.endswith("/status")
        return _FakeResponse(status_code=200, json_obj=payload)

    monkeypatch.setattr(requests, "get", fake_get)
    client = EdgeClient(base_url="http://pi.test:8000")
    snap = client.get_status()

    assert snap.frame_id == 42
    assert snap.motion is True
    assert snap.bbox == (0.1, 0.2, 0.3, 0.4)  # JSON list → tuple
    assert snap.camera_ok is True
    assert snap.version == "v0.1.0"
    assert snap.system == {"cpu_percent": 12.5, "mem_percent": 40.0}


def test_get_status_connection_error_raises_unavailable(monkeypatch):
    def boom(url, **kwargs):
        raise requests.exceptions.ConnectionError("refused")

    monkeypatch.setattr(requests, "get", boom)
    client = EdgeClient(base_url="http://pi.test:8000")
    with pytest.raises(EdgeUnavailable):
        client.get_status()


def test_get_status_non_200_raises_unavailable(monkeypatch):
    monkeypatch.setattr(
        requests, "get", lambda url, **kw: _FakeResponse(status_code=500)
    )
    client = EdgeClient(base_url="http://pi.test:8000")
    with pytest.raises(EdgeUnavailable):
        client.get_status()


# --- Integration: the real edge app in-process ------------------------------


@pytest.fixture
def edge_server(tmp_path, monkeypatch):
    """Run the real edge Flask app over FakeCaptureSource on an ephemeral port.

    Uses werkzeug's ``make_server`` on port 0 in a daemon thread so the test owns
    a genuine HTTP endpoint to point EdgeClient at — no camera, no GPU. The
    grabber runs, so it populates the latest-frame slot on its own. Config is
    pointed at a tmp file so no real settings.json is touched.
    """
    from werkzeug.serving import make_server

    from edge.capture.fake_source import FakeCaptureSource
    from edge.server.app import create_app

    monkeypatch.setenv("CAT_EDGE_CONFIG", str(tmp_path / "settings.json"))

    app = create_app(
        source_factory=lambda device: FakeCaptureSource(device),
        start_grabber=True,
        start_watchdog=False,  # real grabber, but don't arm os._exit inside pytest
    )
    server = make_server("127.0.0.1", 0, app, threaded=True)
    host, port = server.server_address
    import threading

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=5.0)
        app.grabber.stop()


def _wait_for_first_frame(client: EdgeClient, timeout: float = 10.0) -> None:
    """Poll /status until the grabber has published a frame (frame_id > 0)."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            snap = client.get_status()
        except EdgeUnavailable:
            snap = None
        if snap is not None and snap.frame_id > 0:
            return
        time.sleep(0.05)
    raise AssertionError("edge produced no frame within the timeout")


def test_integration_status_and_stream(edge_server):
    import numpy as np

    client = EdgeClient(base_url=edge_server)
    _wait_for_first_frame(client)

    snap = client.get_status()
    assert snap.camera_ok is True
    assert snap.frame_id > 0

    # Pull exactly one frame off the live stream and confirm it decodes.
    for frame in client.iter_stream():
        assert isinstance(frame, StreamFrame)
        assert frame.meta.frame_id > 0
        img = frame.image
        assert isinstance(img, np.ndarray)
        assert img.ndim == 3  # decoded BGR
        break
