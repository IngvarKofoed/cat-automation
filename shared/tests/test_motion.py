"""Tests for the shared MOG2 motion-gate core (shared/motion.py).

shared/motion.py is the single motion core both tiers run: the edge grabber
live, the compute tuning re-run offline. These tests pin the behaviors that
must hold for that shared-ness to be trustworthy, driven by synthetic numpy
frames (no camera):

- Raw motion on a sustained, cat-sized blob, with a roughly-correct bbox.
- The locality gate: a blob below ``min_area`` and a whole-ROI illumination
  jump above ``max_area_fraction`` are both rejected even though MOG2 *sees*
  foreground — only a compact, in-band blob counts.
- The persistence debounce: a blob below ``persistence`` consecutive frames is
  not yet motion; reaching the streak flips it.
- ``reset()`` drops the learned model and zeroes the streak (a fresh relearn).
- A live ``var_threshold`` change is applied via ``setVarThreshold`` WITHOUT
  dropping the learned background — the property that lets the UI tune without
  bursting false motion.
- ``classify_corruption`` (the compute-side, review-only corruption detector that
  also lives in this module) names the check that fired (cast/line/None). It is
  NOT used by the gate — a corrupt frame can still contain a real cat.

MOG2 is stateful, so a fresh gate must first learn the flat synthetic
background (``_warm``) before a blob means anything — the same convergence the
edge's grabber tests rely on.
"""
from __future__ import annotations

import numpy as np
import pytest

from shared.motion import MotionGate, MotionParams

# A synthetic door ROI: flat mid-grey background with a small bright rectangle
# standing in for a cat. Values mirror the edge's ControllableCaptureSource so
# the same params converge here.
_W, _H = 160, 120
_BG_LEVEL = 60
_BLOB_LEVEL = 220
# (x, y, w, h) in px: well inside the frame and ~4.7% of the ROI, comfortably
# inside the default [min_area, max_area_fraction] band.
_BLOB_RECT = (60, 40, 30, 30)

# Production-default motion params (edge/config/settings.py). downscale=160 == the
# ROI width, so no downscale happens and blob geometry maps 1:1 to px fractions.
_BASE = dict(
    var_threshold=16.0,
    learning_rate=0.001,
    min_area=0.01,
    max_area_fraction=0.6,
    persistence=2,
    downscale=160,
)


def _params(**overrides) -> MotionParams:
    return MotionParams(**{**_BASE, **overrides})


def _background() -> np.ndarray:
    return np.full((_H, _W, 3), _BG_LEVEL, dtype=np.uint8)


def _with_blob(rect=_BLOB_RECT, level=_BLOB_LEVEL) -> np.ndarray:
    frame = _background()
    x, y, w, h = rect
    frame[y : y + h, x : x + w] = level
    return frame


def _bright() -> np.ndarray:
    # Whole-ROI brightness jump (a cloud/illumination change), not a compact blob.
    return np.full((_H, _W, 3), _BLOB_LEVEL, dtype=np.uint8)


def _magenta_cast() -> np.ndarray:
    # Whole-frame magenta corruption: green channel collapsed to ~0, blue+red
    # bright — the observed CSI cast. High global chroma AND a dead channel.
    frame = np.zeros((_H, _W, 3), dtype=np.uint8)
    frame[:, :, 0] = 200  # B
    frame[:, :, 1] = 2  # G (collapsed)
    frame[:, :, 2] = 200  # R
    return frame


def _colored_line(rows=(50, 52), color=(20, 20, 200)) -> np.ndarray:
    # A neutral-grey frame with a thin full-width coloured band (the line glitch).
    frame = _background()
    y0, y1 = rows
    frame[y0:y1, :] = color
    return frame


def _colored_blob(rect=_BLOB_RECT, color=(20, 80, 200)) -> np.ndarray:
    # A neutral-grey frame with a COMPACT coloured object (a ginger-ish "cat") —
    # a real coloured region, must NOT be mistaken for corruption.
    frame = _background()
    x, y, w, h = rect
    frame[y : y + h, x : x + w] = color
    return frame


def _vivid_balanced() -> np.ndarray:
    # A uniformly vivid but HEALTHY frame: high global chroma (all pixels
    # (100,150,200) -> per-row chroma 100, well over _CORRUPT_CAST_CHROMA=60) yet
    # NO collapsed channel (min mean 100 > 0.30 * max mean 200 = 60). The cast
    # check requires high chroma AND a dead channel, so this must NOT be corrupt.
    frame = np.empty((_H, _W, 3), dtype=np.uint8)
    frame[:, :, 0] = 100  # B
    frame[:, :, 1] = 150  # G
    frame[:, :, 2] = 200  # R
    return frame


def _mono() -> np.ndarray:
    # A single-channel (grayscale/IR) ROI — no chroma, so the guard must bypass it.
    return np.full((_H, _W), _BG_LEVEL, dtype=np.uint8)


def _warm(gate: MotionGate, params: MotionParams, n: int = 15) -> None:
    """Feed n identical background frames so the flat scene is learned.

    15 is comfortably enough for a noise-free synthetic frame (the edge's own
    grabber tests converge in ~10); a real camera needs far more.
    """
    for _ in range(n):
        gate.process(_background(), params)


# --- raw motion -------------------------------------------------------------


def test_motion_on_sustained_blob_with_roughly_correct_bbox():
    gate = MotionGate()
    params = _params()
    _warm(gate, params)

    motion = False
    for _ in range(params.persistence):
        motion, bbox, area = gate.process(_with_blob(), params)

    assert motion is True
    assert bbox is not None
    assert params.min_area <= area <= params.max_area_fraction
    bx, by, bw, bh = bbox
    x, y, w, h = _BLOB_RECT
    assert bx == pytest.approx(x / _W, abs=0.05)
    assert by == pytest.approx(y / _H, abs=0.05)
    assert bw == pytest.approx(w / _W, abs=0.05)
    assert bh == pytest.approx(h / _H, abs=0.05)


# --- locality gate ----------------------------------------------------------


def test_locality_gate_rejects_blob_below_min_area():
    # A 10x10 blob is ~0.005 of the ROI — MOG2 sees it (area > 0), but it is
    # below min_area (0.01), so it must never count as motion.
    gate = MotionGate()
    params = _params()
    _warm(gate, params)

    motion = False
    area = 0.0
    for _ in range(params.persistence + 2):
        motion, _bbox, area = gate.process(_with_blob(rect=(70, 50, 10, 10)), params)

    assert 0.0 < area < params.min_area
    assert motion is False


def test_locality_gate_rejects_whole_roi_illumination():
    # A whole-ROI brightness jump exceeds max_area_fraction (0.6) and must be
    # rejected — this is exactly how a cloud is told from a cat-sized blob.
    gate = MotionGate()
    params = _params()
    _warm(gate, params)

    motion = True
    area = 0.0
    for _ in range(params.persistence + 2):
        motion, _bbox, area = gate.process(_bright(), params)

    assert area > params.max_area_fraction
    assert motion is False


# --- persistence debounce ---------------------------------------------------


def test_persistence_debounce_requires_consecutive_frames():
    gate = MotionGate()
    params = _params(persistence=3)
    _warm(gate, params)

    # Frames 1 and 2 build the streak but stay below persistence=3.
    m1, _b1, _a1 = gate.process(_with_blob(), params)
    assert m1 is False
    m2, _b2, _a2 = gate.process(_with_blob(), params)
    assert m2 is False
    # Frame 3 reaches the streak and flips motion on.
    m3, _b3, _a3 = gate.process(_with_blob(), params)
    assert m3 is True


def test_persistence_streak_resets_on_a_still_frame():
    # A single non-motion frame breaks the streak, so a following blob frame
    # must climb from zero again rather than fire immediately.
    gate = MotionGate()
    params = _params(persistence=2)
    _warm(gate, params)

    assert gate.process(_with_blob(), params)[0] is False  # streak 1
    assert gate.process(_background(), params)[0] is False  # streak -> 0
    assert gate.process(_with_blob(), params)[0] is False  # streak 1 again, not 2


# --- reset ------------------------------------------------------------------


def test_reset_drops_model_and_zeroes_streak():
    gate = MotionGate()
    params = _params()
    _warm(gate, params)

    for _ in range(params.persistence):
        motion = gate.process(_with_blob(), params)[0]
    assert motion is True
    model_before = gate._mog2
    assert model_before is not None

    gate.reset()
    assert gate._mog2 is None  # model dropped
    assert gate._streak == 0  # debounce zeroed

    # With the model gone the blob scene must be relearned from scratch before
    # motion means anything again, and a fresh model instance is built.
    _warm(gate, params)
    assert gate.process(_background(), params)[0] is False
    assert gate._mog2 is not model_before

    for _ in range(params.persistence):
        motion = gate.process(_with_blob(), params)[0]
    assert motion is True


# --- live var_threshold change ----------------------------------------------


def test_var_threshold_change_is_live_and_keeps_the_background():
    # Changing var_threshold between frames must retune the SAME MOG2 instance
    # (via setVarThreshold), not drop the learned background — dropping it would
    # burst false motion while the model re-adapts.
    gate = MotionGate()
    params = _params(var_threshold=16.0)
    _warm(gate, params)

    model = gate._mog2
    assert model is not None
    assert gate._mog2_var_threshold == 16.0

    retuned = _params(var_threshold=40.0)
    gate.process(_background(), retuned)

    assert gate._mog2 is model  # same instance — background preserved
    assert gate._mog2_var_threshold == 40.0
    assert gate._mog2.getVarThreshold() == pytest.approx(40.0)


# --- classify_corruption: the compute-side corruption detector --------------
#
# ``classify_corruption`` (and ``corruption_thresholds``) live in shared.motion
# but are used ONLY by the compute-side corruption review (the CorruptionAnalyzer
# sweep + the corruption page) — the motion gate does NOT use them (a corrupt
# frame can contain a real cat). It names which check fired ("cast"/"line"/None).


def test_classify_corruption_names_the_check_that_fired():
    from shared.motion import classify_corruption

    assert classify_corruption(_magenta_cast()) == "cast"
    assert classify_corruption(_colored_line()) == "line"
    assert classify_corruption(_background()) is None
    assert classify_corruption(_colored_blob()) is None
    assert classify_corruption(_vivid_balanced()) is None
    assert classify_corruption(_mono()) is None  # 2D ROI bypasses (None, not crash)


def test_corruption_thresholds_exposes_the_constants():
    from shared.motion import (
        _CORRUPT_BASELINE_HALFWIN,
        _CORRUPT_CAST_CHROMA,
        _CORRUPT_CHANNEL_RATIO,
        _CORRUPT_LINE_EXCESS,
        _CORRUPT_LINE_MAX_ROWS,
        corruption_thresholds,
    )

    assert corruption_thresholds() == {
        "cast_chroma": _CORRUPT_CAST_CHROMA,
        "channel_ratio": _CORRUPT_CHANNEL_RATIO,
        "line_excess": _CORRUPT_LINE_EXCESS,
        "baseline_halfwin": _CORRUPT_BASELINE_HALFWIN,
        "line_max_rows": _CORRUPT_LINE_MAX_ROWS,
    }
    # JSON-serializable (it is stamped into a verdict's detail and json.dumps'd).
    import json

    json.dumps(corruption_thresholds())
