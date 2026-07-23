"""Shared MOG2 motion-gate core, used by BOTH tiers.

The edge grabber runs this live over each captured frame's ROI; the compute
tier's offline tuning re-run instantiates the SAME gate over stored frames, so
an offline parameter sweep is identical to the live gate BY CONSTRUCTION — a
param that improves the re-run improves the Pi, and there is no "second MOG2
that drifts." See docs/specs/2026-07-10-motion-gate-diagnostic.md ("Shared
motion core" and the "One shared motion core" key decision).

A per-frame CORRUPT-FRAME GUARD runs at the very top of ``process`` (before
MOG2): the Pi's CSI camera intermittently emits thin coloured lines and a
whole-frame magenta cast that would falsely trip MOG2, so a cheap chroma test
recognizes and skips them — reported as ``corrupt`` on the ``MotionResult``,
with no background update and no debounce change. Living in this shared core
means the edge live gate and the compute offline re-run skip them identically.
See docs/specs/2026-07-23-corrupt-frame-motion-guard.md.

Kept dependency-light per shared/'s discipline: ``cv2``/``numpy`` are imported
LAZILY inside the methods, so ``import shared.motion`` stays cheap and never
drags OpenCV into a caller that only wants the ``MotionParams`` type.
"""
from __future__ import annotations

from functools import lru_cache
from typing import NamedTuple

# Corrupt-frame guard thresholds (hardware-glitch constants, NOT scene tuning —
# so they live here as module constants, out of the persisted ``MotionParams``
# and the tuning UI; see docs/specs/2026-07-23-corrupt-frame-motion-guard.md).
# Conservative + PREVIEW-CALIBRATED: derived from downscaled chat previews (the
# worst case for line dilution), biased so a real cat is never suppressed (a
# faint line may slip until these are tightened on real frames). To be tuned on
# real captured frames via a MogAnalyzer re-run over a bucket holding both
# corrupt frames and cat visits.
_CORRUPT_CAST_CHROMA = 60  # global median row-chroma (a healthy scene reads ~17-19)
_CORRUPT_CHANNEL_RATIO = 0.30  # min-channel mean <= 0.30 * max => a collapsed channel
_CORRUPT_LINE_EXCESS = 22  # per-row chroma over its local baseline to flag the row
_CORRUPT_BASELINE_HALFWIN = 15  # rows each side of a row for its baseline median
_CORRUPT_LINE_MAX_ROWS = 20  # a flagged band this tall or thinner is a line (taller => real)


class MotionResult(NamedTuple):
    """The gate's per-frame decision: the motion verdict plus a corruption flag.

    ``motion``/``bbox``/``area`` are the debounced decision as before (``area``
    the largest foreground blob as a fraction of the ROI, always reported for
    tuning; ``bbox`` that blob normalized to 0..1 when motion is active, else
    ``None``). ``corrupt`` is True when the frame was recognized as CSI
    corruption (a thin coloured line or a whole-frame colour cast) and skipped
    BEFORE MOG2 — in that case ``motion`` is False, ``bbox`` None, ``area`` 0.0.
    A named result (not a bare tuple) so a future per-frame classification field
    is one line, not another contract change.
    """

    motion: bool
    bbox: "tuple | None"
    area: float
    corrupt: bool


def corruption_thresholds() -> dict:
    """The ``_CORRUPT_*`` constants as a plain dict, for stamping into a verdict.

    Exposed so an offline sweep (``compute.analysis.corruption.CorruptionAnalyzer``)
    can stamp the exact thresholds a verdict was computed under into its
    ``detail`` — cheap STALENESS detection: a stored verdict whose stamped
    thresholds differ from these current values predates a constant retune and
    should be re-swept. Plain ints/floats only (JSON-serializable, no numpy), so
    importing this stays as dependency-light as the rest of the module's public
    surface. Keys mirror the constant names without the ``_CORRUPT_`` prefix.
    """
    return {
        "cast_chroma": _CORRUPT_CAST_CHROMA,
        "channel_ratio": _CORRUPT_CHANNEL_RATIO,
        "line_excess": _CORRUPT_LINE_EXCESS,
        "baseline_halfwin": _CORRUPT_BASELINE_HALFWIN,
        "line_max_rows": _CORRUPT_LINE_MAX_ROWS,
    }


def classify_corruption(roi_bgr) -> "str | None":
    """Which CSI-corruption check fires on ``roi_bgr`` — ``"cast"`` / ``"line"`` / ``None``.

    The single detector for the corrupt-frame guard: the live gate calls it via
    the thin ``_is_corrupt`` wrapper, and the offline ``CorruptionAnalyzer`` calls
    it directly to record WHICH check fired (the ``reason``). ``None`` means the
    frame is not corruption. There is exactly ONE implementation of the logic, so
    an offline swept verdict equals the live verdict for the same frame.

    Runs on the FULL, un-downscaled ROI (downscaling would blur a 1-few-row
    line away) BEFORE any resize. The signal is ABSOLUTE BGR chroma, not HSV
    saturation: ``chroma = max(B,G,R) - min(B,G,R)`` per pixel stays ~0 for dark
    greys and for a bright neutral door frame, spiking only on genuinely coloured
    pixels — whereas HSV-S inflates on dark pixels and would flag the neutral
    scene (measured; see the spec's "Alternatives considered").

    One per-row mean-chroma profile drives both checks:

    - **Cast** (``"cast"``): ``median(row_chroma) >= _CORRUPT_CAST_CHROMA`` AND a
      collapsed channel (``min(channel means) <= _CORRUPT_CHANNEL_RATIO * max``).
      Both required, so a merely vivid-but-healthy scene isn't suppressed.
    - **Line** (``"line"``): any THIN band (<= ``_CORRUPT_LINE_MAX_ROWS``
      contiguous rows) whose per-row chroma sits ``>= _CORRUPT_LINE_EXCESS`` above
      a LOCAL median baseline (``+/- _CORRUPT_BASELINE_HALFWIN`` rows). The wide
      local baseline absorbs a bright sky diagonal or the door frame and rises
      inside a large real coloured object (e.g. a ginger cat), so a tall flagged
      band reads as real, not corruption.

    The cast check is evaluated first, so a frame that trips both reports
    ``"cast"``. Non-3-channel (mono/IR) ROIs bypass the guard (chroma needs 3
    channels) and return ``None``. Tiny/short ROIs are handled gracefully — the
    baseline window simply clamps to the rows that exist. ``numpy`` is imported
    lazily here per shared/'s dependency-light discipline.
    """
    import numpy as np

    # Mono/IR (2D) or single-channel ROIs have no chroma to measure — bypass.
    if roi_bgr.ndim != 3 or roi_bgr.shape[2] != 3:
        return None

    # Per-pixel chroma = max - min across BGR. int16 so the uint8 subtraction
    # (max >= min, so never negative) cannot wrap.
    roi = roi_bgr.astype(np.int16)
    chroma = roi.max(axis=2) - roi.min(axis=2)
    row_chroma = chroma.mean(axis=1)  # one mean-chroma value per row

    # --- cast: strong global colour AND a near-dead channel ---
    channel_means = roi_bgr.reshape(-1, 3).mean(axis=0)
    lo, hi = float(channel_means.min()), float(channel_means.max())
    collapsed = lo <= _CORRUPT_CHANNEL_RATIO * hi
    if float(np.median(row_chroma)) >= _CORRUPT_CAST_CHROMA and collapsed:
        return "cast"

    # --- line: a thin band spiking above its LOCAL baseline ---
    n = row_chroma.shape[0]
    w = _CORRUPT_BASELINE_HALFWIN
    # base[r] = median over the clamped window [r-w, r+w]; clamping (vs. edge/
    # reflect padding) keeps an edge line detectable — its window still reaches
    # the neutral rows just inside it rather than repeating the corrupt row.
    baseline = np.empty(n, dtype=np.float64)
    for r in range(n):
        baseline[r] = np.median(row_chroma[max(0, r - w) : r + w + 1])
    flagged = (row_chroma - baseline) >= _CORRUPT_LINE_EXCESS
    # Scan contiguous flagged runs; a run no taller than the thin-band limit is a line.
    run = 0
    for is_flagged in flagged:
        if is_flagged:
            run += 1
        else:
            if 0 < run <= _CORRUPT_LINE_MAX_ROWS:
                return "line"
            run = 0
    if 0 < run <= _CORRUPT_LINE_MAX_ROWS:
        return "line"
    return None


def _is_corrupt(roi_bgr) -> bool:
    """True if ``roi_bgr`` is a CSI-corruption frame (thin colour line OR colour cast).

    The gate's boolean guard, kept as a one-line wrapper over
    ``classify_corruption`` so there is a SINGLE detector: the guard and the
    offline analyzer can never disagree about whether a frame is corrupt.
    """
    return classify_corruption(roi_bgr) is not None


class MotionParams(NamedTuple):
    """The six motion-detection parameters (the exact set the edge persists).

    ``downscale`` is the target ROI width in px before MOG2; the rest tune
    MOG2's variance threshold + background learning rate and the
    locality/persistence decision rule. Passed to ``MotionGate.process`` per
    call rather than held by the gate, so the caller owns them and can retune
    live; the gate holds only the learned model + the debounce streak.
    """

    var_threshold: float
    learning_rate: float
    min_area: float
    max_area_fraction: float
    persistence: int
    downscale: int


@lru_cache(maxsize=1)
def _motion_kernel():
    """The 3x3 OPEN structuring element that despeckles the foreground mask.

    3x3 removes isolated pixels/noise without eroding a cat-sized blob at the
    downscaled resolution. Built lazily (so importing this module doesn't import
    cv2) and memoized so it is created exactly once across all gates — it is a
    constant.
    """
    import cv2

    return cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))


class MotionGate:
    """Stateful MOG2 motion gate: the learned background model + debounce streak.

    Holds NO params — each ``process`` call takes the current ``MotionParams``,
    so the caller (the live grabber, or an offline re-run) can retune between
    frames. The MOG2 instance is reused across calls (its learned background is
    its whole value); only ``reset`` drops it.

    NOT internally locked — the CALLER owns synchronization. The edge grabber
    calls ``process``/``reset`` under its own ``_motion_lock`` (so a concurrent
    relearn can't swap the model mid-computation); the compute offline re-run is
    single-threaded. Do not share one gate across threads without external
    locking.
    """

    def __init__(self) -> None:
        # The MOG2 instance and the var_threshold it was last created/set with,
        # so a change is applied LIVE via setVarThreshold rather than by dropping
        # the learned background. ``_streak`` counts consecutive raw-motion
        # frames for the persistence debounce.
        self._mog2 = None
        self._mog2_var_threshold: "float | None" = None
        self._streak = 0

    def process(self, roi_bgr, params: MotionParams) -> MotionResult:
        """Compute the debounced motion decision for one ALREADY rotated+cropped ROI.

        ``roi_bgr`` is the door-region frame the caller has already transformed
        (rotate+crop stays in the caller); this method is the motion core from
        the downscale step onward. Returns a ``MotionResult`` where ``area`` is
        the largest foreground blob as a fraction of the ROI (always reported,
        for tuning), ``bbox`` is that blob normalized to the ROI (0..1) when
        motion is active (else ``None``), and ``corrupt`` flags a skipped CSI
        corruption frame.

        A corruption frame (thin coloured line or whole-frame colour cast) is
        recognized FIRST, on the full un-downscaled ROI, and skipped entirely:
        it returns ``MotionResult(False, None, 0.0, True)`` WITHOUT running MOG2
        and WITHOUT touching the debounce streak — the background is never
        poisoned and a single glitch mid-crossing neither advances nor resets the
        streak (transparent to gate state). See the module's ``_is_corrupt``.

        Not internally locked — see the class docstring.
        """
        if _is_corrupt(roi_bgr):
            return MotionResult(False, None, 0.0, True)

        import cv2

        height, width = roi_bgr.shape[:2]
        # Downscale to the target width (never upscale a small ROI), keep aspect.
        target_w = max(1, min(int(params.downscale), int(width)))
        target_h = max(1, round(height * (target_w / float(width))))
        small = cv2.resize(
            roi_bgr, (target_w, target_h), interpolation=cv2.INTER_AREA
        )
        # Accept mono/IR sources too, not just 3-channel BGR: cvtColor(BGR2GRAY)
        # on an already-single-channel frame raises, which would otherwise make a
        # perfectly good grayscale night camera error out every iteration.
        if small.ndim == 2:
            gray = small
        elif small.shape[2] == 1:
            gray = small[:, :, 0]
        else:
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

        mog2 = self._ensure_mog2(params)
        mask = mog2.apply(gray, learningRate=params.learning_rate)
        # Keep only hard foreground: with detectShadows=True MOG2 marks shadows
        # as gray 127, so threshold at 254 drops both shadow and background.
        _, fg = cv2.threshold(mask, 254, 255, cv2.THRESH_BINARY)
        fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, _motion_kernel())
        count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(
            fg, connectivity=8
        )
        # Largest NON-background component (label 0 is background).
        best_i, best_px = 0, 0
        for i in range(1, count):
            px = int(stats[i, cv2.CC_STAT_AREA])
            if px > best_px:
                best_i, best_px = i, px

        total = target_w * target_h
        area = best_px / total if total else 0.0
        # Locality gate: reject nothing-there and whole-ROI (illumination) blobs;
        # only a compact, cat-sized blob counts as raw motion.
        raw_motion = (
            best_i > 0 and params.min_area <= area <= params.max_area_fraction
        )
        if raw_motion:
            self._streak += 1
        else:
            self._streak = 0
        motion = self._streak >= params.persistence

        if motion:
            x = int(stats[best_i, cv2.CC_STAT_LEFT])
            y = int(stats[best_i, cv2.CC_STAT_TOP])
            bw = int(stats[best_i, cv2.CC_STAT_WIDTH])
            bh = int(stats[best_i, cv2.CC_STAT_HEIGHT])
            bbox = (x / target_w, y / target_h, bw / target_w, bh / target_h)
        else:
            bbox = None

        return MotionResult(motion, bbox, area, False)

    def _ensure_mog2(self, params: MotionParams):
        """Return the MOG2 instance, creating it lazily and applying live tuning.

        The instance is reused across calls (its learned background is its whole
        value); only ``reset`` drops it. ``var_threshold`` is applied live via
        ``setVarThreshold`` so tuning takes effect WITHOUT a relearn (which would
        burst false motion while the model re-adapts).
        """
        import cv2

        var_threshold = float(params.var_threshold)
        if self._mog2 is None:
            self._mog2 = cv2.createBackgroundSubtractorMOG2(
                varThreshold=var_threshold, detectShadows=True
            )
            self._mog2_var_threshold = var_threshold
        elif var_threshold != self._mog2_var_threshold:
            self._mog2.setVarThreshold(var_threshold)
            self._mog2_var_threshold = var_threshold
        return self._mog2

    def reset(self) -> None:
        """Drop the MOG2 model and zero the debounce streak (relearn next process).

        The model is tied to the exact ROI pixels/dimensions, so anything that
        changes its input imagery — a device swap, a clip/rotation change, or a
        manual relearn — must recreate it, else new pixels would be compared
        against a stale model and burst false motion. The next ``process``
        recreates the instance lazily.

        Not internally locked — see the class docstring.
        """
        self._mog2 = None
        self._mog2_var_threshold = None
        self._streak = 0
