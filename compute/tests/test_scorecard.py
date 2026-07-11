"""Tests for the gate-tuning scorecards (compute/collection/store.py):
``Store.gate_scorecard`` and ``Store.gate_fidelity`` — the offline motion-gate
compare from the motion-gate-diagnostic spec.

No real edge, no ML: ``StreamFrame`` objects are built directly (as in
test_collection.py / test_analysis.py) to drive ``frames.motion``/``frames.area``
(the "live" source), and oracle/slot verdicts are seeded by calling
``write_analysis`` directly. Each test asserts the concrete counts the scorecard
must produce, so the recall/miss-breakdown/visit math is pinned:

- live source vs a yolo oracle: analyzed/present, recall (caught/missed/rate),
  false triggers, miss confidence split, area→knob buckets, and visit clustering
  (caught vs wholly-missed by the ±window rule);
- a slot source reads motion from ``analysis.verdict`` and area from
  ``analysis.score`` — NOT from the live ``frames`` columns;
- the warmup prefix is skipped by id ASC of the scored set;
- an empty slot short-circuits to ``needs_rerun``; a populated slot with nothing
  past warmup returns an all-zero card (not ``needs_rerun``);
- a bad oracle raises;
- fidelity is the slot-verdict-vs-live-motion agreement over the slot's frames.
"""
from __future__ import annotations

import pytest

from compute.collection.store import Store
from compute.ingest import StreamFrame
from shared.wire import StreamFrameMeta

_JPEG_BODY = b"\xff\xd8\xff\xe0" + b"fake-jpeg-body" + b"\xff\xd9"


def _store(tmp_path) -> Store:
    return Store(
        db_path=str(tmp_path / "index.db"),
        media_root=str(tmp_path / "media"),
        max_bytes=10_000_000,
    )


def _seed(store, recv_ts, *, motion, area, yolo=None, slot=None, slot_name="mog2:candidate"):
    """Add one frame with live (motion, area) and optional oracle/slot verdicts.

    ``yolo``/``slot`` are ``(verdict, score)`` tuples (or None to omit). Frames
    are inserted in call order, so id order matches call order; pass recv_ts to
    control visit clustering. Returns the new row id.
    """
    meta = StreamFrameMeta(frame_id=recv_ts, ts=recv_ts, motion=motion, bbox=None, area=area)
    row_id = store.add(StreamFrame(meta, _JPEG_BODY), recv_ts_ms=recv_ts)
    if yolo is not None:
        store.write_analysis(row_id, "yolo", bool(yolo[0]), yolo[1], None)
    if slot is not None:
        store.write_analysis(row_id, slot_name, bool(slot[0]), slot[1], None)
    return row_id


# --- gate_scorecard: live source vs oracle -----------------------------------


def test_scorecard_live_full_breakdown(tmp_path):
    store = _store(tmp_path)
    # Thresholds under test.
    min_area, max_area = 0.01, 0.5  # near_zero = min_area/10 = 0.001

    # Visit A (caught): a present motion frame + a missed frame close in time.
    _seed(store, 10_000, motion=True, area=0.05, yolo=(1, 0.9))    # caught, present
    _seed(store, 10_500, motion=False, area=0.0005, yolo=(1, 0.9))  # missed: near_zero, high
    # Visit B (wholly missed): two present frames, no motion frame within ±window.
    _seed(store, 30_000, motion=False, area=0.005, yolo=(1, 0.35))  # missed: below_min, medium
    _seed(store, 30_500, motion=False, area=0.2, yolo=(1, 0.1))     # missed: in_band, low
    # Visit C (caught): a missed frame + a present motion frame close in time.
    _seed(store, 50_000, motion=False, area=0.7, yolo=(1, 0.6))     # missed: above_max, high
    _seed(store, 50_500, motion=True, area=0.3, yolo=(1, 0.7))      # caught, present
    # False triggers (motion, oracle-absent) — placed far from every visit window.
    _seed(store, 70_000, motion=True, area=0.02, yolo=(0, 0.05))
    _seed(store, 71_000, motion=True, area=0.04, yolo=(0, 0.02))
    # An agree-absent frame (still, oracle-absent): counts toward analyzed only.
    _seed(store, 72_000, motion=False, area=0.0, yolo=(0, 0.0))

    card = store.gate_scorecard(
        "live", "yolo", warmup=0, min_area=min_area, max_area=max_area, persistence=3
    )

    assert card["source"] == "live"
    assert card["oracle"] == "yolo"
    assert card["warmup"] == 0
    assert card["analyzed"] == 9  # every frame carries a yolo verdict
    assert card["present"] == 6   # oracle verdict == 1

    assert card["recall"]["caught"] == 2
    assert card["recall"]["missed"] == 4
    assert card["recall"]["rate"] == pytest.approx(2 / 6)

    assert card["false_triggers"] == {"count": 2}

    # Missed set bucketed by oracle score: high >=0.5 (0.9, 0.6), medium 0.3..0.5
    # (0.35), low the rest (0.1).
    assert card["confidence"] == {"high": 2, "medium": 1, "low": 1}

    # Missed set bucketed by (live) area vs thresholds. near_zero (<0.001) is a
    # subset of below_min (<0.01); below_min + above_max + in_band == missed (4).
    assert card["area_buckets"] == {
        "below_min": 2,   # 0.0005 and 0.005
        "near_zero": 1,   # 0.0005 only
        "above_max": 1,   # 0.7
        "in_band": 1,     # 0.2
    }

    # 3 visits; A and C have a motion frame in their span ±3000ms, B does not.
    assert card["visits"] == {"total": 3, "caught": 2, "wholly_missed": 1}


def test_scorecard_empty_present_has_zero_visit_rate(tmp_path):
    # Oracle sees nothing present anywhere: present == 0, recall rate degrades to
    # 0.0 (no ZeroDivisionError) and there are no visits to cluster.
    store = _store(tmp_path)
    _seed(store, 1_000, motion=True, area=0.02, yolo=(0, 0.1))
    _seed(store, 2_000, motion=False, area=0.0, yolo=(0, 0.0))

    card = store.gate_scorecard("live", "yolo", warmup=0, min_area=0.01, max_area=0.5, persistence=3)
    assert card["present"] == 0
    assert card["recall"] == {"caught": 0, "missed": 0, "rate": 0.0}
    assert card["false_triggers"] == {"count": 1}
    assert card["visits"] == {"total": 0, "caught": 0, "wholly_missed": 0}


# --- gate_scorecard: slot source reads analysis, not frames -------------------


def test_scorecard_slot_reads_verdict_and_score_not_live(tmp_path):
    store = _store(tmp_path)
    # G1: live says motion & area 0.7 (would be caught + above_max), but the SLOT
    # says still & score 0.2 → must count as a miss in the in_band area bucket.
    _seed(store, 1_000, motion=True, area=0.7, yolo=(1, 0.9), slot=(0, 0.2))
    # G2: live still (would be a miss), slot says motion → must count as caught.
    _seed(store, 2_000, motion=False, area=0.0, yolo=(1, 0.8), slot=(1, 0.1))
    # G3: slot motion, oracle absent → a slot false trigger.
    _seed(store, 3_000, motion=False, area=0.0, yolo=(0, 0.05), slot=(1, 0.02))

    card = store.gate_scorecard(
        "mog2:candidate", "yolo", warmup=0, min_area=0.01, max_area=0.5, persistence=3
    )

    assert card["source"] == "mog2:candidate"
    assert card["analyzed"] == 3
    assert card["present"] == 2
    assert card["recall"]["caught"] == 1      # G2, from slot verdict (not live)
    assert card["recall"]["missed"] == 1      # G1, despite live motion=1
    assert card["recall"]["rate"] == pytest.approx(0.5)
    assert card["false_triggers"] == {"count": 1}  # G3
    # G1's miss bucketed by SLOT score 0.2 → in_band, NOT above_max (live 0.7).
    assert card["area_buckets"] == {"below_min": 0, "near_zero": 0, "above_max": 0, "in_band": 1}
    # G1's confidence uses the ORACLE score 0.9 → high.
    assert card["confidence"] == {"high": 1, "medium": 0, "low": 0}


def test_scorecard_slot_zero_rows_needs_rerun(tmp_path):
    store = _store(tmp_path)
    _seed(store, 1_000, motion=True, area=0.05, yolo=(1, 0.9))  # yolo only, no slot

    card = store.gate_scorecard(
        "mog2:candidate", "yolo", warmup=0, min_area=0.01, max_area=0.5, persistence=3
    )
    assert card == {"source": "mog2:candidate", "oracle": "yolo", "needs_rerun": True}


def test_scorecard_slot_populated_but_nothing_past_warmup_is_zero_card(tmp_path):
    # The slot HAS rows but the warmup prefix swallows them all → an all-zero card,
    # NOT needs_rerun (which means "run the slot first").
    store = _store(tmp_path)
    _seed(store, 1_000, motion=True, area=0.05, yolo=(1, 0.9), slot=(1, 0.05))
    _seed(store, 2_000, motion=False, area=0.0, yolo=(1, 0.8), slot=(0, 0.0))

    card = store.gate_scorecard(
        "mog2:candidate", "yolo", warmup=5, min_area=0.01, max_area=0.5, persistence=3
    )
    assert "needs_rerun" not in card
    assert card["analyzed"] == 0
    assert card["present"] == 0
    assert card["recall"] == {"caught": 0, "missed": 0, "rate": 0.0}
    assert card["visits"] == {"total": 0, "caught": 0, "wholly_missed": 0}


# --- gate_scorecard: warmup + validation -------------------------------------


def test_scorecard_warmup_skips_oldest_of_scored_set(tmp_path):
    store = _store(tmp_path)
    # 5 frames, all caught+present; warmup=2 skips the oldest 2 by id.
    for i in range(5):
        _seed(store, 1_000 + i, motion=True, area=0.05, yolo=(1, 0.9))

    card = store.gate_scorecard("live", "yolo", warmup=2, min_area=0.01, max_area=0.5, persistence=3)
    assert card["warmup"] == 2
    assert card["analyzed"] == 3
    assert card["present"] == 3
    assert card["recall"]["caught"] == 3


def test_scorecard_warmup_larger_than_scored_set_is_zero_card_for_live(tmp_path):
    store = _store(tmp_path)
    for i in range(3):
        _seed(store, 1_000 + i, motion=True, area=0.05, yolo=(1, 0.9))

    card = store.gate_scorecard("live", "yolo", warmup=10, min_area=0.01, max_area=0.5, persistence=3)
    assert "needs_rerun" not in card  # live is never needs_rerun
    assert card["analyzed"] == 0
    assert card["present"] == 0


def test_scorecard_rejects_bad_oracle(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        store.gate_scorecard("live", "bogus", min_area=0.01, max_area=0.5, persistence=3)


# --- gate_scorecard: since_id/until_id range scoping -------------------------
#
# The frame-range-groups spec's scoped compare: a caller passes warmup=0
# (the window is warm-started from the frames just before it, so there is no
# cold-start prefix to drop) alongside since_id/until_id, and every one of the
# scorecard's internal queries (threshold probe, aggregate pass, and the
# visit-clustering "interesting" rows) must apply the SAME bounds so the
# counts reflect only the window, not the whole store.


def test_gate_scorecard_scoped_by_since_and_until_id(tmp_path):
    store = _store(tmp_path)
    # Two frames OUTSIDE the window (a false trigger before it, a missed
    # present after it) that would inflate an unscoped scorecard, plus three
    # INSIDE it (caught, missed, false trigger) — scoping must count only the
    # three inside.
    _seed(store, 10_000, motion=True, area=0.05, yolo=(0, 0.05))            # before window: false trigger
    id_start = _seed(store, 20_000, motion=True, area=0.05, yolo=(1, 0.9))  # window start: caught
    _seed(store, 20_500, motion=False, area=0.05, yolo=(1, 0.9))            # window middle: missed
    id_end = _seed(store, 21_000, motion=True, area=0.05, yolo=(0, 0.05))   # window end: false trigger
    _seed(store, 40_000, motion=False, area=0.05, yolo=(1, 0.9))            # after window: missed

    card = store.gate_scorecard(
        "live", "yolo", warmup=0, min_area=0.01, max_area=0.5, persistence=3,
        since_id=id_start, until_id=id_end,
    )
    assert card["analyzed"] == 3
    assert card["present"] == 2
    assert card["recall"]["caught"] == 1
    assert card["recall"]["missed"] == 1
    assert card["recall"]["rate"] == pytest.approx(0.5)
    assert card["false_triggers"] == {"count": 1}

    # Same store, unscoped: sees the extra false trigger + miss that lie
    # outside the window — proof the narrower numbers above came from the
    # scope, not from the fixture data itself.
    whole = store.gate_scorecard("live", "yolo", warmup=0, min_area=0.01, max_area=0.5, persistence=3)
    assert whole["analyzed"] == 5
    assert whole["present"] == 3
    assert whole["recall"]["caught"] == 1
    assert whole["recall"]["missed"] == 2
    assert whole["false_triggers"] == {"count": 2}


def test_gate_scorecard_scoped_needs_rerun_when_slot_unrun_in_window(tmp_path):
    # The slot has verdicts, but only for frames OUTSIDE the scoped window (e.g. a
    # prior re-run scoped to a different group). A scoped scorecard over THIS window
    # must report needs_rerun ("run the slot first"), NOT fabricate an all-zero card
    # from an empty in-window scored set — the needs_rerun check is scoped to the same
    # window, not the whole slot.
    store = _store(tmp_path)
    _seed(store, 10_000, motion=True, area=0.05, yolo=(1, 0.9), slot=(1, 0.05))  # out-of-window: slot present
    id_start = _seed(store, 20_000, motion=True, area=0.05, yolo=(1, 0.9))       # in-window: oracle only, no slot
    id_end = _seed(store, 21_000, motion=False, area=0.0, yolo=(1, 0.8))

    card = store.gate_scorecard(
        "mog2:candidate", "yolo", warmup=0, min_area=0.01, max_area=0.5, persistence=3,
        since_id=id_start, until_id=id_end,
    )
    assert card == {"source": "mog2:candidate", "oracle": "yolo", "needs_rerun": True}

    # Unscoped the slot IS populated (its out-of-window row), so NOT needs_rerun —
    # proof the needs_rerun above came from the window scope, not a globally-empty slot.
    whole = store.gate_scorecard(
        "mog2:candidate", "yolo", warmup=0, min_area=0.01, max_area=0.5, persistence=3
    )
    assert "needs_rerun" not in whole


# --- gate_fidelity -----------------------------------------------------------


def test_gate_fidelity_agreement_over_slot_frames(tmp_path):
    store = _store(tmp_path)
    _seed(store, 1_000, motion=True, area=0.05, slot=(1, 0.05))   # agree (1 == 1)
    _seed(store, 2_000, motion=False, area=0.0, slot=(0, 0.0))    # agree (0 == 0)
    _seed(store, 3_000, motion=True, area=0.05, slot=(0, 0.0))    # disagree (1 != 0)
    # A frame with no slot verdict is NOT compared.
    _seed(store, 4_000, motion=True, area=0.05, yolo=(1, 0.9))

    fidelity = store.gate_fidelity("mog2:candidate")
    assert fidelity["compared"] == 3
    assert fidelity["agree"] == 2
    assert fidelity["rate"] == pytest.approx(2 / 3)


def test_gate_fidelity_empty_slot_is_zero(tmp_path):
    store = _store(tmp_path)
    _seed(store, 1_000, motion=True, area=0.05, yolo=(1, 0.9))
    assert store.gate_fidelity("mog2:candidate") == {"compared": 0, "agree": 0, "rate": 0.0}


def test_gate_fidelity_scoped_by_since_and_until_id(tmp_path):
    store = _store(tmp_path)
    # Frames before and after the window disagree; scoping must drop them from
    # both `compared` and `agree` so the rate reflects only the window.
    _seed(store, 1_000, motion=True, area=0.05, slot=(0, 0.0))              # before window: disagree
    id_start = _seed(store, 2_000, motion=True, area=0.05, slot=(1, 0.9))   # window start: agree
    _seed(store, 3_000, motion=False, area=0.0, slot=(1, 0.1))              # window middle: disagree
    id_end = _seed(store, 4_000, motion=False, area=0.0, slot=(0, 0.0))     # window end: agree
    _seed(store, 5_000, motion=True, area=0.05, slot=(0, 0.0))              # after window: disagree

    scoped = store.gate_fidelity("mog2:candidate", since_id=id_start, until_id=id_end)
    assert scoped == {"compared": 3, "agree": 2, "rate": pytest.approx(2 / 3)}

    unscoped = store.gate_fidelity("mog2:candidate")
    assert unscoped == {"compared": 5, "agree": 2, "rate": pytest.approx(2 / 5)}
