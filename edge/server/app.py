"""Flask app for the edge tier: serve a still, pick a camera, persist the choice.

The integration hub that ties the capture backends and settings together behind
one HTTP server. See docs/ARCHITECTURE.md (Camera source, Config UI) and
docs/specs/2026-07-07-edge-stills-mvp.md.
"""
from __future__ import annotations

import glob
import os
import platform
import threading
from pathlib import Path
from typing import Callable

import cv2
from flask import Flask, jsonify, request, send_from_directory

from edge.capture.base import CaptureError, CaptureSource
from edge.capture.opencv_source import OpenCVCaptureSource
from edge.config.settings import DEFAULTS, load_settings, save_settings

SourceFactory = Callable[["int | str"], CaptureSource]

_UI_DIR = Path(__file__).resolve().parent / "ui"


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


def create_app(source_factory: "SourceFactory | None" = None) -> Flask:
    """Build the Flask app.

    ``source_factory`` maps a device id to a CaptureSource; it defaults to
    ``OpenCVCaptureSource``. It is the injection seam: tests pass
    ``FakeCaptureSource`` so ``/frame`` and ``POST /api/config`` work with no
    camera. No camera is opened here — the OpenCV source opens lazily on first
    read.
    """
    factory: SourceFactory = source_factory or OpenCVCaptureSource

    app = Flask(__name__)

    # The current-source slot: source + its device id, guarded by one lock.
    # /frame reads under the lock; POST /api/config swaps under it.
    lock = threading.Lock()
    try:
        device = _coerce_device(load_settings()["device"])
    except (ValueError, TypeError):
        # A hand-edited settings.json may hold a parseable-but-invalid device
        # (null, "", a float). Fall back to the default rather than crash boot.
        device = _coerce_device(DEFAULTS["device"])
    state = {"source": factory(device), "device": device}

    @app.get("/")
    def index():
        return send_from_directory(_UI_DIR, "index.html")

    @app.get("/frame")
    def frame():
        with lock:
            source: CaptureSource = state["source"]
            try:
                img = source.read()
            except CaptureError as e:
                return jsonify(error=str(e)), 503
        # Encode outside the lock — it works on the already-captured frame, so it
        # needn't serialize other reads or block a device swap.
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])
        if not ok:
            return jsonify(error="failed to encode frame"), 500
        return app.response_class(buf.tobytes(), mimetype="image/jpeg")

    @app.get("/api/config")
    def get_config():
        with lock:
            return jsonify(device=state["device"])

    @app.post("/api/config")
    def set_config():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            # Missing, non-JSON, or valid-but-non-object body (5, "x", [] …):
            # fall through to the device check so it's a clean 400, not a 500.
            body = {}
        try:
            device = _coerce_device(body.get("device"))
        except (ValueError, TypeError):
            return jsonify(error="device must be an int or a non-empty string"), 400

        # Build and validate the candidate OUTSIDE the lock (a real open is slow).
        candidate = factory(device)
        try:
            candidate.read()
        except CaptureError as e:
            candidate.close()
            # 4xx (not /frame's 503): the client picked a device that doesn't
            # work, distinct from a previously-working camera failing at read
            # time. Per docs/specs/2026-07-07-edge-stills-mvp.md.
            return jsonify(error=str(e)), 422

        # Persist BEFORE swapping: if the write fails, the live source and the
        # saved config still agree (both unchanged) instead of diverging, and the
        # old handle is never leaked mid-swap. Outside the lock so the disk write
        # doesn't stall concurrent /frame reads.
        try:
            save_settings({"device": device})
        except OSError as e:
            candidate.close()
            return jsonify(error=f"failed to persist config: {e}"), 500

        # New-before-close: a fast pointer swap under the lock, then close the
        # old source outside it.
        with lock:
            old = state["source"]
            state["source"] = candidate
            state["device"] = device
        old.close()
        return jsonify(device=device)

    @app.get("/api/cameras")
    def cameras():
        system = platform.system()
        if system == "Darwin":
            entries = [{"device": 0, "label": "Built-in webcam (0)"}]
        elif system == "Linux":
            entries = [
                {"device": path, "label": path}
                for path in sorted(glob.glob("/dev/video*"))
            ]
        else:
            entries = []
        return jsonify(cameras=entries)

    return app


if __name__ == "__main__":
    port = int(os.environ.get("CAT_EDGE_PORT", "8000"))
    create_app().run(host="0.0.0.0", port=port, threaded=True)
