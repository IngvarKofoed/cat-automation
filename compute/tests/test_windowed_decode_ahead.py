"""Tests for the WINDOWED sweep's decode-ahead (MOG2/BSUV re-runs).

The windowed path decodes frames on a small thread pool while running inference
serially. That is only sound because decode is a pure function of the file, so the
analyzer must still see every frame EXACTLY ONCE, in id order, whatever the worker
count — which is the invariant these tests pin. A reordering regression would be
invisible in the aggregate (the same verdicts, redistributed across frames), and for a
stateful analyzer it silently changes what every later frame is scored against.

Frames encode their position as a solid gray level (the device
``test_sweep_batching`` uses: solid gray round-trips through JPEG exactly), so a fake
windowed analyzer can record the order it was fed and a test can assert it.
"""
from __future__ import annotations

import pytest

from compute.analysis.base import AnalysisResult
from compute.analysis.runner import AnalysisManager, run_analysis
from compute.collection.store import Store
from compute.ingest import StreamFrame
from shared.wire import StreamFrameMeta

try:
    import cv2
    import numpy as np

    _HAVE_CV = True
except Exception:  # pragma: no cover - exercised only where cv2 is absent
    _HAVE_CV = False

_requires_cv = pytest.mark.skipif(not _HAVE_CV, reason="cv2/numpy required for the sweep tests")

_BAD_JPEG = b"\xff\xd8\xff\xe0" + b"fake-jpeg-body" + b"\xff\xd9"


def _store(tmp_path) -> Store:
    return Store(
        db_path=str(tmp_path / "index.db"),
        media_root=str(tmp_path / "media"),
        max_bytes=10_000_000,
    )


def _jpeg_gray(level: int, h: int = 16, w: int = 16) -> bytes:
    ok, buf = cv2.imencode(".jpg", np.full((h, w, 3), level, dtype=np.uint8))
    assert ok
    return bytes(buf)


def _seed(store: Store, levels: "list[int]") -> None:
    """One frame per level, ids 1..N in order, each a solid-gray JPEG of that level."""
    for i, level in enumerate(levels, start=1):
        meta = StreamFrameMeta(frame_id=i, ts=1_000 + i, motion=False, bbox=None, area=0.0)
        store.add(StreamFrame(meta, _jpeg_gray(level)), recv_ts_ms=1_700_000_000_000 + i)


class StatefulFake:
    """A windowed analyzer that records what it saw, and whose verdict depends on ORDER.

    ``seen`` is the decoded gray level of every frame in the order fed — so a
    reordering shows up directly. ``score`` is a running sum, mirroring how a real
    windowed analyzer (MOG2's background, BSUV's window) folds each frame into state
    that every later verdict depends on: reorder the input and the scores diverge even
    where the per-frame verdicts happen to match.
    """

    windowed = True
    name = "mog2:candidate"

    def __init__(self) -> None:
        self.seen: "list[int]" = []
        self._running = 0.0
        self.prepared_with: "list" = []

    def ensure_available(self) -> None:
        return None

    def prepare(self, store, since_id=None) -> None:
        self.prepared_with.append(since_id)

    def analyze(self, image) -> AnalysisResult:
        level = int(round(float(image.mean())))
        self.seen.append(level)
        self._running += level
        return AnalysisResult(verdict=level >= 127, score=self._running, detail={"n": len(self.seen)})


@_requires_cv
@pytest.mark.parametrize("workers,lookahead", [(1, 1), (1, 8), (2, 8), (4, 3), (8, 16)])
def test_decode_ahead_preserves_order_and_verdicts(tmp_path, monkeypatch, workers, lookahead):
    """Any (workers, lookahead) feeds the analyzer the identical in-order sequence.

    (1, 1) is the pre-change serial shape, so this doubles as the A/B: every other
    configuration must produce byte-identical stored verdicts to it.
    """
    monkeypatch.setattr("compute.analysis.runner._DECODE_WORKERS", workers)
    monkeypatch.setattr("compute.analysis.runner._DECODE_LOOKAHEAD", lookahead)

    levels = [(i * 7) % 256 for i in range(1, 61)]
    store = _store(tmp_path)
    _seed(store, levels)
    analyzer = StatefulFake()
    manager = AnalysisManager()

    run_analysis(store, analyzer, manager)

    assert analyzer.seen == levels, "analyzer saw frames out of order or missed/duplicated one"
    rows = store._conn.execute(
        "SELECT frame_id, verdict, score FROM analysis WHERE analyzer = ? ORDER BY frame_id",
        (analyzer.name,),
    ).fetchall()
    assert [r[0] for r in rows] == list(range(1, len(levels) + 1))
    # The running score pins that each frame was folded into state at its own position.
    expected = []
    running = 0.0
    for lvl in levels:
        running += lvl
        expected.append((1 if lvl >= 127 else 0, running))
    assert [(r[1], r[2]) for r in rows] == expected
    store.close()


@_requires_cv
def test_decode_ahead_matches_serial_exactly(tmp_path, monkeypatch):
    """The parallel-decode sweep and a forced-serial one write identical verdicts."""
    levels = [(i * 13 + 3) % 256 for i in range(1, 81)]

    def run(workers, lookahead, path):
        monkeypatch.setattr("compute.analysis.runner._DECODE_WORKERS", workers)
        monkeypatch.setattr("compute.analysis.runner._DECODE_LOOKAHEAD", lookahead)
        store = _store(path)
        _seed(store, levels)
        analyzer = StatefulFake()
        run_analysis(store, analyzer, AnalysisManager())
        rows = store._conn.execute(
            "SELECT frame_id, verdict, score, detail FROM analysis WHERE analyzer = ? ORDER BY frame_id",
            (analyzer.name,),
        ).fetchall()
        store.close()
        return rows

    serial = run(1, 1, tmp_path / "serial")
    parallel = run(4, 8, tmp_path / "parallel")
    assert serial == parallel
    assert len(serial) == len(levels)


@_requires_cv
def test_undecodable_frame_is_skipped_not_fatal(tmp_path, monkeypatch):
    """A frame whose decode raises on a pool thread is skipped, in position, and counted.

    The decode now fails inside a future, so its exception surfaces at ``result()``
    rather than inline — this pins that it still lands on the same log-and-skip path and
    does not abort the sweep or shift the frames after it.
    """
    monkeypatch.setattr("compute.analysis.runner._DECODE_WORKERS", 3)
    monkeypatch.setattr("compute.analysis.runner._DECODE_LOOKAHEAD", 4)

    store = _store(tmp_path)
    good = [10, 20, 200, 210]
    for i, level in enumerate(good[:2], start=1):
        meta = StreamFrameMeta(frame_id=i, ts=1_000 + i, motion=False, bbox=None, area=0.0)
        store.add(StreamFrame(meta, _jpeg_gray(level)), recv_ts_ms=1_700_000_000_000 + i)
    # id 3 is undecodable
    store.add(
        StreamFrame(StreamFrameMeta(frame_id=3, ts=1_003, motion=False, bbox=None, area=0.0), _BAD_JPEG),
        recv_ts_ms=1_700_000_000_003,
    )
    for i, level in enumerate(good[2:], start=4):
        meta = StreamFrameMeta(frame_id=i, ts=1_000 + i, motion=False, bbox=None, area=0.0)
        store.add(StreamFrame(meta, _jpeg_gray(level)), recv_ts_ms=1_700_000_000_000 + i)

    analyzer = StatefulFake()
    manager = AnalysisManager()
    run_analysis(store, analyzer, manager)

    assert analyzer.seen == good, "the bad frame must be skipped without disturbing the rest"
    ids = [r[0] for r in store._conn.execute(
        "SELECT frame_id FROM analysis WHERE analyzer = ? ORDER BY frame_id", (analyzer.name,)
    ).fetchall()]
    assert ids == [1, 2, 4, 5]
    store.close()


@_requires_cv
def test_cancel_stops_early_and_persists_what_ran(tmp_path, monkeypatch):
    """A stop mid-sweep flushes the verdicts already computed and analyzes no more.

    The lookahead means decodes are in flight when the stop lands; they must be dropped,
    not analyzed, so the stored verdicts stay a prefix of the frame order.
    """
    monkeypatch.setattr("compute.analysis.runner._DECODE_WORKERS", 2)
    monkeypatch.setattr("compute.analysis.runner._DECODE_LOOKAHEAD", 8)
    monkeypatch.setattr("compute.analysis.runner._WRITE_BATCH", 4)

    levels = [(i * 3) % 256 for i in range(1, 61)]
    store = _store(tmp_path)
    _seed(store, levels)
    manager = AnalysisManager()

    class StopsItself(StatefulFake):
        def analyze(self, image):
            res = super().analyze(image)
            if len(self.seen) == 12:
                manager.stop_event.set()
            return res

    analyzer = StopsItself()
    run_analysis(store, analyzer, manager)

    assert analyzer.seen == levels[:12]
    ids = [r[0] for r in store._conn.execute(
        "SELECT frame_id FROM analysis WHERE analyzer = ? ORDER BY frame_id", (analyzer.name,)
    ).fetchall()]
    assert ids == list(range(1, 13)), "verdicts computed before the stop must persist, and only those"
    store.close()
