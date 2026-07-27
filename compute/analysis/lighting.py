"""``LightingAnalyzer`` — the offline sweep of the shared day/night lighting statistic.

Records, per stored frame, how *colourful* the scene is — the signal that separates
colour-daylight from IR-monochrome night. It wraps ``shared.motion.lighting_measure``,
the same function the edge will run once lighting drives live parameter switching, so
a swept value equals a live one BY CONSTRUCTION (the single-detector guarantee
``MogAnalyzer`` gives for MOG2 and ``CorruptionAnalyzer`` for the corrupt-frame test).
See docs/specs/2026-07-27-lighting-flag.md.

**The row carries a STATISTIC, not a verdict.** Where ``CorruptionAnalyzer`` sets
``score=None`` because its guard has no continuous confidence, this analyzer's whole
purpose is the continuous value: ``score`` is the colourfulness, and the day/night
THRESHOLD that interprets it is applied at READ time from the settings KV, never baked
in here. That is what lets the sweep run before the NoIR camera is fitted and be
calibrated afterwards from the recorded distribution — a read-time re-slice, no
re-sweep (the ``oracle_floor`` pattern, changelog 72). ``verdict`` is always ``False``
and no reader consults it; the row exists to carry ``score``.

**Deliberately NOT in the oracle registry** (``ANALYZER_NAMES``), exactly like
``CorruptionAnalyzer``. ``ANALYZER_NAMES`` drives the gate scorecard, the disagreement
view, and the oracle-coverage loop — lighting is not ground truth about cats or motion,
so registering it would wrongly offer it there. Callers construct a
``LightingAnalyzer()`` directly and hand it to ``AnalysisManager.enqueue_analyzer``, as
the tuning path does with ``MogAnalyzer``. Its rows land in the shared ``analysis``
table under the literal name ``"lighting"``.

**Stateless** (``windowed = False``): the measure looks only at the frame it is handed,
so it rides the runner's resumable per-frame path. **No ML extras** — ``cv2``/``numpy``
are base dependencies of ``compute/requirements.txt`` (and imported only lazily, inside
``shared.motion``), so this runs on any box and ``ensure_available`` is a no-op.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from compute.analysis.base import AnalysisResult
from shared.motion import lighting_measure, lighting_version

if TYPE_CHECKING:
    # Type-only, never imported at runtime — keeps ``import compute.analysis.lighting``
    # free of the CV stack. ``np`` names the BGR frame ``analyze`` receives; ``Store``
    # names the handle ``prepare`` ignores.
    import numpy as np

    from compute.collection.store import Store

# The literal ``analysis.analyzer`` value these rows land under. It is half of
# ``PRIMARY KEY (frame_id, analyzer)``, so renaming it after rows exist orphans every
# stored value without a migration — the ``yolo-serial`` lesson (changelog 147), where
# the slug had to stay despite being operator-visible. Presentation renames belong in
# the UI, not here.
LIGHTING_ANALYZER = "lighting"


class LightingAnalyzer:
    """Stateless offline sweep of the shared lighting statistic; an ``Analyzer``.

    ``name`` is the literal ``"lighting"`` — the ``analysis.analyzer`` column value its
    rows land under and what the tuning page queries by. ``windowed = False`` — the
    measure is per-frame, so it rides the runner's stateless path (resumable, skips
    frames that already have a row).
    """

    name = LIGHTING_ANALYZER
    windowed = False

    def ensure_available(self) -> None:
        """No-op: the measure needs only ``cv2``/``numpy`` (base deps), never ML extras.

        The runner calls this synchronously at enqueue (see ``Analyzer.ensure_available``);
        there is nothing optional to verify, so it returns cleanly — a lighting sweep
        never surfaces a 503 for missing deps.
        """
        return None

    def prepare(self, store: "Store", since_id: "int | None" = None) -> None:
        """No-op: a stateless analyzer loads no weights and primes no window.

        The runner still calls it once before the first ``analyze`` (and passes
        ``since_id`` for a scoped windowed analyzer's warm-start); this analyzer ignores
        both, exactly as ``CorruptionAnalyzer`` and the stateless YOLO path do.
        """
        return None

    def analyze(self, image: "np.ndarray") -> AnalysisResult:
        """Reading for one BGR frame: ``score`` = colourfulness, ``detail`` = luma + version.

        ``verdict`` is always ``False`` — there is no boolean truth to record and the
        read path never consults it (see the module docstring). ``detail`` carries the
        frame's mean luminance, so the genuinely ambiguous dark-frame case stays
        recoverable without a re-sweep, and the ``version`` the value was computed
        under, so a formula change is detectable as stale.
        """
        score, luma = lighting_measure(image)
        return AnalysisResult(
            verdict=False,
            score=score,
            detail={"luma": luma, "version": lighting_version()},
        )
