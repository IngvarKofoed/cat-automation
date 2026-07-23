"""``CorruptionAnalyzer`` — the offline sweep of the shared corrupt-frame guard.

Where ``YoloAnalyzer``/``BsuvAnalyzer`` are ground-truth *oracles* about cats /
foreground and ``MogAnalyzer`` is a parameterized MOG2 tuning re-run, this
analyzer records the edge's per-frame CORRUPT-FRAME guard verdict over stored
frames. It wraps ``shared.motion.classify_corruption`` — the EXACT function the
live gate runs at the top of ``MotionGate.process`` — so a swept verdict equals
the live verdict for that frame BY CONSTRUCTION (the same single-detector
guarantee ``MogAnalyzer`` gives for MOG2). The persisted verdict is what lets the
corruption-review page read a cheap stored flag and FILTER a whole range to
corrupt (and corrupt-∧-cat) frames instead of decoding every JPEG on each load.
See docs/specs/2026-07-23-corruption-review-page.md.

**Deliberately NOT in the oracle registry** (``ANALYZER_NAMES``). ``ANALYZER_NAMES``
drives the gate scorecard, the disagreement view, and the oracle-coverage loop —
corruption is not gate ground-truth about cats/motion, so registering it would
wrongly offer it there. The page constructs a ``CorruptionAnalyzer()`` directly
and hands it to ``AnalysisManager.enqueue_analyzer``, exactly as the tuning path
hands over a ``MogAnalyzer`` — no registry entry, no ``get_analyzer`` branch. Its
verdicts land in the shared ``analysis`` table under the literal name
``"corruption"`` (queried by that literal; ``analysis_coverage`` and friends key
on the analyzer name and so work unchanged).

**Stateless** (``windowed = False``, unlike ``MogAnalyzer``/``BsuvAnalyzer``): the
corrupt test looks only at the frame it is handed — no rolling background — so it
rides the runner's stateless (resumable, per-frame) path. **No ML extras** —
``numpy`` is a base dependency of ``compute/requirements.txt`` (and is imported
only lazily, inside ``shared.motion``), so this runs on any box and its
``ensure_available`` is a no-op.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from compute.analysis.base import AnalysisResult
from shared.motion import classify_corruption, corruption_thresholds

if TYPE_CHECKING:
    # Type-only, never imported at runtime — keeps ``import
    # compute.analysis.corruption`` free of the CV stack. ``np`` names the BGR
    # frame ``analyze`` receives; ``Store`` names the handle ``prepare`` ignores.
    import numpy as np

    from compute.collection.store import Store


class CorruptionAnalyzer:
    """Stateless offline re-run of the shared corrupt-frame guard; an ``Analyzer``.

    ``name`` is the literal ``"corruption"`` — the ``analysis.analyzer`` column
    value its verdicts land under and what the review page queries by.
    ``windowed = False`` — the corrupt test is per-frame, so it rides the runner's
    stateless path (resumable, skips already-verdicted frames).
    """

    name = "corruption"
    windowed = False

    def ensure_available(self) -> None:
        """No-op: the guard needs only ``numpy`` (a base dep), never the ML extras.

        The runner calls this synchronously at enqueue (see
        ``Analyzer.ensure_available``); there is nothing optional to verify, so it
        returns cleanly — a corruption sweep never surfaces a 503 for missing deps.
        """
        return None

    def prepare(self, store: "Store", since_id: "int | None" = None) -> None:
        """No-op: a stateless analyzer loads no weights and primes no window.

        The runner still calls it once before the first ``analyze`` (and passes
        ``since_id`` for a scoped windowed analyzer's warm-start); this analyzer
        ignores both, exactly as the stateless YOLO path does.
        """
        return None

    def analyze(self, image: "np.ndarray") -> AnalysisResult:
        """Verdict for one BGR frame: ``verdict`` = corrupt, ``detail`` = reason + thresholds.

        Runs the shared ``classify_corruption`` (the live gate's own detector) on
        the already-ROI ``image``: ``verdict`` is True when it fires, ``score`` is
        ``None`` (the guard has no continuous confidence), and ``detail`` carries
        ``reason`` (``"cast"``/``"line"``, or ``None`` when not corrupt) and the
        ``thresholds`` the verdict was computed under — the stamp the review page
        compares against the current constants to detect stale verdicts.
        """
        reason = classify_corruption(image)
        return AnalysisResult(
            verdict=reason is not None,
            score=None,
            detail={"reason": reason, "thresholds": corruption_thresholds()},
        )
