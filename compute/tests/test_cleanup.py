"""Tests for the cleanup purges (admin-next P7) — the DATA-DESTRUCTIVE slice.

Covers the store primitives (``purge_nonmotion_batch`` / orphan sweep / purge-span
recording) and the ``CleanupManager`` end-to-end, plus the ``/api/cleanup/*`` routes.
All run with a real temp ``Store`` and NO GPU/torch — the purge path is pure SQLite +
filesystem.

The invariants under test (the ones the adversarial verifiers care about):

- a non-motion purge decrements ``_count`` / ``_motion_count`` / ``_total_bytes`` EXACTLY
  (never a raw DELETE that would drift the retention cap);
- it NEVER removes a ``dataset_items`` crop / ``model_versions`` row / durable crop file;
- the orphan sweep is scoped to the frames media dir and never touches referenced files
  nor the sibling dataset/avatar dirs;
- a purge records a span so ``motion_only_spans`` flags the window "unmeasurable" — and
  leaves that method byte-identical when NO purge exists;
- a cancel mid-run leaves the store fully consistent.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from compute.api.app import create_app
from compute.collection.cleanup import CleanupManager
from compute.collection.store import Store
from compute.ingest import StreamFrame
from shared.wire import StreamFrameMeta

_JPEG = b"\xff\xd8\xff\xe0" + b"body" + b"\xff\xd9"


def _frame(frame_id: int, motion: bool, body: bytes = _JPEG) -> StreamFrame:
    meta = StreamFrameMeta(
        frame_id=frame_id, ts=frame_id, motion=motion, bbox=(0.1, 0.1, 0.2, 0.2) if motion else None, area=0.05 if motion else 0.0
    )
    return StreamFrame(meta, body)


def _store(tmp_path, max_bytes: int = 10_000_000) -> Store:
    return Store(
        db_path=str(tmp_path / "index.db"),
        media_root=str(tmp_path / "media"),
        max_bytes=max_bytes,
        dataset_root=str(tmp_path / "dataset"),
    )


def _seed(store: Store, n_motion: int, n_still: int) -> list[int]:
    """Add ``n_motion`` motion frames then ``n_still`` still frames; return all row ids."""
    ids = []
    fid = 1
    for _ in range(n_motion):
        ids.append(store.add(_frame(fid, motion=True), recv_ts_ms=1_000 + fid))
        fid += 1
    for _ in range(n_still):
        ids.append(store.add(_frame(fid, motion=False), recv_ts_ms=1_000 + fid))
        fid += 1
    return ids


def _db_reality(store: Store) -> tuple[int, int, int]:
    """(count, motion_count, total_bytes) read DIRECTLY from the DB, bypassing the counters."""
    row = store._conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(motion), 0), COALESCE(SUM(bytes), 0) FROM frames"
    ).fetchone()
    return int(row[0]), int(row[1]), int(row[2])


def _drain_nonmotion(store: Store, until_id=None, batch: int = 3) -> int:
    """Loop the store's batched purge to completion; return frames deleted."""
    deleted = 0
    while True:
        n, _max = store.purge_nonmotion_batch(until_id, batch)
        if n == 0:
            break
        deleted += n
    return deleted


# --- Counter accounting -------------------------------------------------------


def test_purge_nonmotion_decrements_counters_exactly(tmp_path):
    store = _store(tmp_path)
    _seed(store, n_motion=4, n_still=7)
    # Counters agree with the DB before.
    assert (store._count, store._motion_count, store._total_bytes) == _db_reality(store)

    deleted = _drain_nonmotion(store)

    assert deleted == 7  # every still frame
    # In-memory counters exactly track the DB after the purge.
    assert (store._count, store._motion_count, store._total_bytes) == _db_reality(store)
    stats = store.stats()
    assert stats["count"] == 4
    assert stats["motion_count"] == 4  # motion count UNCHANGED — only still frames dropped
    # And stats() (which reads the in-memory counters) equals the DB truth.
    assert (stats["count"], stats["motion_count"], stats["bytes"]) == _db_reality(store)


def test_purge_nonmotion_cascades_analysis_and_identifications(tmp_path):
    store = _store(tmp_path)
    ids = _seed(store, n_motion=1, n_still=2)
    still_id = ids[1]
    # A verdict + an identification about a STILL frame that is about to be purged.
    store.write_analysis_batch([(still_id, "yolo-serial", False, 0.1, None)])
    mv = store.add_model_version(
        status="active", kind="gallery", backbone="x", imgsz=14, n_cats=1,
        n_vectors=1, threshold=0.3, quality="all", metrics=None, gallery_dir="g",
    )
    store.write_identifications_batch([(still_id, mv, None, None, None)])
    assert store._conn.execute("SELECT COUNT(*) FROM analysis").fetchone()[0] == 1
    assert store._conn.execute("SELECT COUNT(*) FROM identifications").fetchone()[0] == 1

    _drain_nonmotion(store)

    # The frame-keyed rows cascade with the purged frame (same path as eviction).
    assert store._conn.execute("SELECT COUNT(*) FROM analysis").fetchone()[0] == 0
    assert store._conn.execute("SELECT COUNT(*) FROM identifications").fetchone()[0] == 0
    # The model_versions row is PRECIOUS — never touched.
    assert store._conn.execute("SELECT COUNT(*) FROM model_versions").fetchone()[0] == 1


def test_purge_nonmotion_before_until_id_leaves_newer_still_frames(tmp_path):
    store = _store(tmp_path)
    ids = _seed(store, n_motion=0, n_still=6)
    until = ids[2]  # purge only the first three still frames
    deleted = _drain_nonmotion(store, until_id=until, batch=2)
    assert deleted == 3
    remaining = {r[0] for r in store._conn.execute("SELECT id FROM frames").fetchall()}
    assert remaining == set(ids[3:])
    assert (store._count, store._motion_count, store._total_bytes) == _db_reality(store)


# --- Never touches durable output --------------------------------------------


def test_purge_never_deletes_dataset_items_or_crop_files(tmp_path):
    store = _store(tmp_path)
    ids = _seed(store, n_motion=1, n_still=1)
    still_id = ids[1]
    # A durable crop labelled off the STILL frame (the frame is purged; the crop must survive).
    crop_rel = "cat1/crop.jpg"
    crop_abs = os.path.join(store.dataset_root, crop_rel)
    os.makedirs(os.path.dirname(crop_abs), exist_ok=True)
    with open(crop_abs, "wb") as fh:
        fh.write(_JPEG)
    inserted = store.add_dataset_items([
        {"frame_id": still_id, "label_kind": "identified", "cat_id": 1,
         "quality": "gallery", "bbox": [0, 0, 1, 1], "crop_path": crop_rel}
    ])
    assert inserted == 1

    _drain_nonmotion(store)

    # The label row and its crop file both survive the frame's purge.
    assert store._conn.execute("SELECT COUNT(*) FROM dataset_items").fetchone()[0] == 1
    assert os.path.isfile(crop_abs)
    # And the source frame really is gone.
    assert store._conn.execute("SELECT COUNT(*) FROM frames WHERE id = ?", (still_id,)).fetchone()[0] == 0


# --- Orphan sweep -------------------------------------------------------------


def test_orphan_sweep_scoped_and_precise(tmp_path):
    store = _store(tmp_path)
    ids = _seed(store, n_motion=1, n_still=1)
    # A real frame's file must be KEPT (it has a row).
    kept_rel = store._conn.execute("SELECT path FROM frames WHERE id = ?", (ids[0],)).fetchone()[0]
    kept_abs = os.path.join(tmp_path, "media", kept_rel)
    assert os.path.isfile(kept_abs)

    # An orphan JPEG under the media dir (no frames row) — must be swept.
    orphan_abs = os.path.join(tmp_path, "media", "2020-01-01", "00", "orphan.jpg")
    os.makedirs(os.path.dirname(orphan_abs), exist_ok=True)
    with open(orphan_abs, "wb") as fh:
        fh.write(_JPEG)

    # A dataset/avatar file OUTSIDE the media dir — must NOT be swept even though it
    # has no frames row (that is the whole point of scoping to the media dir).
    avatar_abs = os.path.join(store.dataset_root, "avatars", "cat_1.jpg")
    os.makedirs(os.path.dirname(avatar_abs), exist_ok=True)
    with open(avatar_abs, "wb") as fh:
        fh.write(_JPEG)

    est = store.orphan_estimate()
    assert est["count"] == 1  # only the media-dir orphan

    all_rel = list(store.iter_media_relpaths())
    res = store.delete_orphan_batch(all_rel)

    assert res["deleted"] == 1
    assert not os.path.exists(orphan_abs)  # orphan gone
    assert os.path.isfile(kept_abs)  # referenced frame file kept
    assert os.path.isfile(avatar_abs)  # dataset/avatar file untouched
    # No frames rows were harmed.
    assert store._conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0] == 2


def test_iter_media_relpaths_excludes_dataset_dir(tmp_path):
    store = _store(tmp_path)
    _seed(store, n_motion=1, n_still=0)
    # Put a jpg under the dataset root; it must never appear in the media walk.
    stray = os.path.join(store.dataset_root, "sub", "x.jpg")
    os.makedirs(os.path.dirname(stray), exist_ok=True)
    with open(stray, "wb") as fh:
        fh.write(_JPEG)
    rels = list(store.iter_media_relpaths())
    assert all("dataset" not in r for r in rels)
    assert len(rels) == 1  # only the one frame's file


# --- Purge span recording -----------------------------------------------------


def test_motion_only_spans_unchanged_without_purge(tmp_path):
    store = _store(tmp_path)
    _seed(store, n_motion=2, n_still=2)
    # No purge, no mode changes → empty (historical behavior preserved).
    assert store.motion_only_spans() == []
    # With a motion-only mode span but still no purge, the result is exactly the
    # step-function output — the purge integration must not alter it.
    store.record_mode_change(True)  # motion-only ON from the current tail
    baseline = store.motion_only_spans()
    assert baseline == store.motion_only_spans()  # deterministic
    assert store._conn.execute("SELECT COUNT(*) FROM purge_spans").fetchone()[0] == 0


def test_purge_records_span_flagging_window_unmeasurable(tmp_path):
    store = _store(tmp_path)
    ids = _seed(store, n_motion=2, n_still=4)
    assert store.motion_only_spans() == []  # nothing flagged before

    since_id, _ = store.frame_id_bounds()
    mgr = CleanupManager()
    result = _run_to_completion(mgr, lambda: mgr.start_nonmotion(store, until_id=ids[-1], since_id=since_id))

    assert result["kind"] == "nonmotion"
    assert result["deleted"] == 4
    assert result["span_recorded"] is True
    spans = store.motion_only_spans()
    assert len(spans) == 1
    assert spans[0]["start_id"] <= since_id
    assert spans[0]["end_id"] >= ids[-1]


def test_purge_span_survives_and_flags_after_frames_gone(tmp_path):
    # Even after the non-motion frames are purged, a scorecard-style overlap check
    # (bool(motion_only_spans(window))) must warn over the window.
    store = _store(tmp_path)
    ids = _seed(store, n_motion=1, n_still=3)
    store.record_purge_span(ids[0], ids[-1])
    assert bool(store.motion_only_spans(ids[0], ids[-1])) is True
    # A window entirely outside the recorded span is not flagged.
    assert store.motion_only_spans(ids[-1] + 100, ids[-1] + 200) == []


# --- Cancel mid-run leaves a consistent store --------------------------------


def test_cancel_midbatch_leaves_consistent_store(tmp_path):
    store = _store(tmp_path)
    ids = _seed(store, n_motion=2, n_still=10)
    since_id, _ = store.frame_id_bounds()

    # Simulate a cancel after exactly one batch: run one batch, then record the span
    # for ONLY what was purged (the manager's cancel path) and stop.
    n, last_max = store.purge_nonmotion_batch(until_id=ids[-1], batch_size=3)
    assert n == 3
    # Store is fully consistent after the partial purge: counters == DB truth.
    assert (store._count, store._motion_count, store._total_bytes) == _db_reality(store)
    # Recording the truncated span must flag ONLY up to what was actually purged,
    # never up to the intended until_id (frames past last_max still have their stills).
    store.record_purge_span(since_id, last_max)
    spans = store.motion_only_spans()
    assert len(spans) == 1
    assert spans[0]["end_id"] == last_max
    assert last_max < ids[-1]  # did not over-claim the whole window


def test_manager_cancel_is_consistent_and_idle(tmp_path):
    store = _store(tmp_path)
    _seed(store, n_motion=1, n_still=200)
    mgr = CleanupManager()
    since_id, until_id = store.frame_id_bounds()
    mgr.start_nonmotion(store, until_id=until_id, since_id=since_id)
    mgr.cancel()  # may or may not catch it mid-run; either way must end consistent
    _wait_idle(mgr)
    # Whatever ran, the store's counters match the DB and the manager is idle with a result.
    assert (store._count, store._motion_count, store._total_bytes) == _db_reality(store)
    assert mgr.status()["running"] is False
    assert mgr.status()["result"] is not None


# --- Manager end-to-end -------------------------------------------------------


def test_manager_nonmotion_end_to_end(tmp_path):
    store = _store(tmp_path)
    _seed(store, n_motion=3, n_still=8)
    mgr = CleanupManager()
    since_id, until_id = store.frame_id_bounds()
    result = _run_to_completion(mgr, lambda: mgr.start_nonmotion(store, until_id, since_id))
    assert result["deleted"] == 8
    assert result["canceled"] is False
    assert store.stats()["count"] == 3
    assert store.stats()["motion_count"] == 3


def test_manager_orphan_end_to_end(tmp_path):
    store = _store(tmp_path)
    _seed(store, n_motion=1, n_still=1)
    orphan = os.path.join(tmp_path, "media", "d", "h", "o.jpg")
    os.makedirs(os.path.dirname(orphan), exist_ok=True)
    with open(orphan, "wb") as fh:
        fh.write(_JPEG)
    mgr = CleanupManager()
    result = _run_to_completion(mgr, lambda: mgr.start_orphan(store))
    assert result["kind"] == "orphan"
    assert result["deleted"] == 1
    assert not os.path.exists(orphan)


def test_manager_refuses_second_job_while_running(tmp_path):
    store = _store(tmp_path)
    _seed(store, n_motion=1, n_still=500)
    mgr = CleanupManager()
    since_id, until_id = store.frame_id_bounds()
    first = mgr.start_nonmotion(store, until_id, since_id)
    assert first["started"] is True
    # A second start while the first may still be running is refused (busy) OR the
    # first already finished (started True). Only assert the mutual-exclusion shape.
    second = mgr.start_orphan(store)
    if not second["started"]:
        assert second["busy"] is True
    _wait_idle(mgr)


# --- API routes ---------------------------------------------------------------


def _client(tmp_path):
    store = _store(tmp_path)
    app = create_app(store=store, start_collector=False)
    return TestClient(app), store


def test_api_cleanup_estimate_and_run_nonmotion(tmp_path):
    client, store = _client(tmp_path)
    _seed(store, n_motion=2, n_still=5)

    est = client.get("/api/cleanup/estimate", params={"kind": "nonmotion"}).json()
    assert est["kind"] == "nonmotion"
    assert est["count"] == 5
    assert est["bytes"] > 0

    run = client.post("/api/cleanup/run", json={"kind": "nonmotion"})
    assert run.status_code == 200
    # Poll to completion.
    for _ in range(200):
        st = client.get("/api/cleanup/status").json()
        if not st["running"]:
            break
    assert st["result"]["deleted"] == 5
    assert store.stats()["count"] == 2


def test_api_cleanup_before_ts_preceding_all_frames_purges_nothing(tmp_path):
    # A before-date earlier than every frame must purge NOTHING (not the whole store,
    # the trap a None until_id would spring).
    client, store = _client(tmp_path)
    ids = _seed(store, n_motion=1, n_still=3)
    oldest_ts = store._conn.execute("SELECT MIN(recv_ts) FROM frames").fetchone()[0]
    est = client.get(
        "/api/cleanup/estimate", params={"kind": "nonmotion", "before_ts": oldest_ts - 1000}
    ).json()
    assert est["count"] == 0
    client.post("/api/cleanup/run", json={"kind": "nonmotion", "before_ts": oldest_ts - 1000})
    for _ in range(200):
        if not client.get("/api/cleanup/status").json()["running"]:
            break
    assert store.stats()["count"] == 4  # untouched


def test_api_cleanup_estimate_orphan(tmp_path):
    client, store = _client(tmp_path)
    _seed(store, n_motion=1, n_still=0)
    orphan = os.path.join(tmp_path, "media", "d", "h", "o.jpg")
    os.makedirs(os.path.dirname(orphan), exist_ok=True)
    with open(orphan, "wb") as fh:
        fh.write(_JPEG)
    est = client.get("/api/cleanup/estimate", params={"kind": "orphan"}).json()
    assert est == {"kind": "orphan", "count": 1, "bytes": len(_JPEG)}


def test_api_cleanup_bad_kind(tmp_path):
    client, _ = _client(tmp_path)
    assert client.get("/api/cleanup/estimate", params={"kind": "nope"}).status_code == 400
    assert client.post("/api/cleanup/run", json={"kind": "nope"}).status_code == 400


def test_api_cleanup_cancel_when_idle_is_ok(tmp_path):
    client, _ = _client(tmp_path)
    r = client.post("/api/cleanup/cancel")
    assert r.status_code == 200
    assert r.json()["running"] is False


# --- helpers ------------------------------------------------------------------


def _wait_idle(mgr: CleanupManager, tries: int = 500) -> None:
    import time
    for _ in range(tries):
        if not mgr.running:
            return
        time.sleep(0.005)
    raise AssertionError("cleanup manager did not go idle")


def _run_to_completion(mgr: CleanupManager, start) -> dict:
    start()
    _wait_idle(mgr)
    st = mgr.status()
    assert st["error"] is None, st
    return st["result"]


def test_purge_nonmotion_batch_rolls_back_and_resyncs_on_midbatch_error(tmp_path, monkeypatch):
    # A mid-batch delete failure must not drift the counters: the batch rolls back
    # and the in-memory counters resync from the committed DB (mirroring add()'s
    # recovery), so _count/_motion_count/_total_bytes never diverge after an error.
    store = _store(tmp_path)
    _seed(store, n_motion=2, n_still=6)
    before = _db_reality(store)
    real = store._delete_frame_locked
    n_calls = {"n": 0}

    def flaky(*args, **kwargs):
        n_calls["n"] += 1
        if n_calls["n"] == 3:  # fail partway through the batch, after two mutations
            raise RuntimeError("boom mid-batch")
        return real(*args, **kwargs)

    monkeypatch.setattr(store, "_delete_frame_locked", flaky)
    with pytest.raises(RuntimeError):
        store.purge_nonmotion_batch(until_id=None, batch_size=5)
    # Whole batch rolled back → DB unchanged, and the counters equal the DB truth.
    assert _db_reality(store) == before
    assert (store._count, store._motion_count, store._total_bytes) == _db_reality(store)
