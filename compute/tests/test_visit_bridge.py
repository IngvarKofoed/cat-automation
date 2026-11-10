"""The household visit bridges a cat sitting still (docs/specs/2026-11-10-one-visit-for-feed-and-alarm.md).

Under motion-only storage a still cat stores nothing, so the feed's 2 s split cut one sit
into several cards. ``Store._bridges`` is the one rule that joins two consecutive motion
clusters; ``events()`` (the feed) and ``visit_start`` (the alarm's read) both go through it.
Pinned here: each of the rule's five conditions at its edge, the saved start date that keeps
the past as it was, the feed's bridged record and its place before ``min_frames`` and the
page cap, the scan cap and paging, ``visit_start``'s walk and floor, the two agreeing on
every frame, and both new reads' query plans on a store with no ANALYZE stats.
"""
from __future__ import annotations

import json
import os
import random

import pytest

from compute.collection import store as store_mod
from compute.collection.store import (
    _BRIDGE_MAX_GAP_MS,
    _VISIT_GAP_MS,
    VISIT_BRIDGE_SINCE_KEY,
    Store,
)
from compute.ingest import StreamFrame
from shared.wire import StreamFrameMeta

_JPEG = b"\xff\xd8\xff\xe0" + b"fake-jpeg-body" + b"\xff\xd9"
BASE = 1_700_000_000_000
BOX = [10, 10, 50, 50]


def _store(tmp_path, since=BASE - 1) -> Store:
    s = Store(db_path=str(tmp_path / "index.db"), media_root=str(tmp_path / "media"), max_bytes=10**9)
    if since is not None:
        s.set_setting(VISIT_BRIDGE_SINCE_KEY, str(since))
    return s


class Door:
    """Frames into a real store, each with an optional set of stored yolo-serial boxes."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self.seq = 0

    def frames(self, start, n, *, step=100, motion=True, boxes=(BOX + [0.8, 15],), area=0.05):
        """``n`` frames ``step`` ms apart from ``start``, each carrying ``boxes`` (None = no row)."""
        ids = []
        rows = []
        for k in range(n):
            self.seq += 1
            ts = start + k * step
            meta = StreamFrameMeta(frame_id=self.seq, ts=ts, motion=motion, bbox=None, area=area)
            fid = self.store.add(StreamFrame(meta, _JPEG), recv_ts_ms=ts)
            ids.append(fid)
            if boxes is not None:
                cat = [b for b in boxes if len(b) < 6 or b[5] == 15]
                best = max((b[4] for b in cat), default=None)
                rows.append((fid, "yolo-serial", bool(cat), best, {"boxes": [list(b) for b in boxes]}))
        if rows:
            self.store.write_analysis_batch(rows)
        return ids


def _detail(*boxes):
    return json.dumps({"boxes": [list(b) for b in boxes]})


CAT = BOX + [0.8, 15]


# =====================================================================================
#  The rule
# =====================================================================================


def test_a_still_cat_bridges():
    assert Store._bridges((BASE, _detail(CAT)), (BASE + 5_400, _detail(CAT)), BASE)


@pytest.mark.parametrize("since,after_ts,ok", [
    (None, BASE + 5_000, False),                 # no start date saved: bridge nothing
    (BASE + 5_000, BASE + 5_000, True),          # a gap ending AT the date bridges
    (BASE + 5_001, BASE + 5_000, False),         # ... one ending before it never does
])
def test_only_gaps_ending_at_or_after_the_start_date_bridge(since, after_ts, ok):
    assert Store._bridges((BASE, _detail(CAT)), (after_ts, _detail(CAT)), since) is ok


def test_the_gap_is_capped_at_two_minutes():
    assert _BRIDGE_MAX_GAP_MS == 120_000
    assert Store._bridges((BASE, _detail(CAT)), (BASE + 120_000, _detail(CAT)), 0)
    assert not Store._bridges((BASE, _detail(CAT)), (BASE + 120_001, _detail(CAT)), 0)


@pytest.mark.parametrize("before,after", [
    (None, _detail(CAT)),                                    # boundary frame not detected yet
    (_detail(CAT), None),
    (_detail(), _detail(CAT)),                               # detected, no box
    (_detail(BOX + [0.8, 0]), _detail(CAT)),                 # a person, not a cat
    (_detail(BOX + [0.29, 15]), _detail(CAT)),               # best cat box under 0.3
    (_detail(CAT), _detail(BOX + [0.29, 15])),
])
def test_both_boundary_frames_need_a_cat_box_at_the_floor(before, after):
    assert not Store._bridges((BASE, before), (BASE + 5_000, after), 0)


def test_the_floor_is_inclusive():
    assert Store._bridges((BASE, _detail(BOX + [0.3, 15])), (BASE + 5_000, _detail(BOX + [0.3, 15])), 0)


def test_the_best_boxes_must_overlap_by_half():
    # [0,0,10,10] vs [0,0,10,h]: the second lies inside the first, IoU = h / 10.
    a = [0, 0, 10, 10, 0.8, 15]
    at_half = [0, 0, 10, 5, 0.8, 15]
    past_half = [0, 0, 10, 4.9, 0.8, 15]
    assert Store._iou(a, at_half) == pytest.approx(0.5)
    assert Store._bridges((BASE, _detail(a)), (BASE + 5_000, _detail(at_half)), 0)
    assert not Store._bridges((BASE, _detail(a)), (BASE + 5_000, _detail(past_half)), 0)


def test_a_new_cat_after_the_gap_blocks_it_even_when_faint():
    stranger = [200, 200, 240, 240, 0.6, 15]   # weaker than CAT, so CAT stays the best box
    faint = [200, 200, 240, 240, 0.16, 15]
    assert not Store._bridges((BASE, _detail(CAT)), (BASE + 5_000, _detail(CAT, stranger)), 0)
    assert not Store._bridges((BASE, _detail(CAT)), (BASE + 5_000, _detail(CAT, faint)), 0)
    # A cat in the frame BEFORE the gap that has gone after it adds no new cat.
    assert Store._bridges((BASE, _detail(CAT, stranger)), (BASE + 5_000, _detail(CAT)), 0)
    # A person box after the gap is not a cat.
    assert Store._bridges((BASE, _detail(CAT)), (BASE + 5_000, _detail(CAT, [200, 200, 240, 240, 0.9, 0])), 0)
    # Two cats sitting still, both still there: every box after overlaps one before.
    assert Store._bridges((BASE, _detail(CAT, stranger)), (BASE + 5_000, _detail(stranger, CAT)), 0)


def test_the_best_box_is_compared_not_any_box():
    # Before: the best box is elsewhere; after: overlaps only the weaker before-box. Every
    # after-box overlaps a before-box, but the two BEST boxes do not overlap: no bridge.
    weak_here = BOX + [0.4, 15]
    strong_there = [200, 200, 240, 240, 0.9, 15]
    assert not Store._bridges((BASE, _detail(weak_here, strong_there)), (BASE + 5_000, _detail(CAT)), 0)


# =====================================================================================
#  The feed
# =====================================================================================


def _events(store, **kw):
    return store.events(None, None, with_subject=False, **kw)["events"]


def test_a_sit_reads_as_one_visit_whose_record_is_the_union(tmp_path):
    store = _store(tmp_path)
    door = Door(store)
    a = door.frames(BASE, 10, area=0.05)
    b = door.frames(BASE + 900 + 5_400, 8, area=0.07)       # 5.4 s still, then moves again
    [e] = _events(store)
    assert (e["start_id"], e["end_id"]) == (a[0], b[-1])
    assert (e["start_ts"], e["end_ts"]) == (BASE, BASE + 900 + 5_400 + 700)
    assert e["n_frames"] == 18
    assert e["rep_frame_id"] == b[-1]                        # peak area (0.07), ties by id


def test_without_a_start_date_nothing_bridges(tmp_path):
    store = _store(tmp_path, since=None)
    door = Door(store)
    door.frames(BASE, 10)
    door.frames(BASE + 900 + 5_400, 8)
    assert len(_events(store)) == 2
    # The replay's override bridges anyway.
    assert len(_events(store, bridge_since_ms=0)) == 1


def test_the_past_before_the_start_date_is_not_regrouped(tmp_path):
    store = _store(tmp_path, since=BASE + 60_000)
    door = Door(store)
    door.frames(BASE, 10)                                    # a sit split before the date ...
    door.frames(BASE + 6_000, 10)
    door.frames(BASE + 600_000, 10)                          # ... and one after it
    door.frames(BASE + 606_000, 10)
    starts = sorted(e["start_ts"] for e in _events(store))
    assert starts == [BASE, BASE + 6_000, BASE + 600_000]


def test_a_long_sit_with_several_pauses_is_one_visit(tmp_path):
    store = _store(tmp_path)
    door = Door(store)
    t = BASE
    for _ in range(5):
        door.frames(t, 5)
        t += 400 + 30_000                                    # 30 s still each time
    [e] = _events(store)
    assert e["n_frames"] == 25


def test_a_blocked_bridge_splits_as_before(tmp_path):
    store = _store(tmp_path)
    door = Door(store)
    door.frames(BASE, 10)
    door.frames(BASE + 6_000, 10, boxes=(CAT, [300, 300, 340, 340, 0.2, 15]))   # a stranger walks up
    door.frames(BASE + 20_000, 10, boxes=([300, 300, 340, 340, 0.9, 15],))     # cat moved: no overlap
    door.frames(BASE + 40_000, 10, boxes=None)                                 # not yet detected
    assert len(_events(store)) == 4


def test_bridging_runs_before_min_frames_and_the_page_cap(tmp_path):
    store = _store(tmp_path)
    door = Door(store)
    door.frames(BASE, 1)
    door.frames(BASE + 5_000, 1)                             # two 1-frame clusters, one sit
    door.frames(BASE + 600_000, 1)                           # a lone 1-frame visit
    door.frames(BASE + 900_000, 3)
    door.frames(BASE + 905_000, 3)                           # newest: a bridged pair
    got = _events(store, min_frames=2)
    assert [e["n_frames"] for e in got] == [6, 2]
    [newest] = store.events(None, None, limit=1, with_subject=False)["events"]
    assert newest["n_frames"] == 6 and newest["start_ts"] == BASE + 900_000


def test_per_span_reads_run_over_the_bridged_span(tmp_path):
    store = _store(tmp_path)
    tom = store.create_cat("Tom", is_resident=False)["id"]
    os.makedirs(os.path.join(store.models_root, "g1"))
    open(os.path.join(store.models_root, "g1", "gallery.npz"), "wb").close()
    store.promote_model(store.add_model_version(
        "draft", "gallery", "dinov2_vits14", 224, 1, 10, 0.4, "all", None, "g1"
    ))
    model_id = store.active_model()["id"]
    door = Door(store)
    ids = door.frames(BASE, 5) + door.frames(BASE + 400 + 5_000, 5)
    store.write_identifications_batch([(fid, model_id, tom, 0.2, BOX) for fid in ids])
    [e] = store.events(None, None)["events"]
    assert e["identity"]["cat_id"] == tom and e["identity"]["n_frames_voted"] == 10
    assert e["subject"]["kind"] == "cat"


def test_the_scan_cap_drops_a_bridged_visit_whose_head_lies_past_the_window(tmp_path, monkeypatch):
    store = _store(tmp_path)
    door = Door(store)
    door.frames(BASE, 5)
    door.frames(BASE + 5_000, 5)                             # one sit: 10 motion frames
    door.frames(BASE + 300_000, 5)
    monkeypatch.setattr(store_mod, "_EVENT_SCAN_FRAMES", 12)  # cuts the sit's head
    page = store.events(None, None, with_subject=False)
    assert page["truncated"] is True
    assert [e["start_ts"] for e in page["events"]] == [BASE + 300_000]


def test_paging_never_cuts_a_bridged_visit(tmp_path, monkeypatch):
    store = _store(tmp_path)
    door = Door(store)
    rng = random.Random(3)
    t = BASE
    for _ in range(12):
        door.frames(t, rng.randint(1, 4))
        t += rng.choice([1_000, 5_000, 30_000, 200_000])     # within a cluster, bridged, or apart
    whole = sorted((e["start_id"], e["end_id"], e["n_frames"]) for e in _events(store))
    monkeypatch.setattr(store_mod, "_MAX_EVENTS", 2)
    walked, truncated = store._walk_events(1, max_pages=50, with_subject=False)
    assert truncated is False
    assert sorted((e["start_id"], e["end_id"], e["n_frames"]) for e in walked) == whole
    assert len(whole) < 12                                    # some did bridge


def test_the_start_date_is_saved_once_and_kept(tmp_path):
    store = _store(tmp_path, since=None)
    assert store.visit_bridge_since() is None
    assert store.ensure_visit_bridge_since(BASE) == BASE
    assert store.ensure_visit_bridge_since(BASE + 99_999) == BASE
    store.clear()                                             # a timestamp, kept across a Clear
    assert store.visit_bridge_since() == BASE


def test_an_unreadable_start_date_bridges_nothing(tmp_path):
    store = _store(tmp_path, since=None)
    store.set_setting(VISIT_BRIDGE_SINCE_KEY, "garbage")
    assert store.visit_bridge_since() is None


# =====================================================================================
#  visit_start — the alarm's read
# =====================================================================================


def test_visit_start_walks_back_over_a_bridged_sit(tmp_path):
    store = _store(tmp_path)
    door = Door(store)
    a = door.frames(BASE, 5)
    b = door.frames(BASE + 400 + 5_400, 5)
    assert store.visit_start(b[-1], None) == BASE
    assert store.visit_start(a[2], None) == BASE


def test_visit_start_stops_at_a_gap_that_does_not_bridge(tmp_path):
    store = _store(tmp_path)
    door = Door(store)
    door.frames(BASE, 5)
    b = door.frames(BASE + 10_000, 5, boxes=([300, 300, 340, 340, 0.9, 15],))   # elsewhere
    assert store.visit_start(b[-1], None) == BASE + 10_000
    c = door.frames(BASE + 10_400 + _VISIT_GAP_MS, 3, boxes=([300, 300, 340, 340, 0.9, 15],))
    assert store.visit_start(c[-1], None) == BASE + 10_000   # 2 s exactly is the same cluster


def test_visit_start_is_floored(tmp_path):
    store = _store(tmp_path)
    door = Door(store)
    door.frames(BASE, 50)                                    # 5 s of one visit
    last = door.frames(BASE + 5_000, 10)[-1]
    assert store.visit_start(last, BASE + 2_000) == BASE + 2_000    # reaches back past it
    assert store.visit_start(last, BASE) == BASE
    assert store.visit_start(last, BASE - 1) == BASE                 # the visit starts after it
    # A visit that STARTS after the floor, its predecessor (below it) not joining: its own start.
    later = door.frames(BASE + 600_000, 5)
    assert store.visit_start(later[-1], BASE + 599_000) == BASE + 600_000


def test_visit_start_of_a_frame_after_the_last_motion_frame(tmp_path):
    # Tuning: a cat sitting still in view stores non-motion frames after the visit's last
    # motion frame; they belong to the latest visit starting at or before them.
    store = _store(tmp_path)
    door = Door(store)
    door.frames(BASE, 5)
    still = door.frames(BASE + 400 + 100, 30, motion=False)
    assert store.visit_start(still[-1], None) == BASE
    assert store.visit_start(still[-1], BASE + 1_000) == BASE + 1_000


def test_visit_start_of_a_missing_frame_or_with_no_motion_before_it(tmp_path):
    store = _store(tmp_path)
    door = Door(store)
    lone = door.frames(BASE, 3, motion=False)
    assert store.visit_start(lone[-1], BASE - 50) == BASE - 50
    assert store.visit_start(lone[-1], None) is None
    assert store.visit_start(10**9, BASE) == BASE


def test_visit_start_walks_past_a_page(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "_VISIT_START_PAGE", 7)
    store = _store(tmp_path)
    door = Door(store)
    door.frames(BASE, 20)
    last = door.frames(BASE + 1_900 + 4_000, 20)[-1]
    assert store.visit_start(last, None) == BASE
    assert store.visit_start(last, BASE + 1_000) == BASE + 1_000


@pytest.mark.parametrize("seed", range(5))
def test_visit_start_agrees_with_the_feed_on_every_frame(tmp_path, seed):
    store = _store(tmp_path / str(seed))
    door = Door(store)
    rng = random.Random(seed)
    where = [BOX, [300, 300, 340, 340]]
    t = BASE
    for _ in range(15):
        boxes = rng.choice([None, (rng.choice(where) + [rng.choice([0.2, 0.8]), 15],)])
        door.frames(t, rng.randint(1, 6), boxes=boxes)
        t += rng.choice([300, 2_500, 6_000, 60_000, 130_000]) + 500
    events = _events(store)
    for e in events:
        for fid in range(e["start_id"], e["end_id"] + 1):
            assert store.visit_start(fid, None) == e["start_ts"], (fid, e)


# =====================================================================================
#  Plans, on a store with no ANALYZE stats (entry 266)
# =====================================================================================


class _PlanSpy:
    """Wraps the store connection; records the plan of every SELECT the store runs."""

    def __init__(self, conn) -> None:
        self._conn = conn
        self.plans: "list[tuple[str, str]]" = []

    def execute(self, sql, params=()):
        if sql.lstrip().upper().startswith("SELECT"):
            plan = " | ".join(r[-1] for r in self._conn.execute("EXPLAIN QUERY PLAN " + sql, params))
            self.plans.append((sql, plan))
        return self._conn.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_the_boundary_box_read_is_a_primary_key_probe(tmp_path, monkeypatch):
    store = _store(tmp_path)
    door = Door(store)
    door.frames(BASE, 5)
    door.frames(BASE + 5_000, 5)
    spy = _PlanSpy(store._conn)
    monkeypatch.setattr(store, "_conn", spy)
    _events(store)
    reads = [p for sql, p in spy.plans if "FROM analysis WHERE analyzer = 'yolo-serial' AND frame_id IN" in sql]
    assert reads, spy.plans
    for plan in reads:
        assert "sqlite_autoindex_analysis_1 (frame_id=? AND analyzer=?)" in plan, plan
        assert "idx_analysis_analyzer_verdict" not in plan, plan


def test_visit_start_reads_the_motion_index_backwards(tmp_path, monkeypatch):
    store = _store(tmp_path)
    door = Door(store)
    door.frames(BASE, 5)
    last = door.frames(BASE + 5_000, 5)[-1]
    for floor in (None, BASE + 1_000):
        spy = _PlanSpy(store._conn)
        monkeypatch.setattr(store, "_conn", spy)
        store.visit_start(last, floor)
        monkeypatch.setattr(store, "_conn", spy._conn)
        walks = [p for sql, p in spy.plans if sql.startswith("SELECT id, recv_ts FROM frames WHERE motion = 1")]
        boxes = [p for sql, p in spy.plans if "FROM analysis" in sql]
        assert walks and boxes
        for plan in walks:
            assert "idx_frames_motion_recv" in plan and "TEMP B-TREE" not in plan, plan
        for plan in boxes:
            assert "sqlite_autoindex_analysis_1 (frame_id=? AND analyzer=?)" in plan, plan
