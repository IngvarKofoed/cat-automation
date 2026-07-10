"""Shared MOG2 motion-gate core, used by BOTH tiers.

The edge grabber runs this live over each captured frame's ROI; the compute
tier's offline tuning re-run instantiates the SAME gate over stored frames, so
an offline parameter sweep is identical to the live gate BY CONSTRUCTION — a
param that improves the re-run improves the Pi, and there is no "second MOG2
that drifts." See docs/specs/2026-07-10-motion-gate-diagnostic.md ("Shared
motion core" and the "One shared motion core" key decision).

Kept dependency-light per shared/'s discipline: ``cv2``/``numpy`` are imported
LAZILY inside the methods, so ``import shared.motion`` stays cheap and never
drags OpenCV into a caller that only wants the ``MotionParams`` type.
"""
from __future__ import annotations

from functools import lru_cache
from typing import NamedTuple


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

    def process(self, roi_bgr, params: MotionParams):
        """Compute the debounced motion decision for one ALREADY rotated+cropped ROI.

        ``roi_bgr`` is the door-region frame the caller has already transformed
        (rotate+crop stays in the caller); this method is the motion core from
        the downscale step onward. Returns ``(motion, bbox, area)`` where
        ``area`` is the largest foreground blob as a fraction of the ROI (always
        reported, for tuning) and ``bbox`` is that blob normalized to the ROI
        (0..1) when motion is active, else ``None``.

        Not internally locked — see the class docstring.
        """
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

        return motion, bbox, area

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
