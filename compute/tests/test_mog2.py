"""Tests for the offline MOG2 re-run analyzer (compute/analysis/mog2.py) and the
runner's instance-based launch seam (AnalysisManager.enqueue_analyzer).

``MogAnalyzer`` re-runs the SHARED live gate (``shared.motion.MotionGate``) over stored
frames with an explicit ``MotionParams`` set, persisting each verdict to a named slot
(``mog2:baseline`` / ``mog2:candidate``). Because it is the same gate the edge runs, the
synthetic-frame convergence here mirrors ``shared/tests/test_motion.py``: a fresh MOG2
model must first learn a flat background before a compact blob reads as motion.

Two layers, like ``test_analysis.py``:

- **MogAnalyzer directly** — warm-start off a store of background JPEGs, then verdicts on
  raw numpy frames (``verdict`` == the gate's debounced motion, ``score`` == the blob
  area fraction, ``detail`` carries the bbox + the six params and is JSON-serializable).
- **AnalysisManager.enqueue_analyzer** — runs a PRE-CONSTRUCTED instance (no registry
  entry) through the same worker machinery, reporting the instance's own ``.name``;
  mirrors the fake-analyzer style in ``test_analysis.py``.

``cv2``/``numpy`` are a hard dependency here (the gate decodes and computes) but they ARE
in the lean ``compute/requirements.txt``, so these run on this dev box; the guard merely
skips gracefully on a box without OpenCV rather than erroring collection.
"""
from __future__ import annotations

import json
import time

import pytest

from compute.analysis.base import AnalysisResult
from compute.analysis.mog2 import _WARMSTART_ID, _WARMUP, MogAnalyzer
from compute.analysis.runner import AnalysisManager
from compute.collection.store import Store
from compute.ingest import StreamFrame
from shared.motion import MotionParams
from shared.wire import StreamFrameMeta

try:
    import cv2
    import numpy as np

    _HAVE_CV = True
except Exception:  # pragma: no cover - exercised only where cv2 is absent
    _HAVE_CV = False

_requires_cv = pytest.mark.skipif(not _HAVE_CV, reason="cv2/numpy required for the MOG2 tests")

# A synthetic door ROI mirroring shared/tests/test_motion.py: a flat mid-grey background
# with a small bright rectangle standing in for a top-down cat. downscale == the ROI
# width, so no downscale happens and the blob maps 1:1 to px fractions.
_W, _H = 160, 120
_BG_LEVEL = 60
_BLOB_LEVEL = 220
_BLOB_RECT = (60, 40, 30, 30)  # ~4.7% of the ROI, inside the default locality band

# Production-default motion params (edge/config/settings.py).
_BASE_PARAMS = dict(
    var_threshold=16.0,
    learning_rate=0.001,
    min_area=0.01,
    max_area_fraction=0.6,
    persistence=2,
    downscale=160,
)


def _params(**overrides) -> MotionParams:
    return MotionParams(**{**_BASE_PARAMS, **overrides})


def _background() -> "np.ndarray":
    return np.full((_H, _W, 3), _BG_LEVEL, dtype=np.uint8)


def _with_blob() -> "np.ndarray":
    frame = _background()
    x, y, w, h = _BLOB_RECT
    frame[y : y + h, x : x + w] = _BLOB_LEVEL
    return frame


def _jpeg(img: "np.ndarray") -> bytes:
    ok, buf = cv2.imencode(".jpg", img)
    assert ok
    return buf.tobytes()


def _jpeg_gray(level: int) -> bytes:
    """A real solid-gray JPEG at ``level``; a solid colour round-trips exactly, so the
    fake analyzer recovers the level it was stored with (see test_analysis._jpeg_gray)."""
    return _jpeg(np.full((16, 16, 3), level, dtype=np.uint8))


def _frame(frame_id: int, ts: int, body: bytes) -> StreamFrame:
    """A ``StreamFrame`` the store consumes; the gate re-derives motion, so the wire
    motion flag here is irrelevant and left neutral."""
    meta = StreamFrameMeta(frame_id=frame_id, ts=ts, motion=False, bbox=None, area=0.0)
    return StreamFrame(meta, body)


def _store(tmp_path) -> Store:
    return Store(
        db_path=str(tmp_path / "index.db"),
        media_root=str(tmp_path / "media"),
        max_bytes=10_000_000,
    )


def _wait(pred, timeout: float = 5.0, interval: float = 0.005) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return bool(pred())


class _FakeAnalyzer:
    """A stateless controllable stand-in, mirroring test_analysis.FakeAnalyzer.

    ``analyze`` derives its verdict from the frame's own mean gray level (bright ≥ 127 →
    present), so a test fixes each verdict by the JPEG it stores. Its ``name`` is what
    ``enqueue_analyzer`` must report back through ``status()`` — the point of the test.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.windowed = False
        self.prepared_with = None
        self.prepared_since_id = None

    def prepare(self, store, since_id: "int | None" = None) -> None:
        self.prepared_with = store
        self.prepared_since_id = since_id

    def ensure_available(self) -> None:
        pass

    def analyze(self, image) -> AnalysisResult:
        level = float(image.mean())
        return AnalysisResult(verdict=bool(level >= 127.0), score=level, detail=None)


# --- MogAnalyzer: construction ------------------------------------------------


def test_name_is_slot_scoped_and_windowed():
    analyzer = MogAnalyzer(_params(), slot="candidate")
    assert analyzer.name == "mog2:candidate"
    assert analyzer.windowed is True


def test_bad_slot_rejected():
    with pytest.raises(ValueError):
        MogAnalyzer(_params(), slot="bogus")


@_requires_cv
def test_analyze_before_prepare_raises():
    analyzer = MogAnalyzer(_params(), slot="baseline")
    with pytest.raises(RuntimeError):
        analyzer.analyze(_background())


# --- MogAnalyzer: warm-start + verdict/score ----------------------------------


@_requires_cv
def test_warm_starts_then_detects_motion_with_area_and_params(tmp_path):
    store = _store(tmp_path)
    # A run of flat-background JPEGs primes the MOG2 model via warm-start (the windowed
    # priming this analyzer exists to do — cold, the first blob would read against a
    # still-adapting model).
    for i in range(15):
        store.add(_frame(i, i, _jpeg(_background())), recv_ts_ms=1_700_000_000_000 + i)

    params = _params()
    analyzer = MogAnalyzer(params, slot="candidate")
    analyzer.prepare(store)
    # Warm-start replayed the background frames and built the model — not a cold start.
    assert analyzer._gate is not None
    assert analyzer._gate._mog2 is not None

    # A still background frame after warm-start is NOT motion: the flat scene is learned.
    assert analyzer.analyze(_background()).verdict is False

    # A sustained, cat-sized blob flips motion once the persistence streak is met.
    result = None
    for _ in range(params.persistence):
        result = analyzer.analyze(_with_blob())
    assert result.verdict is True
    # score is the largest-blob area fraction, inside the locality band.
    assert params.min_area <= result.score <= params.max_area_fraction
    # detail echoes the normalized bbox and the exact six params, and is JSON-safe
    # (write_analysis json.dumps() it, so an unserializable detail would drop the row).
    assert result.detail["bbox"] is not None
    assert result.detail["params"] == params._asdict()
    json.dumps(result.detail)  # must not raise


@_requires_cv
def test_empty_store_warm_start_is_graceful(tmp_path):
    # Nothing to prime from must not raise — the model builds lazily on the first analyze.
    analyzer = MogAnalyzer(_params(), slot="baseline")
    analyzer.prepare(_store(tmp_path))
    assert analyzer._gate is not None
    # A first frame against a cold model simply yields a verdict (here: no motion).
    assert analyzer.analyze(_background()).verdict is False


# --- MogAnalyzer: prepare() scoping (frame-range groups warm-start) -----------
#
# A scoped run's warm-start must prime from the frames JUST BEFORE the window
# (``since_id``), not from the newest frames in the whole store — see
# ``MogAnalyzer._warm_start`` and docs/specs/2026-07-10-frame-range-groups.md
# ("A scoped windowed re-run warm-starts from the frames immediately before
# start_id"). Unscoped (``since_id=None``) must still prime from the newest
# frames exactly as before — the strict-superset property this feature preserves.


@_requires_cv
def test_prepare_scoped_since_id_anchors_recent_before_at_the_scope_floor(tmp_path, monkeypatch):
    # Spy on the REAL Store.recent_before (not a fake — so the SQL keyset behavior
    # stays exercised) to pin the exact anchor a scoped prepare() hands it: since_id
    # itself, not the newest-frames sentinel an unscoped run falls back to.
    store = _store(tmp_path)
    for i in range(20):
        store.add(_frame(i, i, _jpeg(_background())), recv_ts_ms=1_700_000_000_000 + i)

    calls: "list[tuple[int, int]]" = []
    real_recent_before = Store.recent_before

    def spy(self, frame_id, n):
        calls.append((frame_id, n))
        return real_recent_before(self, frame_id, n)

    monkeypatch.setattr(Store, "recent_before", spy)

    analyzer = MogAnalyzer(_params(), slot="baseline")
    analyzer.prepare(store, since_id=12)

    assert calls == [(12, _WARMUP)]  # the scope floor, not _WARMSTART_ID
    assert analyzer._gate is not None  # priming still built a usable model


@_requires_cv
def test_prepare_unscoped_still_anchors_recent_before_at_the_newest_frames_sentinel(tmp_path, monkeypatch):
    # The superset property: with since_id absent, prepare() must fall back to
    # EXACTLY today's newest-frames sentinel (_WARMSTART_ID) — proving the
    # frame-range-groups scoping addition changes nothing when no scope is given.
    store = _store(tmp_path)
    for i in range(5):
        store.add(_frame(i, i, _jpeg(_background())), recv_ts_ms=1_700_000_000_000 + i)

    calls: "list[tuple[int, int]]" = []
    real_recent_before = Store.recent_before

    def spy(self, frame_id, n):
        calls.append((frame_id, n))
        return real_recent_before(self, frame_id, n)

    monkeypatch.setattr(Store, "recent_before", spy)

    analyzer = MogAnalyzer(_params(), slot="baseline")
    analyzer.prepare(store)  # since_id defaults to None

    assert calls == [(_WARMSTART_ID, _WARMUP)]


@_requires_cv
def test_prepare_scoped_warm_start_replays_only_the_frames_before_since_id(tmp_path):
    # Behavioral pin, not just the call args: clean background frames sit BEFORE
    # since_id and a run of BLOB frames sits strictly AFTER it (frames a scoped
    # warm-start must never see while priming — they stand in for "the rest of a
    # much longer store" beyond the selected window). A correct scoped warm-start
    # primes only off the pre-window background, so the model still reads a still
    # frame as not-motion and a fresh blob as motion after prepare() — exactly like
    # ``test_warm_starts_then_detects_motion_with_area_and_params`` above, just
    # anchored mid-store instead of at the tail.
    store = _store(tmp_path)
    n_bg, n_blob = 15, 5
    for i in range(n_bg):
        store.add(_frame(i, i, _jpeg(_background())), recv_ts_ms=1_700_000_000_000 + i)
    blob_ids = []
    for j in range(n_blob):
        i = n_bg + j
        blob_ids.append(store.add(_frame(i, i, _jpeg(_with_blob())), recv_ts_ms=1_700_000_000_000 + i))

    # Anchor at the first post-window (blob) frame: recent_before(since_id, _WARMUP)
    # then returns exactly the n_bg background frames, none of the withheld blobs.
    since_id = blob_ids[0]

    params = _params()
    analyzer = MogAnalyzer(params, slot="candidate")
    analyzer.prepare(store, since_id=since_id)
    assert analyzer._gate is not None

    # The learned background is the flat scene, not a blob that came after the scope
    # floor: a still frame reads not-motion...
    assert analyzer.analyze(_background()).verdict is False
    # ...and re-presenting the very blob withheld from priming still reads as fresh
    # motion once the persistence streak is met — proof the model never saw it warm-starting.
    result = None
    for _ in range(params.persistence):
        result = analyzer.analyze(_with_blob())
    assert result.verdict is True


# --- AnalysisManager.enqueue_analyzer: pre-constructed instance ---------------


@_requires_cv
def test_enqueue_analyzer_runs_prebuilt_instance_and_reports_its_name(tmp_path):
    store = _store(tmp_path)
    # Two "present" (bright) + two "absent" (dark) frames the fake reads back by mean.
    for i in range(4):
        store.add(_frame(i, i, _jpeg_gray(255 if i < 2 else 0)), recv_ts_ms=1_700_000_000_000 + i)

    fake = _FakeAnalyzer(name="mog2:candidate")
    # Default resolver — enqueue_analyzer must NOT consult it: there is no registry entry
    # for 'mog2:candidate'; it runs the instance handed to it directly.
    manager = AnalysisManager()
    manager.enqueue_analyzer(store, fake)

    assert _wait(lambda: not manager.running), "sweep did not finish within timeout"
    st = manager.status()
    assert st["analyzer"] == "mog2:candidate"  # the INSTANCE .name, not a registry name
    assert st["done"] == st["total"] == 4
    assert st["present"] == 2
    assert st["error"] is None
    assert fake.prepared_with is store  # the worker handed the store to prepare()
    assert store.analysis_summary("mog2:candidate") == {"analyzed": 4, "present": 2}


@_requires_cv
def test_enqueue_analyzer_enqueues_second_job_while_running(tmp_path):
    # The instance-based path shares the walk-away queue: while a fake whose analyze
    # blocks keeps the sweep live, a second enqueue_analyzer lands in the pending FIFO
    # (a DIFFERENT slot → distinct kind → not deduped) rather than raising.
    import threading

    store = _store(tmp_path)
    ids = [store.add(_frame(i, i, _jpeg_gray(200)), recv_ts_ms=1_700_000_000_000 + i) for i in range(3)]

    entered = threading.Event()
    release = threading.Event()

    class _Gated(_FakeAnalyzer):
        def analyze(self, image):
            entered.set()
            release.wait(timeout=5)
            return AnalysisResult(verdict=True, score=1.0, detail=None)

    gated = _Gated(name="mog2:baseline")
    manager = AnalysisManager()
    r1 = manager.enqueue_analyzer(store, gated, since_id=ids[0], until_id=ids[0])
    assert r1["position"] == 0
    try:
        assert entered.wait(timeout=5)
        r2 = manager.enqueue_analyzer(
            store, _FakeAnalyzer(name="mog2:candidate"), since_id=ids[1], until_id=ids[1]
        )
        assert r2["position"] == 1 and r2["deduped"] is False
    finally:
        manager.cancel()
        release.set()
        _wait(lambda: not manager.running)


# --- AnalysisManager.enqueue_analyzer: a REAL MogAnalyzer end to end ----------


@_requires_cv
def test_enqueue_analyzer_runs_real_mog_analyzer_into_its_slot(tmp_path):
    store = _store(tmp_path)
    # A window of background frames then a run of sustained-blob frames.
    n_bg, n_blob = 15, 5
    for i in range(n_bg):
        store.add(_frame(i, i, _jpeg(_background())), recv_ts_ms=1_700_000_000_000 + i)
    for j in range(n_blob):
        i = n_bg + j
        store.add(_frame(i, i, _jpeg(_with_blob())), recv_ts_ms=1_700_000_000_000 + i)

    manager = AnalysisManager()
    manager.enqueue_analyzer(store, MogAnalyzer(_params(), slot="baseline"))

    assert _wait(lambda: not manager.running), "mog2 sweep did not finish"
    st = manager.status()
    assert st["error"] is None
    assert st["analyzer"] == "mog2:baseline"
    # Windowed: every stored frame gets a verdict in the slot. A JSON-serialization
    # failure on detail would land in the per-frame skip path and drop the row, so the
    # full analyzed count also pins that detail (bbox + params) is storable.
    summary = store.analysis_summary("mog2:baseline")
    assert summary["analyzed"] == n_bg + n_blob
    # The sustained blob is detected -> at least one present verdict; the sibling slot
    # (never run) stays empty, proving slots are isolated by analyzer name.
    assert summary["present"] >= 1
    assert store.analysis_summary("mog2:candidate") == {"analyzed": 0, "present": 0}
