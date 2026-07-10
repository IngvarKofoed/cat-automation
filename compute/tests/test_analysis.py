"""Tests for the offline-oracle analysis layer (compute/analysis/, and the
analysis methods on compute/collection/store.py).

Two layers, tested at two levels of realism:

- **Store methods** (``write_analysis``, ``query_disagreements``, the sweep
  iterators, the summary/cascade helpers) never decode a JPEG, so — exactly like
  test_collection.py — they run against ``StreamFrame`` objects built directly
  from the fake ``_JPEG_BODY`` and drive verdicts by calling ``write_analysis``
  straight, giving each test full, independent control over a frame's motion flag
  (``frames``) and its oracle verdict (``analysis``). No cv2 needed.
- **Runner + manager** (``run_analysis``, ``AnalysisManager``) DO decode: the
  runner hard-uses ``cv2.imdecode`` on the stored bytes. So those tests fabricate
  *real* solid-gray JPEGs whose gray level encodes the verdict we want the fake
  oracle to return (bright → present, dark → absent) — solid colours round-trip
  through JPEG exactly (verified), so a scenario stays deterministic. A windowed
  fake instead records the order it is fed frames, proving time-order iteration.

No torch/ultralytics/GPU and no real model: a small ``FakeAnalyzer`` implements the
``Analyzer`` protocol and derives its verdict deterministically from the frame it
is handed, so every scenario is controllable. See
docs/specs/2026-07-09-motion-gate-oracles.md.
"""
from __future__ import annotations

import os
import time

import pytest

from compute.analysis.base import AnalysisResult
from compute.analysis.runner import AnalysisManager, run_analysis
from compute.collection.store import Store
from compute.ingest import StreamFrame
from shared.wire import StreamFrameMeta

# A minimal valid JPEG for the STORE tests, which never decode it (same body the
# collection tests use). The RUNNER tests can't use this — the runner decodes —
# so they build real JPEGs via ``_jpeg_gray`` below.
_JPEG_BODY = b"\xff\xd8\xff\xe0" + b"fake-jpeg-body" + b"\xff\xd9"

# cv2/numpy are a hard dependency of the runner (it decodes), but NOT of the store
# layer. Import them behind a guard so a box without the CV stack still runs the
# store tests and merely skips the sweep tests, rather than erroring the whole file.
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


def _jpeg_gray(level: int) -> bytes:
    """A real solid-gray JPEG at ``level`` (0..255).

    The runner decodes the stored bytes, so a scenario controls what a fake oracle
    sees by the gray level it encodes here: the fake reads the decoded mean back
    out. Solid colours round-trip through JPEG exactly, so the level the fake
    recovers equals the level written — the property every runner test leans on.
    """
    img = np.full((16, 16, 3), level, dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", img)
    assert ok
    return buf.tobytes()


class FakeAnalyzer:
    """A controllable stand-in for a real oracle, satisfying the ``Analyzer`` protocol.

    ``analyze`` derives its verdict deterministically from the frame's own mean gray
    level (bright ≥ 127 → present), so a test fixes each frame's verdict purely by
    the JPEG it stores — no model, no randomness. It also records, in order, every
    level it is fed, so a windowed sweep can be asserted to visit frames in ascending
    time order. ``prepare`` just captures the store handle so the windowed-priming
    hand-off can be checked.
    """

    def __init__(self, name: str = "fake", windowed: bool = False) -> None:
        self.name = name
        self.windowed = windowed
        self.prepared_with = None
        self.seen: "list[float]" = []

    def prepare(self, store) -> None:
        self.prepared_with = store

    def ensure_available(self) -> None:
        # No optional deps to check for the fake; the runner calls this in start().
        pass

    def analyze(self, image) -> AnalysisResult:
        level = float(image.mean())
        self.seen.append(level)
        return AnalysisResult(verdict=bool(level >= 127.0), score=level, detail={"level": level})


class GatedAnalyzer:
    """A stateless fake whose ``analyze`` blocks until released — for the cancel test.

    Deterministic cancellation without timing: the first ``analyze`` sets ``entered``
    (so the test knows a frame is in flight) then blocks on ``release``. The test
    cancels, releases the one in-flight frame, and the sweep must stop at the next
    frame boundary — proving cancel takes effect between frames, not after the whole set.
    """

    def __init__(self, name: str = "gated") -> None:
        self.name = name
        self.windowed = False
        import threading

        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def prepare(self, store) -> None:  # pragma: no cover - trivial
        pass

    def ensure_available(self) -> None:  # pragma: no cover - trivial
        pass

    def analyze(self, image) -> AnalysisResult:
        self.calls += 1
        self.entered.set()
        self.release.wait(timeout=5)
        return AnalysisResult(verdict=True, score=1.0, detail=None)


def _wait(pred, timeout: float = 5.0, interval: float = 0.005) -> bool:
    """Poll ``pred`` until true or ``timeout`` elapses; return its final value."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return bool(pred())


# --- Store: write_analysis + query_disagreements ------------------------------


def test_write_analysis_and_disagreements_both_modes(tmp_path):
    # A frame per (motion, verdict) quadrant, plus an analyzed-nowhere frame, so
    # both disagreement views can be asserted to the exact id — and the agreeing
    # quadrants and the unanalyzed frame are proven to be excluded.
    store = _store(tmp_path)
    ids = {}
    for key, motion in [("f1", False), ("f2", True), ("f3", True), ("f4", False), ("f5", False), ("f6", False)]:
        bbox = (0.0, 0.0, 0.1, 0.1) if motion else None
        ids[key] = store.add(_frame(frame_id=int(key[1]), ts=int(key[1]), motion=motion, bbox=bbox, area=0.1), recv_ts_ms=1_700_000_000_000 + int(key[1]))

    store.write_analysis(ids["f1"], "yolo", True, 0.91, {"boxes": 1})   # motion=0, verdict=1 -> missed
    store.write_analysis(ids["f2"], "yolo", False, 0.02, None)           # motion=1, verdict=0 -> false
    store.write_analysis(ids["f3"], "yolo", True, 0.80, None)            # motion=1, verdict=1 -> agree
    store.write_analysis(ids["f4"], "yolo", False, 0.05, None)           # motion=0, verdict=0 -> agree
    store.write_analysis(ids["f5"], "yolo", True, 0.77, None)            # motion=0, verdict=1 -> missed
    # f6 is left un-analyzed: it must appear in NEITHER view (INNER JOIN excludes it).

    missed, cur = store.query_disagreements("yolo", "missed", cursor=None, limit=100)
    assert [r["id"] for r in missed] == [ids["f5"], ids["f1"]]  # newest-first
    assert cur is None
    # Rows carry the browse shape PLUS the oracle's score, and nothing else.
    assert set(missed[0].keys()) == {"id", "recv_ts", "edge_ts", "frame_id", "motion", "area", "bbox", "url", "score"}
    by_id = {r["id"]: r for r in missed}
    assert by_id[ids["f1"]]["score"] == pytest.approx(0.91)
    assert by_id[ids["f5"]]["score"] == pytest.approx(0.77)

    false, cur = store.query_disagreements("yolo", "false", cursor=None, limit=100)
    assert [r["id"] for r in false] == [ids["f2"]]
    assert false[0]["score"] == pytest.approx(0.02)
    assert cur is None


def test_query_disagreements_only_covers_this_analyzer(tmp_path):
    # A verdict under a different analyzer name must not leak into another's view.
    store = _store(tmp_path)
    fid = store.add(_frame(frame_id=1, motion=False), recv_ts_ms=1_700_000_000_000)
    store.write_analysis(fid, "bsuv", True, 0.5, None)  # missed — but only for 'bsuv'

    rows, _ = store.query_disagreements("yolo", "missed", cursor=None, limit=100)
    assert rows == []
    rows, _ = store.query_disagreements("bsuv", "missed", cursor=None, limit=100)
    assert [r["id"] for r in rows] == [fid]


def test_query_disagreements_keyset_pagination_and_cursor_round_trip(tmp_path):
    # Five "missed" frames (motion=0, verdict=1). Walking with limit 2 must cover
    # every frame once, newest-first, across the opaque id cursor — no gaps/dupes,
    # and the trailing empty page returns a None cursor.
    store = _store(tmp_path)
    ids = [store.add(_frame(frame_id=i, ts=i, motion=False), recv_ts_ms=1_700_000_000_000 + i) for i in range(5)]
    for fid in ids:
        store.write_analysis(fid, "yolo", True, 0.5, None)

    page1, c1 = store.query_disagreements("yolo", "missed", cursor=None, limit=2)
    assert [r["id"] for r in page1] == [ids[4], ids[3]]
    assert c1 == str(ids[3])

    page2, c2 = store.query_disagreements("yolo", "missed", cursor=c1, limit=2)
    assert [r["id"] for r in page2] == [ids[2], ids[1]]
    assert c2 == str(ids[1])

    page3, c3 = store.query_disagreements("yolo", "missed", cursor=c2, limit=2)
    assert [r["id"] for r in page3] == [ids[0]]  # short page → last one
    assert c3 is None

    walked = [r["id"] for r in page1 + page2 + page3]
    assert walked == list(reversed(ids))


def test_query_disagreements_rejects_bad_mode(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        store.query_disagreements("yolo", "bogus", cursor=None, limit=10)


# --- Store: write_analysis idempotency ----------------------------------------


def test_write_analysis_is_idempotent_last_wins(tmp_path):
    # INSERT OR REPLACE on (frame_id, analyzer): a second write for the same pair
    # overwrites — one row, last verdict/score wins. This is what lets a windowed
    # sweep revisit a frame and a re-run overwrite without erroring or duplicating.
    store = _store(tmp_path)
    fid = store.add(_frame(frame_id=1, motion=False), recv_ts_ms=1_700_000_000_000)

    store.write_analysis(fid, "yolo", True, 0.10, None)
    store.write_analysis(fid, "yolo", False, 0.90, None)

    summary = store.analysis_summary("yolo")
    assert summary["analyzed"] == 1     # not two rows
    assert summary["present"] == 0      # last write (verdict False) won
    # A verdict=1 row would surface as a "missed"; the overwrite to 0 clears it.
    rows, _ = store.query_disagreements("yolo", "missed", cursor=None, limit=10)
    assert rows == []


# --- Store: sweep iterators ----------------------------------------------------


def test_iter_unanalyzed_skips_done_and_is_oldest_first(tmp_path):
    store = _store(tmp_path)
    ids = [store.add(_frame(frame_id=i, ts=i), recv_ts_ms=1_700_000_000_000 + i) for i in range(5)]
    # Verdict the 2nd and 4th; the sweep must skip exactly those.
    store.write_analysis(ids[1], "yolo", True, 0.5, None)
    store.write_analysis(ids[3], "yolo", False, 0.5, None)

    yielded = list(store.iter_unanalyzed("yolo"))
    assert [fid for fid, _ in yielded] == [ids[0], ids[2], ids[4]]  # oldest-first, done skipped
    # Second element is the absolute media path, and the file is really there.
    for _, abs_path in yielded:
        assert os.path.isabs(abs_path) and os.path.isfile(abs_path)

    # A different analyzer has verdicted nothing → it sees all five.
    assert [fid for fid, _ in store.iter_unanalyzed("bsuv")] == ids


def test_iter_unanalyzed_paginates_across_batches(tmp_path):
    # A batch smaller than the row count exercises the keyset (f.id > last) loop,
    # proving the batched iterator yields every unanalyzed frame exactly once.
    store = _store(tmp_path)
    ids = [store.add(_frame(frame_id=i, ts=i), recv_ts_ms=1_700_000_000_000 + i) for i in range(7)]
    assert [fid for fid, _ in store.iter_unanalyzed("yolo", batch=2)] == ids


def test_iter_time_order_yields_all_oldest_first(tmp_path):
    store = _store(tmp_path)
    ids = [store.add(_frame(frame_id=i, ts=i), recv_ts_ms=1_700_000_000_000 + i) for i in range(6)]
    # Analyzing some frames must NOT change what iter_time_order yields — it is the
    # windowed driver and always revisits every frame in ascending id order.
    store.write_analysis(ids[0], "bsuv", True, 0.5, None)
    store.write_analysis(ids[3], "bsuv", True, 0.5, None)
    assert [fid for fid, _ in store.iter_time_order(batch=2)] == ids


def test_count_unanalyzed(tmp_path):
    store = _store(tmp_path)
    ids = [store.add(_frame(frame_id=i), recv_ts_ms=1_700_000_000_000 + i) for i in range(4)]
    assert store.count_unanalyzed("yolo") == 4
    store.write_analysis(ids[0], "yolo", True, 0.5, None)
    store.write_analysis(ids[1], "yolo", False, 0.5, None)
    assert store.count_unanalyzed("yolo") == 2
    assert store.count_unanalyzed("bsuv") == 4  # per-analyzer


def test_analysis_summary_counts(tmp_path):
    store = _store(tmp_path)
    # Never-analyzed analyzer reports zeros (COALESCE), not None.
    assert store.analysis_summary("yolo") == {"analyzed": 0, "present": 0}

    ids = [store.add(_frame(frame_id=i), recv_ts_ms=1_700_000_000_000 + i) for i in range(4)]
    store.write_analysis(ids[0], "yolo", True, 0.9, None)
    store.write_analysis(ids[1], "yolo", True, 0.8, None)
    store.write_analysis(ids[2], "yolo", False, 0.1, None)
    assert store.analysis_summary("yolo") == {"analyzed": 3, "present": 2}


def test_recent_before_is_chronological(tmp_path):
    # recent_before returns the n frames JUST BEFORE a given id, in ascending
    # (chronological) order — the replay order a windowed analyzer primes with.
    store = _store(tmp_path)
    ids = [store.add(_frame(frame_id=i, ts=i), recv_ts_ms=1_700_000_000_000 + i) for i in range(6)]
    paths = store.recent_before(ids[4], n=2)  # the two frames before id5: ids[2], ids[3]
    expected = [store.path_for(ids[2]), store.path_for(ids[3])]
    assert paths == expected  # chronological, not reverse

    # Fewer preceding frames than requested → just what exists, still chronological.
    assert store.recent_before(ids[1], n=10) == [store.path_for(ids[0])]
    assert store.recent_before(ids[0], n=5) == []  # nothing before the first


def test_clear_analysis_scopes_to_one_analyzer(tmp_path):
    store = _store(tmp_path)
    ids = [store.add(_frame(frame_id=i), recv_ts_ms=1_700_000_000_000 + i) for i in range(3)]
    for fid in ids:
        store.write_analysis(fid, "yolo", True, 0.5, None)
        store.write_analysis(fid, "bsuv", True, 0.5, None)

    deleted = store.clear_analysis("yolo")
    assert deleted == 3
    assert store.analysis_summary("yolo") == {"analyzed": 0, "present": 0}
    assert store.analysis_summary("bsuv") == {"analyzed": 3, "present": 3}  # untouched


# --- Store: cascade (eviction + clear drop analysis rows) ---------------------


def test_eviction_cascades_to_analysis_rows(tmp_path):
    # Cap fits ~2 frames; verdict each frame right after adding it. As newer adds
    # evict the oldest frames, their analysis rows must go too — no verdict outlives
    # its frame. analysis_summary dropping to the surviving count proves the cascade.
    body_len = len(_JPEG_BODY)
    store = _store(tmp_path, max_bytes=int(body_len * 2.5))
    for i in range(4):
        fid = store.add(_frame(frame_id=i, ts=i), recv_ts_ms=1_700_000_000_000 + i)
        store.write_analysis(fid, "yolo", True, 0.5, None)

    assert store.stats()["count"] == 2            # only the newest two frames survive
    assert store.analysis_summary("yolo")["analyzed"] == 2  # the 2 evicted rows are gone
    # Both survivors are still analyzed → nothing left for a sweep to (re)do.
    assert store.count_unanalyzed("yolo") == 0


def test_clear_removes_analysis_rows(tmp_path):
    store = _store(tmp_path)
    ids = [store.add(_frame(frame_id=i), recv_ts_ms=1_700_000_000_000 + i) for i in range(3)]
    for fid in ids:
        store.write_analysis(fid, "yolo", True, 0.5, None)
    assert store.analysis_summary("yolo")["analyzed"] == 3

    store.clear()
    assert store.stats()["count"] == 0
    assert store.analysis_summary("yolo") == {"analyzed": 0, "present": 0}


# --- Runner: run_analysis (real Store, real JPEG decode, fake oracle) ---------


@_requires_cv
def test_run_analysis_stateless_populates_all_and_present_matches(tmp_path):
    # Bright frames (level 255) are also motion=0, dark frames (level 0) are also
    # motion=1. So after the sweep, the bright/present frames land in the "missed"
    # view and the dark/absent ones in "false" — an end-to-end check that the runner
    # wrote the RIGHT verdict to the RIGHT frame, not just the right totals.
    store = _store(tmp_path)
    bright_ids, dark_ids = [], []
    for i in range(5):
        if i % 2 == 0:
            bright_ids.append(store.add(_frame(frame_id=i, ts=i, motion=False, body=_jpeg_gray(255)), recv_ts_ms=1_700_000_000_000 + i))
        else:
            dark_ids.append(store.add(_frame(frame_id=i, ts=i, motion=True, bbox=(0, 0, 0.1, 0.1), body=_jpeg_gray(0)), recv_ts_ms=1_700_000_000_000 + i))

    fake = FakeAnalyzer(name="fake", windowed=False)
    manager = AnalysisManager()
    run_analysis(store, fake, manager)

    # Every frame got a verdict; the present count equals the bright frames.
    assert store.count_unanalyzed("fake") == 0
    assert store.analysis_summary("fake") == {"analyzed": 5, "present": len(bright_ids)}
    st = manager.status()
    assert st["done"] == 5 and st["total"] == 5 and st["present"] == len(bright_ids)

    # Right verdict to right frame: present ⇒ missed view, absent ⇒ false view.
    missed, _ = store.query_disagreements("fake", "missed", cursor=None, limit=100)
    assert {r["id"] for r in missed} == set(bright_ids)
    assert all(r["score"] > 200 for r in missed)
    false, _ = store.query_disagreements("fake", "false", cursor=None, limit=100)
    assert {r["id"] for r in false} == set(dark_ids)
    assert all(r["score"] < 55 for r in false)


@_requires_cv
def test_run_analysis_windowed_visits_every_frame_in_time_order(tmp_path):
    # Ascending gray levels in insertion (== id/time) order; a windowed sweep drives
    # iter_time_order, so the levels the fake records must come back strictly
    # ascending and cover all frames — proof it saw them in time order, once each.
    store = _store(tmp_path)
    levels = [30, 70, 110, 150, 190, 230]
    for i, lv in enumerate(levels):
        store.add(_frame(frame_id=i, ts=i, body=_jpeg_gray(lv)), recv_ts_ms=1_700_000_000_000 + i)

    fake = FakeAnalyzer(name="fakewin", windowed=True)
    manager = AnalysisManager()
    run_analysis(store, fake, manager)

    assert fake.prepared_with is store            # store handed to prepare() for priming
    assert fake.seen == sorted(fake.seen)         # strictly ascending == time order
    assert len(fake.seen) == len(levels)
    assert store.analysis_summary("fakewin")["analyzed"] == len(levels)
    assert manager.status()["done"] == len(levels)


# --- AnalysisManager: lifecycle ------------------------------------------------


@_requires_cv
def test_manager_start_runs_to_completion(tmp_path):
    store = _store(tmp_path)
    # Two bright (present) + two dark (absent) frames.
    for i in range(4):
        store.add(_frame(frame_id=i, ts=i, body=_jpeg_gray(255 if i < 2 else 0)), recv_ts_ms=1_700_000_000_000 + i)

    fake = FakeAnalyzer(name="fake", windowed=False)
    manager = AnalysisManager(resolver=lambda name: fake)
    manager.start(store, "fake")

    assert _wait(lambda: not manager.running), "sweep did not finish within timeout"
    st = manager.status()
    assert st["running"] is False
    assert st["error"] is None
    assert st["done"] == st["total"] == 4
    assert st["present"] == 2


@_requires_cv
def test_manager_cancel_stops_early(tmp_path):
    # Six frames but a gated fake that blocks inside the first analyze(). Cancelling
    # while that frame is in flight, then releasing it, must stop the sweep at the
    # next frame boundary — one verdict written, not six.
    store = _store(tmp_path)
    for i in range(6):
        store.add(_frame(frame_id=i, ts=i, body=_jpeg_gray(200)), recv_ts_ms=1_700_000_000_000 + i)

    gated = GatedAnalyzer(name="gated")
    manager = AnalysisManager(resolver=lambda name: gated)
    manager.start(store, "gated")

    assert gated.entered.wait(timeout=5), "sweep never reached the first analyze()"
    manager.cancel()        # set the stop flag while frame 1 is blocked
    gated.release.set()     # let that one frame finish; the loop must then break

    assert _wait(lambda: not manager.running), "sweep did not wind down after cancel"
    assert gated.calls == 1
    assert manager.status()["done"] == 1
    assert store.analysis_summary("gated")["analyzed"] == 1
    assert store.count_unanalyzed("gated") == 5  # the rest were never touched


@_requires_cv
def test_manager_start_refuses_second_job_while_running(tmp_path):
    # One job at a time: while a gated sweep is blocked mid-frame, a second start
    # must raise RuntimeError (the API maps this to 409). The gate only trips after
    # a real decode reaches analyze(), so the frames need real JPEGs (and cv2).
    store = _store(tmp_path)
    for i in range(3):
        store.add(_frame(frame_id=i, ts=i, body=_jpeg_gray(200)), recv_ts_ms=1_700_000_000_000 + i)

    gated = GatedAnalyzer(name="gated")
    manager = AnalysisManager(resolver=lambda name: gated)
    manager.start(store, "gated")
    try:
        assert gated.entered.wait(timeout=5) or manager.running
        with pytest.raises(RuntimeError):
            manager.start(store, "gated")
    finally:
        manager.cancel()
        gated.release.set()
        _wait(lambda: not manager.running)


def test_manager_start_surfaces_importerror_from_resolver(tmp_path):
    # A backend whose optional deps are missing raises ImportError from the resolver;
    # because start() resolves SYNCHRONOUSLY before spawning the thread, that error
    # reaches the caller (→ 503) instead of vanishing into the worker — and no job
    # starts, so the manager stays idle for the next attempt.
    store = _store(tmp_path)

    def bad_resolver(name):
        raise ImportError("install compute/requirements-analysis.txt")

    manager = AnalysisManager(resolver=bad_resolver)
    with pytest.raises(ImportError):
        manager.start(store, "yolo")
    assert manager.running is False
    assert manager.status()["error"] is None  # nothing ran, so no recorded error
