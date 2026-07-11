"""``YoloAnalyzer`` — the "is a cat here?" oracle, a thin wrapper over ultralytics.

This is one of the two offline oracles the motion-gate-oracles spec validates
MOG2 against (see ``compute/analysis/base.py`` for the shared contract). It is
deliberately tuned **recall-first**, not for speed or precision: a **large** COCO
model (``yolo11x`` by default) at a **high** ``imgsz`` (1280) and a **low**
confidence threshold (0.15) — because this pass runs offline, where time is cheap
and what we are hunting is exactly the cats MOG2's live gate missed (per the
project memory, the camera is mounted top-down, so a resident is often a small,
partial, oddly-angled dorsal view — precisely the case a stock detector under-
calls at default settings). ``verdict``/``score``/``detail`` all reduce to the
uniform shape ``AnalysisResult`` defines; "present" here means "a COCO 'cat'
(class id 15) was detected at ``conf`` or above."

Lazy-import discipline: ``torch``/``ultralytics`` are imported only inside
``prepare()``, never at module scope, mirroring the ``cv2``/``numpy``-inside-
``.image`` discipline in ``compute/ingest/client.py``'s ``StreamFrame``. That is
what lets ``compute/analysis`` (and anything that merely imports this module,
e.g. the runner's analyzer registry) load fine on the always-on collector's lean
``compute/requirements.txt`` — the heavy ML stack is only ever touched once a
sweep actually starts, and only if the opt-in
``compute/requirements-analysis.txt`` extras are installed.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

from compute.analysis.base import AnalysisResult

if TYPE_CHECKING:
    # Type-only — see the module docstring; never imported at runtime here.
    import numpy as np

    from compute.collection.store import Store

# COCO's fixed 80-class label order (the set both COCO-pretrained YOLO releases
# and ultralytics ship) puts "cat" at index 15 — 0 person, 1 bicycle, ... 14
# bird, 15 cat, 16 dog, ... This is what we filter every detection against;
# every other one of the model's 80 classes is noise for this oracle's purpose.
_COCO_CAT_CLASS_ID = 15

# Env overrides, per the spec — each read once in __init__ and cached, so a
# sweep's per-frame `analyze()` never touches the environment. Defaults favor
# RECALL: the biggest stock COCO model, a high inference resolution (small/
# partial cats keep more pixels at 1280 than the usual 640), and a low
# confidence floor — a hard-mode miss we want surfaced, not filtered out here.
_ENV_WEIGHTS = "CAT_YOLO_WEIGHTS"
_ENV_IMGSZ = "CAT_YOLO_IMGSZ"
_ENV_CONF = "CAT_YOLO_CONF"

_DEFAULT_WEIGHTS = "yolo11x.pt"
_DEFAULT_IMGSZ = 1280
_DEFAULT_CONF = 0.15


class YoloAnalyzer:
    """Stateless per-frame cat detector; satisfies the ``Analyzer`` protocol.

    ``windowed = False`` because ``analyze()`` depends only on the frame it is
    handed — no rolling state across calls — so the runner drives it over just
    the frames lacking a verdict (see ``Store.iter_unanalyzed``), making a sweep
    cheaply resumable. Construction takes optional overrides purely for tests
    (e.g. a tiny weights file); production callers rely on the env vars above.
    """

    name = "yolo"
    windowed = False

    def __init__(
        self,
        weights: "str | None" = None,
        imgsz: "int | None" = None,
        conf: "float | None" = None,
    ) -> None:
        # Explicit arg wins, then the env var, then the recall-first default —
        # the same precedence EdgeClient uses for its base URL.
        self._weights = weights if weights is not None else os.environ.get(_ENV_WEIGHTS, _DEFAULT_WEIGHTS)
        self._imgsz = int(imgsz if imgsz is not None else os.environ.get(_ENV_IMGSZ, _DEFAULT_IMGSZ))
        self._conf = float(conf if conf is not None else os.environ.get(_ENV_CONF, _DEFAULT_CONF))
        # Populated by prepare(); analyze() before prepare() is a caller bug, not
        # a runtime condition to design around, so it fails loud (see analyze()).
        self._model = None
        self._device: "str | None" = None

    def ensure_available(self) -> None:
        """Verify ``torch``/``ultralytics`` import; raise ``ImportError`` with a fix if not.

        The cheap synchronous dep check the runner calls in ``start`` (see
        ``Analyzer.ensure_available``) — just the imports, no weights load — so a
        Run with the analysis extras absent fails at request time (→ 503) rather
        than mid-sweep on the worker thread. ``prepare`` calls it too, so the
        model-load path is never reached without the deps present.
        """
        try:
            import torch  # noqa: F401
            from ultralytics import YOLO  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "YoloAnalyzer requires 'torch' and 'ultralytics', which are NOT "
                "part of the always-on collector's lean compute/requirements.txt "
                "(the analysis oracles' ML deps are opt-in). Install them with: "
                "pip install -r compute/requirements-analysis.txt"
            ) from exc

    def prepare(self, store: "Store", since_id: "int | None" = None) -> None:
        """Load the model once and pick the device; ``store`` / ``since_id`` are unused.

        Both exist only to satisfy the shared ``Analyzer.prepare`` shape — a WINDOWED
        analyzer (BSUV/MOG2) uses ``store`` + ``since_id`` to warm-start a recent-frame
        window scoped to the run, but this analyzer is stateless per-frame, so it has
        nothing to prime. Heavy imports live here (via ``ensure_available``), not at
        module scope (see the module docstring).
        """
        del store, since_id  # unused: stateless — see the docstring above
        self.ensure_available()  # deps checked synchronously in start(); re-checked here
        import torch
        from ultralytics import YOLO

        # cuda > mps > cpu: prefer an NVIDIA GPU (the eventual compute box), fall
        # back to Apple Silicon's MPS backend (this dev box, per the spec's
        # non-goals — BSUV is CUDA-only and tested there instead), else CPU.
        if torch.cuda.is_available():
            self._device = "cuda"
        elif torch.backends.mps.is_available():
            self._device = "mps"
        else:
            self._device = "cpu"

        self._model = YOLO(self._weights)

    def analyze(self, image: "np.ndarray") -> AnalysisResult:
        """Run inference on one BGR frame; return the uniform cat-present verdict.

        Restricts detection to the COCO 'cat' class up front (``classes=``) so the
        model does the minimum work needed for this oracle's question, then keeps
        every surviving box (ultralytics already applied ``conf`` internally).
        Zero detections is the common, expected case (most frames have no cat) —
        it falls straight through to an empty ``boxes`` list, ``verdict=False``,
        ``score=0.0``, no special-casing.
        """
        if self._model is None:
            raise RuntimeError("YoloAnalyzer.analyze() called before prepare()")

        results = self._model.predict(
            image,
            imgsz=self._imgsz,
            conf=self._conf,
            classes=[_COCO_CAT_CLASS_ID],
            device=self._device,
            verbose=False,
        )

        boxes: "list[list[float]]" = []
        for result in results:
            for box in result.boxes:
                x1, y1, x2, y2 = (float(v) for v in box.xyxy[0])
                boxes.append([x1, y1, x2, y2, float(box.conf[0])])

        verdict = bool(boxes)
        score = max((b[4] for b in boxes), default=0.0)
        detail = {"boxes": boxes, "model": self._weights, "device": self._device}
        return AnalysisResult(verdict=verdict, score=score, detail=detail)
