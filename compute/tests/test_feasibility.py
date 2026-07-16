"""Tests for the feasibility probe's metrics (compute.identification.feasibility)
and the store's labelled-crop reader. Pure numpy — no torch / model / matplotlib,
so the separability maths is exercised with synthetic embeddings and the crop
reader against a temp Store. The embedding path (Embedder) and the chart/HTML tool
are torch-/matplotlib-gated and run for real on the compute PC."""
from __future__ import annotations

import os

import numpy as np
import pytest

from compute.collection.store import Store
from compute.identification.feasibility import run_feasibility
from compute.ingest.client import StreamFrame, StreamFrameMeta

_JPEG = b"\xff\xd8\xff\xd9"  # minimal SOI+EOI; store.add writes bytes verbatim (no decode)


def _store(tmp_path) -> Store:
    return Store(db_path=str(tmp_path / "index.db"), media_root=str(tmp_path / "media"), max_bytes=10_000_000)


def _clustered(rng, n_per, centers, noise=0.02):
    ids, vecs = [], []
    for cid, center in enumerate(centers, start=1):
        base = np.array(center, dtype=float)
        for _ in range(n_per):
            ids.append(cid)
            vecs.append(base + rng.normal(0, noise, size=base.shape))
    return ids, np.array(vecs)


def test_run_feasibility_separable_scores_high():
    rng = np.random.default_rng(0)
    ids, emb = _clustered(rng, 8, [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0]], noise=0.02)
    m = run_feasibility(ids, {1: "A", 2: "B", 3: "C"}, emb)
    assert m["n_cats"] == 3 and m["n_crops"] == 24
    assert m["knn"]["accuracy"] >= 0.95
    assert m["distances"]["auc"] >= 0.95
    conf = np.array(m["knn"]["confusion"])
    assert conf.trace() >= 0.95 * conf.sum()  # diagonal-heavy


def test_run_feasibility_overlapping_scores_low():
    rng = np.random.default_rng(1)
    ids = [1] * 20 + [2] * 20
    emb = rng.normal(0, 1, size=(40, 8))  # both cats from ONE distribution → no separation
    m = run_feasibility(ids, {1: "A", 2: "B"}, emb)
    assert m["knn"]["accuracy"] < 0.85
    assert 0.35 <= m["distances"]["auc"] <= 0.65  # ~chance


def test_run_feasibility_shapes_and_bins():
    rng = np.random.default_rng(2)
    ids, emb = _clustered(rng, 5, [[1, 0, 0], [0, 1, 0]], noise=0.05)
    m = run_feasibility(ids, {1: "A", 2: "B"}, emb, n_bins=20)
    assert len(m["projection"]) == 10
    assert all(set(p) == {"x", "y", "cat_index"} for p in m["projection"])
    assert len(m["distances"]["hist"]["edges"]) == 21
    assert len(m["distances"]["hist"]["same"]) == 20
    assert 0.0 <= m["distances"]["suggested_threshold"] <= 2.0


def test_run_feasibility_requires_two_cats():
    emb = np.random.default_rng(3).normal(size=(4, 5))
    with pytest.raises(ValueError):
        run_feasibility([1, 1, 1, 1], {1: "A"}, emb)


def test_run_feasibility_requires_two_crops():
    with pytest.raises(ValueError):
        run_feasibility([1], {1: "A"}, np.zeros((1, 5)))


def _add_frame(store, fid):
    meta = StreamFrameMeta(frame_id=fid, ts=fid, motion=False, bbox=None, area=0.0)
    return store.add(StreamFrame(meta, _JPEG), recv_ts_ms=1000 + fid)


def test_labeled_crops_filters_and_absolutizes(tmp_path):
    store = _store(tmp_path)
    a = store.create_cat("A")
    b = store.create_cat("B")
    f1, f2, f3, f4 = (_add_frame(store, i) for i in (1, 2, 3, 4))
    store.add_dataset_items(
        [
            {"frame_id": f1, "label_kind": "identified", "cat_id": a["id"], "quality": "gallery",
             "bbox": [0, 0, 1, 1], "crop_path": f"cat_{a['id']}/1.jpg"},
            {"frame_id": f2, "label_kind": "identified", "cat_id": b["id"], "quality": "ok",
             "bbox": [0, 0, 1, 1], "crop_path": f"cat_{b['id']}/2.jpg"},
            {"frame_id": f3, "label_kind": "unknown_cat", "cat_id": None, "quality": "ok",
             "bbox": [0, 0, 1, 1], "crop_path": "cat_unknown_cat/3.jpg"},
            {"frame_id": f4, "label_kind": "not_cat", "cat_id": None, "quality": None,
             "bbox": None, "crop_path": None},
        ]
    )
    ident = store.labeled_crops(("identified",))
    assert len(ident) == 2
    assert {r["cat_name"] for r in ident} == {"A", "B"}
    assert all(os.path.isabs(r["crop_path"]) for r in ident)
    assert all(r["crop_path"].startswith(store.dataset_root) for r in ident)
    # not_cat (no crop file) is excluded; unknown_cat included only when asked for.
    both = store.labeled_crops(("identified", "unknown_cat"))
    assert len(both) == 3


def test_labeled_crops_quality_filter(tmp_path):
    store = _store(tmp_path)
    a = store.create_cat("A")
    b = store.create_cat("B")
    f1, f2, f3 = (_add_frame(store, i) for i in (1, 2, 3))
    store.add_dataset_items(
        [
            {"frame_id": f1, "label_kind": "identified", "cat_id": a["id"], "quality": "gallery",
             "bbox": [0, 0, 1, 1], "crop_path": f"cat_{a['id']}/1.jpg"},
            {"frame_id": f2, "label_kind": "identified", "cat_id": b["id"], "quality": "ok",
             "bbox": [0, 0, 1, 1], "crop_path": f"cat_{b['id']}/2.jpg"},
            {"frame_id": f3, "label_kind": "unknown_cat", "cat_id": None, "quality": "poor",
             "bbox": [0, 0, 1, 1], "crop_path": "cat_unknown_cat/3.jpg"},
        ]
    )
    # None (default) = no quality filter — every identified crop.
    assert len(store.labeled_crops(("identified",))) == 2
    # gallery-only drops the ok crop; the grades compose with label_kinds.
    gallery = store.labeled_crops(("identified",), ("gallery",))
    assert [r["cat_name"] for r in gallery] == ["A"]
    assert len(store.labeled_crops(("identified",), ("gallery", "ok"))) == 2
    assert len(store.labeled_crops(("identified", "unknown_cat"), ("poor",))) == 1
    # An explicitly empty selection yields nothing (symmetric with empty label_kinds).
    assert store.labeled_crops(("identified",), ()) == []
    # A bad grade is rejected, never silently ignored.
    with pytest.raises(ValueError):
        store.labeled_crops(("identified",), ("mint",))
