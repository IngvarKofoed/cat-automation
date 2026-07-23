"""``MogAnalyzer`` — the offline re-run of the edge's EXACT motion gate, for tuning.

Where ``YoloAnalyzer``/``BsuvAnalyzer`` are *fixed ground-truth oracles* (registered
in ``ANALYZER_NAMES`` and constructed by ``get_analyzer``), this analyzer is a
*parameterized tuning run*: it re-executes the Pi's live motion gate
(``shared.motion.MotionGate`` — the same code the edge grabber runs) over the stored
frames with a caller-chosen ``MotionParams`` set. Because it is the *same* gate, a
parameter that improves the offline re-run improves the Pi by construction — there
is no "second MOG2 that drifts" (see docs/specs/2026-07-10-motion-gate-diagnostic.md,
"MogAnalyzer" and "The tuning flow"). Its verdicts persist to the ``analysis`` table
under a named slot (``mog2:baseline`` / ``mog2:candidate``) so two re-runs can be
diffed as (missed, false-trigger) scorecards against an oracle.

**Deliberately NOT in the oracle registry.** Its params come from the run request,
which ``get_analyzer(name)`` cannot supply, so the tuning path constructs it directly
and hands the instance to ``AnalysisManager.enqueue_analyzer`` — no ``ANALYZER_NAMES``
entry, no ``get_analyzer`` branch.

``windowed = True`` for the same reason ``BsuvAnalyzer`` is: MOG2 is stateful (its
background model builds frame-by-frame), so the runner must feed it frames in strict
time order and it carries state across ``analyze`` calls. ``prepare`` warm-starts the
background off recent stored frames so the sweep's first scored frames don't burst
false motion against a still-adapting model — mirroring ``BsuvAnalyzer._warm_start``.

**No ML extras.** ``cv2``/``numpy`` are in the LEAN ``compute/requirements.txt`` (the
motion gate already needs OpenCV), so this runs on any box — unlike BSUV's CUDA-bound
net. ``cv2`` is still imported lazily inside methods, keeping ``import
compute.analysis.mog2`` cheap and matching the package's lazy-import discipline. The
top-level ``shared.motion`` import is itself cv2-free (it too imports cv2 lazily).
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from compute.analysis.base import AnalysisResult
from shared.motion import MotionGate, MotionParams

if TYPE_CHECKING:
    # Type-only — see the module docstring; never imported at runtime here, so the
    # "no cv2 at module import" property holds. ``np`` names the BGR frame
    # ``analyze`` receives; ``Store`` names the handle ``prepare`` primes off.
    import numpy as np

    from compute.collection.store import Store

logger = logging.getLogger(__name__)

# How many recent stored frames prime the MOG2 background before the sweep's first
# scored frame. 500 mirrors MOG2's default ``history`` (and the scorecard's warmup
# prefix in the spec), so the cold-started model has stabilized before its verdicts
# count. Negligible over the multi-day collection tuning runs against.
_WARMUP = 500

# ``recent_before(id, n)`` returns the ``n`` frames with row id < ``id`` — so a huge
# sentinel id selects the newest ``n`` frames in the store to prime with (the same
# device ``BsuvAnalyzer`` uses; see its ``_WARMSTART_ID``).
_WARMSTART_ID = 1 << 62

# The two slots a tuning compare diffs: the Pi's current settings vs. the edited
# candidate. The slot is the only thing distinguishing two MOG2 re-runs in the
# shared ``analysis`` table, so it is validated at construction.
_VALID_SLOTS = ("baseline", "candidate")


def _decode_frame(path: str) -> "np.ndarray | None":
    """Decode one stored JPEG to a BGR ndarray, or ``None`` on a missing/corrupt file.

    Used only to prime the warm-start background. ``cv2`` is imported here (not at
    module scope) per the lazy discipline. ``cv2.imread`` returns ``None`` for a file
    retention just evicted or a truncated write, which ``_warm_start`` simply skips —
    a stale warm-start path can't crash a sweep before it begins.
    """
    import cv2

    return cv2.imread(path, cv2.IMREAD_COLOR)


class MogAnalyzer:
    """Windowed offline re-run of the edge MOG2 gate; satisfies the ``Analyzer`` protocol.

    Constructed with an EXPLICIT ``MotionParams`` (from the tuning run request — never
    env vars, since sweeping the params is the whole point) and a ``slot`` naming which
    re-run this is. ``name`` is ``mog2:{slot}`` — the ``analysis.analyzer`` column value
    its verdicts land under, and what ``AnalysisManager.status()['analyzer']`` reports.
    ``windowed = True`` — see the module docstring.
    """

    windowed = True

    def __init__(self, params: MotionParams, slot: str) -> None:
        if slot not in _VALID_SLOTS:
            raise ValueError(f"slot must be one of {_VALID_SLOTS}, got {slot!r}")
        # Params are held on the instance (unlike the gate, which is stateless w.r.t.
        # params and takes them per ``process`` call) because a whole re-run uses one
        # fixed set — the caller's request — and they are also echoed into every
        # verdict's ``detail`` so a stored scorecard records exactly what produced it.
        self._params = params
        self.slot = slot
        self.name = f"mog2:{slot}"
        # Built in prepare(); analyze() before prepare() is a caller bug, not a runtime
        # condition to design around, so it fails loud (see analyze()).
        self._gate: "MotionGate | None" = None

    def ensure_available(self) -> None:
        """Verify ``cv2`` imports; raise ``ImportError`` with a hint if absent.

        Unlike the yolo/bsuv oracles this needs NO opt-in ML extras — ``cv2`` (OpenCV)
        ships in the lean ``compute/requirements.txt`` because the shared motion gate
        depends on it — but the runner still calls this synchronously in
        ``start_analyzer`` (see ``Analyzer.ensure_available``), so a broken or absent
        OpenCV surfaces at request time (→ 503) instead of vanishing into the worker.
        """
        try:
            import cv2  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "MogAnalyzer requires 'cv2' (OpenCV), which ships in the compute "
                "tier's lean compute/requirements.txt (the shared motion gate needs "
                "it) — reinstall with: pip install -r compute/requirements.txt"
            ) from exc

    def prepare(self, store: "Store", since_id: "int | None" = None) -> None:
        """Build a fresh ``MotionGate`` and warm-start its background off recent frames.

        A fresh gate per job so a re-run never inherits a prior sweep's learned model,
        then replay ``_WARMUP`` recent stored frames through ``gate.process`` to prime the
        MOG2 background — mirroring ``BsuvAnalyzer``'s window priming, and the reason this
        analyzer is windowed. Without it the first scored frames would see a still-adapting
        model and burst false motion exactly where recall matters most.

        ``since_id`` scopes the priming: on a scoped run (a group's ``start_id``) the model
        primes from the frames IMMEDIATELY BEFORE the window so it enters that era
        genuinely warm; unscoped (``None``) it primes from the newest frames as before.
        """
        self.ensure_available()
        # Fresh gate per job (no prior sweep's model), primed from recent frames.
        self._gate = MotionGate()
        self._warm_start(store, since_id)

    def _warm_start(self, store: "Store", since_id: "int | None" = None) -> None:
        """Prime the MOG2 background by replaying recent stored frames (best-effort).

        The priming anchor is ``since_id`` on a scoped run, else the ``_WARMSTART_ID``
        sentinel: ``recent_before(anchor, _WARMUP)`` returns the ``_WARMUP`` frames just
        before the anchor in chronological order. Scoped, that is the frames IMMEDIATELY
        BEFORE the window, so the model enters it warm for that era; unscoped, the sentinel
        selects the *newest* frames even though the sweep itself runs oldest-first — the
        same bounded approximation ``BsuvAnalyzer`` makes: there is nothing before the
        oldest frame to prime from, and at a fixed door the scene is largely static, so any
        recent window is a far better prior than a cold model. Frames are replayed in
        chronological order (``recent_before``'s contract); an empty store yields an empty
        prime (graceful cold start). A store-read failure is logged and skipped — priming
        is an optimization, never a correctness requirement, so it must not abort the whole
        sweep before it begins.
        """
        assert self._gate is not None  # set by prepare() immediately before this call
        anchor = since_id if since_id is not None else _WARMSTART_ID
        try:
            paths = store.recent_before(anchor, _WARMUP)
        except Exception:
            logger.warning("mog2: warm-start read failed; starting with a cold model", exc_info=True)
            return
        primed = 0
        for path in paths:
            image = _decode_frame(path)
            if image is not None:
                # Discard the verdict — the point is priming the background model, which
                # gate.process updates as a side effect of MOG2's ``apply``.
                self._gate.process(image, self._params)
                primed += 1
        logger.info("mog2[%s]: warm-started model with %d/%d recent frames", self.slot, primed, _WARMUP)

    def analyze(self, image: "np.ndarray") -> AnalysisResult:
        """Return the motion verdict for one BGR frame (already the ROI the Pi streams).

        Must be called in strict time order (windowed): ``gate.process`` reads *and*
        updates the MOG2 background + debounce streak, so successive calls carry state.
        The verdict is the gate's debounced ``motion`` and the score is the largest
        blob's ``area`` fraction (always reported, so a stored re-run can be re-bucketed
        against thresholds later without re-running); ``detail`` echoes the normalized
        ``bbox``, the exact six params that produced this verdict, and ``corrupt`` —
        the shared gate's flag that this frame was skipped as CSI corruption (a thin
        coloured line or a colour cast), so an offline sweep can count/inspect
        corruption without re-running detection.

        Stored frames are ALREADY rotated+cropped (the Pi streams the ROI), so unlike
        the edge grabber this passes ``image`` straight to the gate with no transform.
        """
        if self._gate is None:
            raise RuntimeError("MogAnalyzer.analyze() called before prepare()")

        result = self._gate.process(image, self._params)
        detail = {
            "bbox": result.bbox,
            "params": self._params._asdict(),
            "corrupt": result.corrupt,
        }
        return AnalysisResult(verdict=result.motion, score=result.area, detail=detail)
