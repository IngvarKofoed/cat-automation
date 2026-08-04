"""Data-integrity tests for the runtime identification layer on
``compute/collection/store.py``: the ``model_versions`` + ``identifications``
tables, the ``promote``/``active_model`` state machine, the ``iter_unidentified``
/ ``count_unidentified`` predicate, the ``write_identifications_batch`` write
guard, and the active-model identity join in ``events()``.

See docs/specs/2026-07-17-identification-gallery-activity.md. Pure-sqlite: NO
torch/numpy/cv2, so it runs anywhere. Mirrors the suite's conventions
(test_events.py / test_feasibility_runs.py): a ``_frame()`` helper builds
``StreamFrame``s directly (never decoded), and a ``_store`` factory opens a Store
under ``tmp_path``. A gallery is faked as a bare ``gallery.npz`` FILE — the store
only ever checks its existence (``os.path.isfile``), never its contents — so
these tests need no real embedding artifact.
"""
from __future__ import annotations

import os

import pytest

from compute.collection.store import Store, _VISIT_GAP_MS
from compute.ingest import StreamFrame
from shared.wire import StreamFrameMeta

# A minimal but genuinely valid JPEG (SOI ... EOI); the store writes it verbatim
# and never decodes it. Its length is the per-frame byte cost the eviction test
# sizes the cap against.
_JPEG_BODY = b"\xff\xd8\xff\xe0" + b"fake-jpeg-body" + b"\xff\xd9"


def _frame(
    frame_id: int = 1,
    ts: int = 1_000,
    motion: bool = False,
    area: float = 0.0,
    bbox=None,
) -> StreamFrame:
    """Build a ``StreamFrame`` directly — the shape ``Store.add`` consumes."""
    meta = StreamFrameMeta(frame_id=frame_id, ts=ts, motion=motion, bbox=bbox, area=area)
    return StreamFrame(meta, _JPEG_BODY)


def _store(tmp_path, max_bytes: int = 10_000_000) -> Store:
    return Store(
        db_path=str(tmp_path / "index.db"),
        media_root=str(tmp_path / "media"),
        max_bytes=max_bytes,
    )


def _add(store: Store, recv_ts_ms: int, *, motion: bool = False, area: float = 0.0, edge_id: int = 1) -> int:
    """Add one frame at ``recv_ts_ms`` and return its store row id."""
    return store.add(_frame(frame_id=edge_id, ts=recv_ts_ms, motion=motion, area=area), recv_ts_ms=recv_ts_ms)


def _boxes_detail(boxes: "list[list[float]]") -> dict:
    """A ``yolo-serial``-shaped ``analysis.detail``: ``{"boxes": [[x1,y1,x2,y2,conf], ...]}``."""
    return {"boxes": boxes}


def _write_gallery_file(store: Store, gallery_dir: str) -> str:
    """Materialise a bare ``gallery.npz`` under ``<models_root>/<gallery_dir>/``.

    The store only checks the file's EXISTENCE (never opens it), so a one-byte
    placeholder is enough to make a version promotable / active.
    """
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
    backbone: str = "dinov2_vits14",
    imgsz: int = 224,
) -> int:
    """Insert a ``model_versions`` row (optionally writing its gallery file) → its id."""
    if write_file:
        _write_gallery_file(store, gallery_dir)
    return store.add_model_version(
        status=status,
        kind="gallery",
        backbone=backbone,
        imgsz=imgsz,
        n_cats=n_cats,
        n_vectors=n_vectors,
        threshold=threshold,
        quality=quality,
        metrics=metrics,
        gallery_dir=gallery_dir,
    )


def _ident_frame_ids(store: Store) -> "list[int]":
    return [
        int(r[0])
        for r in store._conn.execute(
            "SELECT frame_id FROM identifications ORDER BY frame_id"
        ).fetchall()
    ]


def _active_count(store: Store) -> int:
    return int(
        store._conn.execute(
            "SELECT COUNT(*) FROM model_versions WHERE status = 'active'"
        ).fetchone()[0]
    )


# --- Schema ----------------------------------------------------------------


def test_schema_creates_model_versions_and_identifications_tables(tmp_path):
    store = _store(tmp_path)
    tables = {
        row[0]
        for row in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert "model_versions" in tables
    assert "identifications" in tables


# --- Eviction + clear cascade (data integrity) -----------------------------


def test_identifications_evict_with_their_frames(tmp_path):
    # Cap sized to hold exactly three frames; a fourth insert evicts the oldest.
    store = _store(tmp_path, max_bytes=3 * len(_JPEG_BODY))
    vid = _add_version(store)
    cat = store.create_cat("A")["id"]

    f1 = _add(store, 1000)
    f2 = _add(store, 2000)
    f3 = _add(store, 3000)
    store.write_identifications_batch(
        [
            (f1, vid, cat, 0.1, [0, 0, 1, 1]),
            (f2, vid, cat, 0.2, [0, 0, 1, 1]),
            (f3, vid, cat, 0.3, [0, 0, 1, 1]),
        ]
    )
    assert _ident_frame_ids(store) == sorted([f1, f2, f3])

    # Three more inserts evict f1, f2, f3 (oldest-first); their identifications
    # must cascade-delete in _evict_locked, never outliving their frame.
    f4 = _add(store, 4000)
    f5 = _add(store, 5000)
    f6 = _add(store, 6000)

    # Only the newest three frames survive; f1/f2/f3 were evicted.
    survivors = [int(r[0]) for r in store._conn.execute("SELECT id FROM frames ORDER BY id").fetchall()]
    assert survivors == [f4, f5, f6]
    # f4/f5/f6 were never identified, and f1/f2/f3's identifications are gone.
    assert _ident_frame_ids(store) == []


def test_clear_wipes_identifications_but_keeps_model_versions(tmp_path):
    store = _store(tmp_path)
    vid = _add_version(store, metrics={"per_cat": [{"cat_id": 1, "n": 5}]})
    cat = store.create_cat("A")["id"]
    f1 = _add(store, 1000)
    f2 = _add(store, 2000)
    store.write_identifications_batch(
        [(f1, vid, cat, 0.1, [0, 0, 1, 1]), (f2, vid, cat, 0.2, [0, 0, 1, 1])]
    )
    assert _ident_frame_ids(store) == sorted([f1, f2])

    n = store.clear()
    assert n == 2
    assert store.stats()["count"] == 0  # frames wiped
    assert _ident_frame_ids(store) == []  # identifications cascade with the wipe

    # model_versions is precious — it survives a full clear (like cats/dataset_items).
    versions = store.list_model_versions()
    assert [v["id"] for v in versions] == [vid]
    assert versions[0]["metrics"] == {"per_cat": [{"cat_id": 1, "n": 5}]}


# --- promote_model state machine -------------------------------------------


def test_promote_draft_to_active(tmp_path):
    store = _store(tmp_path)
    vid = _add_version(store, gallery_dir="g1", threshold=0.42)
    row = store.promote_model(vid)
    assert row["status"] == "active"
    assert row["id"] == vid
    assert _active_count(store) == 1
    active = store.active_model()
    assert active is not None and active["id"] == vid
    assert active["threshold"] == pytest.approx(0.42)


def test_promote_retires_prior_active_exactly_one_active(tmp_path):
    store = _store(tmp_path)
    v1 = _add_version(store, gallery_dir="g1")
    v2 = _add_version(store, gallery_dir="g2")
    store.promote_model(v1)
    store.promote_model(v2)

    assert _active_count(store) == 1
    by_id = {v["id"]: v["status"] for v in store.list_model_versions()}
    assert by_id[v2] == "active"
    assert by_id[v1] == "retired"
    assert store.active_model()["id"] == v2


def test_promote_rollback_retired_to_active(tmp_path):
    store = _store(tmp_path)
    v1 = _add_version(store, gallery_dir="g1")
    v2 = _add_version(store, gallery_dir="g2")
    store.promote_model(v1)
    store.promote_model(v2)  # v1 -> retired, v2 -> active

    # Roll back to v1: a retired version can be promoted again (ARCHITECTURE.md).
    row = store.promote_model(v1)
    assert row["status"] == "active"
    assert _active_count(store) == 1
    by_id = {v["id"]: v["status"] for v in store.list_model_versions()}
    assert by_id[v1] == "active"
    assert by_id[v2] == "retired"


def test_promote_already_active_is_noop_success(tmp_path):
    store = _store(tmp_path)
    vid = _add_version(store, gallery_dir="g1")
    store.promote_model(vid)
    row = store.promote_model(vid)  # promoting the incumbent again
    assert row["status"] == "active"
    assert _active_count(store) == 1


def test_promote_unknown_id_raises_value_error(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="no such model version"):
        store.promote_model(999_999)


def test_promote_missing_gallery_artifact_raises_value_error(tmp_path):
    store = _store(tmp_path)
    # A version whose gallery.npz was never written on disk cannot be promoted.
    vid = _add_version(store, gallery_dir="ghost", write_file=False)
    with pytest.raises(ValueError, match="gallery artifact missing"):
        store.promote_model(vid)
    assert _active_count(store) == 0  # nothing became active


# --- delete_model_version --------------------------------------------------
#
# The only removal path for the otherwise append-forever `model_versions`
# registry, and it takes files + identification rows with it — so each guard
# gets a test.


def _write_ident(store: Store, frame_id: int, model_id: int) -> None:
    """Insert an ``identifications`` row directly (no frame needed for these tests)."""
    store._conn.execute(
        "INSERT INTO identifications (frame_id, model_version_id, cat_id, distance, bbox, ran_at)"
        " VALUES (?, ?, 1, 0.2, '1,2,3,4', 1000)",
        (frame_id, model_id),
    )
    store._conn.commit()


def test_delete_model_version_removes_row_identifications_and_dir(tmp_path):
    store = _store(tmp_path)
    keep = _add_version(store, gallery_dir="keep")
    drop = _add_version(store, gallery_dir="drop")
    _write_ident(store, 1, drop)
    _write_ident(store, 2, drop)
    _write_ident(store, 1, keep)

    result = store.delete_model_version(drop)

    assert result == {"version_id": drop, "n_identifications": 2, "gallery_removed": True}
    assert [v["id"] for v in store.list_model_versions()] == [keep]
    # Only the deleted version's identifications went; the other version's row stayed.
    assert store._conn.execute(
        "SELECT model_version_id FROM identifications"
    ).fetchall() == [(keep,)]
    assert not os.path.exists(os.path.join(store.models_root, "drop"))
    assert os.path.isfile(os.path.join(store.models_root, "keep", "gallery.npz"))


def test_delete_active_model_version_refused(tmp_path):
    store = _store(tmp_path)
    vid = _add_version(store, gallery_dir="g1")
    store.promote_model(vid)
    with pytest.raises(ValueError, match="cannot delete the active model version"):
        store.delete_model_version(vid)
    # Nothing removed: identification reads this gallery every tick.
    assert [v["id"] for v in store.list_model_versions()] == [vid]
    assert os.path.isfile(os.path.join(store.models_root, "g1", "gallery.npz"))


def test_delete_unknown_model_version_raises(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="no such model version"):
        store.delete_model_version(999_999)


def test_delete_model_version_with_missing_gallery_file(tmp_path):
    store = _store(tmp_path)
    vid = _add_version(store, gallery_dir="ghost", write_file=False)
    result = store.delete_model_version(vid)
    # The row is the point — a version whose artifact is already gone is exactly
    # what an operator wants to clear, so a missing dir is not an error.
    assert result["gallery_removed"] is False
    assert store.list_model_versions() == []


def test_delete_model_version_never_escapes_models_root(tmp_path):
    """A `gallery_dir` that resolves outside models_root must not be rmtree'd.

    `gallery_dir` is written by our own builder as a bare basename, but this is the
    one call that recursively deletes a directory named by a DB column, so a
    hand-edited row must not turn it into "remove any path".
    """
    store = _store(tmp_path)
    outside = tmp_path / "precious"
    outside.mkdir()
    (outside / "keep.txt").write_text("do not delete")
    vid = _add_version(store, gallery_dir=os.path.join("..", "precious"), write_file=False)

    result = store.delete_model_version(vid)

    assert result["gallery_removed"] is False
    assert (outside / "keep.txt").is_file()   # untouched
    assert store.list_model_versions() == []  # the row still goes


# --- active_model: none / present / missing-artifact -----------------------


def test_active_model_none_when_nothing_promoted(tmp_path):
    store = _store(tmp_path)
    _add_version(store, gallery_dir="g1")  # a draft exists, but none is active
    assert store.active_model() is None


def test_active_model_present_exposes_gallery_path(tmp_path):
    store = _store(tmp_path)
    vid = _add_version(store, gallery_dir="g1")
    store.promote_model(vid)
    active = store.active_model()
    assert active is not None
    assert active["id"] == vid
    assert os.path.isabs(active["gallery_path"])
    assert os.path.isfile(active["gallery_path"])
    assert active["gallery_path"] == os.path.join(store.models_root, "g1", "gallery.npz")


def test_active_model_none_when_artifact_missing_on_disk(tmp_path):
    store = _store(tmp_path)
    vid = _add_version(store, gallery_dir="g1")
    store.promote_model(vid)  # promotable while the file exists
    # The row stays active, but the artifact is lost after the fact.
    os.remove(os.path.join(store.models_root, "g1", "gallery.npz"))
    assert store.active_model() is None  # lost artifact reads as "no model"


# --- list_model_versions ----------------------------------------------------


def test_list_model_versions_newest_first_with_gallery_available_flag(tmp_path):
    store = _store(tmp_path)
    v1 = _add_version(store, gallery_dir="g1", threshold=0.3)  # file written
    v2 = _add_version(store, gallery_dir="g2", threshold=None, write_file=False)  # no file

    versions = store.list_model_versions()
    assert [v["id"] for v in versions] == [v2, v1]  # id DESC
    by_id = {v["id"]: v for v in versions}
    assert by_id[v1]["gallery_available"] is True
    assert by_id[v2]["gallery_available"] is False
    assert by_id[v2]["threshold"] is None  # null threshold round-trips as None


# --- iter_unidentified / count_unidentified predicate ----------------------


def test_iter_unidentified_only_yolo_serial_present_with_box(tmp_path):
    store = _store(tmp_path)
    vid = _add_version(store)

    f1 = _add(store, 1000)
    f2 = _add(store, 2000)
    f3 = _add(store, 3000)
    f4 = _add(store, 4000)
    f5 = _add(store, 5000)
    # f1: yolo-serial present with a box → qualifies. Two boxes, highest-conf wins.
    store.write_analysis(f1, "yolo-serial", True, 0.9, _boxes_detail([[0, 0, 5, 5, 0.4], [1, 2, 3, 4, 0.9]]))
    # f2: yolo-serial ABSENT (verdict 0) → excluded.
    store.write_analysis(f2, "yolo-serial", False, 0.1, None)
    # f3: a DIFFERENT analyzer present → excluded (must be yolo-serial).
    store.write_analysis(f3, "yolo", True, 0.9, _boxes_detail([[0, 0, 9, 9, 0.9]]))
    # f4: yolo-serial present with a box → qualifies.
    store.write_analysis(f4, "yolo-serial", True, 0.8, _boxes_detail([[10, 10, 20, 20, 0.8]]))
    # f5: no analysis at all → excluded.

    items = list(store.iter_unidentified(vid))
    assert [it[0] for it in items] == [f1, f4]  # oldest-first, only qualifying frames
    # Highest-confidence box for f1 is the 0.9 one; f4's single box is verbatim.
    assert items[0][2] == [1.0, 2.0, 3.0, 4.0]
    assert items[1][2] == [10.0, 10.0, 20.0, 20.0]
    # Paths are absolute and point at a real stored file.
    assert os.path.isabs(items[0][1]) and os.path.isfile(items[0][1])

    assert store.count_unidentified(vid) == 2  # agrees with the iterator's yield


def test_iter_unidentified_skips_already_identified_and_count_agrees(tmp_path):
    store = _store(tmp_path)
    vid = _add_version(store)
    cat = store.create_cat("A")["id"]

    f1 = _add(store, 1000)
    f2 = _add(store, 2000)
    f3 = _add(store, 3000)
    for fid in (f1, f2, f3):
        store.write_analysis(fid, "yolo-serial", True, 0.9, _boxes_detail([[0, 0, 4, 4, 0.9]]))

    assert [it[0] for it in store.iter_unidentified(vid)] == [f1, f2, f3]
    assert store.count_unidentified(vid) == 3

    # Identify f2 for this model → it drops out of the unidentified set.
    store.write_identifications_batch([(f2, vid, cat, 0.2, [0, 0, 4, 4])])
    assert [it[0] for it in store.iter_unidentified(vid)] == [f1, f3]
    assert store.count_unidentified(vid) == 2


def test_iter_unidentified_is_per_model(tmp_path):
    store = _store(tmp_path)
    v1 = _add_version(store, gallery_dir="g1")
    v2 = _add_version(store, gallery_dir="g2")
    cat = store.create_cat("A")["id"]
    f1 = _add(store, 1000)
    store.write_analysis(f1, "yolo-serial", True, 0.9, _boxes_detail([[0, 0, 4, 4, 0.9]]))
    store.write_identifications_batch([(f1, v1, cat, 0.2, [0, 0, 4, 4])])

    # Identified for v1 but not v2 — a new model makes it fresh un-identified work.
    assert [it[0] for it in store.iter_unidentified(v1)] == []
    assert store.count_unidentified(v1) == 0
    assert [it[0] for it in store.iter_unidentified(v2)] == [f1]
    assert store.count_unidentified(v2) == 1


def test_count_unidentified_reads_writes_made_on_the_shared_connection(tmp_path):
    """It runs on its OWN short-lived connection, so it must not read a stale snapshot.

    The count moved off the shared write-locked connection because unbounded it measures
    ~5 s at the real store's size (4.7M frames), and the whole-store "Identify all" makes
    that call routine — holding the write lock for it is the collector starvation
    changelog 102-105 removed. A second connection only sees COMMITTED data, so this
    pins the property that hazard turns on: every write below lands through
    ``self._conn`` and must be visible to the very next count, with no commit of its own
    in between.
    """
    store = _store(tmp_path)
    vid = _add_version(store)
    cat = store.create_cat("A")["id"]

    assert store.count_unidentified(vid) == 0  # empty store, fresh connection each call

    f1 = _add(store, 1000)
    store.write_analysis(f1, "yolo-serial", True, 0.9, _boxes_detail([[0, 0, 4, 4, 0.9]]))
    assert store.count_unidentified(vid) == 1  # sees the just-written verdict

    f2 = _add(store, 2000)
    store.write_analysis(f2, "yolo-serial", True, 0.8, _boxes_detail([[0, 0, 4, 4, 0.8]]))
    assert store.count_unidentified(vid) == 2

    # A batched identification write must drop f1 out on the next count — this is the
    # resume predicate the pass's progress denominator depends on.
    store.write_identifications_batch([(f1, vid, cat, 0.2, [0, 0, 4, 4])])
    assert store.count_unidentified(vid) == 1
    # The count is documented to match the iterator's yield exactly, and the two now
    # read DIFFERENT connections — so pin that here rather than trusting it.
    assert store.count_unidentified(vid) == len(list(store.iter_unidentified(vid)))


def test_count_unidentified_follows_eviction(tmp_path):
    """Eviction deletes through the SHARED connection; the count reads its own one.

    A frame's `analysis` rows go with it in `_evict_locked`, so an evicted detection
    must leave the denominator too — otherwise the identify pass's progress could never
    reach 100%, since `iter_unidentified` cannot yield a frame that no longer exists.
    Sized like `test_identifications_evict_with_their_frames`: a cap holding exactly
    three frames, so the fourth insert evicts the oldest.
    """
    store = _store(tmp_path, max_bytes=3 * len(_JPEG_BODY))
    vid = _add_version(store)

    f1 = _add(store, 1000)
    f2 = _add(store, 2000)
    f3 = _add(store, 3000)
    for fid in (f1, f2, f3):
        store.write_analysis(fid, "yolo-serial", True, 0.9, _boxes_detail([[0, 0, 4, 4, 0.9]]))
    assert store.count_unidentified(vid) == 3

    _add(store, 4000)  # evicts f1 (oldest-first) along with its yolo-serial verdict
    assert store.count_unidentified(vid) == 2
    assert store.count_unidentified(vid) == len(list(store.iter_unidentified(vid)))


def test_iter_unidentified_until_and_since_id_cap(tmp_path):
    store = _store(tmp_path)
    vid = _add_version(store)
    fids = []
    for i in range(4):
        fid = _add(store, 1000 * (i + 1))
        store.write_analysis(fid, "yolo-serial", True, 0.9, _boxes_detail([[0, 0, 4, 4, 0.9]]))
        fids.append(fid)
    f1, f2, f3, f4 = fids

    assert [it[0] for it in store.iter_unidentified(vid, until_id=f2)] == [f1, f2]
    assert store.count_unidentified(vid, until_id=f2) == 2
    assert [it[0] for it in store.iter_unidentified(vid, since_id=f3)] == [f3, f4]
    assert store.count_unidentified(vid, since_id=f3) == 2


def test_iter_unidentified_yields_no_box_frames_matching_count(tmp_path):
    store = _store(tmp_path)
    vid = _add_version(store)
    f1 = _add(store, 1000)
    f2 = _add(store, 2000)
    store.write_analysis(f1, "yolo-serial", True, 0.9, _boxes_detail([[0, 0, 4, 4, 0.9]]))
    # Present verdict but an EMPTY box list — _best_box returns None. The iterator now
    # YIELDS it (with bbox=None) rather than skipping, so its yield-set matches the
    # box-less count EXACTLY (the identify pass markers it done so it isn't re-attempted
    # forever and the progress bar can reach 100%).
    store.write_analysis(f2, "yolo-serial", True, 0.9, _boxes_detail([]))

    items = list(store.iter_unidentified(vid))
    assert [it[0] for it in items] == [f1, f2]
    boxes = {it[0]: it[2] for it in items}
    assert boxes[f1] == [0, 0, 4, 4]
    assert boxes[f2] is None  # no parsable box → still yielded, bbox=None
    assert store.count_unidentified(vid) == 2


# --- write_identifications_batch: WHERE-EXISTS guard + idempotency ---------


def test_write_identifications_batch_drops_row_for_nonexistent_frame(tmp_path):
    store = _store(tmp_path)
    vid = _add_version(store)
    cat = store.create_cat("A")["id"]
    f1 = _add(store, 1000)

    store.write_identifications_batch(
        [
            (f1, vid, cat, 0.2, [0, 0, 10, 10]),
            (999_999, vid, cat, 0.3, [0, 0, 5, 5]),  # no such frame → dropped by WHERE EXISTS
        ]
    )
    rows = store._conn.execute(
        "SELECT frame_id, model_version_id, cat_id, distance, bbox FROM identifications"
    ).fetchall()
    assert len(rows) == 1
    fid_, mv, cid, dist, bbox = rows[0]
    assert (fid_, mv, cid) == (f1, vid, cat)
    assert dist == pytest.approx(0.2)
    assert bbox == "0,0,10,10"


def test_write_identifications_batch_idempotent_replace(tmp_path):
    store = _store(tmp_path)
    vid = _add_version(store)
    cat_a = store.create_cat("A")["id"]
    cat_b = store.create_cat("B")["id"]
    f1 = _add(store, 1000)

    store.write_identifications_batch([(f1, vid, cat_a, 0.5, [0, 0, 1, 1])])
    store.write_identifications_batch([(f1, vid, cat_b, 0.1, [1, 1, 2, 2])])  # re-run overwrites

    rows = store._conn.execute(
        "SELECT cat_id, distance, bbox FROM identifications WHERE frame_id = ? AND model_version_id = ?",
        (f1, vid),
    ).fetchall()
    assert len(rows) == 1  # PK (frame_id, model_version_id) → one row
    cid, dist, bbox = rows[0]
    assert cid == cat_b
    assert dist == pytest.approx(0.1)
    assert bbox == "1,1,2,2"


def test_write_identifications_batch_empty_is_noop(tmp_path):
    store = _store(tmp_path)
    store.write_identifications_batch([])  # must not error or commit anything
    assert _ident_frame_ids(store) == []


# --- events() active-model identity aggregation ----------------------------


def _one_event_ids(store: Store, base: int, n: int) -> "list[int]":
    """Add ``n`` motion frames close in time (one event) → their store ids."""
    return [_add(store, base + 100 * i, motion=True, area=0.1 * (i + 1)) for i in range(n)]


def test_events_identity_none_when_no_active_model(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    ids = _one_event_ids(store, base, 2)
    # A DRAFT (never-promoted) model with identifications: active_model() is None,
    # so the feed must render exactly like the oracle-free base feed.
    vid = _add_version(store)
    cat = store.create_cat("A")["id"]
    store.write_identifications_batch([(fid, vid, cat, 0.1, [0, 0, 1, 1]) for fid in ids])

    result = store.events(None, None)
    events = result["events"]
    assert len(events) == 1
    ev = events[0]
    assert ev["identity"] is None
    # Base fields are untouched.
    assert ev["n_frames"] == 2
    assert ev["start_id"] == ids[0] and ev["end_id"] == ids[-1]


def test_events_identity_vote_winner(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    f1, f2, f3 = _one_event_ids(store, base, 3)
    vid = _add_version(store, threshold=0.5)
    store.promote_model(vid)
    cat_a = store.create_cat("A")["id"]
    cat_b = store.create_cat("B")["id"]
    # A: two below-threshold frames (0.2, 0.3); B: one (0.1). A wins on count.
    store.write_identifications_batch(
        [
            (f1, vid, cat_a, 0.2, [0, 0, 1, 1]),
            (f2, vid, cat_a, 0.3, [0, 0, 1, 1]),
            (f3, vid, cat_b, 0.1, [0, 0, 1, 1]),
        ]
    )
    ev = store.events(None, None)["events"][0]
    ident = ev["identity"]
    assert ident["cat_id"] == cat_a
    assert ident["cat_name"] == "A"
    assert ident["distance"] == pytest.approx(0.2)  # winner's MIN distance
    assert ident["n_identified"] == 3  # all identified frames in the span
    assert ident["n_frames_voted"] == 2  # A's below-threshold count


def test_events_identity_tie_break_by_min_distance(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    f1, f2 = _one_event_ids(store, base, 2)
    vid = _add_version(store, threshold=0.5)
    store.promote_model(vid)
    cat_a = store.create_cat("A")["id"]
    cat_b = store.create_cat("B")["id"]
    # One below-threshold frame each → count tie; B is nearer (0.2 < 0.4).
    store.write_identifications_batch(
        [(f1, vid, cat_a, 0.4, [0, 0, 1, 1]), (f2, vid, cat_b, 0.2, [0, 0, 1, 1])]
    )
    ident = store.events(None, None)["events"][0]["identity"]
    assert ident["cat_id"] == cat_b
    assert ident["distance"] == pytest.approx(0.2)
    assert ident["n_frames_voted"] == 1
    assert ident["n_identified"] == 2


def test_events_identity_unknown_when_none_below_threshold(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    f1, f2 = _one_event_ids(store, base, 2)
    vid = _add_version(store, threshold=0.1)  # tight cutoff
    store.promote_model(vid)
    cat_a = store.create_cat("A")["id"]
    cat_b = store.create_cat("B")["id"]
    # Nearest matches (0.5, 0.6) are both beyond 0.1 → an unknown cat was seen.
    store.write_identifications_batch(
        [(f1, vid, cat_a, 0.5, [0, 0, 1, 1]), (f2, vid, cat_b, 0.6, [0, 0, 1, 1])]
    )
    ident = store.events(None, None)["events"][0]["identity"]
    assert ident["cat_id"] is None
    assert ident["cat_name"] is None
    assert ident["distance"] == pytest.approx(0.5)  # nearest distance any frame reached
    assert ident["n_identified"] == 2
    assert ident["n_frames_voted"] == 0


def test_events_identity_null_threshold_fails_safe_to_unknown(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    f1, f2, f3 = _one_event_ids(store, base, 3)
    vid = _add_version(store, threshold=None)  # uncomputable cutoff → uncalibrated model
    store.promote_model(vid)
    cat_a = store.create_cat("A")["id"]
    cat_b = store.create_cat("B")["id"]
    # An uncalibrated model (no threshold) must DEGRADE TO UNKNOWN, never confidently
    # name — the CONCEPT/CLAUDE.md fail-safe rule (a foreign cat must not be labelled a
    # resident just because the gallery couldn't be calibrated). So even with clear A-vs-B
    # nearest matches, the event resolves to "unknown cat".
    store.write_identifications_batch(
        [
            (f1, vid, cat_a, 0.5, [0, 0, 1, 1]),
            (f2, vid, cat_a, 0.9, [0, 0, 1, 1]),
            (f3, vid, cat_b, 0.95, [0, 0, 1, 1]),
        ]
    )
    ident = store.events(None, None)["events"][0]["identity"]
    assert ident["cat_id"] is None  # unknown cat — fail-safe
    assert ident["cat_name"] is None
    assert ident["n_frames_voted"] == 0
    assert ident["n_identified"] == 3
    assert ident["distance"] == pytest.approx(0.5)  # nearest distance any frame reached


def test_events_identity_none_when_span_has_no_identifications(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    _one_event_ids(store, base, 2)  # a motion event, but no identifications written
    vid = _add_version(store, threshold=0.5)
    store.promote_model(vid)
    ev = store.events(None, None)["events"][0]
    assert ev["identity"] is None


def test_events_identity_marks_resident_vs_non_resident(tmp_path):
    # The activity feed distinguishes a resident match (our cat) from a named
    # foreign/neighbour match so the UI can flag a known stranger differently.
    store = _store(tmp_path)
    base = 1_700_000_000_000
    vid = _add_version(store, threshold=0.5)
    store.promote_model(vid)
    ours = store.create_cat("Mittens", is_resident=True)["id"]
    neighbour = store.create_cat("Tom", is_resident=False)["id"]

    # Event 1 (resident match), then event 2 far later (non-resident match).
    e1 = [_add(store, base + 100 * i, motion=True, area=0.1) for i in range(2)]
    e2 = [_add(store, base + 10 * _VISIT_GAP_MS + 100 * i, motion=True, area=0.1) for i in range(2)]
    store.write_identifications_batch(
        [
            (e1[0], vid, ours, 0.2, [0, 0, 1, 1]),
            (e2[0], vid, neighbour, 0.2, [0, 0, 1, 1]),
        ]
    )

    events = store.events(None, None)["events"]  # newest-first: e2 then e1
    by_cat = {ev["identity"]["cat_id"]: ev["identity"] for ev in events}
    assert by_cat[ours]["is_resident"] is True
    assert by_cat[neighbour]["is_resident"] is False


def test_events_identity_unknown_cat_carries_null_is_resident(tmp_path):
    # An "unknown cat" (identified but nothing near enough) has no matched cat, so
    # is_resident is None — the UI renders it as unknown regardless.
    store = _store(tmp_path)
    base = 1_700_000_000_000
    (f1,) = _one_event_ids(store, base, 1)
    vid = _add_version(store, threshold=0.1)  # tight cutoff
    store.promote_model(vid)
    cat = store.create_cat("A", is_resident=True)["id"]
    store.write_identifications_batch([(f1, vid, cat, 0.5, [0, 0, 1, 1])])
    ident = store.events(None, None)["events"][0]["identity"]
    assert ident["cat_id"] is None
    assert ident["is_resident"] is None


def test_events_identity_ignores_identifications_outside_event_spans(tmp_path):
    store = _store(tmp_path)
    base = 1_700_000_000_000
    # Event 1 (ids 1,2), a NON-motion gap frame (id 3), then event 2 far later (ids 4,5).
    e1 = [_add(store, base + 100 * i, motion=True, area=0.1) for i in range(2)]
    gap = _add(store, base + 300, motion=False)  # excluded from clustering
    e2 = [_add(store, base + 10 * _VISIT_GAP_MS + 100 * i, motion=True, area=0.1) for i in range(2)]
    assert gap == e1[-1] + 1 and e2[0] == gap + 1  # the gap frame's id sits between the events

    vid = _add_version(store, threshold=0.5)
    store.promote_model(vid)
    cat = store.create_cat("A")["id"]
    # The only identification is on the gap frame — inside [min_start, max_end] but
    # inside NO event's [start_id, end_id], so both events read as unidentified.
    store.write_identifications_batch([(gap, vid, cat, 0.1, [0, 0, 1, 1])])

    events = store.events(None, None)["events"]
    assert len(events) == 2
    assert all(ev["identity"] is None for ev in events)


def test_events_with_subject_false_omits_subject_but_keeps_identical_identity(tmp_path):
    """The identity-only path ``cats_overview`` takes must not change the identity.

    That reuse exists so Cats and Activity can never name the same moment
    differently, so skipping the subject annotation has to leave `identity`
    byte-identical — only `subject`/`detection` may disappear.
    """
    store = _store(tmp_path)
    base = 1_700_000_000_000
    ids = _one_event_ids(store, base, 3)
    vid = _add_version(store, threshold=0.5)
    store.promote_model(vid)
    cat = store.create_cat("A", is_resident=True)["id"]
    store.write_identifications_batch([(fid, vid, cat, 0.2, [0, 0, 1, 1]) for fid in ids])

    annotated = store.events(None, None)["events"]
    identity_only = store.events(None, None, with_subject=False)["events"]

    assert len(annotated) == len(identity_only) == 1
    # subject/detection are ABSENT (not None) — nothing computed them.
    assert "subject" not in identity_only[0] and "detection" not in identity_only[0]
    assert "subject" in annotated[0] and "detection" in annotated[0]
    # Everything else, identity included, is unchanged.
    for key, value in annotated[0].items():
        if key in ("subject", "detection"):
            continue
        assert identity_only[0][key] == value
    assert identity_only[0]["identity"]["cat_id"] == cat


def test_events_with_subject_false_skips_the_subject_promotion(tmp_path):
    """A named match promotes the subject to 'cat' — but only when there IS a subject.

    With no yolo-serial rows the span's subject would be motion-only, which a
    confident named identity promotes to 'cat'. The identity-only path must skip
    that promotion rather than KeyError on the subject it never built.
    """
    store = _store(tmp_path)
    base = 1_700_000_000_000
    ids = _one_event_ids(store, base, 2)
    vid = _add_version(store, threshold=0.5)
    store.promote_model(vid)
    cat = store.create_cat("A", is_resident=True)["id"]
    store.write_identifications_batch([(fid, vid, cat, 0.1, [0, 0, 1, 1]) for fid in ids])

    # With the annotation on, the named match promotes the subject.
    assert store.events(None, None)["events"][0]["subject"]["kind"] == "cat"
    # With it off, the call still succeeds and still names the cat.
    ev = store.events(None, None, with_subject=False)["events"][0]
    assert "subject" not in ev
    assert ev["identity"]["cat_name"] == "A"
