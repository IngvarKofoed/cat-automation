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
    time order. ``prepare`` captures both the store handle AND the scope's
    ``since_id`` floor, so the windowed-priming hand-off *and* the frame-range-groups
    scoping contract (``run_analysis`` must call ``prepare(store, since_id=since)``)
    can both be checked.
    """

    def __init__(self, name: str = "fake", windowed: bool = False) -> None:
        self.name = name
        self.windowed = windowed
        self.prepared_with = None
        self.prepared_since_id = None
        self.seen: "list[float]" = []

    def prepare(self, store, since_id: "int | None" = None) -> None:
        self.prepared_with = store
        self.prepared_since_id = since_id

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

    def prepare(self, store, since_id: "int | None" = None) -> None:  # pragma: no cover - trivial
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


def test_clear_analysis_scoped_to_id_range_preserves_out_of_window(tmp_path):
    # A SCOPED reanalyze clears ONLY the window's verdicts, so re-running an oracle over
    # one group no longer discards every verdict outside it (the whole-store disagreement
    # view and other windows keep theirs).
    store = _store(tmp_path)
    ids = [store.add(_frame(frame_id=i), recv_ts_ms=1_700_000_000_000 + i) for i in range(5)]
    for fid in ids:
        store.write_analysis(fid, "yolo", True, 0.5, None)

    deleted = store.clear_analysis("yolo", since_id=ids[1], until_id=ids[3])
    assert deleted == 3  # only ids[1..3] cleared
    assert store.analysis_summary("yolo") == {"analyzed": 2, "present": 2}  # ids[0], ids[4] survive
    for fid in (ids[1], ids[2], ids[3]):
        assert store.count_unanalyzed("yolo", since_id=fid, until_id=fid) == 1  # verdict gone
    for fid in (ids[0], ids[4]):
        assert store.count_unanalyzed("yolo", since_id=fid, until_id=fid) == 0  # verdict kept


def test_iter_and_count_unanalyzed_motion_only(tmp_path):
    # The tight Activity "Analyze" path: motion_only restricts the stateless sweep to
    # frames.motion=1, so at continuous capture it skips the non-motion majority.
    store = _store(tmp_path)
    ids = [
        store.add(_frame(frame_id=i, ts=i, motion=(i % 2 == 0), area=0.1), recv_ts_ms=1_700_000_000_000 + i)
        for i in range(6)
    ]
    motion_ids = [ids[i] for i in range(6) if i % 2 == 0]
    assert [fid for fid, _ in store.iter_unanalyzed("yolo", motion_only=True)] == motion_ids
    assert store.count_unanalyzed("yolo", motion_only=True) == len(motion_ids)
    # Default (off) still sees every frame, exactly as before.
    assert [fid for fid, _ in store.iter_unanalyzed("yolo")] == ids
    assert store.count_unanalyzed("yolo") == 6


def test_clear_analysis_motion_only_spares_non_motion_verdicts(tmp_path):
    # A motion-only reanalyze clear drops MOTION frames' verdicts (so they re-detect) but
    # SPARES non-motion verdicts a breadth sweep produced — the tight button must not
    # degrade the store's wider coverage.
    store = _store(tmp_path)
    m = store.add(_frame(frame_id=1, ts=1, motion=True, area=0.1), recv_ts_ms=1_700_000_000_001)
    n = store.add(_frame(frame_id=2, ts=2, motion=False, area=0.0), recv_ts_ms=1_700_000_000_002)
    store.write_analysis(m, "yolo-serial", True, 0.9, {"boxes": []})
    store.write_analysis(n, "yolo-serial", False, 0.0, {"boxes": []})

    deleted = store.clear_analysis("yolo-serial", motion_only=True)
    assert deleted == 1  # only the motion frame's verdict
    unanalyzed = {fid for fid, _ in store.iter_unanalyzed("yolo-serial")}
    assert m in unanalyzed      # motion verdict cleared -> frame re-detects
    assert n not in unanalyzed  # non-motion verdict spared


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


# --- Runner: since_id/until_id scoping (frame-range groups) --------------------
#
# A group expands to an inclusive [since_id, until_id] id range that a Run/re-run
# scopes to (see docs/specs/2026-07-10-frame-range-groups.md). Both bounds are
# optional and independent; None on both sides is exactly today's whole-store
# sweep — the existing unscoped tests above pin that superset property.


@_requires_cv
def test_run_analysis_passes_since_id_to_prepare_before_the_scan(tmp_path):
    # prepare() must see the scope's floor BEFORE the sweep iterates, so a windowed
    # analyzer can warm-start off the frames just before it (see MogAnalyzer). No
    # frames are needed here — the point is purely that the anchor makes it to
    # prepare(), not what the (empty) sweep then does with it.
    store = _store(tmp_path)
    fake = FakeAnalyzer(name="anchor", windowed=False)
    manager = AnalysisManager()
    run_analysis(store, fake, manager, since_id=42, until_id=99)

    assert fake.prepared_with is store
    assert fake.prepared_since_id == 42  # the floor, not the ceiling


@_requires_cv
def test_run_analysis_stateless_scoped_by_since_and_until_id(tmp_path):
    # Eight frames, alternating bright/dark; scope to the middle four (ids[2..5]).
    # Only those four may receive a verdict, and the reported total must be the
    # SCOPED todo count, not the whole-store one — the point of threading
    # since_id/until_id through iter_unanalyzed/count_unanalyzed.
    store = _store(tmp_path)
    ids = []
    for i in range(8):
        level = 255 if i % 2 == 0 else 0
        ids.append(
            store.add(
                _frame(frame_id=i, ts=i, motion=False, body=_jpeg_gray(level)),
                recv_ts_ms=1_700_000_000_000 + i,
            )
        )

    since, until = ids[2], ids[5]
    # Captured BEFORE the sweep runs: nothing is verdicted yet, so this is the exact
    # scoped TODO count run_analysis must report as `total` — asserting it AFTER the
    # sweep would trivially read back 0 (everything in range now has a verdict).
    expected_total = store.count_unanalyzed("scoped", since_id=since, until_id=until)

    fake = FakeAnalyzer(name="scoped", windowed=False)
    manager = AnalysisManager()
    run_analysis(store, fake, manager, since_id=since, until_id=until)

    # total reflects the scoped stateless TODO count (4 frames, none done yet).
    assert manager.status()["total"] == expected_total == 4
    assert len(fake.seen) == 4  # exactly the in-window frames were decoded/analyzed

    # Only the windowed frames got a verdict: asking "how many unanalyzed within
    # just this one frame's id" is 0 for every in-window id, 1 (still untouched)
    # for every id outside it.
    for fid in ids[2:6]:
        assert store.count_unanalyzed("scoped", since_id=fid, until_id=fid) == 0
    for fid in ids[:2] + ids[6:]:
        assert store.count_unanalyzed("scoped", since_id=fid, until_id=fid) == 1


@_requires_cv
def test_run_analysis_windowed_scoped_by_since_and_until_id(tmp_path):
    # Ascending gray levels across 7 frames; scope to the middle window (ids[1..4]).
    # A windowed sweep must drive iter_time_order SCOPED to that window — the
    # fake's recorded levels must be exactly that sub-range, strictly ascending —
    # and the total must be store.count_in_range(since, until), not the
    # whole-store count.
    store = _store(tmp_path)
    levels = [30, 70, 110, 150, 190, 230, 250]
    ids = [
        store.add(_frame(frame_id=i, ts=i, body=_jpeg_gray(lv)), recv_ts_ms=1_700_000_000_000 + i)
        for i, lv in enumerate(levels)
    ]

    since, until = ids[1], ids[4]
    fake = FakeAnalyzer(name="scopedwin", windowed=True)
    manager = AnalysisManager()
    run_analysis(store, fake, manager, since_id=since, until_id=until)

    assert fake.seen == [70.0, 110.0, 150.0, 190.0]  # exactly the in-window levels, in time order
    assert manager.status()["total"] == store.count_in_range(since, until) == 4
    assert store.analysis_summary("scopedwin")["analyzed"] == 4  # not all 7 frames


@_requires_cv
def test_run_analysis_since_id_floor_without_until_id_covers_to_latest(tmp_path):
    # A floor with NO ceiling must sweep from since_id through the live horizon —
    # proving the two bounds are independent, not a package deal.
    store = _store(tmp_path)
    ids = [
        store.add(_frame(frame_id=i, ts=i, body=_jpeg_gray(255)), recv_ts_ms=1_700_000_000_000 + i)
        for i in range(5)
    ]

    fake = FakeAnalyzer(name="floor", windowed=False)
    manager = AnalysisManager()
    run_analysis(store, fake, manager, since_id=ids[2], until_id=None)

    assert manager.status()["total"] == 3  # ids[2], ids[3], ids[4]
    assert store.count_unanalyzed("floor") == 2  # ids[0], ids[1] left alone


@_requires_cv
def test_run_analysis_clamps_until_id_to_latest_id(tmp_path, monkeypatch):
    # A requested ceiling far beyond the live horizon (e.g. a stale UI request)
    # must be clamped to store.latest_id() at sweep start, so frames the collector
    # inserts after the sweep began stay out of scope. Simulated here by pinning
    # latest_id() to an EARLIER id than the store actually holds, mirroring "the
    # sweep started before frames 2/3 existed" without needing real concurrency.
    store = _store(tmp_path)
    ids = [
        store.add(_frame(frame_id=i, ts=i, body=_jpeg_gray(255)), recv_ts_ms=1_700_000_000_000 + i)
        for i in range(4)
    ]
    monkeypatch.setattr(store, "latest_id", lambda: ids[1])

    fake = FakeAnalyzer(name="clamp", windowed=False)
    manager = AnalysisManager()
    run_analysis(store, fake, manager, since_id=None, until_id=ids[3] + 100)

    # Clamped to the pinned latest_id() (ids[1]), NOT the requested ceiling: only
    # the first two frames got a verdict, the other two are untouched.
    assert manager.status()["total"] == 2
    assert store.count_unanalyzed("clamp") == 2


# --- AnalysisManager: lifecycle ------------------------------------------------


@_requires_cv
def test_manager_start_runs_to_completion(tmp_path):
    store = _store(tmp_path)
    # Two bright (present) + two dark (absent) frames.
    for i in range(4):
        store.add(_frame(frame_id=i, ts=i, body=_jpeg_gray(255 if i < 2 else 0)), recv_ts_ms=1_700_000_000_000 + i)

    fake = FakeAnalyzer(name="fake", windowed=False)
    manager = AnalysisManager(resolver=lambda name: fake)
    result = manager.enqueue_named(store, "fake")
    assert result["position"] == 0 and result["deduped"] is False

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
    manager.enqueue_named(store, "gated")

    assert gated.entered.wait(timeout=5), "sweep never reached the first analyze()"
    manager.cancel()        # set the stop flag while frame 1 is blocked
    gated.release.set()     # let that one frame finish; the loop must then break

    assert _wait(lambda: not manager.running), "sweep did not wind down after cancel"
    assert gated.calls == 1
    assert manager.status()["done"] == 1
    assert store.analysis_summary("gated")["analyzed"] == 1
    assert store.count_unanalyzed("gated") == 5  # the rest were never touched
    # The canceled job lands a "canceled" terminal state in the history.
    hist = manager.status()["history"]
    assert hist[0]["state"] == "canceled" and hist[0]["error"] is None
    assert hist[0]["done"] == 1


@_requires_cv
def test_manager_enqueues_second_job_while_running(tmp_path):
    # The queue replaces the old refuse-second-job behavior: while a gated sweep is
    # blocked mid-frame, a second enqueue no longer raises RuntimeError — it lands in
    # the pending FIFO and drains after the first. Two DISTINCT windows keep the jobs
    # from deduping. The gate only trips after a real decode reaches analyze(), so the
    # frames need real JPEGs (and cv2).
    store = _store(tmp_path)
    ids = [
        store.add(_frame(frame_id=i, ts=i, body=_jpeg_gray(200)), recv_ts_ms=1_700_000_000_000 + i)
        for i in range(3)
    ]

    gated = GatedAnalyzer(name="gated")
    manager = AnalysisManager(resolver=lambda name: gated)
    r1 = manager.enqueue_named(store, "gated", since_id=ids[0], until_id=ids[0])
    assert r1["position"] == 0 and r1["deduped"] is False
    try:
        assert gated.entered.wait(timeout=5), "first sweep never reached analyze()"
        # Second request enqueues behind the running job rather than raising.
        r2 = manager.enqueue_named(store, "gated", since_id=ids[1], until_id=ids[1])
        assert r2["position"] == 1 and r2["deduped"] is False
        assert r2["running"] is True  # the first job is still the active one
        assert len(manager.status()["queue"]) == 1
    finally:
        gated.release.set()
        _wait(lambda: not manager.running)
    # Both jobs drained serially, each verdicting its own one-frame window.
    assert store.analysis_summary("gated")["analyzed"] == 2
    hist = manager.status()["history"]
    assert [h["state"] for h in hist[:2]] == ["done", "done"]
    assert [h["since_id"] for h in hist[:2]] == [ids[1], ids[0]]  # most-recent first


def test_manager_enqueue_named_surfaces_importerror_from_resolver(tmp_path):
    # A backend whose optional deps are missing raises ImportError from the resolver;
    # because enqueue_named resolves SYNCHRONOUSLY before promoting any job, that error
    # reaches the caller (→ 503) instead of vanishing into the worker — and no job
    # starts, so the manager stays idle for the next attempt.
    store = _store(tmp_path)

    def bad_resolver(name):
        raise ImportError("install compute/requirements-analysis.txt")

    manager = AnalysisManager(resolver=bad_resolver)
    with pytest.raises(ImportError):
        manager.enqueue_named(store, "yolo")
    assert manager.running is False
    assert manager.status()["error"] is None  # nothing ran, so no recorded error
    assert manager.status()["queue"] == []    # and nothing was left pending


# --- AnalysisManager: the walk-away FIFO queue (enqueue / dedup / controls) ----
#
# The motion-detection-workflow spec turns the manager's refuse-second-job into an
# in-memory FIFO drained one at a time. These pin the queue mechanics deterministically
# with a gated fake that freezes the head job mid-frame, so the pending order, dedup,
# and the three controls (cancel / clear_pending / stop_all) can be asserted while a job
# is provably blocked — no sleep-and-hope. Real JPEGs (and cv2) are needed because the
# gate only trips once the worker's decode reaches analyze().


def _seed_real_frames(store, n: int, level: int = 200) -> "list[int]":
    """Add ``n`` real, decodable gray JPEGs and return their row ids in order."""
    return [
        store.add(_frame(frame_id=i, ts=i, body=_jpeg_gray(level)), recv_ts_ms=1_700_000_000_000 + i)
        for i in range(n)
    ]


@_requires_cv
def test_enqueue_fifo_order_and_serial_drain(tmp_path):
    # Head job A runs (position 0) and blocks; B and C enqueue behind it in FIFO order
    # (next-to-run first). Releasing the gate drains all three serially, and the history
    # records three "done" jobs most-recent-first.
    store = _store(tmp_path)
    ids = _seed_real_frames(store, 3)
    gated = GatedAnalyzer(name="yolo")
    manager = AnalysisManager(resolver=lambda name: gated)

    a = manager.enqueue_named(store, "yolo", since_id=ids[0], until_id=ids[0])
    assert a["position"] == 0
    assert gated.entered.wait(timeout=5)
    b = manager.enqueue_named(store, "yolo", since_id=ids[1], until_id=ids[1])
    c = manager.enqueue_named(store, "yolo", since_id=ids[2], until_id=ids[2])
    assert b["position"] == 1 and c["position"] == 2
    assert b["deduped"] is False and c["deduped"] is False

    queue = manager.status()["queue"]
    assert [(j["since_id"], j["until_id"]) for j in queue] == [(ids[1], ids[1]), (ids[2], ids[2])]

    gated.release.set()
    assert _wait(lambda: not manager.running)
    assert store.analysis_summary("yolo")["analyzed"] == 3
    hist = manager.status()["history"]
    assert [h["state"] for h in hist[:3]] == ["done", "done", "done"]
    assert [h["since_id"] for h in hist[:3]] == [ids[2], ids[1], ids[0]]


@_requires_cv
def test_enqueue_dedup_drops_identical_running_and_pending(tmp_path):
    # An identical (kind, params, reanalyze, since_id, until_id) key already running or
    # pending is DROPPED (deduped=True) with the existing job's position — guarding a
    # double-click. A distinct window is a real new job.
    store = _store(tmp_path)
    ids = _seed_real_frames(store, 3)
    gated = GatedAnalyzer(name="yolo")
    manager = AnalysisManager(resolver=lambda name: gated)

    manager.enqueue_named(store, "yolo", since_id=ids[0], until_id=ids[0])  # A: running
    assert gated.entered.wait(timeout=5)

    # Identical to the RUNNING job → deduped onto it at position 0.
    dup_a = manager.enqueue_named(store, "yolo", since_id=ids[0], until_id=ids[0])
    assert dup_a["deduped"] is True and dup_a["position"] == 0

    # A distinct window is a genuine new job at position 1.
    b = manager.enqueue_named(store, "yolo", since_id=ids[1], until_id=ids[1])
    assert b["deduped"] is False and b["position"] == 1

    # Identical to the PENDING job → deduped onto it at its position (1), not appended.
    dup_b = manager.enqueue_named(store, "yolo", since_id=ids[1], until_id=ids[1])
    assert dup_b["deduped"] is True and dup_b["position"] == 1
    assert len(manager.status()["queue"]) == 1  # still just B pending, no duplicate

    gated.release.set()
    _wait(lambda: not manager.running)


@_requires_cv
def test_enqueue_reanalyze_flag_is_part_of_the_dedup_key(tmp_path):
    # reanalyze is IN the key: a plain sweep and a reanalyze sweep of the same
    # oracle+window are DIFFERENT jobs — otherwise the re-verdict would dedup away
    # against the earlier run and silently never happen.
    store = _store(tmp_path)
    ids = _seed_real_frames(store, 2)
    gated = GatedAnalyzer(name="yolo")
    manager = AnalysisManager(resolver=lambda name: gated)

    manager.enqueue_named(store, "yolo", since_id=ids[0], until_id=ids[0])  # running, reanalyze=False
    assert gated.entered.wait(timeout=5)
    r = manager.enqueue_named(store, "yolo", since_id=ids[0], until_id=ids[0], reanalyze=True)
    assert r["deduped"] is False and r["position"] == 1  # reanalyze differs → distinct job

    gated.release.set()
    _wait(lambda: not manager.running)


@_requires_cv
def test_enqueue_analyzer_dedups_on_params_not_just_window(tmp_path):
    # For a pre-built MOG2 slot the params ARE part of the dedup key: re-queuing the same
    # slot+window with DIFFERENT params is the tune loop (NOT a duplicate), while an
    # identical-params re-enqueue is dropped. GatedAnalyzer instances carry a fake
    # ``_params`` tuple, which is exactly what enqueue_analyzer reads for the key.
    store = _store(tmp_path)
    ids = _seed_real_frames(store, 2)
    a1 = GatedAnalyzer(name="mog2:candidate")
    a1._params = (16.0, 0.001, 0.01, 0.6, 2, 320)
    a2 = GatedAnalyzer(name="mog2:candidate")
    a2._params = (20.0, 0.002, 0.02, 0.5, 3, 256)  # same slot+window, DIFFERENT params
    a1b = GatedAnalyzer(name="mog2:candidate")
    a1b._params = (16.0, 0.001, 0.01, 0.6, 2, 320)  # identical params to a1

    manager = AnalysisManager()
    manager.enqueue_analyzer(store, a1, since_id=ids[0], until_id=ids[0])  # running
    assert a1.entered.wait(timeout=5)

    diff = manager.enqueue_analyzer(store, a2, since_id=ids[0], until_id=ids[0])
    assert diff["deduped"] is False and diff["position"] == 1  # different params → new job

    same = manager.enqueue_analyzer(store, a1b, since_id=ids[0], until_id=ids[0])
    assert same["deduped"] is True and same["position"] == 0  # identical to the running job

    a1.release.set()
    a2.release.set()
    _wait(lambda: not manager.running)


@_requires_cv
def test_clear_pending_drops_queue_and_leaves_running(tmp_path):
    # clear_pending empties the pending deque but never touches the running job, which
    # completes normally and (finding nothing pending) promotes nothing.
    store = _store(tmp_path)
    ids = _seed_real_frames(store, 3)
    gated = GatedAnalyzer(name="yolo")
    manager = AnalysisManager(resolver=lambda name: gated)

    manager.enqueue_named(store, "yolo", since_id=ids[0], until_id=ids[0])  # running
    assert gated.entered.wait(timeout=5)
    manager.enqueue_named(store, "yolo", since_id=ids[1], until_id=ids[1])
    manager.enqueue_named(store, "yolo", since_id=ids[2], until_id=ids[2])
    assert len(manager.status()["queue"]) == 2

    manager.clear_pending()
    assert manager.status()["queue"] == []
    assert manager.running is True  # the active job is untouched

    gated.release.set()
    assert _wait(lambda: not manager.running)
    # Only the running job verdicted; the cleared pending jobs never ran (nor were
    # recorded in history).
    assert store.analysis_summary("yolo")["analyzed"] == 1
    hist = manager.status()["history"]
    assert len(hist) == 1 and hist[0]["state"] == "done"


@_requires_cv
def test_stop_all_clears_pending_and_cancels_running(tmp_path):
    # stop_all clears pending AND cancels the running job atomically — the running job
    # winds down (canceled) and nothing pending is promoted.
    store = _store(tmp_path)
    ids = _seed_real_frames(store, 4)
    gated = GatedAnalyzer(name="yolo")
    manager = AnalysisManager(resolver=lambda name: gated)

    manager.enqueue_named(store, "yolo", since_id=ids[0], until_id=ids[3])  # running, 4 frames
    assert gated.entered.wait(timeout=5)
    manager.enqueue_named(store, "yolo", since_id=ids[1], until_id=ids[1])  # pending
    assert len(manager.status()["queue"]) == 1

    manager.stop_all()
    assert manager.status()["queue"] == []

    gated.release.set()  # release the in-flight frame; cancel stops at the next boundary
    assert _wait(lambda: not manager.running)
    assert store.analysis_summary("yolo")["analyzed"] == 1  # one verdict, then canceled
    hist = manager.status()["history"]
    assert len(hist) == 1 and hist[0]["state"] == "canceled"


@_requires_cv
def test_history_records_failed_state_on_fatal_error(tmp_path):
    # A fatal error inside the sweep (here: prepare raises) lands "failed" + the message
    # in both status().error and the history record, so a returning operator can tell a
    # silent partial failure from a clean drain.
    store = _store(tmp_path)
    store.add(_frame(frame_id=1, body=_jpeg_gray(0)), recv_ts_ms=1_700_000_000_000)

    class _Boom(FakeAnalyzer):
        def prepare(self, store, since_id: "int | None" = None) -> None:
            raise RuntimeError("prepare boom")

    manager = AnalysisManager(resolver=lambda name: _Boom(name="yolo"))
    manager.enqueue_named(store, "yolo")
    assert _wait(lambda: not manager.running)
    st = manager.status()
    assert st["error"] == "prepare boom"
    assert st["history"][0]["state"] == "failed"
    assert st["history"][0]["error"] == "prepare boom"
