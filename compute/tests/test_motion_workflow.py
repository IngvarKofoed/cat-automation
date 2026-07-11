"""Tests for the motion-detection-workflow store + collector layer
(compute/collection/store.py, compute/collection/collector.py).

The motion-detection-workflow spec layers a batch of new, cv2-free machinery onto
the store and the collector:

- a ``settings`` KV (``get_setting`` / ``set_setting``) that survives ``clear``;
- an append-only ``mode_changes`` log the collector stamps on every motion-only
  flip, from which ``motion_only_spans`` reconstructs the ON sub-ranges of any id
  window (so a bucket overlapping a motion-only stretch is flagged "misses
  unmeasurable" rather than read as perfect recall);
- clock→id resolution (``resolve_ts_range``) and evenly-spaced decimation
  (``sample_frames``) for the windowed frame viewer;
- the density-timeline (``timeline_bins``) and the worst-first visit inbox
  (``visits``) read endpoints, both judged against the LIVE ``frames.motion`` gate;
- ``recent_before_rows`` (the row-shaped filmstrip sibling of ``recent_before``);
- the collector's motion-only drop of non-motion frames, and ``CollectorManager``'s
  runtime ``set_motion_only`` / launch-time ``restore_motion_only``.

None of these decode a JPEG, so — exactly like ``test_collection.py`` /
``test_scorecard.py`` — every test builds ``StreamFrame`` objects directly from a
minimal fake body and drives oracle verdicts with ``write_analysis``. No cv2, no
model, no network. See docs/specs/2026-07-11-motion-detection-workflow.md.
"""
from __future__ import annotations

import threading

import pytest

from compute.collection.collector import CollectorManager, run_collector
from compute.collection.store import Store
from compute.ingest import StreamFrame
from shared.wire import StreamFrameMeta

# A minimal valid JPEG — the store writes it verbatim and never decodes it, so its
# realism is irrelevant here (only the store/collector logic is under test).
_JPEG_BODY = b"\xff\xd8\xff\xe0" + b"fake-jpeg-body" + b"\xff\xd9"


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


def _store(tmp_path) -> Store:
    return Store(
        db_path=str(tmp_path / "index.db"),
        media_root=str(tmp_path / "media"),
        max_bytes=10_000_000,
    )


class _ListClient:
    """A stand-in edge whose stream is a fixed list of frames, then ends.

    ``run_collector`` consumes ``iter_stream_reconnecting()``; a finite list lets the
    loop drain and return without a background thread, so the collector's per-frame
    motion-only decision can be asserted against what actually landed in the store.
    """

    def __init__(self, frames: "list[StreamFrame]") -> None:
        self._frames = frames

    def iter_stream_reconnecting(self):
        return iter(self._frames)


# --- Store: settings KV -------------------------------------------------------


def test_get_set_setting_roundtrip_and_replace(tmp_path):
    store = _store(tmp_path)
    assert store.get_setting("motion_only") is None  # unset → None
    store.set_setting("motion_only", "1")
    assert store.get_setting("motion_only") == "1"
    store.set_setting("motion_only", "0")  # INSERT OR REPLACE overwrites in place
    assert store.get_setting("motion_only") == "0"
    # Independent keys don't collide.
    store.set_setting("collector_running", "1")
    assert store.get_setting("collector_running") == "1"
    assert store.get_setting("motion_only") == "0"


def test_settings_survive_clear(tmp_path):
    # settings is CONFIG, not frame data — clear() wipes frames/analysis/groups/
    # mode_changes but must leave settings intact.
    store = _store(tmp_path)
    store.set_setting("collector_running", "1")
    store.set_setting("motion_only", "1")
    store.add(_frame(frame_id=1), recv_ts_ms=1_700_000_000_000)

    store.clear()
    assert store.get_setting("collector_running") == "1"
    assert store.get_setting("motion_only") == "1"


# --- Store: record_mode_change + motion_only_spans ----------------------------


def test_motion_only_spans_empty_when_no_changes(tmp_path):
    store = _store(tmp_path)
    assert store.motion_only_spans() == []
    # A store recording only full-capture (OFF) has no ON spans either.
    store.record_mode_change(False)
    store.add(_frame(frame_id=1), recv_ts_ms=1_700_000_000_000)
    assert store.motion_only_spans() == []


def test_motion_only_spans_reconstructs_on_subranges(tmp_path):
    store = _store(tmp_path)
    store.record_mode_change(False)  # full capture from the start (at_id 0)
    for i in range(5):  # ids 1..5
        store.add(_frame(frame_id=i), recv_ts_ms=1_700_000_000_000 + i)
    store.record_mode_change(True)  # flip ON at id 5
    for i in range(5):  # ids 6..10
        store.add(_frame(frame_id=100 + i), recv_ts_ms=1_700_000_000_100 + i)
    store.record_mode_change(False)  # flip OFF at id 10
    for i in range(5):  # ids 11..15
        store.add(_frame(frame_id=200 + i), recv_ts_ms=1_700_000_000_200 + i)

    # The single ON segment runs from the ON flip (id 5) to the next flip (id 10).
    assert store.motion_only_spans() == [{"start_id": 5, "end_id": 10}]


def test_motion_only_spans_clipped_to_window(tmp_path):
    store = _store(tmp_path)
    store.record_mode_change(False)
    for i in range(5):  # ids 1..5
        store.add(_frame(frame_id=i), recv_ts_ms=1_700_000_000_000 + i)
    store.record_mode_change(True)  # ON at id 5
    for i in range(5):  # ids 6..10
        store.add(_frame(frame_id=100 + i), recv_ts_ms=1_700_000_000_100 + i)
    store.record_mode_change(False)  # OFF at id 10
    for i in range(5):  # ids 11..15
        store.add(_frame(frame_id=200 + i), recv_ts_ms=1_700_000_000_200 + i)

    # A window straddling the ON span end clips to the overlap.
    assert store.motion_only_spans(since_id=7, until_id=12) == [{"start_id": 7, "end_id": 10}]
    # A window strictly inside the ON span returns the clipped window itself.
    assert store.motion_only_spans(since_id=6, until_id=9) == [{"start_id": 6, "end_id": 9}]
    # A window wholly in full-capture territory returns nothing.
    assert store.motion_only_spans(since_id=11, until_id=15) == []


def test_motion_only_spans_coalesces_redundant_flips(tmp_path):
    # A redundant flip (same state as the running one) leaves the step function
    # unchanged — two consecutive ON rows read as one continuous ON span.
    store = _store(tmp_path)
    store.record_mode_change(True)  # ON at id 0
    for i in range(3):  # ids 1..3
        store.add(_frame(frame_id=i), recv_ts_ms=1_700_000_000_000 + i)
    store.record_mode_change(True)  # redundant ON (coalesced away)
    for i in range(3):  # ids 4..6
        store.add(_frame(frame_id=10 + i), recv_ts_ms=1_700_000_000_100 + i)

    # One ON span, from the first ON flip to the store end (id 6, no successor).
    assert store.motion_only_spans() == [{"start_id": 0, "end_id": 6}]


def test_motion_only_last_on_span_runs_to_store_end_or_until(tmp_path):
    store = _store(tmp_path)
    store.record_mode_change(False)
    for i in range(3):  # ids 1..3
        store.add(_frame(frame_id=i), recv_ts_ms=1_700_000_000_000 + i)
    store.record_mode_change(True)  # ON at id 3, never turned off
    for i in range(3):  # ids 4..6
        store.add(_frame(frame_id=10 + i), recv_ts_ms=1_700_000_000_100 + i)

    # until_id None → the trailing ON span runs to the store end (MAX id = 6).
    assert store.motion_only_spans() == [{"start_id": 3, "end_id": 6}]
    # until_id given → the trailing ON span is capped at it.
    assert store.motion_only_spans(until_id=5) == [{"start_id": 3, "end_id": 5}]


def test_clear_drops_mode_changes(tmp_path):
    # mode_changes is keyed to frame ids (rowid reuse hazard), so clear() drops it —
    # and does NOT re-seed (that is the API route's job when collection is live).
    store = _store(tmp_path)
    store.record_mode_change(True)
    store.add(_frame(frame_id=1), recv_ts_ms=1_700_000_000_000)
    assert store.motion_only_spans() == [{"start_id": 0, "end_id": 1}]

    store.clear()
    assert store.motion_only_spans() == []  # log wiped, not re-seeded


# --- Store: resolve_ts_range (clock → id) -------------------------------------


def test_resolve_ts_range_nearest_at_or_after_and_before(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    ids = [store.add(_frame(frame_id=i), recv_ts_ms=base + i * 100) for i in range(5)]  # recv_ts base+0..400

    # Exact bounds resolve to the exact endpoints.
    assert store.resolve_ts_range(base, base + 400) == (ids[0], ids[4])
    # start at-or-after: base+50 → nearest at-or-after is base+100 (ids[1]).
    assert store.resolve_ts_range(base + 50, None) == (ids[1], None)
    # end at-or-before: base+250 → nearest at-or-before is base+200 (ids[2]).
    assert store.resolve_ts_range(None, base + 250) == (None, ids[2])
    # None bounds stay None (unbounded on that side).
    assert store.resolve_ts_range(None, None) == (None, None)
    # A start past the newest frame matches nothing → None on that side.
    assert store.resolve_ts_range(base + 10_000, None) == (None, None)
    # An end before the oldest frame matches nothing → None on that side.
    assert store.resolve_ts_range(None, base - 1) == (None, None)


# --- Store: sample_frames (even decimation) -----------------------------------


def test_sample_frames_even_spacing_includes_first(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    ids = [store.add(_frame(frame_id=i), recv_ts_ms=base + i) for i in range(10)]  # ids 1..10

    # count=4 over 10 matched → stride ceil(10/4)=3 → rn 1,4,7,10 → ids[0,3,6,9].
    sampled = store.sample_frames(None, None, 4)
    assert [f["id"] for f in sampled] == [ids[0], ids[3], ids[6], ids[9]]
    assert sampled[0]["id"] == ids[0]  # the FIRST frame is always included
    assert set(sampled[0].keys()) == {"id", "recv_ts", "url"}
    assert sampled[0]["url"] == f"/media/{ids[0]}"


def test_sample_frames_clamps_scopes_and_empty(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    ids = [store.add(_frame(frame_id=i), recv_ts_ms=base + i) for i in range(5)]

    # count larger than matched → stride 1 → every frame in range.
    assert [f["id"] for f in store.sample_frames(None, None, 100)] == ids
    # Scoped to a sub-window.
    assert [f["id"] for f in store.sample_frames(ids[1], ids[3], 100)] == ids[1:4]
    # count <= 0 clamps to 1 → stride = matched → only the first frame.
    assert [f["id"] for f in store.sample_frames(None, None, 0)] == [ids[0]]
    # A window matching no frame is empty.
    assert store.sample_frames(ids[4] + 1, None, 10) == []


def test_sample_frames_by_interval_is_a_time_rate(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    # Three frames 1 s apart, a gap, then three more — so time-bucketing (not index
    # decimation) is what is exercised, and an empty bucket must yield no frame.
    tss = [0, 1000, 2000, 10000, 11000, 12000]
    ids = [store.add(_frame(frame_id=i), recv_ts_ms=base + t) for i, t in enumerate(tss)]

    # interval 3000 ms → buckets (ts - base)//3000 = 0,0,0,3,3,4. One frame per NON-empty
    # bucket, the earliest: ids[0] (bucket 0), ids[3] (bucket 3), ids[5] (bucket 4).
    # Buckets 1 and 2 are empty (the gap) and correctly contribute nothing — the property
    # index decimation lacked.
    got = store.sample_frames_by_interval(None, None, 3000)
    assert [f["id"] for f in got] == [ids[0], ids[3], ids[5]]
    assert set(got[0].keys()) == {"id", "recv_ts", "url"}
    assert got[0]["url"] == f"/media/{ids[0]}"

    # Buckets are measured from the WINDOW's first recv_ts: scoped to the last three
    # (span 2 s < interval) they collapse to a single bucket → just its earliest frame.
    assert [f["id"] for f in store.sample_frames_by_interval(ids[3], None, 3000)] == [ids[3]]
    # A window matching no frame is empty.
    assert store.sample_frames_by_interval(ids[5] + 1, None, 3000) == []


def test_analysis_coverage_scopes_to_window(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    ids = [store.add(_frame(frame_id=i), recv_ts_ms=base + i) for i in range(6)]  # ids 1..6
    store.write_analysis(ids[1], "yolo", True, 0.9, None)   # present
    store.write_analysis(ids[2], "yolo", False, 0.1, None)  # analyzed, absent
    store.write_analysis(ids[4], "yolo", True, 0.8, None)   # present

    # Whole store: 3 of 6 analyzed, 2 present.
    assert store.analysis_coverage("yolo") == {"total": 6, "analyzed": 3, "present": 2}
    # Scoped to ids[1..3]: 3 frames, ids[1]+ids[2] analyzed (1 present), ids[3] unanalyzed.
    assert store.analysis_coverage("yolo", ids[1], ids[3]) == {"total": 3, "analyzed": 2, "present": 1}
    # An oracle with no verdicts in the window → zeroes, but total still counts the frames.
    assert store.analysis_coverage("bsuv", ids[0], ids[2]) == {"total": 3, "analyzed": 0, "present": 0}


# --- Store: timeline_bins -----------------------------------------------------


def test_timeline_bins_counts_disagreements_per_bin(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    # Bin 0 (recv_ts 0..49): a miss + a caught present frame.
    f1 = store.add(_frame(frame_id=1, motion=False), recv_ts_ms=base + 0)
    f2 = store.add(_frame(frame_id=2, motion=True, bbox=(0, 0, 0.1, 0.1), area=0.05), recv_ts_ms=base + 10)
    # Bin 1 (recv_ts 50..100): a false trigger + an unanalyzed still frame.
    f3 = store.add(_frame(frame_id=3, motion=True, bbox=(0, 0, 0.1, 0.1), area=0.05), recv_ts_ms=base + 90)
    store.add(_frame(frame_id=4, motion=False), recv_ts_ms=base + 100)
    store.write_analysis(f1, "yolo", True, 0.9, None)   # motion 0, present → miss
    store.write_analysis(f2, "yolo", True, 0.8, None)   # motion 1, present → caught (agree)
    store.write_analysis(f3, "yolo", False, 0.1, None)  # motion 1, absent → false trigger
    # f4 left unanalyzed → contributes to total/motion only

    bins = store.timeline_bins(None, None, "yolo", 2)
    assert len(bins) == 2
    b0, b1 = bins
    assert (b0["t0"], b0["t1"]) == (base + 0, base + 50)
    assert b0["total"] == 2 and b0["motion"] == 1
    assert b0["present"] == 2 and b0["missed"] == 1 and b0["false"] == 0
    assert (b1["t0"], b1["t1"]) == (base + 50, base + 100)
    assert b1["total"] == 2 and b1["motion"] == 1
    assert b1["present"] == 0 and b1["missed"] == 0 and b1["false"] == 1


def test_timeline_bins_empty_window_is_empty(tmp_path):
    store = _store(tmp_path)
    assert store.timeline_bins(None, None, "yolo", 10) == []


def test_timeline_bins_single_timestamp_collapses_to_one_bin(tmp_path):
    # Every frame shares one recv_ts (span 0): they all land in a single bin whose
    # t0 == t1, rather than dividing by zero.
    store = _store(tmp_path)
    base = 1_700_000_000_000
    for i in range(3):
        fid = store.add(_frame(frame_id=i, motion=False), recv_ts_ms=base)
        store.write_analysis(fid, "yolo", True, 0.9, None)
    bins = store.timeline_bins(None, None, "yolo", 5)
    assert len(bins) == 1
    assert bins[0]["t0"] == base and bins[0]["t1"] == base
    assert bins[0]["total"] == 3 and bins[0]["missed"] == 3


# --- Store: visits (the worst-first inbox) ------------------------------------


def test_visits_rejects_bad_mode(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        store.visits(None, None, "yolo", "bogus")


def test_visits_missed_mode_worst_first(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    # Visit M1 (wholly missed, 2 present frames close in time, no motion nearby).
    m1a = store.add(_frame(frame_id=1, motion=False), recv_ts_ms=base + 10_000)
    m1b = store.add(_frame(frame_id=2, motion=False), recv_ts_ms=base + 11_000)
    # Visit M2 (caught: one present frame with a live-motion frame within ±window).
    m2 = store.add(_frame(frame_id=3, motion=False), recv_ts_ms=base + 30_000)
    catcher = store.add(
        _frame(frame_id=4, motion=True, bbox=(0, 0, 0.1, 0.1), area=0.05), recv_ts_ms=base + 31_000
    )
    # Visit M3 (wholly missed, 1 present frame).
    m3 = store.add(_frame(frame_id=5, motion=False), recv_ts_ms=base + 50_000)
    store.write_analysis(m1a, "yolo", True, 0.9, None)
    store.write_analysis(m1b, "yolo", True, 0.7, None)
    store.write_analysis(m2, "yolo", True, 0.95, None)
    store.write_analysis(catcher, "yolo", False, 0.1, None)  # motion, oracle-absent (not a missed frame)
    store.write_analysis(m3, "yolo", True, 0.6, None)

    visits = store.visits(None, None, "yolo", "missed")
    # Worst-first: wholly-missed before caught; then n_frames desc; then peak score desc.
    # M1 (missed, 2 frames) → M3 (missed, 1 frame) → M2 (caught).
    assert [v["start_id"] for v in visits] == [m1a, m3, m2]
    assert visits[0]["caught"] is False and visits[0]["n_frames"] == 2
    assert visits[0]["start_id"] == m1a and visits[0]["end_id"] == m1b
    assert visits[0]["rep_frame_id"] == m1a  # highest oracle score (0.9) in the cluster
    assert visits[0]["present_count"] == 2
    assert visits[1]["caught"] is False  # M3
    assert visits[2]["caught"] is True   # M2 sorts last (caught)


def test_visits_false_mode_worst_first_by_length_then_area(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    # Visit FA (2 false frames close in time): motion=1, oracle-absent.
    fa1 = store.add(_frame(frame_id=1, motion=True, bbox=(0, 0, 0.1, 0.1), area=0.3), recv_ts_ms=base + 1_000)
    fa2 = store.add(_frame(frame_id=2, motion=True, bbox=(0, 0, 0.1, 0.1), area=0.5), recv_ts_ms=base + 2_000)
    # Visit FB (1 false frame), higher area but shorter.
    fb = store.add(_frame(frame_id=3, motion=True, bbox=(0, 0, 0.1, 0.1), area=0.8), recv_ts_ms=base + 10_000)
    for fid in (fa1, fa2, fb):
        store.write_analysis(fid, "yolo", False, 0.05, None)

    visits = store.visits(None, None, "yolo", "false")
    # Sort: n_frames desc, then peak area desc → the 2-frame FA before the 1-frame FB.
    assert [v["n_frames"] for v in visits] == [2, 1]
    assert visits[0]["start_id"] == fa1 and visits[0]["end_id"] == fa2
    assert visits[0]["rep_frame_id"] == fa2  # highest area (0.5) in FA
    assert visits[1]["rep_frame_id"] == fb


def test_visits_conflict_mode_compares_two_oracles(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    # Visit CA (2 frames where yolo and bsuv disagree).
    c1 = store.add(_frame(frame_id=1, motion=False), recv_ts_ms=base + 1_000)
    c2 = store.add(_frame(frame_id=2, motion=False), recv_ts_ms=base + 2_000)
    # Visit CB (1 disagreeing frame), smaller score gap.
    c3 = store.add(_frame(frame_id=3, motion=False), recv_ts_ms=base + 10_000)
    # An AGREEING frame (both oracles present) — must be excluded from the conflict set.
    c_agree = store.add(_frame(frame_id=4, motion=False), recv_ts_ms=base + 20_000)

    store.write_analysis(c1, "yolo", True, 0.9, None)
    store.write_analysis(c1, "bsuv", False, 0.2, None)   # gap 0.7, yolo present
    store.write_analysis(c2, "yolo", False, 0.3, None)
    store.write_analysis(c2, "bsuv", True, 0.6, None)    # gap 0.3, bsuv present
    store.write_analysis(c3, "yolo", True, 0.5, None)
    store.write_analysis(c3, "bsuv", False, 0.1, None)   # gap 0.4
    store.write_analysis(c_agree, "yolo", True, 0.9, None)
    store.write_analysis(c_agree, "bsuv", True, 0.9, None)  # agree → not a conflict

    visits = store.visits(None, None, "yolo", "conflict")  # oracle arg is ignored
    # Sort: n_frames desc, then peak score-gap desc → CA (2 frames) before CB (1 frame).
    assert [v["n_frames"] for v in visits] == [2, 1]
    assert visits[0]["start_id"] == c1 and visits[0]["end_id"] == c2
    assert visits[0]["rep_frame_id"] == c1  # its present oracle (yolo) is most confident (0.9)
    assert visits[1]["rep_frame_id"] == c3
    # The agreeing frame appears in NO conflict visit.
    assert all(c_agree not in (v["start_id"], v["end_id"]) for v in visits)


def test_visits_scoped_by_since_and_until_id(tmp_path):
    # The id window narrows the clustered set — the out-of-window missed frames drop out.
    store = _store(tmp_path)
    base = 1_700_000_000_000
    ids = []
    for i in range(5):
        fid = store.add(_frame(frame_id=i, motion=False), recv_ts_ms=base + i * 10_000)
        store.write_analysis(fid, "yolo", True, 0.9, None)  # every frame is a wholly-missed visit
        ids.append(fid)

    scoped = store.visits(ids[1], ids[3], "yolo", "missed")
    assert {v["start_id"] for v in scoped} == set(ids[1:4])  # only the in-window visits
    assert len(scoped) == 3


# --- Store: recent_before_rows ------------------------------------------------


def test_recent_before_rows_is_chronological_with_urls(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    ids = [store.add(_frame(frame_id=i), recv_ts_ms=base + i) for i in range(6)]

    rows = store.recent_before_rows(ids[4], n=2)  # the two frames just before id ids[4]
    assert [r["id"] for r in rows] == [ids[2], ids[3]]  # chronological (id ASC), not reversed
    assert rows[0]["url"] == f"/media/{ids[2]}"
    assert set(rows[0].keys()) == {"id", "recv_ts", "url"}

    # Fewer preceding frames than requested → just what exists, still chronological.
    assert [r["id"] for r in store.recent_before_rows(ids[1], n=10)] == [ids[0]]
    assert store.recent_before_rows(ids[0], n=5) == []  # nothing before the first


# --- Collector: motion-only drop ----------------------------------------------


def test_run_collector_records_initial_mode_before_first_frame(tmp_path):
    # run_collector stamps the starting mode once, before the loop — so an empty stream
    # still opens a defined span (a run started motion-only opens an ON span at id 0).
    store = _store(tmp_path)
    run_collector(_ListClient([]), store, threading.Event(), motion_only=lambda: True)
    assert store.motion_only_spans() == [{"start_id": 0, "end_id": 0}]


def test_run_collector_drops_non_motion_frames_when_motion_only(tmp_path):
    store = _store(tmp_path)
    frames = [
        _frame(frame_id=1, motion=True, bbox=(0, 0, 0.1, 0.1), area=0.05),
        _frame(frame_id=2, motion=False),
        _frame(frame_id=3, motion=True, bbox=(0, 0, 0.1, 0.1), area=0.05),
        _frame(frame_id=4, motion=False),
    ]
    run_collector(_ListClient(frames), store, threading.Event(), motion_only=lambda: True)

    rows, _ = store.query(cursor=None, limit=100, motion="all", order="time")
    assert len(rows) == 2  # only the two motion frames were stored
    assert all(r["motion"] is True for r in rows)


def test_run_collector_stores_every_frame_in_full_capture(tmp_path):
    # The default always-False getter is the old always-store-everything behavior.
    store = _store(tmp_path)
    frames = [
        _frame(frame_id=1, motion=True, bbox=(0, 0, 0.1, 0.1), area=0.05),
        _frame(frame_id=2, motion=False),
        _frame(frame_id=3, motion=False),
    ]
    run_collector(_ListClient(frames), store, threading.Event())  # motion_only default: False
    assert store.stats()["count"] == 3


def test_run_collector_live_toggle_takes_effect_mid_run(tmp_path):
    # motion_only is read fresh per frame from a mutable holder, so flipping it partway
    # through the stream drops only the non-motion frames seen AFTER the flip.
    store = _store(tmp_path)
    flag = {"on": False}
    frames = [
        _frame(frame_id=1, motion=False),  # stored (full capture)
        _frame(frame_id=2, motion=False),  # dropped once the flag flips below
    ]

    class _Flipping(_ListClient):
        def iter_stream_reconnecting(self):
            for i, fr in enumerate(self._frames):
                if i == 1:
                    flag["on"] = True  # flip to motion-only before the 2nd frame is judged
                yield fr

    run_collector(_Flipping(frames), store, threading.Event(), motion_only=lambda: flag["on"])
    rows, _ = store.query(cursor=None, limit=100, motion="all", order="time")
    assert [r["frame_id"] for r in rows] == [1]  # only the pre-flip full-capture frame


# --- CollectorManager: motion-only intent -------------------------------------


def test_collector_manager_set_motion_only_records_and_persists(tmp_path):
    store = _store(tmp_path)
    mgr = CollectorManager(client=None, store=store)
    assert mgr.current_motion_only is False

    mgr.set_motion_only(True)
    assert mgr.current_motion_only is True
    assert store.get_setting("motion_only") == "1"  # always persisted
    # A real flip recorded a mode-change boundary → one ON span at the store start.
    assert store.motion_only_spans() == [{"start_id": 0, "end_id": 0}]

    # A no-op flip (same value) does NOT append a redundant mode row, but still persists.
    mgr.set_motion_only(True)
    assert store.motion_only_spans() == [{"start_id": 0, "end_id": 0}]
    assert store.get_setting("motion_only") == "1"

    mgr.set_motion_only(False)
    assert mgr.current_motion_only is False
    assert store.get_setting("motion_only") == "0"


def test_restore_motion_only_sets_flag_without_writing_store(tmp_path):
    # restore_motion_only seeds the in-memory flag at launch but must NEVER write the
    # store (changelog 28: a bare launch never silently persists).
    store = _store(tmp_path)
    mgr = CollectorManager(client=None, store=store)
    mgr.restore_motion_only(True)
    assert mgr.current_motion_only is True
    assert store.get_setting("motion_only") is None  # no setting written
    assert store.motion_only_spans() == []           # no mode-change row written
