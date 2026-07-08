"""Persistence for edge camera settings, backed by a JSON file on disk."""

import json
import os
from pathlib import Path

DEFAULTS = {"device": 0, "rotation": 0, "clip": None, "fps": 5}


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
