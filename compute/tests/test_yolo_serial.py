"""The ``yolo-serial`` A/B variant + its registry wiring (no torch/ultralytics, no model).

``yolo-serial`` is the SAME ``YoloAnalyzer`` in its pre-batching, bare-per-frame call
shape, registered as a DISTINCT oracle so its verdicts occupy their own ``analysis``
rows and a scorecard can compare batched-``yolo`` vs serial-``yolo`` against MOG2 over
the identical frames (the "did batching move a verdict?" A/B). These tests pin the two
properties that make that A/B meaningful, WITHOUT loading a real model:

- the registry resolves ``"yolo-serial"`` to a serial-configured ``YoloAnalyzer``, so
  every ``ANALYZER_NAMES``-validated endpoint (run / coverage / scorecard /
  disagreement) accepts it with no per-route change; and
- ``analyze_batch`` funnels through the BARE single-image ``predict`` per frame (the
  old call shape) rather than one batched list call — even when handed several frames.

A recording stub stands in for the loaded ultralytics model (the model is set only in
``prepare``, which we skip), so no torch/ultralytics/GPU is touched: the routing (bare
vs list) is all these tests observe. Importing ``compute.analysis.yolo`` is safe here
because its heavy imports are lazy (inside ``prepare``/``ensure_available``), never at
module scope.
"""
from __future__ import annotations

import pytest

from compute.analysis import ANALYZER_NAMES, get_analyzer
from compute.analysis.yolo import YoloAnalyzer

np = pytest.importorskip("numpy")


class _FakeResults:
    """Minimal stand-in for one ultralytics ``Results``: no boxes → verdict False."""

    boxes: list = []


class _RecordingModel:
    """Records whether each ``predict`` call got a bare ndarray or a list, plus the count.

    Returns a one-``Results`` list for a bare image (ultralytics wraps a single input
    in a list) and an N-``Results`` list for a list of N — matching how ``_predict_one``
    indexes ``[0]`` and ``_predict``/``analyze_batch`` iterate the whole list.
    """

    def __init__(self) -> None:
        self.calls: "list[tuple[str, int]]" = []

    def predict(self, arg, **kwargs):
        if isinstance(arg, list):
            self.calls.append(("list", len(arg)))
            return [_FakeResults() for _ in arg]
        self.calls.append(("bare", 1))
        return [_FakeResults()]


def _img():
    return np.zeros((4, 4, 3), dtype=np.uint8)


def test_yolo_serial_registered_as_distinct_oracle():
    assert "yolo-serial" in ANALYZER_NAMES
    default = get_analyzer("yolo")
    serial = get_analyzer("yolo-serial")
    assert default.name == "yolo" and serial.name == "yolo-serial"
    assert serial.windowed is False
    # Pinned to 1 so its runner path stays one-frame-at-a-time (see YoloAnalyzer.__init__).
    assert serial.batch_size == 1


def test_serial_analyze_batch_predicts_each_frame_bare():
    # The A/B faithfulness property: the serial variant issues one BARE-image predict
    # per frame (the pre-optimization call shape), never one batched list call — even
    # when analyze_batch is handed several frames.
    a = YoloAnalyzer(serial=True)
    a._model = _RecordingModel()
    results = a.analyze_batch([_img(), _img(), _img()])
    assert len(results) == 3
    assert all(r.verdict is False and r.score == 0.0 for r in results)
    assert a._model.calls == [("bare", 1), ("bare", 1), ("bare", 1)]


def test_default_analyze_batch_predicts_one_batched_list():
    # The default (batched) variant issues ONE predict() over the whole list — the
    # throughput path the serial variant is the A/B counterpart to.
    a = YoloAnalyzer()  # serial=False
    a._model = _RecordingModel()
    results = a.analyze_batch([_img(), _img(), _img()])
    assert len(results) == 3
    assert a._model.calls == [("list", 3)]


def test_serial_and_default_share_inference_params():
    # Same imgsz/conf/half so the A/B differs ONLY in call shape — not in a param that
    # could independently move a verdict and confound the comparison.
    default = YoloAnalyzer()
    serial = YoloAnalyzer(serial=True)
    assert (serial._imgsz, serial._conf, serial._half) == (default._imgsz, default._conf, default._half)


def test_scorecard_oracle_set_covers_every_registered_oracle():
    # Regression guard: the scorecard's accepted-oracle set is DERIVED from
    # ANALYZER_NAMES, not a hand-kept second list. A hardcoded copy let `yolo-serial`
    # 500 `/api/tuning/compare` (gate_scorecard raised ValueError) even though it was a
    # registered oracle. Pin the invariant so any future oracle is scoreable by default.
    from compute.collection.store import _SCORECARD_ORACLES

    assert set(ANALYZER_NAMES) <= set(_SCORECARD_ORACLES)
