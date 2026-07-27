"""Persistence for edge camera settings, backed by a JSON file on disk."""

import json
import os
from pathlib import Path

DEFAULTS = {
    "device": 0,
    "rotation": 0,
    "clip": None,
    "fps": 5,
    # Lens focus. None = continuous autofocus (the camera keeps itself focused —
    # a safe zero-config default, since a fresh install doesn't know the flap
    # distance). A number = manual focus LOCKED at that many dioptres (1/metres;
    # 0 = infinity, higher = nearer). A fixed door scene is sharpest and most
    # predictable at a locked manual position, so the UI's "autofocus once"
    # finds a value and stores it here. Inert on cameras without focus control
    # (Module 1/2, USB) — see the CaptureSource focus contract in capture/base.py.
    "focus": None,
    # White balance. None = continuous auto WB. A [red, blue] pair = AWB OFF,
    # locked at those gains. A fixed door scene wants the lock for the same reason
    # it wants a locked lens: auto WB re-estimates the illuminant every frame, so a
    # static scene's pixels drift — moving the day/night colourfulness statistic and
    # smearing the day colour cue identification depends on. Critical on a NoIR
    # sensor, where daylight's NIR component gives AWB a moving target all day. The
    # UI's "Lock white balance" finds a pair and stores it here. Inert on cameras
    # without gain control — see the CaptureSource AWB contract in capture/base.py.
    "awb_gains": None,
    # libcamera tuning file name, or None for the backend default. A NoIR sensor
    # wants "imx708_noir.json": the default tuning assumes an IR-cut filter that
    # isn't there, so its colour matrices and AWB priors are wrong. Applied when the
    # camera next opens; an unloadable name falls back to the default (and logs).
    "tuning_file": None,
    "var_threshold": 16.0,
    "learning_rate": 0.001,
    "min_area": 0.01,
    "max_area_fraction": 0.6,
    "persistence": 2,
    # ROI width (px) MOG2 runs on. 320 (not 160): the morphology OPEN kernel is a
    # fixed 3x3, so a coarser ROI erodes a proportionally larger chunk of a
    # cat-sized blob and drops it below min_area within seconds — 320 keeps the
    # blob robust. Still cheap on a Pi 3 (~4x the motion pixels of 160, low ms).
    "motion_downscale": 320,
    # Autonomous night-light scheduler (see edge/server/night_light.py). Drives one
    # relay channel LOW (= on, active-low board) from sunset − on_before_sunset_min
    # to sunrise + off_after_sunrise_min, sun times computed offline from lat/lon
    # (default Copenhagen). Off by default; edited via GET/POST /api/night-light and
    # carried through the whole-config assembly so a camera POST never wipes it.
    "night_light": {
        "enabled": False,
        "channel": "channel1",
        "on_before_sunset_min": 30,
        "off_after_sunrise_min": 30,
        "latitude": 55.676,
        "longitude": 12.568,
    },
}


def _config_path() -> Path:
    """Resolve the settings file path from the environment (read at call time)."""
    return Path(os.environ.get("CAT_EDGE_CONFIG", "edge/config/settings.json"))


def load_settings() -> dict:
    """Read the settings file, filling in defaults; never raises."""
    path = _config_path()
    try:
        with path.open() as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULTS)
    if not isinstance(data, dict):
        # Valid JSON but not an object (null, a number, a list) — a manual edit
        # or partial write. Fall back to defaults rather than crashing callers.
        return dict(DEFAULTS)
    return {**DEFAULTS, **data}


def save_settings(settings: dict) -> None:
    """Write settings to the JSON file, creating the parent directory if needed."""
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        json.dump(settings, f, indent=2)
