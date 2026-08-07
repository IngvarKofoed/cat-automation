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


def test_scorecard_oracle_floor_drops_low_confidence_phantoms(tmp_path):
    # A low-conf oracle (YOLO at conf 0.15) hallucinates cats on empty frames; those
    # phantoms inflate present/missed and fragment into extra visits. ``oracle_floor``
    # re-slices "present" to verdicts at/above the floor, over the SAME stored rows.
    store = _store(tmp_path)
    min_area, max_area = 0.01, 0.5

    # Two low-conf phantoms, 10s apart (each its own visit), gate correctly silent.
    _seed(store, 10_000, motion=False, area=0.0, yolo=(1, 0.2))
    _seed(store, 20_000, motion=False, area=0.0, yolo=(1, 0.2))
    # A real, high-conf visit the gate wholly missed (two frames close in time).
    _seed(store, 50_000, motion=False, area=0.005, yolo=(1, 0.9))
    _seed(store, 50_500, motion=False, area=0.005, yolo=(1, 0.8))
    # A real, high-conf caught frame (gate fired).
    _seed(store, 70_000, motion=True, area=0.05, yolo=(1, 0.85))
    # A low-conf detection the gate DID fire on: present+caught below the floor, but a
    # false trigger once floored out (gate fired where the oracle isn't sure).
    _seed(store, 90_000, motion=True, area=0.03, yolo=(1, 0.2))

    # Unfloored: every verdict==1 is present — phantoms and all.
    card = store.gate_scorecard(
        "live", "yolo", warmup=0, min_area=min_area, max_area=max_area, persistence=3,
        oracle_floor=0.0,
    )
    assert card["present"] == 6
    assert card["recall"]["caught"] == 2 and card["recall"]["missed"] == 4
    assert card["false_triggers"] == {"count": 0}
    assert card["visits"] == {"total": 5, "caught": 2, "wholly_missed": 3}

    # Floored at 0.3: the three score-0.2 rows are no longer "present". The two
    # phantom visits vanish, the low-conf caught frame becomes a false trigger, and
    # visit recall is now measured against the real cats only.
    floored = store.gate_scorecard(
        "live", "yolo", warmup=0, min_area=min_area, max_area=max_area, persistence=3,
        oracle_floor=0.3,
    )
    assert floored["present"] == 3
    assert floored["recall"]["caught"] == 1 and floored["recall"]["missed"] == 2
    assert floored["recall"]["rate"] == pytest.approx(1 / 3)
    assert floored["false_triggers"] == {"count": 1}
    assert floored["confidence"] == {"high": 2, "medium": 0, "low": 0}
    assert floored["visits"] == {"total": 2, "caught": 1, "wholly_missed": 1}


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


# --- gate_scorecard: optional day/night split (admin-next P2) ----------------
#
# The split is per-VISIT — visit recall is the metric (changelog 46) — so each
# visit is bucketed by ``is_night`` of its FIRST present frame's recv_ts, and a
# dusk/dawn-straddling visit counts once. Absent ``is_night`` no ``"split"`` key
# is added (byte-identical). The bucketing math is pinned here with a synthetic
# ``is_night`` (no astral); the real sun-time path lives in test_suntimes.py.


def test_scorecard_split_absent_is_night_adds_no_split_key(tmp_path):
    store = _store(tmp_path)
    _seed(store, 10_000, motion=True, area=0.05, yolo=(1, 0.9))
    card = store.gate_scorecard("live", "yolo", warmup=0, min_area=0.01, max_area=0.5, persistence=3)
    assert "split" not in card


def test_scorecard_split_buckets_visits_by_first_present_frame(tmp_path):
    store = _store(tmp_path)
    # Visit A (caught): present+motion then a missed present, close in time.
    _seed(store, 10_000, motion=True, area=0.05, yolo=(1, 0.9))
    _seed(store, 10_500, motion=False, area=0.05, yolo=(1, 0.9))
    # Visit B (wholly missed): two present frames, no motion frame within ±window.
    _seed(store, 30_000, motion=False, area=0.005, yolo=(1, 0.9))
    _seed(store, 30_500, motion=False, area=0.2, yolo=(1, 0.9))
    # Visit C (caught): a missed present then a present+motion, close in time.
    _seed(store, 50_000, motion=False, area=0.05, yolo=(1, 0.9))
    _seed(store, 50_500, motion=True, area=0.05, yolo=(1, 0.9))

    # night iff first present recv_ts >= 40_000 -> A,B are day, C is night.
    card = store.gate_scorecard(
        "live", "yolo", warmup=0, min_area=0.01, max_area=0.5, persistence=3,
        is_night=lambda ts: ts >= 40_000,
    )
    assert card["visits"] == {"total": 3, "caught": 2, "wholly_missed": 1}
    assert card["split"]["day"] == {"total": 2, "caught": 1, "wholly_missed": 1}
    assert card["split"]["night"] == {"total": 1, "caught": 1, "wholly_missed": 0}
    # The split partitions the combined visit counts EXACTLY.
    assert card["split"]["day"]["total"] + card["split"]["night"]["total"] == card["visits"]["total"]
    assert card["split"]["day"]["caught"] + card["split"]["night"]["caught"] == card["visits"]["caught"]


def test_scorecard_split_straddling_visit_counts_once_in_first_frame_bucket(tmp_path):
    # A single visit whose first present frame sits on the DAY side of the boundary
    # but whose later frame (same cluster) sits on the NIGHT side must count ONCE,
    # in the day bucket — never split into two half-visits.
    store = _store(tmp_path)
    _seed(store, 39_000, motion=True, area=0.05, yolo=(1, 0.9))   # first present -> day
    _seed(store, 39_500, motion=False, area=0.05, yolo=(1, 0.9))  # +500ms -> night side, same visit
    card = store.gate_scorecard(
        "live", "yolo", warmup=0, min_area=0.01, max_area=0.5, persistence=3,
        is_night=lambda ts: ts >= 39_250,
    )
    assert card["visits"] == {"total": 1, "caught": 1, "wholly_missed": 0}
    assert card["split"]["day"] == {"total": 1, "caught": 1, "wholly_missed": 0}
    assert card["split"]["night"] == {"total": 0, "caught": 0, "wholly_missed": 0}


def test_scorecard_split_empty_present_is_all_zero(tmp_path):
    store = _store(tmp_path)
    _seed(store, 1_000, motion=True, area=0.02, yolo=(0, 0.1))  # nothing present anywhere
    card = store.gate_scorecard(
        "live", "yolo", warmup=0, min_area=0.01, max_area=0.5, persistence=3,
        is_night=lambda ts: True,
    )
    assert card["visits"] == {"total": 0, "caught": 0, "wholly_missed": 0}
    assert card["split"]["day"] == {"total": 0, "caught": 0, "wholly_missed": 0}
    assert card["split"]["night"] == {"total": 0, "caught": 0, "wholly_missed": 0}


def test_scorecard_split_needs_rerun_slot_carries_no_split(tmp_path):
    # A slot with zero rows short-circuits to needs_rerun even with is_night set —
    # nothing to score, so no split.
    store = _store(tmp_path)
    _seed(store, 1_000, motion=True, area=0.05, yolo=(1, 0.9))
    card = store.gate_scorecard(
        "mog2:candidate", "yolo", warmup=0, min_area=0.01, max_area=0.5, persistence=3,
        is_night=lambda ts: False,
    )
    assert card == {"source": "mog2:candidate", "oracle": "yolo", "needs_rerun": True}


# --- gate_scorecard: missed_visits records -----------------------------------


def test_scorecard_missed_visits_absent_flag_adds_no_key(tmp_path):
    store = _store(tmp_path)
    _seed(store, 10_000, motion=False, area=0.005, yolo=(1, 0.9))
    card = store.gate_scorecard("live", "yolo", warmup=0, min_area=0.01, max_area=0.5, persistence=3)
    assert "missed_visits" not in card


def test_scorecard_missed_visits_length_equals_wholly_missed(tmp_path):
    # THE invariant the whole design exists for: the list is derived from the same
    # spans the count is, so it can never disagree with the headline number.
    store = _store(tmp_path)
    # Caught visit.
    _seed(store, 10_000, motion=True, area=0.05, yolo=(1, 0.9))
    _seed(store, 10_500, motion=False, area=0.05, yolo=(1, 0.8))
    # Two wholly-missed visits, far apart and far from any motion frame.
    _seed(store, 30_000, motion=False, area=0.005, yolo=(1, 0.7))
    _seed(store, 30_500, motion=False, area=0.006, yolo=(1, 0.6))
    _seed(store, 60_000, motion=False, area=0.007, yolo=(1, 0.5))
    # A false trigger, far from every visit: motion without an oracle verdict.
    _seed(store, 90_000, motion=True, area=0.02, yolo=(0, 0.05))

    card = store.gate_scorecard(
        "live", "yolo", warmup=0, min_area=0.01, max_area=0.5, persistence=3,
        missed_visits=True,
    )
    assert card["visits"]["wholly_missed"] == 2
    assert len(card["missed_visits"]) == card["visits"]["wholly_missed"]
    # And the CAUGHT visit is absent — the list is misses only.
    assert all(rec["start_ts"] >= 30_000 for rec in card["missed_visits"])


def test_scorecard_missed_visits_records_are_chronological_with_span_bounds(tmp_path):
    # Deliberately NOT visits()' worst-first: a stable time order is what makes
    # toggling between two columns' panels a visual diff. Seeded so a worst-first
    # sort (n_present desc) would invert the two records.
    store = _store(tmp_path)
    early = _seed(store, 30_000, motion=False, area=0.005, yolo=(1, 0.4))  # 1 frame
    late_a = _seed(store, 60_000, motion=False, area=0.006, yolo=(1, 0.9))  # 3 frames
    late_b = _seed(store, 60_500, motion=False, area=0.007, yolo=(1, 0.5))
    late_c = _seed(store, 61_000, motion=False, area=0.008, yolo=(1, 0.6))

    card = store.gate_scorecard(
        "live", "yolo", warmup=0, min_area=0.01, max_area=0.5, persistence=3,
        missed_visits=True,
    )
    recs = card["missed_visits"]
    assert [r["start_ts"] for r in recs] == [30_000, 60_000]  # chronological, not worst-first
    assert {k: recs[0][k] for k in ("start_id", "end_id", "start_ts", "end_ts", "n_present", "rep_frame_id")} == {
        "start_id": early, "end_id": early, "start_ts": 30_000, "end_ts": 30_000,
        "n_present": 1, "rep_frame_id": early,
    }
    assert recs[0]["peak_score"] == pytest.approx(0.4)
    # The id span bounds the whole cluster — it is what the UI fetches the strip by.
    assert recs[1]["start_id"] == late_a
    assert recs[1]["end_id"] == late_c
    assert recs[1]["end_ts"] == 61_000
    assert recs[1]["n_present"] == 3
    # rep = highest oracle score in the cluster (0.9), NOT the last frame.
    assert recs[1]["rep_frame_id"] == late_a
    assert recs[1]["peak_score"] == pytest.approx(0.9)
    assert late_b not in (recs[1]["rep_frame_id"],)


def test_scorecard_missed_visits_unscored_rows_never_win_the_rep_pick(tmp_path):
    # NULL oracle score sorts as -inf (visits()' rule), so a scored peer always wins;
    # an all-unscored span reports peak_score None rather than fabricating a number.
    store = _store(tmp_path)
    _seed(store, 30_000, motion=False, area=0.005, yolo=(1, None))
    scored = _seed(store, 30_500, motion=False, area=0.006, yolo=(1, 0.2))
    _seed(store, 30_900, motion=False, area=0.007, yolo=(1, None))
    # A second, wholly unscored visit.
    _seed(store, 60_000, motion=False, area=0.005, yolo=(1, None))

    card = store.gate_scorecard(
        "live", "yolo", warmup=0, min_area=0.01, max_area=0.5, persistence=3,
        missed_visits=True,
    )
    recs = card["missed_visits"]
    assert len(recs) == 2
    assert recs[0]["rep_frame_id"] == scored
    assert recs[0]["peak_score"] == pytest.approx(0.2)
    assert recs[1]["peak_score"] is None


def test_scorecard_missed_visits_night_tag_matches_split_bucketing(tmp_path):
    # The per-record night flag must agree with how the SPLIT counted the same
    # visit — both bucket by the span's FIRST present frame.
    store = _store(tmp_path)
    _seed(store, 30_000, motion=False, area=0.005, yolo=(1, 0.9))   # day
    _seed(store, 60_000, motion=False, area=0.005, yolo=(1, 0.9))   # night
    _seed(store, 60_500, motion=False, area=0.005, yolo=(1, 0.9))   # same visit

    card = store.gate_scorecard(
        "live", "yolo", warmup=0, min_area=0.01, max_area=0.5, persistence=3,
        is_night=lambda ts: ts >= 40_000, missed_visits=True,
    )
    assert [r["night"] for r in card["missed_visits"]] == [False, True]
    assert card["split"]["day"]["wholly_missed"] == 1
    assert card["split"]["night"]["wholly_missed"] == 1


def test_scorecard_missed_visits_no_night_key_without_is_night(tmp_path):
    store = _store(tmp_path)
    _seed(store, 30_000, motion=False, area=0.005, yolo=(1, 0.9))
    card = store.gate_scorecard(
        "live", "yolo", warmup=0, min_area=0.01, max_area=0.5, persistence=3,
        missed_visits=True,
    )
    assert "night" not in card["missed_visits"][0]


def test_scorecard_missed_visits_needs_rerun_slot_carries_no_key(tmp_path):
    store = _store(tmp_path)
    _seed(store, 1_000, motion=True, area=0.05, yolo=(1, 0.9))
    card = store.gate_scorecard(
        "mog2:candidate", "yolo", warmup=0, min_area=0.01, max_area=0.5, persistence=3,
        missed_visits=True,
    )
    assert card == {"source": "mog2:candidate", "oracle": "yolo", "needs_rerun": True}


def test_scorecard_missed_visits_for_a_slot_uses_slot_verdicts_not_live_motion(tmp_path):
    # The whole reason this isn't Store.visits: a slot column's misses must be judged
    # against the SLOT's verdicts. Here the live gate fired but the candidate slot did
    # not, so the visit is a miss for the slot and caught for live.
    store = _store(tmp_path)
    _seed(store, 30_000, motion=True, area=0.05, yolo=(1, 0.9), slot=(0, 0.001))
    _seed(store, 30_500, motion=True, area=0.05, yolo=(1, 0.8), slot=(0, 0.001))

    live = store.gate_scorecard(
        "live", "yolo", warmup=0, min_area=0.01, max_area=0.5, persistence=3,
        missed_visits=True,
    )
    slot = store.gate_scorecard(
        "mog2:candidate", "yolo", warmup=0, min_area=0.01, max_area=0.5, persistence=3,
        missed_visits=True,
    )
    assert live["missed_visits"] == []
    assert len(slot["missed_visits"]) == 1
    assert slot["missed_visits"][0]["n_present"] == 2


def test_scorecard_missed_visits_respects_oracle_floor_and_warmup(tmp_path):
    # The records come from the same warmed-up, floored ``interesting`` rows the
    # counts do — so a phantom floored out of the count is absent from the list too.
    store = _store(tmp_path)
    _seed(store, 10_000, motion=False, area=0.005, yolo=(1, 0.2))   # phantom, floored out
    _seed(store, 40_000, motion=False, area=0.005, yolo=(1, 0.9))   # real miss

    card = store.gate_scorecard(
        "live", "yolo", warmup=0, min_area=0.01, max_area=0.5, persistence=3,
        oracle_floor=0.3, missed_visits=True,
    )
    assert card["visits"]["wholly_missed"] == 1
    assert [r["start_ts"] for r in card["missed_visits"]] == [40_000]

    # Warm-up drops the oldest scored row, taking its visit out of both count and list.
    warmed = store.gate_scorecard(
        "live", "yolo", warmup=1, min_area=0.01, max_area=0.5, persistence=3,
        oracle_floor=0.0, missed_visits=True,
    )
    assert len(warmed["missed_visits"]) == warmed["visits"]["wholly_missed"] == 1
    assert warmed["missed_visits"][0]["start_ts"] == 40_000


# --- missed_visits: per-frame reject attribution + the recommended knob ---------


def test_classify_miss_mirrors_the_gate_band_test():
    from compute.collection.store import classify_miss

    mn, mx = 0.01, 0.5                        # near_zero cutoff = mn/10 = 0.001
    assert classify_miss(0.0, mn, mx) == "near_zero"
    assert classify_miss(0.0005, mn, mx) == "near_zero"
    assert classify_miss(0.005, mn, mx) == "below_min"
    assert classify_miss(0.2, mn, mx) == "in_band"       # in band -> only the debounce dropped it
    assert classify_miss(0.7, mn, mx) == "above_max"
    # The band is INCLUSIVE at both ends, exactly as MotionGate.process tests it.
    assert classify_miss(mn, mn, mx) == "in_band"
    assert classify_miss(mx, mn, mx) == "in_band"
    # No stored reading means "MOG2 saw nothing", not an unknown to hide.
    assert classify_miss(None, mn, mx) == "near_zero"


def test_missed_visit_attributes_a_full_frame_cat_to_above_max(tmp_path):
    # The case that prompted this: YOLO is certain across many frames, but every blob
    # exceeded max_area_fraction (a cat directly under a top-down camera fills the ROI),
    # so the gate discarded them as whole-ROI light changes.
    store = _store(tmp_path)
    for i in range(6):
        _seed(store, 30_000 + i * 200, motion=False, area=0.85, yolo=(1, 0.94))

    card = store.gate_scorecard(
        "live", "yolo", warmup=0, min_area=0.01, max_area=0.5, persistence=2,
        missed_visits=True,
    )
    rec = card["missed_visits"][0]
    assert rec["buckets"] == {"near_zero": 0, "below_min": 0, "above_max": 6, "in_band": 0}
    assert rec["reason"] == "above_max"
    assert rec["fix"] == {"param": "max_area_fraction", "direction": "up"}
    assert [f["bucket"] for f in rec["frames"]] == ["above_max"] * 6
    assert rec["frames"][0]["area"] == pytest.approx(0.85)
    assert [f["id"] for f in rec["frames"]] == sorted(f["id"] for f in rec["frames"])


def test_missed_visit_in_band_frames_blame_the_debounce(tmp_path):
    # Area inside the band yet motion=0 means ONLY persistence dropped it.
    store = _store(tmp_path)
    for i in range(4):
        _seed(store, 30_000 + i * 200, motion=False, area=0.2, yolo=(1, 0.9))

    card = store.gate_scorecard(
        "live", "yolo", warmup=0, min_area=0.01, max_area=0.5, persistence=5,
        missed_visits=True,
    )
    rec = card["missed_visits"][0]
    assert rec["reason"] == "in_band"
    assert rec["fix"] == {"param": "persistence", "direction": "down"}


def test_missed_visit_near_zero_beats_below_min_on_a_tie(tmp_path):
    # Equal counts -> _MISS_BUCKET_PRIORITY decides, and near_zero (MOG2 saw nothing)
    # outranks below_min: at ~0 area, lowering min_area buys nothing.
    store = _store(tmp_path)
    _seed(store, 30_000, motion=False, area=0.0, yolo=(1, 0.9))      # near_zero
    _seed(store, 30_200, motion=False, area=0.005, yolo=(1, 0.9))    # below_min

    card = store.gate_scorecard(
        "live", "yolo", warmup=0, min_area=0.01, max_area=0.5, persistence=2,
        missed_visits=True,
    )
    rec = card["missed_visits"][0]
    assert rec["buckets"]["near_zero"] == 1 and rec["buckets"]["below_min"] == 1
    assert rec["reason"] == "near_zero"
    assert rec["fix"] == {"param": "var_threshold", "direction": "down"}


def test_missed_visit_buckets_partition_the_visits_frames(tmp_path):
    # Per-visit buckets are EXCLUSIVE (they must sum to n_present) — unlike the
    # window-wide area_buckets, where near_zero is a subset of below_min.
    store = _store(tmp_path)
    for area in (0.0, 0.0005, 0.005, 0.2, 0.7, 0.9):
        _seed(store, 30_000 + int(area * 1000) * 10, motion=False, area=area, yolo=(1, 0.9))

    card = store.gate_scorecard(
        "live", "yolo", warmup=0, min_area=0.01, max_area=0.5, persistence=2,
        missed_visits=True,
    )
    for rec in card["missed_visits"]:
        assert sum(rec["buckets"].values()) == rec["n_present"] == len(rec["frames"])


def test_missed_visit_slot_attributes_against_its_own_area_not_the_live_gate(tmp_path):
    # The reason this isn't `f.area`: a slot column must be attributed by the area ITS
    # re-run measured. Here the live gate recorded a tiny blob but the candidate slot
    # measured an over-max one, so the two columns must name DIFFERENT knobs.
    store = _store(tmp_path)
    for i in range(3):
        _seed(store, 30_000 + i * 200, motion=False, area=0.0,
              yolo=(1, 0.9), slot=(0, 0.85))

    live = store.gate_scorecard(
        "live", "yolo", warmup=0, min_area=0.01, max_area=0.5, persistence=2,
        missed_visits=True,
    )
    slot = store.gate_scorecard(
        "mog2:candidate", "yolo", warmup=0, min_area=0.01, max_area=0.5, persistence=2,
        missed_visits=True,
    )
    assert live["missed_visits"][0]["reason"] == "near_zero"
    assert slot["missed_visits"][0]["reason"] == "above_max"
    assert slot["missed_visits"][0]["frames"][0]["area"] == pytest.approx(0.85)


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


# --- scoped query plan (the join-order pin) ----------------------------------
#
# A scoped card pins ``frames`` as the outer loop with CROSS JOIN. Without the pin, and
# with no ``ANALYZE`` stats — what the real store has — SQLite drives from
# ``idx_analysis_analyzer_verdict (analyzer=?)``, walks the oracle's WHOLE partition and
# applies the window bound only after probing ``frames``: a one-day compare pays for
# every verdict in the store. It is the fifth instance of that class (changelog
# 229/265/276/307/385), and the numbers are identical either way, so nothing but a plan
# assertion can catch a regression.


def _scorecard_plans(store, **kwargs) -> "list[tuple[str, str]]":
    """(label, first plan line) for each scored-set query one gate_scorecard issues."""
    real = store._conn

    class Spy:
        def __init__(self): self.sql = []
        def execute(self, sql, params=()):
            self.sql.append((sql, list(params)))
            return real.execute(sql, params)
        def __getattr__(self, n): return getattr(real, n)

    spy = Spy()
    store._conn = spy
    try:
        store.gate_scorecard(**kwargs)
    finally:
        store._conn = real

    out = []
    for sql, params in spy.sql:
        if " FROM frames f" not in sql:
            continue
        label = ("threshold" if sql.lstrip().startswith("SELECT f.id")
                 else "aggregate" if "COUNT(*)" in sql else "interesting")
        plan = real.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
        out.append((label, plan[0][-1], sql))
    return out


@pytest.mark.parametrize("source", ["live", "mog2:candidate"])
def test_scoped_scorecard_drives_from_the_frames_id_range(tmp_path, source):
    store = _store(tmp_path)
    ids = [_seed(store, 1_000 + i * 100, motion=bool(i % 2), area=0.05,
                 yolo=(i % 3 == 0, 0.9), slot=(i % 2, 0.05)) for i in range(12)]

    plans = _scorecard_plans(
        store, source=source, oracle="yolo", warmup=0, min_area=0.01, max_area=0.5,
        persistence=3, since_id=ids[2], until_id=ids[9],
    )
    assert plans, "no scored-set query was issued"
    for label, first, sql in plans:
        assert first.startswith("SEARCH f USING INTEGER PRIMARY KEY"), (
            f"{source} {label} no longer seeks the frames id range: {first}"
        )
        assert "CROSS JOIN" in sql, f"{source} {label} lost the join-order pin"


@pytest.mark.parametrize("source", ["live", "mog2:candidate"])
def test_unscoped_scorecard_is_not_pinned(tmp_path, source):
    # Unscoped there is no range to seek, so pinning buys nothing and costs a little on a
    # slot column — the SQL stays byte-for-byte what it was before the pin existed.
    store = _store(tmp_path)
    for i in range(12):
        _seed(store, 1_000 + i * 100, motion=bool(i % 2), area=0.05,
              yolo=(i % 3 == 0, 0.9), slot=(i % 2, 0.05))

    plans = _scorecard_plans(
        store, source=source, oracle="yolo", warmup=0, min_area=0.01,
        max_area=0.5, persistence=3,
    )
    assert plans
    for label, _first, sql in plans:
        assert "CROSS JOIN" not in sql, f"unscoped {source} {label} must not be pinned"


def test_pin_does_not_move_any_number(tmp_path):
    """A window covering every frame must score exactly as the unscoped card does.

    The pin only changes the plan, so a scope that admits everything has to reproduce the
    unscoped card key-for-key — which is what makes it safe to apply on one path only.
    """
    store = _store(tmp_path)
    ids = []
    for i in range(24):
        ids.append(_seed(store, 1_000 + i * 100, motion=bool(i % 2), area=0.004 * (i % 5),
                         yolo=(i % 3 == 0, 0.2 + 0.1 * (i % 8)), slot=(i % 2, 0.004 * (i % 7))))

    for source in ("live", "mog2:candidate"):
        for floor in (0.0, 0.3):
            scoped = store.gate_scorecard(
                source, "yolo", warmup=0, min_area=0.01, max_area=0.5, persistence=3,
                oracle_floor=floor, since_id=ids[0], until_id=ids[-1], missed_visits=True,
            )
            whole = store.gate_scorecard(
                source, "yolo", warmup=0, min_area=0.01, max_area=0.5, persistence=3,
                oracle_floor=floor, missed_visits=True,
            )
            assert scoped == whole, f"{source} floor={floor}: pinned card differs from unscoped"


# --- count_before_capped (the compare's warm-up shortfall) --------------------


def test_count_before_capped_saturates_at_the_cap(tmp_path):
    store = _store(tmp_path)
    ids = [_seed(store, 1_000 + i * 10, motion=False, area=0.0) for i in range(20)]

    # Exact below the cap, saturated at it — never more, so the caller's
    # max(0, WARMUP - n) shortfall is identical to what an exact count would give.
    assert store.count_before_capped(ids[0], 5) == 1
    assert store.count_before_capped(ids[4], 5) == 5
    assert store.count_before_capped(ids[19], 5) == 5
    assert store.count_before_capped(ids[19], 100) == 20
    assert store.count_before_capped(ids[0] - 1, 100) == 0
    assert store.count_before_capped(ids[19], 0) == 0


def test_count_before_capped_matches_count_in_range_under_the_cap(tmp_path):
    # Below saturation the two must agree exactly — that equivalence is what makes it
    # safe to swap the capped form into the warm-up calculation.
    store = _store(tmp_path)
    ids = [_seed(store, 1_000 + i * 10, motion=False, area=0.0) for i in range(12)]
    for anchor in ids:
        assert store.count_before_capped(anchor, 1_000) == store.count_in_range(until_id=anchor)
