"""Tests for the Training page's durable validation-run history — the
``feasibility_runs`` table and its ``Store`` methods, plus ``count_identified_crops``
(compute/collection/store.py).

See docs/specs/2026-07-16-training-page.md. Pure-sqlite: no torch/matplotlib/cv2,
so it runs anywhere. Mirrors test_annotation.py's conventions — a ``_frame()``
helper builds ``StreamFrame``s directly (never decoded), and a ``_store`` factory
opens a Store under ``tmp_path``. Because ``add_dataset_items`` resolves
``src_recv_ts`` from a live frame, each labelled crop is anchored to a frame added
first.
"""
from __future__ import annotations

import os

import pytest

from compute.collection.store import Store
from compute.ingest import StreamFrame
from shared.wire import StreamFrameMeta

# A minimal but genuinely valid JPEG (SOI ... EOI); these tests never decode it.
_JPEG_BODY = b"\xff\xd8\xff\xe0" + b"fake-jpeg-body" + b"\xff\xd9"


def _frame(frame_id: int = 1, ts: int = 1_000, motion: bool = False) -> StreamFrame:
    """Build a ``StreamFrame`` directly — the shape ``Store.add`` consumes."""
    meta = StreamFrameMeta(frame_id=frame_id, ts=ts, motion=motion, bbox=None, area=0.0)
    return StreamFrame(meta, _JPEG_BODY)


def _store(tmp_path, max_bytes: int = 10_000_000) -> Store:
    return Store(
        db_path=str(tmp_path / "index.db"),
        media_root=str(tmp_path / "media"),
        max_bytes=max_bytes,
    )


def _label(store: Store, cat_id: int, frame_id: int, quality: str = "gallery") -> None:
    """Add a live frame then an ``identified`` crop row anchored to it."""
    row_id = store.add(_frame(frame_id=frame_id, ts=frame_id * 1000), recv_ts_ms=frame_id * 1000)
    n = store.add_dataset_items(
        [
            {
                "frame_id": row_id,
                "label_kind": "identified",
                "cat_id": cat_id,
                "quality": quality,
                "bbox": [0, 0, 10, 10],
                "crop_path": f"c{frame_id}.jpg",
            }
        ]
    )
    assert n == 1


def _write_report(store: Store, report_dir: str) -> None:
    """Materialise a run's ``feasibility.html`` under the store's training_root."""
    d = os.path.join(store.training_root, report_dir)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "feasibility.html"), "w", encoding="utf-8") as fh:
        fh.write("<html>report</html>")


# --- Schema ----------------------------------------------------------------


def test_schema_creates_feasibility_runs_table(tmp_path):
    store = _store(tmp_path)
    tables = {
        row[0]
        for row in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert "feasibility_runs" in tables


def test_training_root_under_collection_root(tmp_path):
    store = _store(tmp_path)
    assert store.training_root == os.path.join(str(tmp_path), "training")
    # Lazy: not created eagerly by __init__ (reports are rare).
    assert not os.path.isdir(store.training_root)


# --- add + list roundtrip --------------------------------------------------


def test_add_and_list_roundtrip_and_ordering(tmp_path):
    store = _store(tmp_path)
    r1 = store.add_feasibility_run("all", 10, 3, 0.8, 0.9, 0.42, "1000-all", notes="first")
    r2 = store.add_feasibility_run("gallery", 5, 2, 0.6, None, None, "2000-gallery")
    assert r2 > r1

    runs = store.feasibility_runs()
    assert [r["run_id"] for r in runs] == [r2, r1]  # most-recent-first
    newest = runs[0]
    assert newest["quality"] == "gallery"
    assert newest["n_crops"] == 5
    assert newest["n_cats"] == 2
    assert newest["auc"] is None
    assert newest["threshold"] is None
    assert newest["notes"] is None

    oldest = runs[1]
    assert oldest["knn_accuracy"] == pytest.approx(0.8)
    assert oldest["auc"] == pytest.approx(0.9)
    assert oldest["threshold"] == pytest.approx(0.42)
    assert oldest["notes"] == "first"
    assert oldest["ts"] > 0


def test_feasibility_runs_limit(tmp_path):
    store = _store(tmp_path)
    for i in range(5):
        store.add_feasibility_run("all", i + 2, 2, 0.5, 0.5, 0.5, f"{i}-all")
    limited = store.feasibility_runs(limit=2)
    assert len(limited) == 2  # newest two only
    assert limited[0]["n_crops"] == 6
    assert limited[1]["n_crops"] == 5


# --- report_available / report path ---------------------------------------


def test_report_available_true_and_false(tmp_path):
    store = _store(tmp_path)
    have = store.add_feasibility_run("all", 4, 2, 0.7, 0.7, 0.3, "have")
    _write_report(store, "have")
    _missing = store.add_feasibility_run("gallery", 4, 2, 0.7, 0.7, 0.3, "missing")

    by_id = {r["run_id"]: r for r in store.feasibility_runs()}
    assert by_id[have]["report_available"] is True
    assert by_id[_missing]["report_available"] is False

    have_path = store.feasibility_run_report_path(have)
    assert have_path is not None and os.path.isfile(have_path)
    assert store.feasibility_run_report_path(_missing) is None
    assert store.feasibility_run_report_path(999_999) is None  # unknown id


# --- pruning ---------------------------------------------------------------


def test_prune_keeps_newest_k_and_swallows_missing(tmp_path):
    store = _store(tmp_path)
    # Add the dir-less run FIRST so it is the OLDEST — this puts it inside the
    # prune slice (not the kept-newest tail), which is what actually exercises the
    # "dir already gone / never written" skip. Adding it last (highest id) would
    # keep it and never touch that path — the bug this test now guards against.
    missing_id = store.add_feasibility_run("all", 9, 2, 0.5, 0.5, 0.5, "never-written")
    ids = []
    for i in range(4):
        rid = store.add_feasibility_run("all", i + 2, 2, 0.5, 0.5, 0.5, f"r{i}")
        ids.append(rid)
        _write_report(store, f"r{i}")

    # 5 runs; newest-first: r3, r2, r1, r0, never-written. keep=2 => r3, r2 kept.
    # Older set to prune: r1, r0 (dirs on disk) + never-written (NO dir → skipped
    # by the isdir guard, must not raise). So exactly 2 dirs are removed.
    removed = store.prune_feasibility_reports(keep=2)
    assert removed == 2
    assert os.path.isdir(os.path.join(store.training_root, "r3"))
    assert os.path.isdir(os.path.join(store.training_root, "r2"))
    assert not os.path.isdir(os.path.join(store.training_root, "r1"))
    assert not os.path.isdir(os.path.join(store.training_root, "r0"))

    # ALL rows survive pruning — only dirs go, including the dir-less run's row.
    assert len(store.feasibility_runs()) == 5
    assert store.feasibility_run_report_path(missing_id) is None
    assert store.feasibility_run_report_path(ids[3]) is not None  # newest-kept still serves


def test_prune_noop_when_fewer_than_keep(tmp_path):
    store = _store(tmp_path)
    store.add_feasibility_run("all", 2, 2, 0.5, 0.5, 0.5, "only")
    _write_report(store, "only")
    assert store.prune_feasibility_reports(keep=25) == 0
    assert os.path.isdir(os.path.join(store.training_root, "only"))


# --- count_identified_crops ------------------------------------------------


def test_count_identified_crops_with_and_without_quality(tmp_path):
    store = _store(tmp_path)
    a = store.create_cat("Mittens", is_resident=True)["id"]
    b = store.create_cat("Whiskers", is_resident=True)["id"]
    _label(store, a, frame_id=1, quality="gallery")
    _label(store, a, frame_id=2, quality="ok")
    _label(store, b, frame_id=3, quality="gallery")
    _label(store, b, frame_id=4, quality="poor")

    # No filter: every identified crop, across both cats.
    assert store.count_identified_crops(None) == (4, 2)
    # Gallery-only: one per cat.
    assert store.count_identified_crops(("gallery",)) == (2, 2)
    # Gallery+ok: three crops but ok is only cat a.
    assert store.count_identified_crops(("gallery", "ok")) == (3, 2)
    # A grade only cat b has.
    assert store.count_identified_crops(("poor",)) == (1, 1)
    # Empty tuple selects nothing (symmetric with labeled_crops).
    assert store.count_identified_crops(()) == (0, 0)


def test_count_identified_crops_rejects_bad_grade(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        store.count_identified_crops(("gallery", "bogus"))


# --- clear() preserves feasibility_runs ------------------------------------


def test_clear_preserves_feasibility_runs_but_wipes_frames(tmp_path):
    store = _store(tmp_path)
    store.add(_frame(frame_id=1, ts=1000), recv_ts_ms=1000)
    store.add(_frame(frame_id=2, ts=2000), recv_ts_ms=2000)
    rid = store.add_feasibility_run("all", 4, 2, 0.7, 0.7, 0.3, "keepme", notes="precious")

    assert store.stats()["count"] == 2
    n = store.clear()
    assert n == 2
    assert store.stats()["count"] == 0  # frames wiped

    runs = store.feasibility_runs()
    assert len(runs) == 1  # history survives
    assert runs[0]["run_id"] == rid
    assert runs[0]["notes"] == "precious"
