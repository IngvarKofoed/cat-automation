"""Tests for the user app's one-tap confirm — ``Store.visit_label_state`` plus
``GET``/``POST /api/label/visit``.

See docs/specs/2026-08-13-user-app-visit-labelling.md. What earns a test here is the
set of things that can only go wrong SILENTLY, since the write lands behind the reader:

1. A CONTESTED span (two cats identified in one visit) must be refused.
   ``_aggregate_identity`` returns one winner and its result cannot distinguish "the
   other frames were too far" from "the other frames named another cat", so nothing
   downstream could notice one cat's crops filed under another cat's name.
2. The probe and the write must refuse under the SAME conditions — the whole reason the
   rule lives in one store method.
3. A PART-LABELLED span stays confirmable for its undecided remainder: event spans grow
   (changelog 224), so this is the routine state of any visit that lingered.
4. ``inserted == 0`` keeps the ⚑, because that is the ordinary aged-out path.
5. The rows carry ``source='user-confirm'``, which is what makes the channel auditable
   and excludable later.

No torch and no model artifact beyond a stub gallery.npz, but the frames carry REAL
JPEG bytes (test_user_dashboard.py's ``_real_jpeg`` convention): a confirm writes
``identified`` rows, and ``_commit_label`` cuts a crop per frame first — so with an
undecodable body every row is skipped and ``inserted`` is 0 whatever the route does.
"""
from __future__ import annotations

import os

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from compute.collection.store import Store

_CAT = 15  # COCO class id


def _real_jpeg() -> bytes:
    """A genuine 32x32 JPEG — decodable, and big enough for the 10x10 box below."""
    ok, buf = cv2.imencode(".jpg", np.full((32, 32, 3), 128, dtype=np.uint8))
    assert ok
    return buf.tobytes()


_JPEG_BODY = _real_jpeg()


def _frame(frame_id: int, ts: int, *, motion: bool = True):
    from compute.ingest import StreamFrame
    from shared.wire import StreamFrameMeta

    meta = StreamFrameMeta(frame_id=frame_id, ts=ts, motion=motion, bbox=None, area=0.5)
    return StreamFrame(meta, _JPEG_BODY)


def _store(tmp_path) -> Store:
    return Store(
        db_path=str(tmp_path / "index.db"),
        media_root=str(tmp_path / "media"),
        max_bytes=10_000_000,
    )


_next_edge_id = [0]


def _visit(store: Store, at_ts: int, *, n: int = 3) -> "tuple[int, int]":
    """Add one cluster of ``n`` frames → its (start_id, end_id). Chronological order."""
    ids = []
    for i in range(n):
        _next_edge_id[0] += 1
        ids.append(store.add(_frame(_next_edge_id[0], at_ts + i * 200), recv_ts_ms=at_ts + i * 200))
    return ids[0], ids[-1]


def _cat_box(store: Store, span, conf: float = 0.9) -> None:
    for fid in range(span[0], span[1] + 1):
        store.write_analysis(
            fid, "yolo-serial", True, conf, {"boxes": [[0, 0, 10, 10, conf, _CAT]]}
        )


def _version(store: Store, *, threshold: "float | None" = 0.5) -> int:
    d = os.path.join(store.models_root, "g")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "gallery.npz"), "wb") as fh:
        fh.write(b"\x00")
    vid = store.add_model_version(
        status="draft", kind="gallery", backbone="dinov2_vits14", imgsz=224,
        n_cats=2, n_vectors=10, threshold=threshold, quality="gallery",
        metrics=None, gallery_dir="g",
    )
    store.promote_model(vid)
    return vid


def _name(store: Store, frame_ids, vid: int, cat_id: int, dist: float = 0.1) -> None:
    store.write_identifications_batch([(fid, vid, cat_id, dist, [0, 0, 1, 1]) for fid in frame_ids])


def _client(tmp_path) -> "tuple[TestClient, Store]":
    from compute.api.app import create_app

    store = _store(tmp_path)
    app = create_app(store=store, start_collector=False)
    return TestClient(app), store


def _rows(store: Store) -> "list[dict]":
    """Every ``dataset_items`` row — read directly, since no public reader returns
    ``source`` and provenance is exactly what these tests are checking."""
    with store._lock:
        rows = store._conn.execute(
            "SELECT src_frame_id, label_kind, cat_id, quality, source FROM dataset_items"
            " ORDER BY src_frame_id"
        ).fetchall()
    return [
        {"frame_id": r[0], "label_kind": r[1], "cat_id": r[2], "quality": r[3], "source": r[4]}
        for r in rows
    ]


def _named_visit(store, *, threshold=0.5, dist=0.1, resident=True, n=3):
    """A confirmable visit: boxes, an active model, one cat named on every frame."""
    vid = _version(store, threshold=threshold)
    cat = store.create_cat("Mittens", is_resident=resident)["id"]
    span = _visit(store, 1_000_000, n=n)
    _cat_box(store, span)
    _name(store, range(span[0], span[1] + 1), vid, cat, dist=dist)
    return span, cat, vid


# --- 1. the contested guard ------------------------------------------------


def test_two_cats_in_one_span_refuses_the_confirmation(tmp_path):
    """The guard that has no downstream backstop.

    `_aggregate_identity` picks the cat with the most below-threshold frames and its
    return says nothing about the runner-up, so a one-tap confirm over the whole span
    would file the second cat's crops under the winner's name with nothing able to
    notice. Tailgating is expected at this door (changelog 319).
    """
    store = _store(tmp_path)
    vid = _version(store)
    mittens = store.create_cat("Mittens", is_resident=True)["id"]
    sultan = store.create_cat("Store Sultan", is_resident=False)["id"]
    span = _visit(store, 1_000_000, n=4)
    _cat_box(store, span)
    # Mittens wins the vote 3-1 — exactly the shape that looks decided from outside.
    _name(store, [span[0], span[0] + 1, span[0] + 2], vid, mittens, dist=0.1)
    _name(store, [span[1]], vid, sultan, dist=0.2)

    state = store.visit_label_state("yolo-serial", *span)

    assert state["can_confirm"] is False
    assert state["reason"] == "contested"
    assert state["contested_cat_ids"] == sorted([mittens, sultan])
    # The aggregate still names a winner — which is precisely why the spread is needed.
    assert state["identity"]["cat_id"] == mittens


def test_a_second_cat_above_threshold_is_not_contested(tmp_path):
    """Only BELOW-threshold votes contest: a far match is the 'too far' case, not a rival.

    Without this the guard would refuse ordinary visits — a single cat's span routinely
    carries a stray far match from another gallery vector.
    """
    store = _store(tmp_path)
    vid = _version(store, threshold=0.5)
    mittens = store.create_cat("Mittens", is_resident=True)["id"]
    sultan = store.create_cat("Store Sultan", is_resident=False)["id"]
    span = _visit(store, 1_000_000, n=3)
    _cat_box(store, span)
    _name(store, [span[0], span[0] + 1], vid, mittens, dist=0.1)
    _name(store, [span[1]], vid, sultan, dist=0.9)   # above 0.5 — too far to count

    state = store.visit_label_state("yolo-serial", *span)

    assert state["contested_cat_ids"] == []
    assert state["can_confirm"] is True
    assert state["identity"]["cat_id"] == mittens


def test_uncalibrated_model_is_unnamed_not_contested(tmp_path):
    """A NULL threshold degrades to *unknown cat*, so there is no name to say yes to.

    The open-set fail-safe, and it must not be reported as a contest: two cats both
    'below' nothing is not a rivalry, it is an uncalibrated gallery.
    """
    store = _store(tmp_path)
    vid = _version(store, threshold=None)
    a = store.create_cat("Mittens", is_resident=True)["id"]
    b = store.create_cat("Store Sultan", is_resident=False)["id"]
    span = _visit(store, 1_000_000, n=2)
    _cat_box(store, span)
    _name(store, [span[0]], vid, a)
    _name(store, [span[1]], vid, b)

    state = store.visit_label_state("yolo-serial", *span)

    assert state["reason"] == "unnamed"
    assert state["contested_cat_ids"] == []


# --- 2. the reason ladder --------------------------------------------------


def test_no_crop_when_every_box_is_below_the_queue_floor(tmp_path):
    """Named for what is MISSING, not "nothing was detected".

    `_present_frames` floors at _ANNOTATE_MIN_CONF, so a span of faint boxes has no crop
    to label while the detector plainly did look. Calling that 'no detection' would send
    the reader to Analyse, which re-runs the detector and finds the same faint boxes.
    """
    store = _store(tmp_path)
    span, _cat, _vid = _named_visit(store)
    # Re-write every verdict below the floor.
    for fid in range(span[0], span[1] + 1):
        store.write_analysis(
            fid, "yolo-serial", True, 0.2, {"boxes": [[0, 0, 10, 10, 0.2, _CAT]]}
        )

    state = store.visit_label_state("yolo-serial", *span)

    assert state["reason"] == "no_crop"
    assert state["n_present"] == 0


def test_a_retired_cat_cannot_be_confirmed(tmp_path):
    """The feed still shows a retired cat's name (a gallery can predate its retirement),
    but the desk's picker offers only active cats and no build enrols a retired one
    (changelog 335) — so a confirm would write a label nothing will ever read.
    """
    store = _store(tmp_path)
    span, cat, _vid = _named_visit(store)
    store.update_cat(cat, {"active": False})

    state = store.visit_label_state("yolo-serial", *span)

    assert state["reason"] == "retired"
    assert state["identity"]["cat_id"] == cat   # still named — that is the trap


def test_no_model_reads_unnamed_with_no_identity(tmp_path):
    store = _store(tmp_path)
    span = _visit(store, 1_000_000)
    _cat_box(store, span)

    state = store.visit_label_state("yolo-serial", *span)

    assert state == {
        "can_confirm": False,
        "reason": "unnamed",
        "identity": None,
        "contested_cat_ids": [],
        "has_model": False,
        "n_present": 3,
        "n_undecided": 3,
        "existing": [],
    }


# --- 3. the part-labelled span --------------------------------------------


def test_a_part_labelled_span_stays_confirmable_for_the_remainder(tmp_path):
    """Event spans GROW (changelog 224), so this is routine, not an edge.

    Gating `can_confirm` on `existing` being empty would strand the late frames of every
    confirmed visit — and it strands the most frames on the visits that lingered longest.
    """
    client, store = _client(tmp_path)
    span, cat, _vid = _named_visit(store, n=4)

    # Label the first two frames only — a previous gesture over a shorter span.
    first = client.post(
        "/api/label",
        json={
            "decision": "identified",
            "cat_id": cat,
            "frames": [{"frame_id": span[0], "bbox": [0, 0, 10, 10], "quality": "ok"},
                       {"frame_id": span[0] + 1, "bbox": [0, 0, 10, 10], "quality": "ok"}],
        },
    )
    assert first.json()["inserted"] == 2

    state = store.visit_label_state("yolo-serial", *span)
    assert state["can_confirm"] is True, "the undecided tail must stay confirmable"
    assert state["n_undecided"] == 2
    assert state["existing"] == [
        {"label_kind": "identified", "cat_id": cat, "cat_name": "Mittens", "n_frames": 2}
    ]

    # Confirming writes ONLY the remainder — add_dataset_items skips the decided pair.
    res = client.post(
        "/api/label/visit",
        json={"start_id": span[0], "end_id": span[1], "cat_id": cat},
    )
    assert res.status_code == 200
    assert res.json()["inserted"] == 2
    assert store.visit_label_state("yolo-serial", *span)["reason"] == "all_labelled"


# --- 4. the write path ----------------------------------------------------


def test_confirm_writes_the_span_and_stamps_user_confirm(tmp_path):
    client, store = _client(tmp_path)
    span, cat, _vid = _named_visit(store)

    res = client.post(
        "/api/label/visit",
        json={"start_id": span[0], "end_id": span[1], "cat_id": cat},
    )

    assert res.status_code == 200
    body = res.json()
    assert body["inserted"] == 3 and body["cat_id"] == cat and body["cat_name"] == "Mittens"
    sources = {row["source"] for row in _rows(store)}
    assert sources == {"user-confirm"}, "provenance is what makes the channel auditable"


def test_confirming_a_different_cat_than_the_span_resolves_is_refused(tmp_path):
    """`cat_id` is a concurrency check, not an instruction.

    The threshold is applied at READ time and restates history when changed (changelog
    425), so the name a phone displayed can differ from the name the server would give
    the same span a moment later — and "yes" has to mean yes to what was on screen.
    """
    client, store = _client(tmp_path)
    span, cat, _vid = _named_visit(store)
    other = store.create_cat("Store Sultan", is_resident=False)["id"]

    res = client.post(
        "/api/label/visit",
        json={"start_id": span[0], "end_id": span[1], "cat_id": other},
    )

    assert res.status_code == 409
    assert isinstance(res.json()["detail"], str), "the phone renders `detail` as a string"
    assert "Mittens" in res.json()["detail"]
    assert _rows(store) == []


def test_the_probe_and_the_write_refuse_together(tmp_path):
    """One rule, two callers. A probe that permits what the write refuses would draw a
    button the server then rejects — the failure lands behind the reader, who has
    already been advanced to the next visit.
    """
    client, store = _client(tmp_path)
    vid = _version(store)
    a = store.create_cat("Mittens", is_resident=True)["id"]
    b = store.create_cat("Store Sultan", is_resident=False)["id"]
    span = _visit(store, 1_000_000, n=2)
    _cat_box(store, span)
    _name(store, [span[0]], vid, a)
    _name(store, [span[1]], vid, b)

    probe = client.get(f"/api/label/visit?start_id={span[0]}&end_id={span[1]}")
    assert probe.status_code == 200
    assert probe.json()["can_confirm"] is False

    write = client.post(
        "/api/label/visit",
        json={"start_id": span[0], "end_id": span[1], "cat_id": a},
    )
    assert write.status_code == 409
    assert "more than one cat" in write.json()["detail"]


def test_a_recorded_confirm_clears_an_overlapping_flag(tmp_path):
    client, store = _client(tmp_path)
    span, cat, _vid = _named_visit(store)
    store.add_label_flag(span[0], span[0] + 1)   # marked over a SHORTER span

    body = client.post(
        "/api/label/visit",
        json={"start_id": span[0], "end_id": span[1], "cat_id": cat},
    ).json()

    assert body["inserted"] == 3
    assert body["flag_cleared"] == 1
    assert store.list_label_flags() == []


def test_a_confirm_that_records_nothing_keeps_the_flag(tmp_path):
    """`inserted: 0` is a 200 that wrote nothing, and the mark must survive it.

    Flags are never pruned by eviction (changelog 223), so clearing one here would
    discard both the mark and the decision in silence — the work would be lost with
    nothing left pointing at it.

    Reaching that branch needs a span that is still CONFIRMABLE when the route probes it
    and yet inserts nothing, so the frame ROWS are left in place and only their JPEGs are
    removed: `_commit_label` skips a frame whose file is gone from disk, so `rows` comes
    out empty. Deleting the rows instead would 409 at `no_crop` long before the flag code
    — which is how the first version of this test passed against the bug it names.
    """
    client, store = _client(tmp_path)
    span, cat, _vid = _named_visit(store)
    store.add_label_flag(*span)
    assert store.visit_label_state("yolo-serial", *span)["can_confirm"] is True

    for _fid, (_recv_ts, path) in store.frame_sources(list(range(span[0], span[1] + 1))).items():
        os.remove(path)

    res = client.post(
        "/api/label/visit",
        json={"start_id": span[0], "end_id": span[1], "cat_id": cat},
    )

    assert res.status_code == 200, "the span was confirmable — this is not a refusal"
    assert res.json()["inserted"] == 0
    assert res.json()["flag_cleared"] == 0
    assert len(store.list_label_flags()) == 1


# --- 5. bounds ------------------------------------------------------------


def test_span_bounds_are_required_and_capped(tmp_path):
    from compute.api.app import _MAX_VISIT_SPAN

    client, store = _client(tmp_path)
    span, cat, _vid = _named_visit(store)

    # Both bounds required — an omitted one elsewhere means "whole store".
    assert client.post("/api/label/visit", json={"start_id": 1, "cat_id": cat}).status_code == 422
    assert client.get("/api/label/visit?start_id=1").status_code == 422
    # A bool must not coerce to id 1.
    assert client.post(
        "/api/label/visit", json={"start_id": True, "end_id": 5, "cat_id": cat}
    ).status_code == 422
    # Inverted, and wider than one visit.
    assert client.post(
        "/api/label/visit", json={"start_id": 9, "end_id": 2, "cat_id": cat}
    ).status_code == 400
    too_wide = client.post(
        "/api/label/visit",
        json={"start_id": 1, "end_id": 2 + _MAX_VISIT_SPAN, "cat_id": cat},
    )
    assert too_wide.status_code == 400
    assert "one visit" in too_wide.json()["detail"]


def test_every_refusal_reason_has_a_sentence(tmp_path):
    """A rung added to `visit_label_state` without wording here would reach a reader as
    a bare 409, so the two tables are pinned together.
    """
    from compute.api.app import _VISIT_LABEL_REFUSALS

    reasons = {"no_crop", "all_labelled", "unnamed", "retired", "contested"}
    assert set(_VISIT_LABEL_REFUSALS) == reasons
    assert all(isinstance(v, str) and v for v in _VISIT_LABEL_REFUSALS.values())


# --- 6. repairs from the review pass -------------------------------------


def test_a_stale_pre_clear_label_is_not_read_as_this_visits(tmp_path):
    """`existing` keys on the (src_frame_id, src_recv_ts) PAIR, not the id alone.

    `frames.id` has no AUTOINCREMENT and `clear()` deliberately spares `dataset_items`,
    so ids restart at 1 and a new visit lands on a reused range. Keyed on id alone, a
    stale pre-clear row matches by numeric coincidence and the phone reports
    "Labelled: OldCat" over a visit nobody ever labelled — in the feature whose whole
    purpose is finding mislabels. Every sibling reader keys on both columns.
    """
    store = _store(tmp_path)
    old_cat = store.create_cat("OldCat", is_resident=True)["id"]
    old_span = _visit(store, 500_000, n=2)
    _cat_box(store, old_span)
    store.add_dataset_items([
        {"frame_id": fid, "label_kind": "identified", "cat_id": old_cat, "quality": "ok",
         "bbox": [0, 0, 10, 10], "crop_path": None, "source": "detector"}
        for fid in range(old_span[0], old_span[1] + 1)
    ])
    assert len(_rows(store)) == 2

    store.clear()                     # frames go, dataset_items deliberately stay
    assert len(_rows(store)) == 2, "labels are the precious output — clear() spares them"

    # Capture resumes; ids restart at 1, so the new visit reuses the old id range.
    span, cat, _vid = _named_visit(store)
    assert span[0] == 1, "the reuse this guard is about"

    state = store.visit_label_state("yolo-serial", *span)

    assert state["existing"] == [], "a pre-clear row must not read as this visit's label"
    assert state["can_confirm"] is True
    assert state["reason"] == "ok"


def test_a_label_whose_frame_evicted_still_counts(tmp_path):
    """The other side of that guard: eviction never reuses an id, so a row whose frame is
    simply GONE is still this span's label. Dropping it would make a fully-labelled span
    read as unlabelled once its frames aged out.
    """
    client, store = _client(tmp_path)
    span, cat, _vid = _named_visit(store, n=2)
    client.post("/api/label/visit", json={"start_id": span[0], "end_id": span[1], "cat_id": cat})
    assert store.visit_label_state("yolo-serial", *span)["existing"][0]["n_frames"] == 2

    with store._lock:
        store._conn.execute("DELETE FROM frames WHERE id BETWEEN ? AND ?", span)
        store._conn.commit()

    existing = store.visit_label_state("yolo-serial", *span)["existing"]
    assert existing == [
        {"label_kind": "identified", "cat_id": cat, "cat_name": "Mittens", "n_frames": 2}
    ]


def test_an_unknown_oracle_is_refused_on_both_routes(tmp_path):
    """Matching every other oracle-taking route. An unregistered name reaches
    `_present_frames` as a predicate matching no rows, so without this a typo reads as an
    honest "no crop to label here" rather than as a bad request.
    """
    client, store = _client(tmp_path)
    span, cat, _vid = _named_visit(store)

    probe = client.get(f"/api/label/visit?start_id={span[0]}&end_id={span[1]}&oracle=nope")
    assert probe.status_code == 400 and "unknown oracle" in probe.json()["detail"]

    write = client.post(
        "/api/label/visit?oracle=nope",
        json={"start_id": span[0], "end_id": span[1], "cat_id": cat},
    )
    assert write.status_code == 400
    assert _rows(store) == [], "a refused request writes nothing"
