"""Flask app for the edge tier: serve stills/stream, pick a camera, persist config.

The integration hub that ties the capture backends, the background grabber, and
settings together behind one HTTP server. ``/frame`` and ``/stream`` both serve
from the grabber's latest-frame slot (never a direct source read). See
docs/ARCHITECTURE.md (Camera source, Config UI) and
docs/specs/2026-07-07-edge-stream-live-fps.md.
"""
from __future__ import annotations

import glob
import os
import platform
import threading
import time
from pathlib import Path
from typing import Callable

import cv2
from flask import Flask, Response, jsonify, request, send_from_directory

from edge.capture.base import CaptureError, CaptureSource
from edge.capture.factory import create_source
from edge.clip.transform import crop, rotate
from edge.config.settings import DEFAULTS, load_settings, save_settings
from edge.server.grabber import Grabber

SourceFactory = Callable[["int | str"], CaptureSource]

_UI_DIR = Path(__file__).resolve().parent / "ui"

# How long /frame blocks for the very first frame on a cold boot (out-waiting
# camera warmup) and how long a /stream generator waits between frame checks.
_FIRST_FRAME_WAIT_S = 5.0
_STREAM_WAIT_S = 5.0


def _coerce_device(device: "int | str") -> "int | str":
    """Validate a device id and coerce an all-digits string to an int.

    Raises ValueError if it is not an int or a non-empty string.
    """
    if isinstance(device, bool):  # bool is an int subclass; reject it explicitly
        raise ValueError("device must be an int or a non-empty string")
    if isinstance(device, int):
        return device
    if isinstance(device, str) and device:
        return int(device) if device.isdigit() else device
    raise ValueError("device must be an int or a non-empty string")


def _valid_rotation(rotation) -> bool:
    """True if `rotation` is one of the accepted clockwise angles."""
    return rotation in (0, 90, 180, 270)


def _valid_clip(clip) -> bool:
    """True if `clip` is None (no crop) or a well-formed normalized rect.

    A rect is {x, y, w, h} with numeric values, 0<=x, 0<=y, w>0, h>0, and the
    box fully inside the frame (x+w<=1, y+h<=1).
    """
    if clip is None:
        return True
    if not isinstance(clip, dict):
        return False
    try:
        x, y, w, h = clip["x"], clip["y"], clip["w"], clip["h"]
    except (KeyError, TypeError):
        return False
    for v in (x, y, w, h):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return False
    return x >= 0 and y >= 0 and w > 0 and h > 0 and x + w <= 1 and y + h <= 1


def _valid_fps(fps) -> bool:
    """True if `fps` is a real number (not a bool) in the inclusive range 1..30."""
    if isinstance(fps, bool):  # bool is an int subclass; reject it explicitly
        return False
    if not isinstance(fps, (int, float)):
        return False
    return 1 <= fps <= 30


def _encode_jpeg(img) -> "tuple[bool, bytes]":
    """Encode a BGR frame as JPEG q90; return (ok, bytes) — bytes empty on failure."""
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])
    return ok, (buf.tobytes() if ok else b"")


def _list_v4l2_cameras() -> "list[dict]":
    """USB/UVC and other V4L2 capture nodes on Linux."""
    return [{"device": path, "label": path} for path in sorted(glob.glob("/dev/video*"))]


def _list_csi_cameras() -> "list[dict]":
    """Pi CSI cameras detected via Picamera2, or [] if unavailable (not a Pi)."""
    try:
        from picamera2 import Picamera2

        infos = Picamera2.global_camera_info()
    except Exception:  # noqa: BLE001 - picamera2 absent / not a Pi / libcamera error
        return []
    entries = []
    for i, info in enumerate(infos):
        model = info.get("Model", "camera") if isinstance(info, dict) else "camera"
        entries.append({"device": f"csi:{i}", "label": f"Pi Camera CSI {i} ({model})"})
    return entries


def _enumerate_cameras() -> "list[dict]":
    """Selectable cameras for this host — OS-specific (see the MVP spec)."""
    system = platform.system()
    if system == "Darwin":
        return [{"device": 0, "label": "Built-in webcam (0)"}]
    if system == "Linux":
        # CSI first — it's the intended door camera when present.
        return _list_csi_cameras() + _list_v4l2_cameras()
    return []


def create_app(
    source_factory: "SourceFactory | None" = None, start_grabber: bool = True
) -> Flask:
    """Build the Flask app.

    ``source_factory`` maps a device id to a CaptureSource; it defaults to
    ``OpenCVCaptureSource``. It is the injection seam: tests pass
    ``FakeCaptureSource`` so ``/frame`` and ``POST /api/config`` work with no
    camera. No camera is opened here — the OpenCV source opens lazily on first
    read.

    ``start_grabber`` controls the background grab loop: it starts by default,
    but tests pass ``False`` and drive ``app.grabber.grab_once()`` to populate
    the slot deterministically without a free-spinning thread.
    """
    factory: SourceFactory = source_factory or create_source

    app = Flask(__name__)

    # The current-source slot: source + its device id + transform/fps config,
    # guarded by one lock. The grabber snapshots (source, fps) under it each
    # iteration; POST /api/config swaps under it; the serving routes snapshot
    # rotation/clip/fps under it.
    lock = threading.Lock()
    settings = load_settings()
    try:
        device = _coerce_device(settings["device"])
    except (ValueError, TypeError):
        # A hand-edited settings.json may hold a parseable-but-invalid device
        # (null, "", a float). Fall back to the default rather than crash boot.
        device = _coerce_device(DEFAULTS["device"])
    # fps has no fail-safe transform like rotation/clip, and /frame's staleness
    # check divides by it, so an invalid stored fps falls back to the default
    # rather than crashing a request or the grab loop.
    fps = settings["fps"] if _valid_fps(settings["fps"]) else DEFAULTS["fps"]
    # rotation/clip need no validation at load: the transform functions are
    # fail-safe, so a bad stored value degrades to 0°/full-frame at render time.
    state = {
        "source": factory(device),
        "device": device,
        "rotation": settings["rotation"],
        "clip": settings["clip"],
        "fps": fps,
    }

    def read_config() -> "tuple[CaptureSource, float]":
        # Hand the grabber the current source + fps under the lock; it releases
        # the lock before calling source.read(), so grabs never block config.
        with lock:
            return state["source"], state["fps"]

    grabber = Grabber(read_config)
    app.grabber = grabber
    if start_grabber:
        grabber.start()

    def _render(snap, raw: bool = False) -> "tuple[bool, bytes]":
        # The single transform+encode boundary shared by /frame and /stream:
        # rotate, then crop unless raw, then JPEG-encode the raw slot frame with
        # the current config. One place so the still and the stream can't silently
        # diverge on quality/color/crop rules.
        with lock:
            rotation, clip = state["rotation"], state["clip"]
        img = rotate(snap.frame, rotation)
        if not raw:
            img = crop(img, clip)
        return _encode_jpeg(img)

    def _build_part(snap) -> "bytes | None":
        # Encode the (cropped) slot frame and frame it as one multipart part.
        # None if encoding fails.
        ok, data = _render(snap)
        if not ok:
            return None
        return (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n"
            b"Content-Length: " + str(len(data)).encode() + b"\r\n"
            b"X-Frame-Id: " + str(snap.frame_id).encode() + b"\r\n"
            b"X-Timestamp: " + str(snap.ts).encode() + b"\r\n"
            b"\r\n" + data + b"\r\n"
        )

    @app.get("/")
    def index():
        return send_from_directory(_UI_DIR, "index.html")

    @app.get("/frame")
    def frame():
        # raw is on when the param is present and truthy: raw preview skips the
        # crop (oriented but uncropped) so the UI can drag the ROI on it.
        raw = request.args.get("raw") not in (None, "", "0", "false")
        # Serve the grabber's latest frame, not a fresh read. Wait only on a true
        # cold boot (no frame yet AND no error reported) to out-wait camera warmup
        # without hanging on a hard failure.
        snap = grabber.snapshot()
        if snap.frame is None and snap.last_error is None:
            snap = grabber.wait_first(timeout=_FIRST_FRAME_WAIT_S)
        with lock:
            cur_fps = state["fps"]
        # A single dropped grab must NOT 503 while a fresh frame still sits in the
        # slot — USB cameras drop reads routinely and the grabber self-heals on the
        # next tick. last_error alone isn't fatal; only the absence of a usable,
        # non-stale frame is.
        if snap.frame is None:
            return jsonify(error=snap.last_error or "no frame available"), 503
        # Reject a silently-frozen frame: a wedged camera stops advancing. Delta is
        # monotonic, not wall-clock (the Pi has no RTC, so an NTP step after boot
        # could otherwise make a fresh frame read as stale).
        stale_ms = max(2000, round(8000 / cur_fps))
        if (time.monotonic() - snap.mono) * 1000 > stale_ms:
            return jsonify(error=snap.last_error or "frame stale"), 503
        ok, data = _render(snap, raw=raw)
        if not ok:
            return jsonify(error="failed to encode frame"), 500
        resp = app.response_class(data, mimetype="image/jpeg")
        # Same frame-identity headers as /stream's parts, so a client polling
        # /frame can order and dedupe stills (per CHANGELOG #9).
        resp.headers["X-Frame-Id"] = str(snap.frame_id)
        resp.headers["X-Timestamp"] = str(snap.ts)
        return resp

    @app.get("/stream")
    def stream():
        # Continuous MJPEG over HTTP. Pacing comes from the grabber: the
        # generator blocks until frame_id advances, so idle clients don't
        # busy-loop and the cadence is the configured fps.
        def gen():
            last_sent_id = 0
            try:
                snap = grabber.snapshot()
                if snap.frame_id > 0:
                    part = _build_part(snap)
                    if part is not None:
                        last_sent_id = snap.frame_id
                        yield part
                while True:
                    snap = grabber.wait_next(last_sent_id, timeout=_STREAM_WAIT_S)
                    if snap.frame_id > last_sent_id and snap.frame is not None:
                        part = _build_part(snap)
                        if part is not None:
                            last_sent_id = snap.frame_id
                            yield part
            except (GeneratorExit, OSError):
                # Client disconnected (Live toggled off, or the PC dropped the
                # stream): end quietly rather than surfacing a broken-pipe
                # traceback in the server log. BrokenPipe/ConnectionReset are
                # OSError subclasses; GeneratorExit fires when the response closes.
                return

        return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame")

    @app.get("/api/config")
    def get_config():
        with lock:
            return jsonify(
                device=state["device"],
                rotation=state["rotation"],
                clip=state["clip"],
                fps=state["fps"],
            )

    @app.post("/api/config")
    def set_config():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            # Missing, non-JSON, or valid-but-non-object body (5, "x", [] …):
            # treat as empty so it becomes a clean 400, not a 500.
            body = {}
        if not any(k in body for k in ("device", "rotation", "clip", "fps")):
            return jsonify(error="config must set at least one of device, rotation, clip, fps"), 400

        # Validate every present field BEFORE any camera work; 400 on the first bad
        # one. device is now optional (the UI sends rotation-only / clip-only POSTs).
        device = None
        if "device" in body:
            try:
                device = _coerce_device(body["device"])
            except (ValueError, TypeError):
                return jsonify(error="device must be an int or a non-empty string"), 400
        if "rotation" in body and not _valid_rotation(body["rotation"]):
            return jsonify(error="rotation must be one of 0, 90, 180, 270"), 400
        if "clip" in body and not _valid_clip(body["clip"]):
            return jsonify(error="clip must be null or a normalized rect within the frame"), 400
        if "fps" in body and not _valid_fps(body["fps"]):
            return jsonify(error="fps must be a number between 1 and 30"), 400

        # Snapshot the current config to overlay the present fields onto and to
        # detect whether the device actually changes.
        cur_device = state["device"]
        cur_rotation = state["rotation"]
        cur_clip = state["clip"]
        cur_fps = state["fps"]

        # Build+validate a new source only when the device changes (a real open is
        # slow, so do it OUTSIDE the lock).
        device_changed = "device" in body and device != cur_device
        candidate = None
        if device_changed:
            candidate = factory(device)
            try:
                candidate.read()
            except CaptureError as e:
                candidate.close()
                # 4xx (not /frame's 503): the client picked a device that doesn't
                # work, distinct from a previously-working camera failing at read
                # time. Per docs/specs/2026-07-07-edge-stills-mvp.md.
                return jsonify(error=str(e)), 422

        # Assemble the COMPLETE next config from the current state overlaid with the
        # present fields, and persist it BEFORE swapping. save_settings overwrites
        # the whole file, so it must get the full dict — a single key would wipe the
        # others (e.g. changing the camera would erase a saved ROI/rotation). If the
        # write fails, the live source and the saved config still agree (unchanged).
        next_config = {
            "device": device if "device" in body else cur_device,
            "rotation": body["rotation"] if "rotation" in body else cur_rotation,
            "clip": body["clip"] if "clip" in body else cur_clip,
            "fps": body["fps"] if "fps" in body else cur_fps,
        }
        try:
            save_settings(next_config)
        except OSError as e:
            if candidate is not None:
                candidate.close()
            return jsonify(error=f"failed to persist config: {e}"), 500

        # New-before-close: a fast pointer swap under the lock (only if the device
        # changed), plus the transform/fps params; then close the old source
        # outside it. fps and rotation/clip are picked up live by the grabber and
        # the serving routes on their next lock-guarded read.
        with lock:
            old = None
            if device_changed:
                old = state["source"]
                state["source"] = candidate
                state["device"] = device
            state["rotation"] = next_config["rotation"]
            state["clip"] = next_config["clip"]
            state["fps"] = next_config["fps"]
        if old is not None:
            old.close()
        if device_changed:
            # Publish a frame from the NEW device now, so /frame (which serves the
            # slot) doesn't hand back the previous camera's cached frame until the
            # background grabber next advances.
            grabber.grab_once()
        return jsonify(next_config)

    @app.get("/api/cameras")
    def cameras():
        return jsonify(cameras=_enumerate_cameras())

    return app


if __name__ == "__main__":
    port = int(os.environ.get("CAT_EDGE_PORT", "8000"))
    create_app().run(host="0.0.0.0", port=port, threaded=True)
