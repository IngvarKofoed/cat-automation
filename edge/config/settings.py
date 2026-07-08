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
