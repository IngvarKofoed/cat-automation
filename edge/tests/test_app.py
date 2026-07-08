"""Tests for the edge Flask app (edge/server/app.py), against FakeCaptureSource.

No real camera or settings.json is touched: CAT_EDGE_CONFIG is pointed at a
tmp file for every test. See docs/specs/2026-07-07-edge-stills-mvp.md and
docs/specs/2026-07-07-edge-clip-rotation.md.
"""
from __future__ import annotations

import json

import cv2
import numpy as np
import pytest

from edge.capture.base import CaptureError, CaptureSource
from edge.capture.fake_source import FakeCaptureSource
from edge.capture.opencv_source import OpenCVCaptureSource
from edge.clip.transform import crop, rotate
from edge.config import settings
from edge.server.app import create_app


class FailingCaptureSource(CaptureSource):
    """A capture source whose read() always fails, simulating a bad device."""

    def __init__(self, device: "int | str" = 0) -> None:
        del device

    def read(self):
        raise CaptureError("cannot open device")

    def close(self) -> None:
        pass


class FlakyCaptureSource(CaptureSource):
    """Succeeds on the first read, then fails — models a transient dropped grab."""

    def __init__(self, device: "int | str" = 0) -> None:
        del device
        self._reads = 0

    def read(self):
        self._reads += 1
        if self._reads == 1:
            return FakeCaptureSource().read()
        raise CaptureError("transient read failure")

    def close(self) -> None:
        pass


def _factory_with_bad_device(bad_device: "int | str"):
    """A source_factory where `bad_device` yields a source that fails to read.

    Any other device yields a working FakeCaptureSource.
    """

    def factory(device: "int | str") -> CaptureSource:
        if device == bad_device:
            return FailingCaptureSource(device)
        return FakeCaptureSource(device)

    return factory


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    """Point CAT_EDGE_CONFIG at a tmp file so no real settings.json is touched."""
    path = tmp_path / "settings.json"
    monkeypatch.setenv("CAT_EDGE_CONFIG", str(path))
    return path


@pytest.fixture
def client(config_path):
    """A test client for an app wired to the fake capture source.

    The grabber never auto-starts in tests: start_grabber=False plus one
    grab_once() populates the latest-frame slot deterministically, so /frame
    tests get a frame without a free-spinning background thread.
    """
    app = create_app(source_factory=FakeCaptureSource, start_grabber=False)
    app.grabber.grab_once()
    return app.test_client()


# --- /frame ---


def test_frame_returns_jpeg(client):
    # Grab immediately before the request so the slot frame is fresh regardless
    # of how slow the host is between fixture setup and here (the /frame staleness
    # window is wall-clock-independent but time-based).
    client.application.grabber.grab_once()
    resp = client.get("/frame")
    assert resp.status_code == 200
    assert resp.content_type == "image/jpeg"
    assert resp.data[:2] == b"\xff\xd8"


# --- grabber determinism ---


def test_grab_once_populates_slot_then_frame_returns_jpeg(config_path):
    app = create_app(source_factory=FakeCaptureSource, start_grabber=False)
    assert app.grabber.snapshot().frame_id == 0

    app.grabber.grab_once()
    snap = app.grabber.snapshot()
    assert snap.frame_id == 1
    assert snap.frame is not None
    assert snap.last_error is None

    resp = app.test_client().get("/frame")
    assert resp.status_code == 200
    assert resp.content_type == "image/jpeg"
    assert resp.data[:2] == b"\xff\xd8"


def test_frame_503_when_grab_fails(config_path):
    app = create_app(source_factory=FailingCaptureSource, start_grabber=False)
    app.grabber.grab_once()
    snap = app.grabber.snapshot()
    assert snap.frame_id == 0
    assert snap.last_error is not None

    resp = app.test_client().get("/frame")
    assert resp.status_code == 503
    assert "error" in resp.get_json()


def test_frame_serves_fresh_frame_despite_later_grab_error(config_path):
    # A single dropped grab sets last_error but leaves a fresh frame in the slot;
    # /frame must serve it, not 503 (USB cameras drop reads routinely).
    app = create_app(source_factory=FlakyCaptureSource, start_grabber=False)
    app.grabber.grab_once()  # success: frame in slot, frame_id 1
    app.grabber.grab_once()  # failure: last_error set, frame retained
    snap = app.grabber.snapshot()
    assert snap.frame is not None and snap.last_error is not None

    resp = app.test_client().get("/frame")
    assert resp.status_code == 200
    assert resp.content_type == "image/jpeg"


def test_frame_carries_frame_identity_headers(client):
    client.application.grabber.grab_once()
    resp = client.get("/frame")
    assert resp.status_code == 200
    # Same identity headers as /stream parts, so a polling client can order/dedupe.
    assert resp.headers.get("X-Frame-Id")
    assert resp.headers.get("X-Timestamp")


# --- GET /stream ---


def test_stream_returns_multipart_with_first_frame(config_path):
    app = create_app(source_factory=FakeCaptureSource, start_grabber=False)
    app.grabber.grab_once()
    client = app.test_client()

    resp = client.get("/stream", buffered=False)
    assert resp.status_code == 200
    assert resp.content_type.startswith("multipart/x-mixed-replace")

    # Pull exactly one part off the endless stream; never touch resp.data.
    part = next(resp.response)
    assert b"\xff\xd8" in part
    assert b"X-Frame-Id" in part
    assert b"X-Timestamp" in part
    resp.close()


# --- GET /api/config ---


def test_get_config_returns_default_device(client):
    resp = client.get("/api/config")
    assert resp.status_code == 200
    assert resp.get_json() == {"device": 0, "rotation": 0, "clip": None, "fps": 5}


# --- POST /api/config: valid device ---


def test_post_config_valid_device_updates_and_persists(client, config_path):
    resp = client.post("/api/config", json={"device": 1})
    assert resp.status_code == 200
    assert resp.get_json() == {"device": 1, "rotation": 0, "clip": None, "fps": 5}

    resp = client.get("/api/config")
    assert resp.get_json() == {"device": 1, "rotation": 0, "clip": None, "fps": 5}

    assert settings.load_settings()["device"] == 1
    saved = json.loads(config_path.read_text())
    assert saved["device"] == 1


# --- POST /api/config: invalid device ---


def test_post_config_missing_device_is_400(client):
    resp = client.post("/api/config", json={})
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_post_config_none_device_is_400(client):
    resp = client.post("/api/config", json={"device": None})
    assert resp.status_code == 400


def test_post_config_wrong_type_device_is_400(client):
    resp = client.post("/api/config", json={"device": 3.14})
    assert resp.status_code == 400


def test_post_config_non_object_body_is_400(client):
    # A valid-but-non-object JSON body must be a clean 400, not a 500.
    resp = client.post("/api/config", data="5", content_type="application/json")
    assert resp.status_code == 400


# --- POST /api/config: candidate source fails to open ---


def test_post_config_source_open_failure_keeps_previous(config_path):
    app = create_app(
        source_factory=_factory_with_bad_device(bad_device=99), start_grabber=False
    )
    client = app.test_client()

    resp = client.post("/api/config", json={"device": 99})
    assert 400 <= resp.status_code < 500

    # Previous source/device is kept.
    resp = client.get("/api/config")
    assert resp.get_json() == {"device": 0, "rotation": 0, "clip": None, "fps": 5}

    # Nothing was ever persisted for the failed switch.
    assert not config_path.exists()


# --- /api/cameras ---


def test_cameras_lists_structural_entries(client):
    resp = client.get("/api/cameras")
    assert resp.status_code == 200
    body = resp.get_json()
    assert "cameras" in body
    assert isinstance(body["cameras"], list)
    for entry in body["cameras"]:
        assert "device" in entry
        assert "label" in entry


def test_cameras_includes_detected_csi(client, monkeypatch):
    # When Picamera2 reports CSI cameras on Linux, they appear in the list.
    from edge.server import app as appmod

    monkeypatch.setattr(appmod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(appmod, "_list_v4l2_cameras", lambda: [])
    monkeypatch.setattr(
        appmod, "_list_csi_cameras",
        lambda: [{"device": "csi:0", "label": "Pi Camera CSI 0 (imx708)"}],
    )
    devices = [c["device"] for c in client.get("/api/cameras").get_json()["cameras"]]
    assert "csi:0" in devices


# --- capture-source factory routing ---


def test_factory_routes_csi_to_picamera():
    from edge.capture.factory import create_source
    from edge.capture.picamera_source import PicameraCaptureSource

    assert isinstance(create_source("csi:0"), PicameraCaptureSource)
    assert isinstance(create_source("csi"), PicameraCaptureSource)


def test_factory_routes_index_and_path_to_opencv():
    from edge.capture.factory import create_source
    from edge.capture.opencv_source import OpenCVCaptureSource

    assert isinstance(create_source(0), OpenCVCaptureSource)
    assert isinstance(create_source("/dev/video0"), OpenCVCaptureSource)


def test_picamera_read_without_picamera2_raises_captureerror():
    # On a non-Pi dev box picamera2 is absent, so read() must surface a clean
    # CaptureError (the ImportError is caught), never crash.
    try:
        import picamera2  # noqa: F401

        pytest.skip("picamera2 is installed; this checks the absent-dependency path")
    except ImportError:
        pass
    from edge.capture.picamera_source import PicameraCaptureSource

    with pytest.raises(CaptureError):
        PicameraCaptureSource(0).read()


# --- capture-source poisoned close ---


def test_fake_source_read_after_close_raises_and_does_not_reopen():
    source = FakeCaptureSource()
    source.read()  # sanity: works before close
    source.close()
    with pytest.raises(CaptureError):
        source.read()
    source.close()  # still idempotent after poisoning


def test_opencv_source_read_after_close_raises_and_does_not_reopen():
    # close() poisons the source before any read() ever touches hardware, so
    # this needs no real camera.
    source = OpenCVCaptureSource(0)
    source.close()
    with pytest.raises(CaptureError):
        source.read()
    source.close()  # still idempotent after poisoning


def test_picamera_source_read_after_close_raises_and_does_not_reopen():
    # The CSI backend must honor the same contract: the _closed guard runs before
    # _ensure_open, so read()-after-close raises without importing picamera2 or
    # touching hardware (this is the swap race the poisoning seals for CSI).
    from edge.capture.picamera_source import PicameraCaptureSource

    source = PicameraCaptureSource(0)
    source.close()
    with pytest.raises(CaptureError):
        source.read()
    source.close()  # still idempotent after poisoning


# --- / (config UI) ---


def test_index_serves_html_with_capture_button(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Capture" in resp.data


# --- settings.py, tested directly ---


def test_load_settings_missing_file_returns_defaults(config_path):
    assert settings.load_settings() == settings.DEFAULTS


def test_load_settings_corrupt_file_returns_defaults(config_path):
    config_path.write_text("not valid json {{{")
    assert settings.load_settings() == settings.DEFAULTS


def test_load_settings_non_dict_json_returns_defaults(config_path):
    # Valid JSON but not an object must fall back to defaults, not crash callers.
    config_path.write_text("null")
    assert settings.load_settings() == settings.DEFAULTS
    config_path.write_text("[1, 2, 3]")
    assert settings.load_settings() == settings.DEFAULTS


def test_create_app_bad_persisted_device_does_not_crash(config_path):
    # A hand-edited, parseable-but-invalid device must not wedge startup.
    config_path.write_text(json.dumps({"device": None}))
    app = create_app(
        source_factory=FakeCaptureSource, start_grabber=False
    )  # must not raise
    resp = app.test_client().get("/api/config")
    assert resp.status_code == 200
    # fell back to the default device; rotation/clip/fps default too
    assert resp.get_json() == {"device": 0, "rotation": 0, "clip": None, "fps": 5}


def test_save_settings_then_load_settings_roundtrips(config_path):
    full = {
        "device": "/dev/video0",
        "rotation": 90,
        "clip": {"x": 0, "y": 0, "w": 0.5, "h": 0.5},
        "fps": 5,
    }
    settings.save_settings(full)
    assert settings.load_settings() == full


# --- edge/clip/transform.py: rotate() ---


def test_rotate_90_swaps_width_and_height():
    frame = FakeCaptureSource().read()
    assert frame.shape == (240, 320, 3)
    assert rotate(frame, 90).shape == (320, 240, 3)


def test_rotate_180_keeps_dimensions():
    frame = FakeCaptureSource().read()
    assert rotate(frame, 180).shape == (240, 320, 3)


def test_rotate_0_returns_unchanged():
    frame = FakeCaptureSource().read()
    result = rotate(frame, 0)
    assert result.shape == (240, 320, 3)
    assert np.array_equal(result, frame)


def test_rotate_unknown_degrees_returns_unchanged():
    # Fail-safe: an unrecognized angle is treated like 0.
    frame = FakeCaptureSource().read()
    result = rotate(frame, 45)
    assert result.shape == (240, 320, 3)
    assert np.array_equal(result, frame)


# --- edge/clip/transform.py: crop() ---


def test_crop_half_rect_returns_quarter_area():
    frame = FakeCaptureSource().read()
    result = crop(frame, {"x": 0, "y": 0, "w": 0.5, "h": 0.5})
    assert result.shape == (120, 160, 3)


def test_crop_none_clip_returns_unchanged():
    frame = FakeCaptureSource().read()
    result = crop(frame, None)
    assert result.shape == (240, 320, 3)
    assert np.array_equal(result, frame)


def test_crop_empty_region_returns_unchanged():
    # Fail-safe: a zero-area or malformed/empty clip falls back to the full frame.
    frame = FakeCaptureSource().read()
    assert np.array_equal(crop(frame, {"x": 0, "y": 0, "w": 0, "h": 0}), frame)
    assert np.array_equal(crop(frame, {}), frame)


# --- POST /api/config: rotation-only ---


def test_post_config_rotation_updates_without_device_change(client, config_path):
    resp = client.post("/api/config", json={"rotation": 90})
    assert resp.status_code == 200
    assert resp.get_json() == {"device": 0, "rotation": 90, "clip": None, "fps": 5}

    resp = client.get("/api/config")
    assert resp.get_json() == {"device": 0, "rotation": 90, "clip": None, "fps": 5}

    saved = json.loads(config_path.read_text())
    assert saved == {"device": 0, "rotation": 90, "clip": None, "fps": 5}


# --- POST /api/config: clip-only ---


def test_post_config_clip_updates_and_persists(client, config_path):
    clip = {"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.5}
    resp = client.post("/api/config", json={"clip": clip})
    assert resp.status_code == 200
    assert resp.get_json() == {"device": 0, "rotation": 0, "clip": clip, "fps": 5}

    resp = client.get("/api/config")
    assert resp.get_json()["clip"] == clip

    saved = json.loads(config_path.read_text())
    assert saved["clip"] == clip


def test_post_config_clip_null_clears(client, config_path):
    clip = {"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.5}
    client.post("/api/config", json={"clip": clip})

    resp = client.post("/api/config", json={"clip": None})
    assert resp.status_code == 200
    assert resp.get_json()["clip"] is None

    resp = client.get("/api/config")
    assert resp.get_json()["clip"] is None


# --- POST /api/config: rotation/clip validation ---


def test_post_config_invalid_rotation_is_400(client):
    resp = client.post("/api/config", json={"rotation": 45})
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_post_config_invalid_clip_is_400(client):
    resp = client.post("/api/config", json={"clip": {"x": 0, "y": 0, "w": 2, "h": 1}})
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_post_config_empty_body_is_400(client):
    # None of device/rotation/clip present.
    resp = client.post("/api/config", json={})
    assert resp.status_code == 400
    assert "error" in resp.get_json()


# --- POST /api/config: fps validation ---


@pytest.mark.parametrize("fps", [5, 10, 30])
def test_post_config_valid_fps_updates_and_round_trips(client, fps):
    resp = client.post("/api/config", json={"fps": fps})
    assert resp.status_code == 200
    assert resp.get_json()["fps"] == fps

    resp = client.get("/api/config")
    assert resp.get_json()["fps"] == fps


@pytest.mark.parametrize("fps", [0, 31, "x", True])
def test_post_config_invalid_fps_is_400(client, fps):
    resp = client.post("/api/config", json={"fps": fps})
    assert resp.status_code == 400
    assert "error" in resp.get_json()


# --- POST /api/config: a device change must not wipe rotation/clip ---


def test_post_config_device_change_preserves_clip(client, config_path):
    clip = {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}
    resp = client.post("/api/config", json={"clip": clip})
    assert resp.status_code == 200

    resp = client.post("/api/config", json={"device": 1})
    assert resp.status_code == 200
    assert resp.get_json() == {"device": 1, "rotation": 0, "clip": clip, "fps": 5}

    saved = json.loads(config_path.read_text())
    assert saved["device"] == 1
    assert saved["clip"] == clip


# --- /frame: rotation + clip end-to-end ---


def _decode_jpeg(data: bytes) -> np.ndarray:
    return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)


def test_frame_applies_configured_rotation(client):
    resp = client.post("/api/config", json={"rotation": 90})
    assert resp.status_code == 200

    client.application.grabber.grab_once()  # fresh slot frame before the GET
    resp = client.get("/frame")
    assert resp.status_code == 200
    img = _decode_jpeg(resp.data)
    assert img.shape == (320, 240, 3)


def test_frame_applies_configured_clip(client):
    resp = client.post("/api/config", json={"clip": {"x": 0, "y": 0, "w": 0.5, "h": 0.5}})
    assert resp.status_code == 200

    client.application.grabber.grab_once()  # fresh slot frame before the GET
    resp = client.get("/frame")
    assert resp.status_code == 200
    img = _decode_jpeg(resp.data)
    assert img.shape[0] < 240
    assert img.shape[1] < 320


def test_frame_raw_skips_crop_but_keeps_rotation(client):
    resp = client.post("/api/config", json={"rotation": 90})
    assert resp.status_code == 200
    resp = client.post("/api/config", json={"clip": {"x": 0, "y": 0, "w": 0.5, "h": 0.5}})
    assert resp.status_code == 200

    client.application.grabber.grab_once()  # fresh slot frame before the GET
    resp = client.get("/frame?raw=1")
    assert resp.status_code == 200
    img = _decode_jpeg(resp.data)
    assert img.shape == (320, 240, 3)  # rotated, but the crop was skipped
