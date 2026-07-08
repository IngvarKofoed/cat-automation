"""Flask app for the edge tier: serve stills/stream, pick a camera, persist config.

The integration hub that ties the capture backends, the background grabber, and
settings together behind one HTTP server. ``/frame`` and ``/stream`` both serve
from the grabber's latest-frame slot (never a direct source read). See
Motion runs in the grabber and is published as a PULL signal (GET /status +
X-Motion stream headers); it does NOT gate frame delivery — /frame and /stream
serve every frame exactly as before. See docs/ARCHITECTURE.md (Camera source,
Config UI), docs/specs/2026-07-07-edge-stream-live-fps.md, and
docs/specs/2026-07-08-edge-motion-detection.md.
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
from edge.server.grabber import GrabConfig, Grabber, MotionConfig

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


def _is_number(v) -> bool:
    """True if `v` is a real int/float — bool (an int subclass) is rejected."""
    return not isinstance(v, bool) and isinstance(v, (int, float))


def _is_int(v) -> bool:
    """True if `v` is a real int — bool (an int subclass) is rejected."""
    return isinstance(v, int) and not isinstance(v, bool)


def _valid_fps(fps) -> bool:
    """True if `fps` is a real number (not a bool) in the inclusive range 1..30."""
    return _is_number(fps) and 1 <= fps <= 30


# Motion-config keys: (validator, 400 message). One table drives load-time
# defaulting, the POST presence check, per-key validation, and the state
# snapshot, so the six motion keys are handled uniformly with fps/rotation/clip.
# Ranges per docs/specs/2026-07-08-edge-motion-detection.md; bool is rejected
# wherever a number/int is expected (mirrors _valid_fps).
_MOTION_VALIDATORS = {
    "var_threshold": (
        lambda v: _is_number(v) and v > 0,
        "var_threshold must be a number greater than 0",
    ),
    "learning_rate": (
        lambda v: _is_number(v) and 0 <= v <= 1,
        "learning_rate must be a number between 0 and 1",
    ),
    "min_area": (
        lambda v: _is_number(v) and 0 <= v < 1,
        "min_area must be a number in [0, 1)",
    ),
    "max_area_fraction": (
        lambda v: _is_number(v) and 0 < v <= 1,
        "max_area_fraction must be a number in (0, 1]",
    ),
    "persistence": (
        lambda v: _is_int(v) and v >= 1,
        "persistence must be an integer >= 1",
    ),
    "motion_downscale": (
        lambda v: _is_int(v) and 32 <= v <= 640,
        "motion_downscale must be an integer between 32 and 640",
    ),
}

# Every settable config key (for the POST presence check + its error message).
_CONFIG_KEYS = ("device", "rotation", "clip", "fps", *_MOTION_VALIDATORS)


def _encode_jpeg(img) -> "tuple[bool, bytes]":
    """Encode a BGR frame as JPEG q90; return (ok, bytes) — bytes empty on failure."""
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])
    return ok, (buf.tobytes() if ok else b"")


def _frame_is_stale(snap, fps) -> bool:
    """True if the slot's frame is older than the freshness budget for ``fps``.

    Catches a silently-frozen frame from a wedged camera. The delta is monotonic
    (the Pi has no RTC, so an NTP step after boot must not make a fresh frame read
    as stale), and the 2 s floor keeps fast tests from flaking. Shared by /frame
    and /status so both derive liveness from one rule.
    """
    stale_ms = max(2000, round(8000 / fps))
    return (time.monotonic() - snap.mono) * 1000 > stale_ms


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
    # Motion params: like fps they flow into compute (MOG2/decision rule) with no
    # fail-safe transform, so an invalid stored value falls back to the default.
    for key, (validator, _msg) in _MOTION_VALIDATORS.items():
        state[key] = settings[key] if validator(settings[key]) else DEFAULTS[key]

    def read_config() -> GrabConfig:
        # Hand the grabber the current source + pacing + transform + motion params
        # under the lock; it releases the lock before calling source.read(), so
        # grabs never block config reads or a device swap.
        with lock:
            return GrabConfig(
                source=state["source"],
                fps=state["fps"],
                rotation=state["rotation"],
                clip=state["clip"],
                motion=MotionConfig(
                    var_threshold=state["var_threshold"],
                    learning_rate=state["learning_rate"],
                    min_area=state["min_area"],
                    max_area_fraction=state["max_area_fraction"],
                    persistence=state["persistence"],
                    downscale=state["motion_downscale"],
                ),
            )

    grabber = Grabber(read_config)
    app.grabber = grabber
    if start_grabber:
        grabber.start()

    def _render(snap, raw: bool = False, overlay: bool = False) -> "tuple[bool, bytes]":
        # The single transform+encode boundary shared by /frame and /stream:
        # rotate, then crop unless raw, then JPEG-encode the raw slot frame with
        # the current config. One place so the still and the stream can't silently
        # diverge on quality/color/crop rules.
        with lock:
            rotation, clip = state["rotation"], state["clip"]
        img = rotate(snap.frame, rotation)
        if not raw:
            img = crop(img, clip)
        if overlay and not raw and snap.bbox is not None:
            # Draw the motion bbox onto the served frame for the config UI (an
            # <img> can't read multipart headers). Copy first: img may alias the
            # shared slot frame (rotation 0 + no clip return the raw frame/a view
            # of it), and cv2.rectangle mutates in place. bbox is normalized to
            # the ROI, i.e. to exactly this rotated+cropped img.
            img = img.copy()
            h, w = img.shape[:2]
            bx, by, bw, bh = snap.bbox
            x0, y0 = int(round(bx * w)), int(round(by * h))
            x1, y1 = int(round((bx + bw) * w)), int(round((by + bh) * h))
            cv2.rectangle(img, (x0, y0), (x1, y1), (0, 255, 0), 2)
        return _encode_jpeg(img)

    def _build_part(snap, overlay: bool = False) -> "bytes | None":
        # Encode the (cropped) slot frame and frame it as one multipart part.
        # None if encoding fails. Motion rides the part headers so a stream client
        # gets the pull signal inline (X-Motion always; X-Bbox/X-Area on motion).
        ok, data = _render(snap, overlay=overlay)
        if not ok:
            return None
        part = (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n"
            b"Content-Length: " + str(len(data)).encode() + b"\r\n"
            b"X-Frame-Id: " + str(snap.frame_id).encode() + b"\r\n"
            b"X-Timestamp: " + str(snap.ts).encode() + b"\r\n"
            b"X-Motion: " + (b"1" if snap.motion else b"0") + b"\r\n"
        )
        if snap.motion and snap.bbox is not None:
            bx, by, bw, bh = snap.bbox
            part += (
                b"X-Bbox: " + f"{bx},{by},{bw},{bh}".encode() + b"\r\n"
                b"X-Area: " + str(snap.area).encode() + b"\r\n"
            )
        return part + b"\r\n" + data + b"\r\n"

    @app.get("/")
    def index():
        return send_from_directory(_UI_DIR, "index.html")

    @app.get("/frame")
    def frame():
        # raw is on when the param is present and truthy: raw preview skips the
        # crop (oriented but uncropped) so the UI can drag the ROI on it.
        raw = request.args.get("raw") not in (None, "", "0", "false")
        # overlay draws the motion bbox onto the frame (ignored when raw). /frame
        # carries NO motion headers — poll /status for the pull signal.
        overlay = request.args.get("overlay") not in (None, "", "0", "false")
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
        # Reject a silently-frozen frame: a wedged camera stops advancing (shared
        # freshness rule with /status; monotonic, not wall-clock — see helper).
        if _frame_is_stale(snap, cur_fps):
            return jsonify(error=snap.last_error or "frame stale"), 503
        ok, data = _render(snap, raw=raw, overlay=overlay)
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
        # busy-loop and the cadence is the configured fps. overlay is read here
        # (in the request context) since the generator runs outside it.
        overlay = request.args.get("overlay") not in (None, "", "0", "false")

        def gen():
            last_sent_id = 0
            try:
                snap = grabber.snapshot()
                if snap.frame_id > 0:
                    part = _build_part(snap, overlay=overlay)
                    if part is not None:
                        last_sent_id = snap.frame_id
                        yield part
                while True:
                    snap = grabber.wait_next(last_sent_id, timeout=_STREAM_WAIT_S)
                    if snap.frame_id > last_sent_id and snap.frame is not None:
                        part = _build_part(snap, overlay=overlay)
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

    @app.get("/status")
    def status():
        # The pullable motion + camera-health snapshot (see the motion spec). A
        # plain snapshot, no waiting; a client correlates it to stream frames by
        # frame_id. camera_ok = no error AND a fresh (non-stale) frame, using the
        # same monotonic staleness rule as /frame (the Pi has no RTC).
        snap = grabber.snapshot()
        with lock:
            cur_fps = state["fps"]
        camera_ok = (
            snap.last_error is None
            and snap.frame is not None
            and not _frame_is_stale(snap, cur_fps)
        )
        return jsonify(
            frame_id=snap.frame_id,
            ts=snap.ts,
            motion=snap.motion,
            bbox=list(snap.bbox) if snap.bbox is not None else None,
            area=snap.area,
            camera_ok=camera_ok,
            last_error=snap.last_error,
        )

    @app.post("/api/motion/reset")
    def motion_reset():
        # Manual "Relearn background" from the config UI: drop the MOG2 model so
        # the next grab relearns the scene from scratch.
        grabber.reset_motion()
        return jsonify(ok=True)

    @app.get("/api/config")
    def get_config():
        with lock:
            return jsonify(
                device=state["device"],
                rotation=state["rotation"],
                clip=state["clip"],
                fps=state["fps"],
                var_threshold=state["var_threshold"],
                learning_rate=state["learning_rate"],
                min_area=state["min_area"],
                max_area_fraction=state["max_area_fraction"],
                persistence=state["persistence"],
                motion_downscale=state["motion_downscale"],
            )

    @app.post("/api/config")
    def set_config():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            # Missing, non-JSON, or valid-but-non-object body (5, "x", [] …):
            # treat as empty so it becomes a clean 400, not a 500.
            body = {}
        if not any(k in body for k in _CONFIG_KEYS):
            return jsonify(
                error="config must set at least one of " + ", ".join(_CONFIG_KEYS)
            ), 400

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
        for key, (validator, msg) in _MOTION_VALIDATORS.items():
            if key in body and not validator(body[key]):
                return jsonify(error=msg), 400

        # Cross-field: min_area must stay below max_area_fraction, or the locality
        # gate (min <= area <= max) can never be satisfied and motion is silently
        # impossible. Check the EFFECTIVE values (a partial POST merges with the
        # live state) here — before any camera work — so a bad pair can't leak an
        # opened candidate source.
        eff_min = body["min_area"] if "min_area" in body else state["min_area"]
        eff_max = (
            body["max_area_fraction"]
            if "max_area_fraction" in body
            else state["max_area_fraction"]
        )
        if eff_min >= eff_max:
            return jsonify(error="min_area must be less than max_area_fraction"), 400

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
        for key in _MOTION_VALIDATORS:
            next_config[key] = body[key] if key in body else state[key]
        try:
            save_settings(next_config)
        except OSError as e:
            if candidate is not None:
                candidate.close()
            return jsonify(error=f"failed to persist config: {e}"), 500

        # A device/rotation/clip change re-draws the ROI the MOG2 model is tied to,
        # so relearn from scratch; else new pixels compare against a stale model and
        # burst false motion. Computed before the swap; the reset itself runs after.
        roi_changed = (
            device_changed
            or ("rotation" in body and body["rotation"] != cur_rotation)
            or ("clip" in body and body["clip"] != cur_clip)
        )

        # New-before-close: a fast pointer swap under the lock (only if the device
        # changed), plus the transform/fps/motion params; then close the old source
        # outside it. fps, rotation/clip, and the motion params are picked up live by
        # the grabber and the serving routes on their next lock-guarded read.
        with lock:
            old = None
            if device_changed:
                old = state["source"]
                state["source"] = candidate
                state["device"] = device
            state["rotation"] = next_config["rotation"]
            state["clip"] = next_config["clip"]
            state["fps"] = next_config["fps"]
            for key in _MOTION_VALIDATORS:
                state[key] = next_config[key]
        if old is not None:
            old.close()
        if roi_changed:
            # Relearn the background against the new ROI (safe before grab_once,
            # which recomputes motion from the fresh model).
            grabber.reset_motion()
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
