"""Tests for the batched/prefetched stateless sweep (the yolo-sweep-throughput spec).

Two layers, exactly as test_analysis.py splits them:

- **Store methods** (``write_analysis_batch``, ``close``, and the WAL journal mode)
  never decode a JPEG, so — like test_collection.py — they run against
  ``StreamFrame`` objects built from the fake ``_JPEG_BODY`` and seed verdicts by
  calling the store directly. No cv2 needed; they run everywhere.
- **Runner batched path** (``run_analysis`` over a stateless analyzer) DOES decode
  (the producer thread ``cv2.imdecode``s the stored bytes), so those tests fabricate
  *real* solid-gray JPEGs whose gray level encodes the verdict a fake oracle
  returns (bright ≥ 127 → present). Solid gray round-trips through JPEG exactly
  (cv2's default q95 makes the DC quant step divide evenly), so ``image.mean()``
  recovers the written level bit-for-bit — which lets a test assert BOTH the verdict
  of each frame and the ORDER the analyzer saw them, proving the producer/consumer
  hand-off preserves id order.

No torch/ultralytics/GPU and no real model. The fakes here satisfy the ``Analyzer``
protocol AND opt into the batched fast path by exposing ``analyze_batch`` +
``batch_size`` (except ``PerImageFake``, which deliberately omits ``analyze_batch``
to exercise the runner's per-image fallback). ``BatchFakeAnalyzer.analyze_batch``
asserts every batch it receives is single-shape, so the runner's shape-boundary
chunking guarantee is checked on every batched test, not just the dedicated one.
See docs/specs/2026-07-11-yolo-sweep-throughput.md.
"""
from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from compute.analysis.base import AnalysisResult
from compute.analysis.runner import AnalysisManager, run_analysis
from compute.collection.store import Store
from compute.ingest import StreamFrame
from shared.wire import StreamFrameMeta

# A minimal but valid JPEG the STORE tests never decode (same body the collection
# tests use). It is deliberately UNDECODABLE by cv2 — the runner's decode-failure
# path leans on exactly that (see the skip test).
_JPEG_BODY = b"\xff\xd8\xff\xe0" + b"fake-jpeg-body" + b"\xff\xd9"

# cv2/numpy are a hard dependency of the runner (its producer decodes) but NOT of
# the store layer. Guard-import so a box without the CV stack still runs the store
# tests and merely skips the sweep tests, rather than erroring the whole file.
try:
    import cv2
    import numpy as np

    _HAVE_CV = True
except Exception:  # pragma: no cover - exercised only where cv2 is absent
    _HAVE_CV = False

_requires_cv = pytest.mark.skipif(not _HAVE_CV, reason="cv2/numpy required for the sweep tests")


def _frame(
    frame_id: int = 1,
    ts: int = 1_000,
    motion: bool = False,
    bbox=None,
    area: float = 0.0,
    body: bytes = _JPEG_BODY,
) -> StreamFrame:
    """Build a ``StreamFrame`` directly — the shape ``Store.add`` consumes."""
    meta = StreamFrameMeta(frame_id=frame_id, ts=ts, motion=motion, bbox=bbox, area=area)
    return StreamFrame(meta, body)


def _store(tmp_path, max_bytes: int = 10_000_000) -> Store:
    return Store(
        db_path=str(tmp_path / "index.db"),
        media_root=str(tmp_path / "media"),
        max_bytes=max_bytes,
    )


def _jpeg_gray(level: int, h: int = 16, w: int = 16) -> bytes:
    """A real solid-gray ``h×w`` JPEG at ``level`` (0..255).

    The runner decodes the stored bytes, so a scenario controls what a fake oracle
    sees by the gray level it encodes here; the fake reads the decoded mean back.
    Solid gray round-trips through JPEG exactly, so the mean the fake recovers
    equals the level written — the property every runner test leans on. ``h``/``w``
    let the shape-boundary test store frames of DIFFERING dimensions (both multiples
    of 8, so the round-trip stays exact).
    """
    img = np.full((h, w, 3), level, dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", img)
    assert ok
    return buf.tobytes()


def _wait(pred, timeout: float = 5.0, interval: float = 0.005) -> bool:
    """Poll ``pred`` until true or ``timeout`` elapses; return its final value."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return bool(pred())


def _producer_alive() -> bool:
    """Whether the sweep's decode-ahead producer thread is still alive — the
    anti-wedge invariant is that no error path leaves one lingering."""
    return any(t.name == "analysis-producer" and t.is_alive() for t in threading.enumerate())


# --- Fake analyzers ------------------------------------------------------------


class BatchFakeAnalyzer:
    """Stateless fake that opts into the batched fast path (``analyze_batch`` + ``batch_size``).

    Verdict derived from a frame's mean gray (bright ≥ 127 → present), exactly like
    test_analysis.py's ``FakeAnalyzer``, so a test fixes each frame's verdict by the
    JPEG it stores. Records, in call order, every level it is fed (``seen``) so
    id-order preservation across the producer/consumer hand-off is assertable, and
    the size + single-shape of each batch (``batch_calls`` / ``batch_shapes``) so the
    runner's batching and shape-boundary chunking can be pinned. ``analyze_batch``
    ASSERTS its input is single-shape — the runner's guarantee — so every batched
    test enforces it for free.
    """

    def __init__(self, name: str = "fakebatch", batch_size: int = 4) -> None:
        self.name = name
        self.windowed = False
        self.batch_size = batch_size
        self.prepared_with = None
        self.prepared_since_id = None
        self.batch_calls: "list[int]" = []
        self.batch_shapes: "list[tuple]" = []
        self.seen: "list[float]" = []

    def prepare(self, store, since_id: "int | None" = None) -> None:
        self.prepared_with = store
        self.prepared_since_id = since_id

    def ensure_available(self) -> None:
        pass

    @staticmethod
    def _verdict(image) -> AnalysisResult:
        level = float(image.mean())
        return AnalysisResult(verdict=bool(level >= 127.0), score=level, detail={"level": level})

    def analyze(self, image) -> AnalysisResult:
        # The per-image path: a size-1 batch, the ``_batch_analyze`` fallback, or a
        # failed batch's per-frame retry all land here.
        self.seen.append(float(image.mean()))
        return self._verdict(image)

    def analyze_batch(self, images) -> "list[AnalysisResult]":
        shapes = {im.shape for im in images}
        assert len(shapes) == 1, f"runner mixed frame shapes in one batch: {shapes}"
        self.batch_calls.append(len(images))
        self.batch_shapes.append(next(iter(shapes)))
        results = []
        for im in images:
            self.seen.append(float(im.mean()))
            results.append(self._verdict(im))
        return results


class BatchBoomFake(BatchFakeAnalyzer):
    """``analyze_batch`` always raises (simulates a CUDA OOM); ``analyze`` works — so
    the runner's per-image fallback must still verdict every frame in the batch."""

    def analyze_batch(self, images) -> "list[AnalysisResult]":
        self.batch_calls.append(len(images))
        raise RuntimeError("simulated CUDA OOM")


class PerImageFake:
    """A stateless fake that sets ``batch_size`` but deliberately omits ``analyze_batch``.

    Proves a future stateless backend WITHOUT the batched method still sweeps
    correctly: the runner batches the QUEUE but ``_batch_analyze`` falls back to a
    per-image ``analyze`` loop, so every frame is verdicted (just unbatched).
    """

    def __init__(self, name: str = "perimage", batch_size: int = 4) -> None:
        self.name = name
        self.windowed = False
        self.batch_size = batch_size
        self.seen: "list[float]" = []

    def prepare(self, store, since_id: "int | None" = None) -> None:
        pass

    def ensure_available(self) -> None:
        pass

    def analyze(self, image) -> AnalysisResult:
        level = float(image.mean())
        self.seen.append(level)
        return AnalysisResult(verdict=bool(level >= 127.0), score=level, detail=None)

    # No analyze_batch — the runner probes with getattr and falls back per-image.


class GatedBatchAnalyzer:
    """A batched fake whose ``analyze_batch`` blocks until released — for the cancel test.

    Deterministic cancellation without timing: the first batch sets ``entered`` (so
    the test knows a batch is in flight) then blocks on ``release``. The test cancels,
    releases the one in-flight batch, and the sweep must stop at the NEXT batch
    boundary — proving cancel takes effect between batches, not after the whole set.
    """

    def __init__(self, name: str = "gatedbatch", batch_size: int = 2) -> None:
        self.name = name
        self.windowed = False
        self.batch_size = batch_size
        self.entered = threading.Event()
        self.release = threading.Event()
        self.batch_calls: "list[int]" = []

    def prepare(self, store, since_id: "int | None" = None) -> None:
        pass

    def ensure_available(self) -> None:
        pass

    def analyze_batch(self, images) -> "list[AnalysisResult]":
        self.batch_calls.append(len(images))
        self.entered.set()
        self.release.wait(timeout=5)
        return [AnalysisResult(verdict=True, score=1.0, detail=None) for _ in images]

    def analyze(self, image) -> AnalysisResult:  # pragma: no cover - batch path is used
        return AnalysisResult(verdict=True, score=1.0, detail=None)


# --- Store: write_analysis_batch ----------------------------------------------


def test_write_analysis_batch_writes_multiple_in_one_call(tmp_path):
    # One executemany persists every row: three frames, two present.
    store = _store(tmp_path)
    ids = [store.add(_frame(frame_id=i, ts=i), recv_ts_ms=1_700_000_000_000 + i) for i in range(3)]
    store.write_analysis_batch(
        [
            (ids[0], "yolo", True, 0.91, {"boxes": 1}),
            (ids[1], "yolo", False, 0.02, None),
            (ids[2], "yolo", True, 0.50, None),
        ]
    )
    assert store.analysis_summary("yolo") == {"analyzed": 3, "present": 2}
    # And the exact verdict landed on the exact frame (present ⇒ still-frame "missed").
    missed, _ = store.query_disagreements("yolo", "missed", cursor=None, limit=100)
    assert {r["id"] for r in missed} == {ids[0], ids[2]}


def test_write_analysis_batch_is_idempotent_last_wins(tmp_path):
    # INSERT OR REPLACE on (frame_id, analyzer): a second batch over the same frames
    # overwrites — one row each, last verdict wins — which is what lets a re-run
    # re-verdict without erroring or duplicating.
    store = _store(tmp_path)
    ids = [store.add(_frame(frame_id=i, ts=i), recv_ts_ms=1_700_000_000_000 + i) for i in range(3)]
    store.write_analysis_batch([(fid, "yolo", True, 0.10, None) for fid in ids])
    store.write_analysis_batch([(fid, "yolo", False, 0.90, None) for fid in ids])
    assert store.analysis_summary("yolo") == {"analyzed": 3, "present": 0}  # not 6 rows; last won


def test_write_analysis_batch_drops_verdict_for_evicted_frame(tmp_path):
    # The WHERE EXISTS guard: a verdict can never outlive its frame. A tight cap
    # evicts the two oldest frames; a batch that references one evicted id AND a live
    # id must persist ONLY the live one — the evicted row's verdict is silently
    # dropped (it describes a frame that no longer exists).
    body_len = len(_JPEG_BODY)
    store = _store(tmp_path, max_bytes=int(body_len * 2.5))  # fits ~2 frames
    ids = [store.add(_frame(frame_id=i, ts=i), recv_ts_ms=1_700_000_000_000 + i) for i in range(4)]
    assert store.stats()["count"] == 2  # ids[0], ids[1] evicted; ids[2], ids[3] survive
    evicted, live = ids[0], ids[3]

    store.write_analysis_batch(
        [(evicted, "yolo", True, 0.9, None), (live, "yolo", True, 0.8, None)]
    )
    assert store.analysis_summary("yolo") == {"analyzed": 1, "present": 1}  # only the live frame
    assert store.count_unanalyzed("yolo", since_id=live, until_id=live) == 0  # live got its verdict


def test_write_analysis_batch_empty_is_a_noop(tmp_path):
    # Empty rows: no commit, no error.
    store = _store(tmp_path)
    store.add(_frame(frame_id=1), recv_ts_ms=1_700_000_000_000)
    store.write_analysis_batch([])
    assert store.analysis_summary("yolo") == {"analyzed": 0, "present": 0}


# --- Store: close() + WAL journal mode ----------------------------------------


def test_store_uses_wal_journal_mode(tmp_path):
    # WAL is set store-wide in __init__ and persists in the DB file header, so a
    # fresh connection reads it back as 'wal' — the cheaper-commit lever the batched
    # sweep relies on.
    db_path = str(tmp_path / "index.db")
    store = _store(tmp_path)
    store.add(_frame(frame_id=1), recv_ts_ms=1_700_000_000_000)

    probe = sqlite3.connect(db_path)
    try:
        (mode,) = probe.execute("PRAGMA journal_mode").fetchone()
    finally:
        probe.close()
    assert mode.lower() == "wal"


def test_store_close_checkpoints_is_idempotent_and_persists(tmp_path):
    # close() checkpoints (TRUNCATE) the WAL and closes without error, twice (the
    # second call after the connection is closed swallows the error), and the data
    # committed before close survives a reopen — proof the checkpoint flushed it.
    db_path = str(tmp_path / "index.db")
    media_root = str(tmp_path / "media")
    store = Store(db_path=db_path, media_root=media_root, max_bytes=10_000_000)
    fid = store.add(_frame(frame_id=1), recv_ts_ms=1_700_000_000_000)
    store.write_analysis_batch([(fid, "yolo", True, 0.9, None)])

    store.close()
    store.close()  # idempotent — no raise on a second call

    reopened = Store(db_path=db_path, media_root=media_root, max_bytes=10_000_000)
    assert reopened.stats()["count"] == 1
    assert reopened.analysis_summary("yolo") == {"analyzed": 1, "present": 1}


# --- Runner: the stateless batched path ---------------------------------------


@_requires_cv
def test_run_analysis_batched_populates_all_and_preserves_order(tmp_path):
    # Seven single-shape frames, distinct gray levels; batch_size=3 → batches of
    # [3,3,1]. Every frame must get its verdict, the present count must equal the
    # bright frames, and the analyzer must have SEEN the frames oldest-first — proof
    # the producer/consumer hand-off preserves id order across batches.
    store = _store(tmp_path)
    levels = [10, 30, 200, 50, 220, 70, 240]  # 200/220/240 are present (≥127)
    for i, lv in enumerate(levels):
        store.add(_frame(frame_id=i, ts=i, body=_jpeg_gray(lv)), recv_ts_ms=1_700_000_000_000 + i)

    fake = BatchFakeAnalyzer(name="fb", batch_size=3)
    manager = AnalysisManager()
    run_analysis(store, fake, manager)

    present = sum(1 for lv in levels if lv >= 127)
    assert store.count_unanalyzed("fb") == 0
    assert store.analysis_summary("fb") == {"analyzed": len(levels), "present": present}
    assert fake.prepared_with is store
    assert fake.seen == [float(lv) for lv in levels]  # oldest-first, order preserved
    assert fake.batch_calls == [3, 3, 1]              # actually batched (not batch-1)
    st = manager.status()
    assert st["done"] == len(levels) and st["total"] == len(levels) and st["present"] == present


@_requires_cv
def test_run_analysis_falls_back_to_per_image_without_analyze_batch(tmp_path):
    # A stateless analyzer that sets batch_size but omits analyze_batch: the runner
    # batches the queue but _batch_analyze loops analyze() per frame — every frame is
    # still verdicted, in order. Proves omitting analyze_batch costs speed, not
    # correctness.
    store = _store(tmp_path)
    levels = [200, 10, 220, 30, 240]
    for i, lv in enumerate(levels):
        store.add(_frame(frame_id=i, ts=i, body=_jpeg_gray(lv)), recv_ts_ms=1_700_000_000_000 + i)

    fake = PerImageFake(name="pi", batch_size=4)
    manager = AnalysisManager()
    run_analysis(store, fake, manager)

    present = sum(1 for lv in levels if lv >= 127)
    assert store.count_unanalyzed("pi") == 0
    assert store.analysis_summary("pi") == {"analyzed": len(levels), "present": present}
    assert fake.seen == [float(lv) for lv in levels]  # all via per-image analyze(), in order


# --- Runner: robustness (skip / batch-error fallback / cancel / anti-wedge) ----


@_requires_cv
def test_run_analysis_skips_undecodable_frame_and_completes(tmp_path):
    # An undecodable frame (the fake body, which cv2.imdecode returns None for) sits
    # between two good frames. The producer detects the decode failure and ships a
    # skip-marker; the consumer counts+logs it and moves on, so the sweep COMPLETES
    # with the two good frames verdicted and the bad one left un-verdicted.
    store = _store(tmp_path)
    good1 = store.add(_frame(frame_id=0, ts=0, body=_jpeg_gray(200)), recv_ts_ms=1_700_000_000_000)
    bad = store.add(_frame(frame_id=1, ts=1, body=_JPEG_BODY), recv_ts_ms=1_700_000_000_001)
    good2 = store.add(_frame(frame_id=2, ts=2, body=_jpeg_gray(220)), recv_ts_ms=1_700_000_000_002)

    fake = BatchFakeAnalyzer(name="skip", batch_size=4)
    manager = AnalysisManager()
    run_analysis(store, fake, manager)

    assert store.analysis_summary("skip") == {"analyzed": 2, "present": 2}  # good1, good2
    assert store.count_unanalyzed("skip", since_id=bad, until_id=bad) == 1  # bad left un-verdicted
    assert store.count_unanalyzed("skip", since_id=good1, until_id=good1) == 0
    assert store.count_unanalyzed("skip", since_id=good2, until_id=good2) == 0
    assert manager.status()["done"] == 2
    assert not _producer_alive()


@_requires_cv
def test_run_analysis_batch_error_falls_back_to_per_image(tmp_path):
    # analyze_batch always raises (an OOM stand-in), but analyze() works: the runner
    # must catch the batch failure and retry it per-image so one bad batch never drops
    # a batch of good verdicts. Every frame ends up verdicted.
    store = _store(tmp_path)
    levels = [200, 10, 220]  # single shape → one batch that fails, then per-image retry
    for i, lv in enumerate(levels):
        store.add(_frame(frame_id=i, ts=i, body=_jpeg_gray(lv)), recv_ts_ms=1_700_000_000_000 + i)

    fake = BatchBoomFake(name="boom", batch_size=8)
    manager = AnalysisManager()
    run_analysis(store, fake, manager)

    present = sum(1 for lv in levels if lv >= 127)
    assert store.count_unanalyzed("boom") == 0
    assert store.analysis_summary("boom") == {"analyzed": len(levels), "present": present}
    assert fake.batch_calls == [3]                    # one batch WAS attempted (and failed)
    assert fake.seen == [float(lv) for lv in levels]  # then analyze() ran per-image, in order
    assert manager.status()["done"] == len(levels)


@_requires_cv
def test_manager_cancel_stops_batched_sweep_between_batches(tmp_path):
    # Six single-shape frames, a gated batch fake (batch_size=2) that blocks inside
    # the first analyze_batch. Cancelling while that batch is in flight, then releasing
    # it, must stop the sweep at the next batch boundary — one batch (2 verdicts)
    # written, not six — and leave the manager idle with a "canceled" terminal state.
    store = _store(tmp_path)
    for i in range(6):
        store.add(_frame(frame_id=i, ts=i, body=_jpeg_gray(200)), recv_ts_ms=1_700_000_000_000 + i)

    gated = GatedBatchAnalyzer(name="gb", batch_size=2)
    manager = AnalysisManager(resolver=lambda name: gated)
    manager.enqueue_named(store, "gb")

    assert gated.entered.wait(timeout=5), "batched sweep never reached analyze_batch()"
    manager.cancel()        # set the stop flag while the first batch is blocked
    gated.release.set()     # let that batch finish; the loop must then break at the boundary

    assert _wait(lambda: not manager.running), "batched sweep did not wind down after cancel"
    assert store.analysis_summary("gb")["analyzed"] == 2  # exactly one batch, then stopped
    assert store.count_unanalyzed("gb") == 4              # the rest were never touched
    hist = manager.status()["history"]
    assert hist[0]["state"] == "canceled" and hist[0]["error"] is None
    assert not _producer_alive()


@_requires_cv
def test_run_analysis_consumer_fatal_does_not_wedge(tmp_path, monkeypatch):
    # The anti-wedge property: a consumer-side FATAL (write_analysis_batch raising, an
    # sqlite I/O error) must NOT hang. run_analysis's finally sets abort, drains, and
    # joins the producer before the error propagates — so run_analysis RAISES (surfacing
    # the error to _run) and leaves no lingering producer, rather than deadlocking on a
    # producer parked at a full queue.
    store = _store(tmp_path)
    for i in range(4):
        store.add(_frame(frame_id=i, ts=i, body=_jpeg_gray(200)), recv_ts_ms=1_700_000_000_000 + i)

    def _boom(rows):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(store, "write_analysis_batch", _boom)

    fake = BatchFakeAnalyzer(name="cf", batch_size=2)
    manager = AnalysisManager()
    with pytest.raises(sqlite3.OperationalError):
        run_analysis(store, fake, manager)

    # The producer was joined on the way out — no orphan thread survives to race the
    # next job.
    assert _wait(lambda: not _producer_alive()), "producer thread lingered after consumer-fatal"


@_requires_cv
def test_manager_consumer_fatal_records_failed_and_goes_idle(tmp_path, monkeypatch):
    # Same consumer-fatal, driven through the manager: the worker turns the raised
    # error into a "failed" terminal state on status().error + history, the manager
    # goes idle (not stuck running), and no producer lingers — the whole one-at-a-time
    # queue is unwedged and ready for the next job.
    store = _store(tmp_path)
    for i in range(4):
        store.add(_frame(frame_id=i, ts=i, body=_jpeg_gray(200)), recv_ts_ms=1_700_000_000_000 + i)

    def _boom(rows):
        raise RuntimeError("write boom")

    monkeypatch.setattr(store, "write_analysis_batch", _boom)

    fake = BatchFakeAnalyzer(name="cf2", batch_size=2)
    manager = AnalysisManager(resolver=lambda name: fake)
    manager.enqueue_named(store, "cf2")

    assert _wait(lambda: not manager.running), "manager stuck running after consumer-fatal"
    st = manager.status()
    assert st["error"] == "write boom"
    assert st["history"][0]["state"] == "failed"
    assert st["history"][0]["error"] == "write boom"
    assert not _producer_alive()


@_requires_cv
def test_run_analysis_producer_fatal_reraises_after_join(tmp_path, monkeypatch):
    # The producer-fatal path: the iterator itself breaks (sqlite / keyset bug). The
    # producer captures it, its finally still enqueues the sentinel so the consumer
    # never blocks, and after joining the producer run_analysis RE-RAISES the captured
    # error — surfacing through _run into status().error exactly as the old serial loop
    # did — with no lingering producer.
    store = _store(tmp_path)
    for i in range(3):
        store.add(_frame(frame_id=i, ts=i, body=_jpeg_gray(200)), recv_ts_ms=1_700_000_000_000 + i)

    def _bad_iter(*args, **kwargs):
        raise RuntimeError("iterator boom")
        yield  # pragma: no cover - unreachable, only makes this a generator

    monkeypatch.setattr(store, "iter_unanalyzed", _bad_iter)

    fake = BatchFakeAnalyzer(name="pf", batch_size=2)
    manager = AnalysisManager()
    with pytest.raises(RuntimeError, match="iterator boom"):
        run_analysis(store, fake, manager)

    assert _wait(lambda: not _producer_alive()), "producer thread lingered after producer-fatal"


# --- Runner: shape-boundary chunking ------------------------------------------


@_requires_cv
def test_run_analysis_batches_never_mix_frame_shapes(tmp_path):
    # Frames of DIFFERING dimensions, interleaved so a naive fixed-size batch WOULD
    # mix them: [A, A, B, B, A] with batch_size=4. The runner must flush at each
    # dimension boundary, so every batch letterboxes exactly as the single-image path
    # would. BatchFakeAnalyzer.analyze_batch asserts single-shape per batch (would
    # raise if the runner mixed them), and here we pin the exact chunking AND that
    # every differently-sized frame still got its correct verdict, in order.
    store = _store(tmp_path)
    A = (16, 16, 3)
    B = (24, 32, 3)
    # (h, w, level) — level ≥127 ⇒ present.
    specs = [
        (16, 16, 200),  # A, present
        (16, 16, 10),   # A, absent
        (24, 32, 220),  # B (shape change mid-batch), present
        (24, 32, 30),   # B, absent
        (16, 16, 240),  # A again, present
    ]
    for i, (h, w, lv) in enumerate(specs):
        store.add(_frame(frame_id=i, ts=i, body=_jpeg_gray(lv, h, w)), recv_ts_ms=1_700_000_000_000 + i)

    fake = BatchFakeAnalyzer(name="shape", batch_size=4)
    manager = AnalysisManager()
    run_analysis(store, fake, manager)  # analyze_batch asserts each batch is single-shape

    present = sum(1 for (_h, _w, lv) in specs if lv >= 127)
    assert store.count_unanalyzed("shape") == 0
    assert store.analysis_summary("shape") == {"analyzed": len(specs), "present": present}
    # Chunking split exactly at the two shape boundaries, never at batch_size (=4):
    assert fake.batch_calls == [2, 2, 1]
    assert fake.batch_shapes == [A, B, A]
    # Every frame — across differing shapes — was seen in id order with its true level.
    assert fake.seen == [float(lv) for (_h, _w, lv) in specs]
