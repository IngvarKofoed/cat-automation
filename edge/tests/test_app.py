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


class ControllableCaptureSource(CaptureSource):
    """A synthetic source for motion tests, switchable between three scenes.

    FakeCaptureSource's static gradient is unusable for motion tests: MOG2
    absorbs it as background on frame one and never flags anything. This
    source instead returns a uniform "background" frame by default, and can
    be switched to a "blob" (a small filled rectangle in a sub-region — a
    stand-in for a cat) or "bright" (the whole frame lightened — a stand-in
    for a cloud/illumination change) so tests can drive MOG2 deterministically
    via repeated ``grab_once()`` calls.
    """

    WIDTH = 160
    HEIGHT = 120
    BACKGROUND_LEVEL = 60
    BRIGHT_LEVEL = 200
    BLOB_LEVEL = 220
    # Pixel rect (x, y, w, h) for "blob" mode: well inside the frame and small
    # relative to it (~4.7% of the ROI), comfortably inside DEFAULTS'
    # [min_area, max_area_fraction] band.
    BLOB_RECT = (60, 40, 30, 30)

    def __init__(self, device: "int | str" = 0) -> None:
        del device  # unused: kept only for interface compatibility
        self._closed = False
        self.mode = "background"  # "background" | "blob" | "bright"

    def read(self) -> np.ndarray:
        if self._closed:
            raise CaptureError("controllable capture source is closed")
        frame = np.full((self.HEIGHT, self.WIDTH, 3), self.BACKGROUND_LEVEL, dtype=np.uint8)
        if self.mode == "blob":
            x, y, w, h = self.BLOB_RECT
            frame[y : y + h, x : x + w] = self.BLOB_LEVEL
        elif self.mode == "bright":
            frame[:] = self.BRIGHT_LEVEL
        return frame

    def close(self) -> None:
        self._closed = True


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


# The full default /api/config body, including the 6 motion keys. Every exact
# config-dict assertion should build off this (with overrides) rather than
# spell out the dict by hand, so a future key addition can't silently leave a
# stale assertion behind (bit us with `fps` last increment).
_DEFAULT_CONFIG = dict(settings.DEFAULTS)


def _expected_config(**overrides) -> dict:
    """The full /api/config dict, with defaults overridden by any kwargs."""
    return {**_DEFAULT_CONFIG, **overrides}


@pytest.fixture
def motion_app(config_path):
    """An app wired to a ControllableCaptureSource, for driving MOG2 motion
    detection deterministically via grab_once(), using the production default
    motion params (settings.DEFAULTS) unchanged. Returns (app, source) so a
    test can flip the source's mode.
    """
    created = {}

    def factory(device):
        created["source"] = ControllableCaptureSource(device)
        return created["source"]

    app = create_app(source_factory=factory, start_grabber=False)
    return app, created["source"]


def _warm_up(grabber, n: int = 10) -> None:
    """Feed the grabber `n` identical background frames to converge MOG2.

    10 is comfortably enough here: the scene is a flat, noise-free synthetic
    frame, so the default var_threshold/learning_rate settle within a couple
    of frames (verified empirically) — nowhere near the minutes a real, noisy
    camera scene needs (see the spec's "paused-cat absorption" section).
    """
    for _ in range(n):
        grabber.grab_once()


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
    # A single grab can't meet the default persistence (2 consecutive frames),
    # so the first part's motion is deterministically off with no bbox.
    assert b"X-Motion: 0" in part
    assert b"X-Bbox" not in part
    # X-Area is emitted on EVERY part now (the shared serializer always writes it,
    # matching /status and the grabber's always-reported area), even when idle.
    assert b"X-Area" in part
    resp.close()


def _pull_one_part(app) -> "tuple[bytes, bytes]":
    """Open /stream, pull exactly one multipart part, split header block/body.

    Returns (header_block, jpeg_body): the header block through its terminating
    blank line (ending CRLF CRLF), and the JPEG body with its trailing CRLF
    stripped. Closes the endless stream so the test doesn't leak the connection.
    """
    resp = app.test_client().get("/stream", buffered=False)
    try:
        part = next(resp.response)
    finally:
        resp.close()
    sep = part.index(b"\r\n\r\n") + 4
    return part[:sep], part[sep:-2]  # -2 drops the part's trailing CRLF


def test_stream_part_bytes_exact_when_motion_inactive(config_path):
    # Byte-exact lock on the motion-INACTIVE part: it must spell out X-Motion: 0,
    # carry NO X-Bbox, and — the intended contract change — DO carry X-Area even
    # when idle. The expected block is hand-written (independent of shared.wire's
    # construction) so a regression in the serializer is actually caught.
    app = create_app(source_factory=FakeCaptureSource, start_grabber=False)
    app.grabber.grab_once()
    snap = app.grabber.snapshot()
    assert snap.motion is False and snap.bbox is None

    header_block, body = _pull_one_part(app)
    expected = (
        b"--frame\r\n"
        b"Content-Type: image/jpeg\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        b"X-Frame-Id: " + str(snap.frame_id).encode() + b"\r\n"
        b"X-Timestamp: " + str(snap.ts).encode() + b"\r\n"
        b"X-Motion: 0\r\n"
        b"X-Area: " + str(snap.area).encode() + b"\r\n"
        b"\r\n"
    )
    assert header_block == expected


def test_stream_part_bytes_exact_when_motion_active(motion_app):
    # Byte-exact lock on the motion-ACTIVE part: X-Bbox MUST appear (its four
    # comma-joined floats) and MUST precede X-Area — the historical byte order the
    # compute parser and this serializer both depend on.
    app, source = motion_app
    grabber = app.grabber
    _warm_up(grabber)
    source.mode = "blob"
    for _ in range(settings.DEFAULTS["persistence"]):
        grabber.grab_once()
    snap = grabber.snapshot()
    assert snap.motion is True and snap.bbox is not None

    header_block, body = _pull_one_part(app)
    bx, by, bw, bh = snap.bbox
    expected = (
        b"--frame\r\n"
        b"Content-Type: image/jpeg\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        b"X-Frame-Id: " + str(snap.frame_id).encode() + b"\r\n"
        b"X-Timestamp: " + str(snap.ts).encode() + b"\r\n"
        b"X-Motion: 1\r\n"
        b"X-Bbox: " + f"{bx},{by},{bw},{bh}".encode() + b"\r\n"
        b"X-Area: " + str(snap.area).encode() + b"\r\n"
        b"\r\n"
    )
    assert header_block == expected


# --- GET /api/config ---


def test_get_config_returns_default_device(client):
    resp = client.get("/api/config")
    assert resp.status_code == 200
    assert resp.get_json() == _expected_config()


# --- POST /api/config: valid device ---


def test_post_config_valid_device_updates_and_persists(client, config_path):
    resp = client.post("/api/config", json={"device": 1})
    assert resp.status_code == 200
    assert resp.get_json() == _expected_config(device=1)

    resp = client.get("/api/config")
    assert resp.get_json() == _expected_config(device=1)

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
    assert resp.get_json() == _expected_config()

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
    # fell back to the default device; rotation/clip/fps/motion default too
    assert resp.get_json() == _expected_config()


def test_save_settings_then_load_settings_roundtrips(config_path):
    full = {
        "device": "/dev/video0",
        "rotation": 90,
        "clip": {"x": 0, "y": 0, "w": 0.5, "h": 0.5},
        "fps": 5,
        # Non-default motion values, so a roundtrip bug (e.g. load_settings
        # silently falling back to DEFAULTS) can't hide behind equal values.
        "var_threshold": 20.0,
        "learning_rate": 0.05,
        "min_area": 0.02,
        "max_area_fraction": 0.5,
        "persistence": 3,
        "motion_downscale": 200,
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
    assert resp.get_json() == _expected_config(rotation=90)

    resp = client.get("/api/config")
    assert resp.get_json() == _expected_config(rotation=90)

    saved = json.loads(config_path.read_text())
    assert saved == _expected_config(rotation=90)


# --- POST /api/config: clip-only ---


def test_post_config_clip_updates_and_persists(client, config_path):
    clip = {"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.5}
    resp = client.post("/api/config", json={"clip": clip})
    assert resp.status_code == 200
    assert resp.get_json() == _expected_config(clip=clip)

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


# --- POST /api/config: motion key validation ---


@pytest.mark.parametrize(
    "key, value",
    [
        ("var_threshold", 20.0),
        ("learning_rate", 0.05),
        ("min_area", 0.02),
        ("max_area_fraction", 0.5),
        ("persistence", 3),
        ("motion_downscale", 200),
    ],
)
def test_post_config_valid_motion_key_updates_and_round_trips(client, key, value):
    resp = client.post("/api/config", json={key: value})
    assert resp.status_code == 200
    assert resp.get_json()[key] == value

    resp = client.get("/api/config")
    assert resp.get_json()[key] == value


@pytest.mark.parametrize(
    "key, value",
    [
        ("var_threshold", 0),
        ("var_threshold", -1),
        ("var_threshold", True),
        ("learning_rate", -0.1),
        ("learning_rate", 1.1),
        ("learning_rate", True),
        ("min_area", -0.01),
        ("min_area", 1),
        ("min_area", True),
        ("max_area_fraction", 0),
        ("max_area_fraction", 1.1),
        ("max_area_fraction", True),
        ("persistence", 0),
        ("persistence", 1.5),
        ("persistence", True),
        ("motion_downscale", 16),
        ("motion_downscale", 1000),
        ("motion_downscale", True),
    ],
)
def test_post_config_invalid_motion_key_is_400(client, key, value):
    resp = client.post("/api/config", json={key: value})
    assert resp.status_code == 400
    assert "error" in resp.get_json()


# --- POST /api/config: a device change must not wipe rotation/clip ---


def test_post_config_device_change_preserves_clip(client, config_path):
    clip = {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}
    resp = client.post("/api/config", json={"clip": clip})
    assert resp.status_code == 200

    resp = client.post("/api/config", json={"device": 1})
    assert resp.status_code == 200
    assert resp.get_json() == _expected_config(device=1, clip=clip)

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


# --- /frame: overlay ---


def test_frame_overlay_returns_jpeg(client):
    # Smoke check: the overlay draw-and-encode path succeeds whether or not
    # the slot happens to have a bbox (a single grab never meets persistence).
    client.application.grabber.grab_once()
    resp = client.get("/frame?overlay=1")
    assert resp.status_code == 200
    assert resp.content_type == "image/jpeg"
    assert resp.data[:2] == b"\xff\xd8"


# --- GET /status ---


def test_status_returns_documented_shape_with_camera_ok_true(client):
    client.application.grabber.grab_once()
    resp = client.get("/status")
    assert resp.status_code == 200
    body = resp.get_json()
    for key in ("frame_id", "ts", "motion", "bbox", "area", "camera_ok", "last_error", "version"):
        assert key in body
    assert isinstance(body["version"], str) and body["version"]  # baked value, or "unknown"
    assert body["camera_ok"] is True
    assert body["last_error"] is None
    # A single grab can't meet the default persistence (2) — deterministic.
    assert body["motion"] is False
    assert body["bbox"] is None


def test_status_reflects_failing_source(config_path):
    app = create_app(source_factory=FailingCaptureSource, start_grabber=False)
    app.grabber.grab_once()
    resp = app.test_client().get("/status")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["camera_ok"] is False
    assert body["last_error"] is not None


def test_status_includes_system_metrics(client, monkeypatch):
    # /status must carry a `system` key (dict or None per SystemMetrics.sample()'s
    # contract) alongside the existing camera/motion fields — see
    # docs/specs/2026-07-08-edge-system-metrics.md. Mock psutil so this asserts a
    # populated dict regardless of whether the real dependency is installed.
    from edge.server import metrics as metrics_mod

    total = 4 * 1024**3  # 4 GB, in bytes, so mem_total_mb rounds to a sane figure
    stub = _StubPsutil(cpu_percent=5.0, vm=_StubVirtualMemory(total=total, available=total // 2, percent=75.0))
    monkeypatch.setattr(metrics_mod, "psutil", stub)

    client.application.grabber.grab_once()
    resp = client.get("/status")
    assert resp.status_code == 200
    body = resp.get_json()
    assert "system" in body
    system = body["system"]
    assert isinstance(system, dict)
    for key in ("cpu_percent", "mem_percent", "mem_used_mb", "mem_total_mb"):
        assert key in system
    assert system["mem_total_mb"] > 0
    assert 0 <= system["mem_percent"] <= 100


# --- edge/server/metrics.py: SystemMetrics ---


class _StubVirtualMemory:
    """Stand-in for psutil.virtual_memory()'s return value."""

    def __init__(self, total: int, available: int, percent: float) -> None:
        self.total = total
        self.available = available
        self.percent = percent


class _StubPsutil:
    """Minimal stand-in for the psutil module, controllable per test."""

    def __init__(self, cpu_percent: float, vm: _StubVirtualMemory) -> None:
        self._cpu_percent = cpu_percent
        self._vm = vm

    def cpu_percent(self, interval=None):
        del interval  # SystemMetrics must call this non-blocking (interval=None)
        return self._cpu_percent

    def virtual_memory(self):
        return self._vm


def test_sample_returns_none_when_psutil_unavailable(monkeypatch):
    from edge.server import metrics as metrics_mod

    monkeypatch.setattr(metrics_mod, "psutil", None)
    assert metrics_mod.SystemMetrics().sample() is None


def test_sample_cpu_percent_none_until_first_window_elapses(monkeypatch):
    # The first sample() must not trust psutil's first cpu_percent() reading
    # (it's meaningless with no prior window), so cpu_percent starts None.
    from edge.server import metrics as metrics_mod

    stub = _StubPsutil(cpu_percent=42.0, vm=_StubVirtualMemory(total=1000, available=400, percent=60.0))
    monkeypatch.setattr(metrics_mod, "psutil", stub)

    sampler = metrics_mod.SystemMetrics()
    first = sampler.sample()
    assert first is not None
    assert first["cpu_percent"] is None

    # Advance the sampler past CPU_WINDOW_S without waiting in real time.
    sampler._last_cpu_mono -= metrics_mod.CPU_WINDOW_S
    second = sampler.sample()
    assert second["cpu_percent"] == 42.0


def test_sample_caches_cpu_percent_within_window(monkeypatch):
    # Within the same CPU_WINDOW_S, a changed underlying reading must not show
    # up yet — sample() reuses the cached value instead of re-reading psutil.
    from edge.server import metrics as metrics_mod

    stub = _StubPsutil(cpu_percent=10.0, vm=_StubVirtualMemory(total=1000, available=400, percent=60.0))
    monkeypatch.setattr(metrics_mod, "psutil", stub)

    sampler = metrics_mod.SystemMetrics()
    sampler.sample()  # primes the window; cpu_percent still None here
    sampler._last_cpu_mono -= metrics_mod.CPU_WINDOW_S
    sampler.sample()  # crosses the window once, caches 10.0

    stub._cpu_percent = 99.0  # would show up only on a fresh psutil read
    still_cached = sampler.sample()
    assert still_cached["cpu_percent"] == 10.0


def test_sample_memory_derived_from_total_minus_available(monkeypatch):
    # mem_used_mb/mem_percent must come from total-available, not psutil's
    # platform-dependent .used, for Linux/macOS consistency.
    from edge.server import metrics as metrics_mod

    total = 8 * 1024**2  # 8 MB, in bytes, for round-number expectations
    available = 3 * 1024**2  # 3 MB available -> 5 MB used
    stub = _StubPsutil(cpu_percent=0.0, vm=_StubVirtualMemory(total=total, available=available, percent=62.5))
    monkeypatch.setattr(metrics_mod, "psutil", stub)

    result = metrics_mod.SystemMetrics().sample()
    assert result is not None
    assert result["mem_total_mb"] == 8
    assert result["mem_used_mb"] == 5
    assert result["mem_percent"] == 62.5


# --- motion detection (grabber, via a controllable synthetic source) ---


def test_motion_true_on_sustained_blob_with_roughly_correct_bbox(motion_app):
    app, source = motion_app
    grabber = app.grabber
    _warm_up(grabber)

    source.mode = "blob"
    for _ in range(settings.DEFAULTS["persistence"]):
        grabber.grab_once()

    snap = grabber.snapshot()
    assert snap.motion is True
    assert snap.bbox is not None
    bx, by, bw, bh = snap.bbox
    x, y, w, h = ControllableCaptureSource.BLOB_RECT
    width, height = ControllableCaptureSource.WIDTH, ControllableCaptureSource.HEIGHT
    assert bx == pytest.approx(x / width, abs=0.05)
    assert by == pytest.approx(y / height, abs=0.05)
    assert bw == pytest.approx(w / width, abs=0.05)
    assert bh == pytest.approx(h / height, abs=0.05)


def test_motion_false_below_persistence(motion_app):
    app, source = motion_app
    grabber = app.grabber
    _warm_up(grabber)

    source.mode = "blob"
    grabber.grab_once()  # a single frame — below the default persistence of 2
    assert grabber.snapshot().motion is False


def test_motion_false_on_global_brightness_change(motion_app):
    # A whole-ROI illumination change (a cloud) must be rejected by
    # max_area_fraction, distinguishing it from a compact, cat-sized blob.
    app, source = motion_app
    grabber = app.grabber
    _warm_up(grabber)

    source.mode = "bright"
    for _ in range(settings.DEFAULTS["persistence"] + 1):
        grabber.grab_once()
    assert grabber.snapshot().motion is False


# --- POST /api/motion/reset ---


def test_motion_reset_allows_relearn_and_retrigger(motion_app):
    app, source = motion_app
    grabber = app.grabber
    _warm_up(grabber)

    source.mode = "blob"
    for _ in range(settings.DEFAULTS["persistence"]):
        grabber.grab_once()
    assert grabber.snapshot().motion is True

    resp = app.test_client().post("/api/motion/reset")
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}

    # The reset drops the model entirely, so the (now-blob) scene must be
    # relearned as background from scratch before motion means anything again.
    source.mode = "background"
    _warm_up(grabber)
    assert grabber.snapshot().motion is False

    # A fresh blob re-triggers motion, proving the old model was actually
    # dropped rather than left in place with stale state.
    source.mode = "blob"
    for _ in range(settings.DEFAULTS["persistence"]):
        grabber.grab_once()
    assert grabber.snapshot().motion is True


# --- review regressions: motion never gates delivery; config cross-check; reset clears signal ---


class MonoCaptureSource(CaptureSource):
    """Returns a single-channel (grayscale/IR) frame — models a night camera.

    Regression guard for the delivery/motion split: previously _compute_motion
    called cv2.cvtColor(BGR2GRAY) on this 2D frame, which raised and failed the
    whole grab, so a perfectly working mono camera looked dead.
    """

    def __init__(self, device: "int | str" = 0) -> None:
        del device

    def read(self) -> np.ndarray:
        return np.full((120, 160), 80, dtype=np.uint8)  # 2D, single-channel

    def close(self) -> None:
        pass


def test_mono_camera_delivers_frame_and_motion_never_gates(config_path):
    # A motion-compute quirk (here a 2D mono frame) must NOT suppress delivery.
    app = create_app(source_factory=MonoCaptureSource, start_grabber=False)
    app.grabber.grab_once()
    snap = app.grabber.snapshot()
    assert snap.frame is not None      # frame published despite the mono ROI
    assert snap.frame_id == 1
    assert snap.last_error is None     # a good read is never marked failed by motion
    resp = app.test_client().get("/frame")
    assert resp.status_code == 200
    assert resp.content_type == "image/jpeg"


def test_post_config_min_area_ge_max_area_fraction_is_400(client):
    # Both individually valid, but an inverted pair makes motion impossible.
    resp = client.post("/api/config", json={"min_area": 0.7, "max_area_fraction": 0.6})
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_post_config_partial_min_area_checked_against_live_max(client):
    # A min_area-only POST is cross-checked against the persisted max (default 0.6).
    assert client.post("/api/config", json={"min_area": 0.9}).status_code == 400
    # A value that stays below the live max is accepted.
    assert client.post("/api/config", json={"min_area": 0.05}).status_code == 200


def test_reset_motion_clears_published_signal(motion_app):
    # reset_motion must neutralize the last published motion/bbox so a config
    # change can't serve a stale motion=true / old-ROI bbox until the next grab.
    app, source = motion_app
    grabber = app.grabber
    _warm_up(grabber)
    source.mode = "blob"
    for _ in range(settings.DEFAULTS["persistence"]):
        grabber.grab_once()
    assert grabber.snapshot().motion is True

    grabber.reset_motion()
    snap = grabber.snapshot()          # no new grab yet
    assert snap.motion is False
    assert snap.bbox is None
    assert snap.area == 0.0
