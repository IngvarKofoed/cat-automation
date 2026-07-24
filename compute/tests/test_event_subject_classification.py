"""Tests for the event-subject-classification feature (compute/collection/store.py).

See docs/specs/2026-07-22-event-subject-classification.md. Pure-sqlite: NO
torch/ultralytics/GPU is needed anywhere here — every ``yolo-serial`` verdict is
inserted directly via ``Store.write_analysis`` with a crafted ``detail['boxes']``,
mirroring test_identification_store.py's ``_boxes_detail`` pattern. Mirrors the
suite's other conventions (test_events.py / test_identification_store.py): a
``_frame()``/``_add()`` helper builds ``StreamFrame``s directly, and a ``_store``
factory opens a Store under ``tmp_path``.

Covers:
- events()'s subject ladder (cat / person / bird / unrecognized / motion_only),
  including cat-always-wins precedence and identity staying intact alongside it.
- The preserved box-reading contract: a person-only detail has NO cat box
  (_best_box -> None, verdict/score would be cat-only), a coexisting cat+person
  detail still yields the cat box, and a legacy 5-element box is still a cat.
- Store.labeled_cat_motion_floor(): learns a floor from labelled cat visits,
  ignores evicted frames, and returns None with too few visits.
- events()'s floor fallback to _SUBJECT_FLOOR_DEFAULT when the active model
  carries no subject_floor in its metrics.
"""
from __future__ import annotations

import os

import pytest

from compute.collection.store import (
    Store,
    _VISIT_GAP_MS,
    _SUBJECT_FLOOR_DEFAULT,
    _ANNOTATE_MIN_CONF,
    _CORRUPTION_ANALYZER,
)
from compute.ingest import StreamFrame
from shared.wire import StreamFrameMeta

_JPEG_BODY = b"\xff\xd8\xff\xe0" + b"fake-jpeg-body" + b"\xff\xd9"

_CAT, _PERSON, _BIRD = 15, 0, 14


def _frame(frame_id: int = 1, ts: int = 1_000, motion: bool = False, area: float = 0.0, bbox=None) -> StreamFrame:
    meta = StreamFrameMeta(frame_id=frame_id, ts=ts, motion=motion, bbox=bbox, area=area)
    return StreamFrame(meta, _JPEG_BODY)


def _store(tmp_path) -> Store:
    return Store(
        db_path=str(tmp_path / "index.db"),
        media_root=str(tmp_path / "media"),
        max_bytes=10_000_000,
    )


def _add(store: Store, recv_ts_ms: int, *, motion: bool = False, area: float = 0.0, edge_id: int = 1) -> int:
    """Add one frame at ``recv_ts_ms`` and return its store row id."""
    return store.add(_frame(frame_id=edge_id, ts=recv_ts_ms, motion=motion, area=area), recv_ts_ms=recv_ts_ms)


def _one_event_ids(store: Store, base: int, n: int, area: float = 0.1) -> "list[int]":
    """Add ``n`` motion frames close in time (one event) -> their store ids."""
    return [_add(store, base + 100 * i, motion=True, area=area) for i in range(n)]


def _boxes_detail(boxes: "list[list[float]]") -> dict:
    """A yolo-serial-shaped analysis.detail: ``{"boxes": [[x1,y1,x2,y2,conf,cls], ...]}``."""
    return {"boxes": boxes}


def _write_gallery_file(store: Store, gallery_dir: str) -> str:
    d = os.path.join(store.models_root, gallery_dir)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "gallery.npz")
    with open(path, "wb") as fh:
        fh.write(b"\x00")
    return path


def _add_version(
    store: Store,
    *,
    status: str = "draft",
    gallery_dir: str = "g",
    threshold: "float | None" = 0.5,
    write_file: bool = True,
    n_cats: int = 2,
    n_vectors: int = 10,
    quality: str = "gallery",
    metrics: "dict | None" = None,
) -> int:
    if write_file:
        _write_gallery_file(store, gallery_dir)
    return store.add_model_version(
        status=status,
        kind="gallery",
        backbone="dinov2_vits14",
        imgsz=224,
        n_cats=n_cats,
        n_vectors=n_vectors,
        threshold=threshold,
        quality=quality,
        metrics=metrics,
        gallery_dir=gallery_dir,
    )


# --- events() subject ladder -------------------------------------------------


def test_subject_cat_present_wins_even_with_person_present(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    (f1,) = _one_event_ids(store, base, 1)
    # A single frame with BOTH a person box and a cat box: cat must win precedence.
    store.write_analysis(
        f1, "yolo-serial", True, 0.9,
        _boxes_detail([[0, 0, 5, 5, 0.9, _PERSON], [1, 1, 4, 4, 0.85, _CAT]]),
    )
    ev = store.events(None, None)["events"][0]
    assert ev["subject"] == {"kind": "cat"}


def test_subject_cat_present_identity_still_populated(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    vid = _add_version(store, threshold=0.5)
    store.promote_model(vid)
    cat = store.create_cat("Mittens", is_resident=True)["id"]
    (f1,) = _one_event_ids(store, base, 1)
    store.write_analysis(f1, "yolo-serial", True, 0.9, _boxes_detail([[0, 0, 4, 4, 0.9, _CAT]]))
    store.write_identifications_batch([(f1, vid, cat, 0.1, [0, 0, 1, 1])])

    ev = store.events(None, None)["events"][0]
    assert ev["subject"] == {"kind": "cat"}
    assert ev["identity"] is not None and ev["identity"]["cat_id"] == cat


def test_subject_promoted_to_cat_by_named_identity_below_conf_floor(tmp_path):
    # A real resident whose only cat box sat in the recall-first [0.15, 0.3) band: the
    # subject ladder alone can't confirm 'cat' (< _ANNOTATE_MIN_CONF), but a confident
    # NAMED gallery match (distance <= threshold) must PROMOTE the subject to 'cat' so
    # the resident is never hidden behind a motion chip.
    store = _store(tmp_path)
    base = 1_700_000_000_000
    vid = _add_version(store, threshold=0.5)
    store.promote_model(vid)
    cat = store.create_cat("Mittens", is_resident=True)["id"]
    (f1,) = _one_event_ids(store, base, 1)
    low = _ANNOTATE_MIN_CONF - 0.1  # a cat box below the ladder's present-floor
    store.write_analysis(f1, "yolo-serial", True, low, _boxes_detail([[0, 0, 4, 4, low, _CAT]]))
    store.write_identifications_batch([(f1, vid, cat, 0.1, [0, 0, 1, 1])])

    ev = store.events(None, None)["events"][0]
    assert ev["subject"] == {"kind": "cat"}
    assert ev["identity"]["cat_id"] == cat and ev["identity"]["is_resident"] is True


def test_subject_not_promoted_by_unknown_cat_identity(tmp_path):
    # An "unknown cat" identity (a far match, cat_id null) at low box confidence may be
    # an empty-scene phantom — it must NOT promote the subject to 'cat' (stays phantom-safe).
    store = _store(tmp_path)
    base = 1_700_000_000_000
    vid = _add_version(store, threshold=0.5)
    store.promote_model(vid)
    cat = store.create_cat("Mittens", is_resident=True)["id"]
    (f1,) = _one_event_ids(store, base, 1)  # default area 0.1 >= floor -> unrecognized
    low = _ANNOTATE_MIN_CONF - 0.1
    store.write_analysis(f1, "yolo-serial", True, low, _boxes_detail([[0, 0, 4, 4, low, _CAT]]))
    # distance 0.9 > threshold 0.5 -> _aggregate_identity yields an unknown cat (cat_id None).
    store.write_identifications_batch([(f1, vid, cat, 0.9, [0, 0, 1, 1])])

    ev = store.events(None, None)["events"][0]
    assert ev["identity"]["cat_id"] is None          # unknown cat
    assert ev["subject"]["kind"] == "unrecognized"   # NOT promoted to cat


def test_subject_person_only(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    (f1,) = _one_event_ids(store, base, 1)
    store.write_analysis(f1, "yolo-serial", False, 0.0, _boxes_detail([[0, 0, 5, 5, 0.8, _PERSON]]))
    ev = store.events(None, None)["events"][0]
    assert ev["subject"]["kind"] == "person"
    assert ev["subject"]["conf"] == pytest.approx(0.8)
    # No cat detected -> identity join finds nothing either.
    assert ev["identity"] is None


def test_subject_bird_only(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    (f1,) = _one_event_ids(store, base, 1)
    store.write_analysis(f1, "yolo-serial", False, 0.0, _boxes_detail([[0, 0, 5, 5, 0.5, _BIRD]]))
    ev = store.events(None, None)["events"][0]
    assert ev["subject"]["kind"] == "bird"
    assert ev["subject"]["conf"] == pytest.approx(0.5)


def test_subject_no_yolo_rows_above_floor_is_unrecognized(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    # No yolo-serial analysis at all; area comfortably above the default floor.
    _one_event_ids(store, base, 3, area=_SUBJECT_FLOOR_DEFAULT["min_area"] * 10)
    ev = store.events(None, None)["events"][0]
    assert ev["subject"]["kind"] == "unrecognized"
    assert ev["subject"]["peak_area"] == pytest.approx(_SUBJECT_FLOOR_DEFAULT["min_area"] * 10)
    assert ev["subject"]["n_frames"] == 3


def test_subject_below_floor_is_motion_only(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    # A single frame, area far below the default floor and n_frames < min_frames.
    (f1,) = _one_event_ids(store, base, 1, area=_SUBJECT_FLOOR_DEFAULT["min_area"] / 10)
    ev = store.events(None, None)["events"][0]
    assert ev["subject"]["kind"] == "motion_only"
    assert ev["subject"]["n_frames"] == 1


def test_subject_class_below_annotate_min_conf_does_not_count_as_present(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    # A single low area/frame-count frame -> would be motion_only, but give it a
    # cat box BELOW _ANNOTATE_MIN_CONF: must not promote to 'cat'.
    (f1,) = _one_event_ids(store, base, 1, area=_SUBJECT_FLOOR_DEFAULT["min_area"] / 10)
    low_conf = _ANNOTATE_MIN_CONF - 0.05
    assert low_conf >= 0.0
    store.write_analysis(f1, "yolo-serial", False, 0.0, _boxes_detail([[0, 0, 4, 4, low_conf, _CAT]]))
    ev = store.events(None, None)["events"][0]
    assert ev["subject"]["kind"] == "motion_only"


def test_subject_merges_max_confidence_across_span_frames(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    ids = _one_event_ids(store, base, 2)
    store.write_analysis(ids[0], "yolo-serial", False, 0.0, _boxes_detail([[0, 0, 4, 4, 0.31, _PERSON]]))
    store.write_analysis(ids[1], "yolo-serial", False, 0.0, _boxes_detail([[0, 0, 4, 4, 0.77, _PERSON]]))
    ev = store.events(None, None)["events"][0]
    assert ev["subject"] == {"kind": "person", "conf": pytest.approx(0.77)}


# --- `corrupted` rung + per-visit detection aggregates ----------------------
# (visit-detection-aggregates spec, docs/specs/2026-07-23-visit-detection-aggregates.md)


def test_subject_corrupted_when_no_detection_and_corruption_present(tmp_path):
    # A visit that would be 'unrecognized' (area above floor) with NO YOLO detection but
    # a corruption verdict on one of its frames files as 'corrupted' — the new rung
    # between bird and unrecognized.
    store = _store(tmp_path)
    base = 1_700_000_000_000
    ids = _one_event_ids(store, base, 2, area=_SUBJECT_FLOOR_DEFAULT["min_area"] * 10)
    store.write_analysis(ids[0], _CORRUPTION_ANALYZER, True, None, {"reason": "cast"})
    ev = store.events(None, None)["events"][0]
    assert ev["subject"] == {"kind": "corrupted"}


def test_subject_cat_in_corrupt_frame_stays_cat(tmp_path):
    # corrupted fires only when YOLO detected NOTHING, so a cat box (>= floor) sharing a
    # frame with a corruption verdict still wins — the corruption fail-safe is preserved.
    store = _store(tmp_path)
    base = 1_700_000_000_000
    (f1,) = _one_event_ids(store, base, 1)
    store.write_analysis(f1, "yolo-serial", True, 0.9, _boxes_detail([[0, 0, 4, 4, 0.9, _CAT]]))
    store.write_analysis(f1, _CORRUPTION_ANALYZER, True, None, {"reason": "line"})
    ev = store.events(None, None)["events"][0]
    assert ev["subject"] == {"kind": "cat"}


def test_subject_no_detection_no_corruption_falls_back_to_unrecognized(tmp_path):
    # Coverage fallback: no YOLO detection AND no corruption verdict over the span → the
    # ladder falls through the (un-fired) corrupted rung to unrecognized. Never crashes.
    store = _store(tmp_path)
    base = 1_700_000_000_000
    _one_event_ids(store, base, 2, area=_SUBJECT_FLOOR_DEFAULT["min_area"] * 10)
    ev = store.events(None, None)["events"][0]
    assert ev["subject"]["kind"] == "unrecognized"


def test_subject_corruption_verdict_zero_does_not_trigger_corrupted(tmp_path):
    # A corruption row that says NOT corrupt (verdict=0) must not fire the rung — the
    # lookup filters verdict=1.
    store = _store(tmp_path)
    base = 1_700_000_000_000
    ids = _one_event_ids(store, base, 2, area=_SUBJECT_FLOOR_DEFAULT["min_area"] * 10)
    store.write_analysis(ids[0], _CORRUPTION_ANALYZER, False, None, {"reason": None})
    ev = store.events(None, None)["events"][0]
    assert ev["subject"]["kind"] == "unrecognized"


def test_subject_corruption_anywhere_in_visit_span_triggers_corrupted(tmp_path):
    # A corruption verdict on ANY frame in the visit's id span [start_id, end_id] — even a
    # NON-motion frame between the motion frames — files a no-detection visit as `corrupted`.
    # Deliberately span-based (not motion-filtered): a glitch anywhere in the visit's window
    # is enough; a corruption-adjacent missed cat landing here is an accepted loss.
    store = _store(tmp_path)
    base = 1_700_000_000_000
    above = _SUBJECT_FLOOR_DEFAULT["min_area"] * 10
    a = _add(store, base + 0, motion=True, area=above)
    d = _add(store, base + 50, motion=False, area=0.0)  # in span [a, c], NON-motion
    _c = _add(store, base + 100, motion=True, area=above)
    store.write_analysis(d, _CORRUPTION_ANALYZER, True, None, {"reason": "cast"})
    ev = store.events(None, None)["events"][0]
    assert ev["subject"] == {"kind": "corrupted"}


def test_detection_aggregates_over_motion_frames_raw_no_floor(tmp_path):
    # Three motion frames (A sub-0.3 cat, B person 0.8, C nothing) + one NON-motion frame
    # D (strong cat box) whose id falls inside the span. The aggregates: count only motion
    # frames with a detection (A, B), over the visit's motion-frame count (3), and record
    # RAW confidences (A's 0.2 counts despite being below _ANNOTATE_MIN_CONF). D's 0.95
    # must be excluded from the ratio (non-motion).
    store = _store(tmp_path)
    base = 1_700_000_000_000
    a = _add(store, base + 0, motion=True, area=0.1)
    b = _add(store, base + 100, motion=True, area=0.1)
    d = _add(store, base + 150, motion=False, area=0.1)  # in span, must NOT count
    c = _add(store, base + 200, motion=True, area=0.1)
    low = _ANNOTATE_MIN_CONF - 0.1
    store.write_analysis(a, "yolo-serial", False, 0.0, _boxes_detail([[0, 0, 4, 4, low, _CAT]]))
    store.write_analysis(b, "yolo-serial", False, 0.0, _boxes_detail([[0, 0, 4, 4, 0.8, _PERSON]]))
    store.write_analysis(d, "yolo-serial", True, 0.95, _boxes_detail([[0, 0, 4, 4, 0.95, _CAT]]))
    # C: no yolo-serial row at all -> no detection.
    ev = store.events(None, None)["events"][0]
    assert ev["n_frames"] == 3  # A, B, C clustered; D (non-motion) not a member
    det = ev["detection"]
    assert det["ratio"] == pytest.approx(2 / 3)          # A + B detected, C none; over 3 motion frames
    assert det["conf_max"] == pytest.approx(0.8)          # max(0.2, 0.8)
    assert det["conf_mean"] == pytest.approx((low + 0.8) / 2)


def test_detection_aggregates_unmeasured_visit_is_null(tmp_path):
    # An UN-SWEPT visit (no yolo-serial rows on its motion frames) → "not measured":
    # ratio is null (not 0.0), so a consumer can't mistake it for a measured miss.
    store = _store(tmp_path)
    base = 1_700_000_000_000
    _one_event_ids(store, base, 2, area=0.1)
    ev = store.events(None, None)["events"][0]
    assert ev["detection"] == {"ratio": None, "conf_max": None, "conf_mean": None}


def test_detection_aggregates_measured_miss_is_zero(tmp_path):
    # A visit whose motion frames WERE swept but YOLO detected nothing (empty boxes) →
    # a real miss: ratio 0.0 (distinct from the unmeasured null above), confs null.
    store = _store(tmp_path)
    base = 1_700_000_000_000
    ids = _one_event_ids(store, base, 2, area=0.1)
    for fid in ids:
        store.write_analysis(fid, "yolo-serial", False, 0.0, _boxes_detail([]))
    ev = store.events(None, None)["events"][0]
    assert ev["detection"] == {"ratio": 0.0, "conf_max": None, "conf_mean": None}


# --- Preserved box-reading contract (scorecard / identify path) -------------


def test_yolo_row_with_person_box_only_has_no_cat_box_and_verdict_zero(tmp_path):
    # A yolo-serial analysis row whose detail carries a person box but no cat box:
    # verdict/score stay 0 (cat-only), and _best_box (used by identify/annotation)
    # must return None -- the identify path must never treat this as a cat frame.
    store = _store(tmp_path)
    base = 1_700_000_000_000
    (f1,) = _one_event_ids(store, base, 1)
    store.write_analysis(f1, "yolo-serial", False, 0.0, _boxes_detail([[0, 0, 5, 5, 0.9, _PERSON]]))
    row = store._conn.execute(
        "SELECT verdict, score, detail FROM analysis WHERE frame_id = ?", (f1,)
    ).fetchone()
    assert row[0] == 0
    assert row[1] == 0.0
    assert Store._best_box(row[2]) is None


def test_best_box_returns_cat_box_when_person_and_cat_coexist(tmp_path):
    detail = _boxes_detail(
        [[0, 0, 5, 5, 0.95, _PERSON], [1, 1, 4, 4, 0.5, _CAT], [2, 2, 3, 3, 0.99, _BIRD]]
    )
    box, conf = Store._best_box(__import__("json").dumps(detail))
    assert conf == pytest.approx(0.5)
    assert box == [1.0, 1.0, 4.0, 4.0]


def test_best_box_treats_legacy_5_element_box_as_cat(tmp_path):
    import json

    # Old-format row: no class tag, 5-element box -- must still resolve as a cat.
    detail = {"boxes": [[0, 0, 4, 4, 0.7]]}
    box, conf = Store._best_box(json.dumps(detail))
    assert conf == pytest.approx(0.7)
    assert box == [0.0, 0.0, 4.0, 4.0]


def test_best_box_none_when_only_non_cat_classes_present(tmp_path):
    import json

    detail = {"boxes": [[0, 0, 5, 5, 0.9, _PERSON], [0, 0, 5, 5, 0.9, _BIRD]]}
    assert Store._best_box(json.dumps(detail)) is None


def test_subject_classes_reads_all_classes_with_per_class_max(tmp_path):
    import json

    detail = {
        "boxes": [
            [0, 0, 4, 4, 0.3, _CAT],
            [0, 0, 4, 4, 0.6, _CAT],
            [0, 0, 4, 4, 0.4, _PERSON],
        ]
    }
    classes = Store._subject_classes(json.dumps(detail))
    assert classes == {_CAT: pytest.approx(0.6), _PERSON: pytest.approx(0.4)}


def test_subject_classes_legacy_5_element_box_counts_as_cat(tmp_path):
    import json

    detail = {"boxes": [[0, 0, 4, 4, 0.55]]}
    assert Store._subject_classes(json.dumps(detail)) == {15: pytest.approx(0.55)}


def test_subject_classes_tolerant_of_missing_or_malformed_detail(tmp_path):
    assert Store._subject_classes(None) == {}
    assert Store._subject_classes("") == {}
    assert Store._subject_classes("not json") == {}
    assert Store._subject_classes('{"boxes": null}') == {}


# --- labeled_cat_motion_floor() ---------------------------------------------


def _label_visit(store: Store, frame_ids: "list[int]", cat_id: int) -> None:
    store.add_dataset_items(
        [
            {
                "frame_id": fid,
                "label_kind": "identified",
                "cat_id": cat_id,
                "quality": "gallery",
            }
            for fid in frame_ids
        ]
    )


def test_labeled_cat_motion_floor_returns_none_with_too_few_visits(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    cat = store.create_cat("A")["id"]
    # Only 2 visits -- below the n_visits >= 3 gate.
    for i in range(2):
        ids = _one_event_ids(store, base + i * 10 * _VISIT_GAP_MS, 2, area=0.2)
        _label_visit(store, ids, cat)
    assert store.labeled_cat_motion_floor() is None


def test_labeled_cat_motion_floor_returns_dict_with_enough_visits(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    cat = store.create_cat("A")["id"]
    for i in range(4):
        ids = _one_event_ids(store, base + i * 10 * _VISIT_GAP_MS, 3, area=0.1 * (i + 1))
        _label_visit(store, ids, cat)
    floor = store.labeled_cat_motion_floor()
    assert floor is not None
    assert floor["source"] == "labeled"
    assert floor["n_visits"] == 4
    assert floor["min_area"] >= 0.0
    assert floor["min_frames"] >= 1


def test_labeled_cat_motion_floor_skips_evicted_frames(tmp_path):
    # Cap sized to hold only a handful of frames, so early-labelled visits get
    # evicted by the time we ask for the floor.
    store = Store(
        db_path=str(tmp_path / "index.db"),
        media_root=str(tmp_path / "media"),
        max_bytes=6 * len(_JPEG_BODY),
    )
    base = 1_700_000_000_000
    cat = store.create_cat("A")["id"]
    # Three visits of 2 frames each = 6 frames -- fits the cap exactly.
    all_ids = []
    for i in range(3):
        ids = _one_event_ids(store, base + i * 10 * _VISIT_GAP_MS, 2, area=0.2)
        all_ids.append(ids)
        _label_visit(store, ids, cat)
    assert store.labeled_cat_motion_floor()["n_visits"] == 3

    # One more visit evicts the OLDEST visit's frames (oldest-first eviction).
    ids4 = _one_event_ids(store, base + 30 * _VISIT_GAP_MS, 2, area=0.2)
    _label_visit(store, ids4, cat)

    # The first visit's frames are gone; its label row survives (dataset_items is
    # eviction-proof) but no longer contributes motion data to the floor.
    survivors = {int(r[0]) for r in store._conn.execute("SELECT id FROM frames").fetchall()}
    assert not set(all_ids[0]) & survivors

    floor = store.labeled_cat_motion_floor()
    # Still >= 3 visits survive (visits 2, 3, 4), since only visit 1 was evicted.
    assert floor is not None
    assert floor["n_visits"] == 3


# --- events() falls back to the default floor -------------------------------


def test_events_uses_default_floor_when_no_active_model(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    (f1,) = _one_event_ids(store, base, 1, area=_SUBJECT_FLOOR_DEFAULT["min_area"] * 10)
    ev = store.events(None, None)["events"][0]
    assert ev["subject"]["kind"] == "unrecognized"


def test_events_uses_default_floor_when_active_model_metrics_has_no_subject_floor(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    # Active model exists but its metrics carry no subject_floor key.
    vid = _add_version(store, metrics={"per_cat": []})
    store.promote_model(vid)
    (f1,) = _one_event_ids(store, base, 1, area=_SUBJECT_FLOOR_DEFAULT["min_area"] * 10)
    ev = store.events(None, None)["events"][0]
    assert ev["subject"]["kind"] == "unrecognized"


def test_events_uses_learned_floor_from_active_model_metrics(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    # A tight learned floor (higher than default) -- an event that clears the
    # DEFAULT floor but NOT this learned one must classify as motion_only.
    learned_floor = {"min_area": 0.5, "min_frames": 50, "n_visits": 10, "source": "labeled"}
    vid = _add_version(store, metrics={"subject_floor": learned_floor})
    store.promote_model(vid)

    area = _SUBJECT_FLOOR_DEFAULT["min_area"] * 10  # clears default, not the learned floor
    assert area < learned_floor["min_area"]
    (f1,) = _one_event_ids(store, base, 1, area=area)
    ev = store.events(None, None)["events"][0]
    assert ev["subject"]["kind"] == "motion_only"


def test_events_partial_stamped_floor_fills_missing_key_from_default(tmp_path):
    # A stamped subject_floor missing a key (here min_frames) must NOT crash events():
    # the resolver merges it over _SUBJECT_FLOOR_DEFAULT, so the missing key falls back
    # rather than KeyError-ing the whole feed. min_area from the stamp is honoured; the
    # default min_frames (2) fills the gap. One frame, area below the stamped min_area
    # and n_frames (1) below the default min_frames -> motion_only, no exception.
    store = _store(tmp_path)
    base = 1_700_000_000_000
    vid = _add_version(store, metrics={"subject_floor": {"min_area": 0.5}})
    store.promote_model(vid)
    (f1,) = _one_event_ids(store, base, 1, area=0.1)  # < 0.5 stamped min_area; 1 < default min_frames
    ev = store.events(None, None)["events"][0]
    assert ev["subject"]["kind"] == "motion_only"
