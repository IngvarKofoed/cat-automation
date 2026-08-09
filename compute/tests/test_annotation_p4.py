"""Tests for the admin-next P4 annotation BACKEND slice on
``compute/collection/store.py`` + the ``/api/label/*`` routes on
``compute/api/app.py``.

See docs/specs/2026-07-25-admin-next-redesign.md (Page 4 — Annotation) and
docs/NEW_ADMIN_PLAN.md (P4). Covers the four additive pieces:

1. BOUNDED queue — ``Store.annotation_queue_page`` scans only the newest N
   present-undecided frames and caps the returned visits (no whole-store scan).
2. IGNORE — an ``ignored`` ``dataset_items`` label (no crop) that marks an event
   decided so the queue drops it, reversible via delete/relabel.
3. Per-cat day/night COVERAGE — ``Store.cat_regime_coverage`` splits each cat's
   ``identified`` crops day vs night by an injected classifier.
4. Below-threshold DISTANCE-SORT — the queue orders worst-first by identification
   distance when an active model exists (never-identified events after).

Pure-sqlite where possible (no torch/CUDA/model); the crop-writing label route is
skipped when cv2 is absent. Mirrors the suite's conventions (test_annotation.py,
test_identification_store.py): a ``_frame()`` helper builds ``StreamFrame``s
directly and a bare ``gallery.npz`` FILE fakes a promotable model (the store only
checks the file's existence).
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from compute.collection.store import Store

try:
    import cv2  # noqa: F401
    import numpy as np  # noqa: F401

    _HAVE_CV = True
except Exception:  # pragma: no cover - exercised only where cv2 is absent
    _HAVE_CV = False

_requires_cv = pytest.mark.skipif(not _HAVE_CV, reason="cv2/numpy required for the crop/label route")

# A minimal valid JPEG (SOI ... EOI); written verbatim, never decoded here.
_JPEG_BODY = b"\xff\xd8\xff\xe0" + b"fake-jpeg-body" + b"\xff\xd9"


def _frame(frame_id: int = 1, ts: int = 1_000, motion: bool = False, bbox=None, area: float = 0.0):
    from compute.ingest import StreamFrame
    from shared.wire import StreamFrameMeta

    meta = StreamFrameMeta(frame_id=frame_id, ts=ts, motion=motion, bbox=bbox, area=area)
    return StreamFrame(meta, _JPEG_BODY)


def _store(tmp_path, max_bytes: int = 10_000_000) -> Store:
    return Store(
        db_path=str(tmp_path / "index.db"),
        media_root=str(tmp_path / "media"),
        max_bytes=max_bytes,
    )


def _boxes_detail(boxes):
    return {"boxes": boxes}


def _present(store: Store, recv_ts_ms: int, *, edge_id: int = 1, score: float = 0.9) -> int:
    """Add one frame at ``recv_ts_ms`` with a yolo-serial box → a queue-present frame id."""
    fid = store.add(_frame(frame_id=edge_id, ts=recv_ts_ms), recv_ts_ms=recv_ts_ms)
    store.write_analysis(fid, "yolo-serial", True, score, _boxes_detail([[0, 0, 10, 10, score]]))
    return fid


def _write_gallery_file(store: Store, gallery_dir: str = "g") -> None:
    d = os.path.join(store.models_root, gallery_dir)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "gallery.npz"), "wb") as fh:
        fh.write(b"\x00")


def _promote_model(store: Store, *, threshold: "float | None" = 0.5, gallery_dir: str = "g") -> int:
    _write_gallery_file(store, gallery_dir)
    vid = store.add_model_version(
        status="draft", kind="gallery", backbone="dinov2_vits14", imgsz=224,
        n_cats=2, n_vectors=10, threshold=threshold, quality="gallery",
        metrics=None, gallery_dir=gallery_dir,
    )
    store.promote_model(vid)
    return vid


# --- 1. BOUNDED queue ----------------------------------------------------------


def test_queue_page_returns_recent_visits_no_model(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    # Three well-separated (> _VISIT_GAP_MS) single-frame visits.
    f1 = _present(store, base)
    f2 = _present(store, base + 10_000, edge_id=2)
    f3 = _present(store, base + 20_000, edge_id=3)

    page = store.annotation_queue_page("yolo-serial")
    assert page["has_model"] is False
    assert page["ordered_by"] == "recent"
    assert page["truncated"] is False
    # Newest-first with no model.
    assert [v["frames"][0]["id"] for v in page["visits"]] == [f3, f2, f1]
    # Each visit carries the frame-id span + the (null) model fields.
    v = page["visits"][0]
    assert v["start_id"] == f3 and v["end_id"] == f3
    assert v["distance"] is None and v["uncertain"] is None


def test_queue_page_limit_caps_and_marks_truncated(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    ids = [_present(store, base + i * 10_000, edge_id=i + 1) for i in range(5)]

    page = store.annotation_queue_page("yolo-serial", limit=2)
    assert page["truncated"] is True
    assert len(page["visits"]) == 2
    # Newest two survive.
    assert [v["frames"][0]["id"] for v in page["visits"]] == [ids[4], ids[3]]


def test_queue_page_scan_frames_bounds_and_drops_oldest_partial(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    ids = [_present(store, base + i * 10_000, edge_id=i + 1) for i in range(4)]

    # Scan only the newest 2 frames (+1 lookahead detects more remain → capped). The
    # oldest of the two scanned is dropped as possibly head-truncated → one visit left.
    page = store.annotation_queue_page("yolo-serial", scan_frames=2)
    assert page["truncated"] is True
    assert [v["frames"][0]["id"] for v in page["visits"]] == [ids[3]]


def test_queue_page_scoped_by_since_until_id(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    ids = [_present(store, base + i * 10_000, edge_id=i + 1) for i in range(3)]

    page = store.annotation_queue_page("yolo-serial", since_id=ids[1], until_id=ids[1])
    assert [v["frames"][0]["id"] for v in page["visits"]] == [ids[1]]
    assert page["truncated"] is False


def test_queue_page_excludes_decided(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    f1 = _present(store, base)
    f2 = _present(store, base + 10_000, edge_id=2)
    store.add_dataset_items([{"frame_id": f1, "label_kind": "not_cat"}])

    page = store.annotation_queue_page("yolo-serial")
    assert [v["frames"][0]["id"] for v in page["visits"]] == [f2]


def test_queue_page_empty_when_no_present_frames(tmp_path):
    store = _store(tmp_path)
    page = store.annotation_queue_page("yolo-serial")
    assert page == {
        "visits": [], "truncated": False, "ordered_by": "recent", "has_model": False,
        "hidden_confident": 0, "hidden_thin": 0, "hidden_total": 0,
    }


# --- 4. DISTANCE-SORT (needs an active model) ----------------------------------


def test_queue_page_distance_sort_worst_first(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    fa = _present(store, base)               # nearest 0.9 -> worst, uncertain
    fb = _present(store, base + 10_000, edge_id=2)   # nearest 0.2 -> confident
    fc = _present(store, base + 20_000, edge_id=3)   # never identified
    vid = _promote_model(store, threshold=0.5)
    cat = store.create_cat("A")["id"]
    store.write_identifications_batch(
        [
            (fa, vid, cat, 0.9, [0, 0, 1, 1]),
            (fb, vid, cat, 0.2, [0, 0, 1, 1]),
        ]
    )

    page = store.annotation_queue_page("yolo-serial")
    assert page["has_model"] is True
    assert page["ordered_by"] == "distance"
    order = [v["frames"][0]["id"] for v in page["visits"]]
    # Worst (largest distance) first, then confident, then never-identified LAST.
    assert order == [fa, fb, fc]

    by_id = {v["frames"][0]["id"]: v for v in page["visits"]}
    assert by_id[fa]["distance"] == pytest.approx(0.9) and by_id[fa]["uncertain"] is True
    assert by_id[fb]["distance"] == pytest.approx(0.2) and by_id[fb]["uncertain"] is False
    assert by_id[fc]["distance"] is None and by_id[fc]["uncertain"] is True


def test_queue_page_distance_sort_null_threshold_all_uncertain(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    fa = _present(store, base)
    fb = _present(store, base + 10_000, edge_id=2)
    vid = _promote_model(store, threshold=None)  # uncalibrated → nothing is "confident"
    cat = store.create_cat("A")["id"]
    store.write_identifications_batch(
        [(fa, vid, cat, 0.9, [0, 0, 1, 1]), (fb, vid, cat, 0.1, [0, 0, 1, 1])]
    )
    page = store.annotation_queue_page("yolo-serial")
    by_id = {v["frames"][0]["id"]: v for v in page["visits"]}
    # An uncalibrated model can never confidently match → both uncertain, still
    # ordered worst-first (0.9 before 0.1).
    assert [v["frames"][0]["id"] for v in page["visits"]] == [fa, fb]
    assert by_id[fa]["uncertain"] is True and by_id[fb]["uncertain"] is True


# --- 4b. MIN-FRAMES floor ------------------------------------------------------
#
# A single-frame visit yields ONE crop, which will not be gallery-grade — so a
# gallery-only build and a gallery-only validation run both ignore it entirely, and
# labelling it costs operator attention for nothing.


def _visit(store: Store, base_ts: int, n: int, *, edge: int, dist: "float | None" = None,
           vid: "int | None" = None, cat: "int | None" = None) -> "list[int]":
    """One visit of ``n`` boxed frames 200 ms apart (well inside ``_VISIT_GAP_MS``)."""
    fids = [_present(store, base_ts + i * 200, edge_id=edge + i) for i in range(n)]
    if dist is not None:
        store.write_identifications_batch([(f, vid, cat, dist, [0, 0, 1, 1]) for f in fids])
    return fids


def test_queue_page_min_frames_drops_thin_visits_and_counts_them(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    thin = _visit(store, base, 1, edge=1)
    thick = _visit(store, base + 10_000, 3, edge=10)

    page = store.annotation_queue_page("yolo-serial", min_frames=2)
    assert [v["start_id"] for v in page["visits"]] == [thick[0]]
    assert page["hidden_thin"] == 1
    # The dropped one is genuinely still in the store — this filters the PAGE, not the
    # backlog: it reappears the moment the floor comes off.
    assert len(store.annotation_queue_page("yolo-serial")["visits"]) == 2
    assert thin  # (named for readability above)


def test_queue_page_min_frames_default_is_a_no_op(tmp_path):
    """The regression guard on rewriting the `uncertain_only` block."""
    store = _store(tmp_path)
    base = 1_700_000_000_000
    _visit(store, base, 1, edge=1)
    _visit(store, base + 10_000, 3, edge=10)

    assert store.annotation_queue_page("yolo-serial", min_frames=1) == \
        store.annotation_queue_page("yolo-serial")
    # Zero/negative degrade to the no-op rather than raising, and there is deliberately no
    # upper clamp — an absurd floor legitimately empties the queue, and `hidden_thin` is
    # what keeps that from being silent.
    assert store.annotation_queue_page("yolo-serial", min_frames=0) == \
        store.annotation_queue_page("yolo-serial")
    huge = store.annotation_queue_page("yolo-serial", min_frames=9_999)
    assert huge["visits"] == [] and huge["hidden_thin"] == 2


def _four_bucket_store(tmp_path):
    """2 confident+thick, 2 uncertain+thick, 1 confident+thin, 3 uncertain+thin."""
    store = _store(tmp_path)
    base = 1_700_000_000_000
    vid = _promote_model(store, threshold=0.5)
    cat = store.create_cat("A")["id"]
    conf, unc = 0.2, 0.9   # below / above the 0.5 threshold
    slot, edge = 0, 1

    def add(n, dist):
        nonlocal slot, edge
        _visit(store, base + slot * 10_000, n, edge=edge, dist=dist, vid=vid, cat=cat)
        slot += 1
        edge += n
    for _ in range(2):
        add(3, conf)     # confident + thick
    for _ in range(2):
        add(3, unc)      # uncertain + thick
    add(1, conf)         # confident + thin  → counted in NEITHER
    for _ in range(3):
        add(1, unc)      # uncertain + thin
    return store


def test_queue_page_hidden_counts_are_what_relaxing_that_control_reveals(tmp_path):
    """Untick either control and EXACTLY the quoted number of visits appears.

    Nothing else catches a re-sequencing of the two filters: both orderings produce the
    same surviving set and differ only in these counts.
    """
    store = _four_bucket_store(tmp_path)
    both = store.annotation_queue_page("yolo-serial", uncertain_only=True, min_frames=2)
    shown = len(both["visits"])
    assert shown == 2                      # uncertain AND thick
    assert both["hidden_confident"] == 2   # confident AND thick — NOT the 3 confident total
    assert both["hidden_thin"] == 3        # thin AND uncertain — NOT the 4 thin total

    # Relaxing one control reveals exactly the count that was quoted beside it.
    relax_conf = store.annotation_queue_page("yolo-serial", uncertain_only=False, min_frames=2)
    assert len(relax_conf["visits"]) == shown + both["hidden_confident"]
    relax_thin = store.annotation_queue_page("yolo-serial", uncertain_only=True, min_frames=1)
    assert len(relax_thin["visits"]) == shown + both["hidden_thin"]


def test_queue_page_visit_failing_both_filters_is_counted_in_neither(tmp_path):
    store = _four_bucket_store(tmp_path)
    both = store.annotation_queue_page("yolo-serial", uncertain_only=True, min_frames=2)
    total = len(store.annotation_queue_page("yolo-serial")["visits"])
    hidden = total - len(both["visits"])
    # The confident+thin visit is revealed by relaxing NEITHER control alone, so no
    # single-control number can honestly claim it — hence the counts must not be summed
    # or presented as a total anywhere in the UI.
    assert hidden == both["hidden_confident"] + both["hidden_thin"] + 1
    # ...which is why `hidden_total` is MEASURED, not summed: it is the only field a
    # "nothing left" readout can honestly gate on.
    assert both["hidden_total"] == hidden


def test_queue_page_hidden_total_survives_when_both_counts_are_zero(tmp_path):
    """The queue must not read as CLEAR when every remaining visit fails BOTH filters.

    Every visit here is confident AND thin, so `hidden_confident` (confident AND thick)
    and `hidden_thin` (thin AND uncertain) are both 0 while three visits sit undecided —
    exactly what a caller gating on those two alone would call an empty queue. Reached by
    the ordinary default floor plus "hide confident matches", not a corner case.
    """
    store = _store(tmp_path)
    base = 1_700_000_000_000
    vid = _promote_model(store, threshold=0.5)
    cat = store.create_cat("A")["id"]
    for slot in range(3):
        _visit(store, base + slot * 10_000, 1, edge=slot + 1, dist=0.2, vid=vid, cat=cat)

    page = store.annotation_queue_page("yolo-serial", uncertain_only=True, min_frames=2)
    assert page["visits"] == []
    assert page["hidden_confident"] == 0 and page["hidden_thin"] == 0
    assert page["hidden_total"] == 3        # the work that is still there
    # And the three are genuinely still undecided — this filters the page, not the backlog.
    assert len(store.annotation_queue_page("yolo-serial")["visits"]) == 3


def test_queue_page_min_frames_applied_before_the_limit_cap(tmp_path):
    """Fails if the filter block moves below the truncation."""
    store = _store(tmp_path)
    base = 1_700_000_000_000
    edge = 1
    # The thin visits must be the NEWEST, or the test is hollow: ordering is newest-first,
    # so were the thick ones newest they would survive the cap whether the filter ran
    # before it or after, and a filter-moved-below-truncation regression would pass.
    for slot in range(3):            # 3 thick visits, oldest
        _visit(store, base + slot * 10_000, 2, edge=edge)
        edge += 2
    for slot in range(3, 11):        # 8 thin visits, newest — they would eat the page
        _visit(store, base + slot * 10_000, 1, edge=edge)
        edge += 1

    page = store.annotation_queue_page("yolo-serial", limit=3, min_frames=2)
    assert len(page["visits"]) == 3
    assert all(len(v["frames"]) >= 2 for v in page["visits"])
    assert page["hidden_thin"] == 8


# --- 2. IGNORE label -----------------------------------------------------------


def test_ignore_marks_decided_and_drops_from_queue(tmp_path):
    store = _store(tmp_path)
    fid = _present(store, 1_700_000_000_000)
    assert len(store.annotation_visits("yolo-serial")) == 1

    inserted = store.add_dataset_items([{"frame_id": fid, "label_kind": "ignored"}])
    assert inserted == 1
    # Both the unpaginated queue and the bounded page drop the ignored event.
    assert store.annotation_visits("yolo-serial") == []
    assert store.annotation_queue_page("yolo-serial")["visits"] == []


def test_ignore_row_carries_no_crop_cat_or_quality(tmp_path):
    store = _store(tmp_path)
    fid = _present(store, 1_700_000_000_000)
    store.add_dataset_items([{"frame_id": fid, "label_kind": "ignored"}])
    row = store._conn.execute(
        "SELECT cat_id, label_kind, quality, bbox, crop_path FROM dataset_items"
    ).fetchone()
    assert row == (None, "ignored", None, None, None)


def test_ignore_reversible_via_delete_requeues(tmp_path):
    store = _store(tmp_path)
    fid = _present(store, 1_700_000_000_000)
    store.add_dataset_items([{"frame_id": fid, "label_kind": "ignored"}])
    assert store.annotation_visits("yolo-serial") == []

    removed = store.delete_dataset_items([fid])
    # Ignored rows have no crop file, so crop_path is None (delete still requeues).
    assert removed == [{"frame_id": fid, "crop_path": None}]
    assert len(store.annotation_visits("yolo-serial")) == 1  # back in the queue


def test_ignore_visible_in_labeled_visits_for_undo(tmp_path):
    store = _store(tmp_path)
    fid = _present(store, 1_700_000_000_000)
    store.add_dataset_items([{"frame_id": fid, "label_kind": "ignored"}])
    labeled = store.labeled_visits("yolo-serial")
    assert len(labeled) == 1
    assert labeled[0]["label_kind"] == "ignored"
    assert labeled[0]["frames"][0]["id"] == fid


def test_ignore_excluded_from_gallery_and_floor_readers(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    cat = store.create_cat("A", is_resident=True)["id"]
    # One real identified crop, plus one ignored event.
    f_id = _present(store, base)
    f_ig = _present(store, base + 10_000, edge_id=2)
    store.add_dataset_items(
        [
            {"frame_id": f_id, "label_kind": "identified", "cat_id": cat,
             "quality": "gallery", "bbox": [0, 0, 10, 10], "crop_path": f"cat_{cat}/a.jpg"},
            {"frame_id": f_ig, "label_kind": "ignored"},
        ]
    )
    # labeled_crops (default identified) and count_identified_crops ignore the ignored row.
    crops = store.labeled_crops()
    assert [c["src_frame_id"] for c in crops] == [f_id]
    assert store.count_identified_crops(None) == (1, 1)
    # The motion floor learns from identified+unknown_cat only — never ignored.
    rows = store._conn.execute(
        "SELECT COUNT(*) FROM dataset_items d JOIN frames f"
        " ON f.id = d.src_frame_id AND f.recv_ts = d.src_recv_ts"
        " WHERE d.label_kind = 'ignored'"
    ).fetchone()
    assert rows[0] == 1  # the ignored row exists but is excluded by the floor's WHERE


# --- 3. Per-cat day/night COVERAGE ---------------------------------------------


def _label_identified(store: Store, fid: int, cat_id: int) -> None:
    store.add_dataset_items(
        [{"frame_id": fid, "label_kind": "identified", "cat_id": cat_id,
          "quality": "ok", "bbox": [0, 0, 10, 10], "crop_path": f"cat_{cat_id}/{fid}.jpg"}]
    )


def test_cat_regime_coverage_splits_day_night(tmp_path):
    store = _store(tmp_path)
    a = store.create_cat("A", is_resident=True)["id"]
    store.create_cat("B", is_resident=True)  # a resident with zero crops
    # Cat A: two day crops (recv_ts < 4000) + one night crop (>= 4000).
    _label_identified(store, store.add(_frame(frame_id=1), recv_ts_ms=1000), a)
    _label_identified(store, store.add(_frame(frame_id=2), recv_ts_ms=2000), a)
    _label_identified(store, store.add(_frame(frame_id=3), recv_ts_ms=5000), a)

    coverage = store.cat_regime_coverage(is_night=lambda ts: ts >= 4000)
    # Every roster cat is present, ordered by id (creation order).
    assert [c["cat_name"] for c in coverage] == ["A", "B"]
    ca, cb = coverage
    assert ca == {"cat_id": a, "cat_name": "A", "is_resident": True, "active": True,
                  "total": 3, "day": 2, "night": 1}
    # A zero-crop resident surfaces with night 0, so the operator knows to go capture it.
    assert cb["total"] == 0 and cb["day"] == 0 and cb["night"] == 0


def test_cat_regime_coverage_no_classifier_totals_only(tmp_path):
    store = _store(tmp_path)
    a = store.create_cat("A")["id"]
    _label_identified(store, store.add(_frame(frame_id=1), recv_ts_ms=1000), a)
    _label_identified(store, store.add(_frame(frame_id=2), recv_ts_ms=2000), a)

    coverage = store.cat_regime_coverage(is_night=None)
    assert coverage[0]["total"] == 2
    assert coverage[0]["day"] is None and coverage[0]["night"] is None


def test_cat_regime_coverage_excludes_non_identified_and_cropless(tmp_path):
    store = _store(tmp_path)
    a = store.create_cat("A")["id"]
    # An identified crop counts; unknown_cat / not_cat / ignored / crop-less do NOT.
    _label_identified(store, store.add(_frame(frame_id=1), recv_ts_ms=1000), a)
    store.add_dataset_items(
        [
            {"frame_id": store.add(_frame(frame_id=2), recv_ts_ms=2000),
             "label_kind": "unknown_cat", "quality": "ok",
             "bbox": [0, 0, 5, 5], "crop_path": "cat_unknown_cat/x.jpg"},
            {"frame_id": store.add(_frame(frame_id=3), recv_ts_ms=3000), "label_kind": "not_cat"},
            {"frame_id": store.add(_frame(frame_id=4), recv_ts_ms=4000), "label_kind": "ignored"},
            # An identified row with NO crop file (crop_path None) is excluded too.
            {"frame_id": store.add(_frame(frame_id=5), recv_ts_ms=5000),
             "label_kind": "identified", "cat_id": a, "quality": None, "crop_path": None},
        ]
    )
    coverage = store.cat_regime_coverage(is_night=lambda ts: False)
    assert coverage[0]["total"] == 1  # only the one identified-with-crop row


# --- API wiring ----------------------------------------------------------------


class _FakeClient:
    def iter_stream_reconnecting(self):
        return iter(())


@pytest.fixture
def make_app(tmp_path):
    def _make():
        from compute.api.app import create_app

        store = _store(tmp_path)
        app = create_app(store=store, client=_FakeClient(), start_collector=False)
        return TestClient(app), store

    return _make


def test_api_label_queue_returns_bounded_page(make_app):
    client, store = make_app()
    base = 1_700_000_000_000
    ids = [_present(store, base + i * 10_000, edge_id=i + 1) for i in range(3)]

    resp = client.get("/api/label/queue", params={"limit": 2})
    assert resp.status_code == 200
    body = resp.json()
    assert body["truncated"] is True
    assert [v["frames"][0]["id"] for v in body["visits"]] == [ids[2], ids[1]]
    assert body["has_model"] is False and body["ordered_by"] == "recent"


def test_api_label_queue_min_frames_param(make_app):
    client, store = make_app()
    base = 1_700_000_000_000
    _visit(store, base, 1, edge=1)
    _visit(store, base + 10_000, 3, edge=10)

    # Omitted → the no-op default, plus the new counter at zero (additive: no other
    # caller of this endpoint changes).
    body = client.get("/api/label/queue").json()
    assert len(body["visits"]) == 2 and body["hidden_thin"] == 0

    body = client.get("/api/label/queue", params={"min_frames": 2}).json()
    assert len(body["visits"]) == 1 and body["hidden_thin"] == 1


def test_api_label_queue_unknown_oracle_is_400(make_app):
    client, _store = make_app()
    assert client.get("/api/label/queue", params={"oracle": "bogus"}).status_code == 400


def test_api_label_queue_inverted_bounds_is_400(make_app):
    client, _store = make_app()
    resp = client.get("/api/label/queue", params={"since_id": 90, "until_id": 10})
    assert resp.status_code == 400


def test_api_label_ignore_drops_and_delete_requeues(make_app):
    client, store = make_app()
    fid = _present(store, 1_700_000_000_000)

    resp = client.post("/api/label/ignore", json={"frame_ids": [fid]})
    assert resp.status_code == 200
    assert resp.json() == {"ignored": 1}
    # Dropped from both queue endpoints.
    assert client.get("/api/label/visits").json()["visits"] == []
    assert client.get("/api/label/queue").json()["visits"] == []

    # Reversible: delete requeues it.
    resp = client.post("/api/label/delete", json={"frame_ids": [fid]})
    assert resp.status_code == 200
    assert resp.json() == {"deleted": 1, "crops_removed": 0}
    assert len(client.get("/api/label/queue").json()["visits"]) == 1


def test_api_label_ignore_skips_evicted_frame(make_app):
    client, _store = make_app()
    resp = client.post("/api/label/ignore", json={"frame_ids": [999_999]})
    assert resp.status_code == 200
    assert resp.json() == {"ignored": 0}  # no live frame to anchor the label


@_requires_cv
def test_api_label_relabel_from_ignored_to_identified(make_app):
    # Ignore is reversible via relabel too: an ignored event can be re-decided to a cat.
    client, store = make_app()
    fid = store.add(_frame(frame_id=1), recv_ts_ms=1_700_000_000_000)
    # A real decodable JPEG so the relabel crop materialises.
    with open(store.path_for(fid), "wb") as fh:
        img = np.full((64, 64, 3), 180, dtype=np.uint8)
        ok, buf = cv2.imencode(".jpg", img)
        assert ok
        fh.write(buf.tobytes())
    store.write_analysis(fid, "yolo-serial", True, 0.9, _boxes_detail([[0, 0, 40, 40, 0.9]]))
    cat = client.post("/api/cats", json={"name": "Mittens", "is_resident": True}).json()

    client.post("/api/label/ignore", json={"frame_ids": [fid]})
    resp = client.post(
        "/api/label/relabel",
        json={"decision": "identified", "cat_id": cat["id"],
              "frames": [{"frame_id": fid, "bbox": [0, 0, 40, 40], "quality": "gallery"}]},
    )
    assert resp.status_code == 200
    assert resp.json()["deleted"] == 1 and resp.json()["inserted"] == 1
    row = store._conn.execute("SELECT cat_id, label_kind FROM dataset_items").fetchone()
    assert row == (cat["id"], "identified")


def test_api_label_regime_coverage_unavailable_without_location(make_app):
    client, store = make_app()
    a = store.create_cat("A", is_resident=True)["id"]
    _label_identified(store, store.add(_frame(frame_id=1), recv_ts_ms=1000), a)

    resp = client.get("/api/label/regime-coverage")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert body["reason"] == "location_unset"
    # Totals still reported; the split is null when unavailable.
    assert body["cats"][0]["total"] == 1
    assert body["cats"][0]["day"] is None and body["cats"][0]["night"] is None


def test_api_label_regime_coverage_available_with_location(make_app):
    from compute.analysis.suntimes import astral_available

    client, store = make_app()
    a = store.create_cat("A", is_resident=True)["id"]
    for i, ts in enumerate((1_700_000_000_000, 1_700_000_050_000)):
        _label_identified(store, store.add(_frame(frame_id=i + 1), recv_ts_ms=ts), a)
    store.set_location(55.6761, 12.5683)  # Copenhagen

    resp = client.get("/api/label/regime-coverage")
    assert resp.status_code == 200
    body = resp.json()
    if not astral_available():  # pragma: no cover - astral is a listed compute dep
        assert body["available"] is False and body["reason"] == "astral_unavailable"
        return
    assert body["available"] is True
    assert body["location"] == {"latitude": 55.6761, "longitude": 12.5683}
    cat = body["cats"][0]
    # Whatever the boundary, the split partitions every crop exactly.
    assert cat["total"] == 2
    assert cat["day"] + cat["night"] == cat["total"]
