"""Tests for the runtime gallery + identify layer (compute/identification/gallery.py).

See docs/specs/2026-07-17-identification-gallery-activity.md. Mirrors the existing
identification suite's discipline (test_probe.py / test_feasibility.py): the pure-numpy
pieces (``match`` / ``load_gallery``) are exercised with hand-built vectors and a
hand-written ``gallery.npz``, no torch involved at all. ``build_gallery`` and
``run_identify`` DO reach a real ``Embedder`` internally, so those are exercised by
monkeypatching ``compute.identification.gallery.Embedder`` to a stub — its
``prepare()`` is a no-op and ``embed_paths``/``embed_crops`` return deterministic
synthetic vectors keyed by path (a dict the test controls), never touching
cv2/torch. Everything else (the store, the dataset/analysis rows the identify pass
reads) is a real temp ``Store``, so a successful run is checked by actually reading
back its ``identifications`` rows.
"""
from __future__ import annotations

import os

import numpy as np
import pytest

from compute.collection.store import Store
from compute.identification import gallery
from compute.identification.gallery import Gallery, build_gallery, load_gallery, match, run_identify
from compute.ingest.client import StreamFrame, StreamFrameMeta

_JPEG = b"\xff\xd8\xff\xd9"  # minimal SOI+EOI; store.add writes bytes verbatim (no decode)


def _store(tmp_path) -> Store:
    return Store(db_path=str(tmp_path / "index.db"), media_root=str(tmp_path / "media"), max_bytes=10_000_000)


def _add_frame(store: Store, fid: int) -> int:
    meta = StreamFrameMeta(frame_id=fid, ts=fid, motion=False, bbox=None, area=0.0)
    return store.add(StreamFrame(meta, _JPEG), recv_ts_ms=1000 + fid)


def _stub_embedder_factory(vectors: "dict[str, list[float]]", *, skip: "set[str] | None" = None):
    """Build an ``Embedder``-shaped stub class bound to ``vectors`` (path -> raw vector).

    A fresh class is returned per call so each test's fixture is independent (the
    factory closes over ``vectors``/``skip``, not a shared mutable). No cv2/torch:
    ``prepare()`` is a no-op, and both ``embed_paths`` (paths) and ``embed_crops``
    (``(path, box)`` pairs — the box is ignored, matching how the real ``Embedder``
    only depends on the decoded pixels) just look ``vectors`` up by path. A path in
    ``skip`` is dropped from the output — simulating an undecodable crop — so its
    position is absent from ``kept``, exactly the contract ``build_gallery`` /
    ``run_identify`` rely on to re-align labels/frame-ids to the returned rows.
    """
    skip_set = set(skip or ())

    class _StubEmbedder:
        def __init__(self, model: "str | None" = None, imgsz: "int | None" = None) -> None:
            # Matches BOTH real call shapes: build_gallery's bare ``Embedder()`` and
            # run_identify's ``Embedder(model=..., imgsz=...)``.
            self.model_name = model or "stub-backbone"
            self._imgsz = imgsz if imgsz is not None else 32
            self.prepared = False

        @property
        def backbone(self) -> str:
            return self.model_name

        @property
        def imgsz(self) -> int:
            return self._imgsz

        def prepare(self) -> None:
            self.prepared = True

        def _lookup(self, paths: "list[str]"):
            vecs, kept = [], []
            for i, p in enumerate(paths):
                if p in skip_set:
                    continue
                vecs.append(vectors[p])
                kept.append(i)
            arr = np.asarray(vecs, dtype=np.float32) if vecs else np.zeros((0, 0), dtype=np.float32)
            return arr, kept

        def embed_paths(self, paths, batch_size=32, progress=None):
            if progress is not None:
                progress(0, len(paths))
            arr, kept = self._lookup(paths)
            if progress is not None:
                progress(len(paths), len(paths))
            return arr, kept

        def embed_crops(self, items, batch_size=32, progress=None):
            paths = [p for p, _box in items]
            if progress is not None:
                progress(0, len(items))
            arr, kept = self._lookup(paths)
            if progress is not None:
                progress(len(items), len(items))
            return arr, kept

    return _StubEmbedder


class _NeverConstructed:
    """A stand-in Embedder that fails the test if it is ever instantiated.

    Used to prove ``build_gallery``'s cold-start guard returns BEFORE constructing
    an embedder at all — the real reason the guard is cheap on a fresh install.
    """

    def __init__(self, *args, **kwargs) -> None:
        raise AssertionError("Embedder must not be constructed when the label-count guard fires")


# --- match() -------------------------------------------------------------------


def test_match_returns_nearest_cat_and_distance():
    gal = Gallery(
        vectors=np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]], dtype=np.float32),
        cat_ids=np.array([10, 20, 30]),
        backbone="stub",
        imgsz=32,
    )
    # q0 sits closest to cat 20's axis (mostly-y, small noise); q1 is an exact
    # (rescaled) match for cat 30's axis, so its distance must land at 0.
    queries = np.array([[0.1, 0.95, 0.05], [0.0, 0.0, 7.0]])
    result = match(gal, queries)
    assert len(result) == 2
    cat0, dist0 = result[0]
    assert cat0 == 20
    assert 0.0 < dist0 < 0.2
    assert result[1] == (30, pytest.approx(0.0, abs=1e-6))


def test_match_handles_multiple_vectors_per_cat():
    # Cat 1 is enrolled with two poses/vectors; k=1 must still resolve correctly
    # against whichever of its own vectors is nearer, not a per-cat centroid.
    gal = Gallery(
        vectors=np.array([[1.0, 0.0], [0.9, 0.1], [0.0, 1.0]], dtype=np.float32),
        cat_ids=np.array([1, 1, 2]),
        backbone="stub",
        imgsz=16,
    )
    result = match(gal, np.array([[0.95, 0.05]]))
    assert len(result) == 1
    cat_id, dist = result[0]
    assert cat_id == 1
    assert dist >= 0.0


def test_match_empty_gallery_returns_empty_list():
    gal = Gallery(
        vectors=np.zeros((0, 4), dtype=np.float32),
        cat_ids=np.zeros((0,), dtype=np.int64),
        backbone="stub",
        imgsz=32,
    )
    assert match(gal, np.array([[1.0, 0.0, 0.0, 0.0]])) == []


def test_match_empty_queries_returns_empty_list():
    gal = Gallery(
        vectors=np.array([[1.0, 0.0]], dtype=np.float32),
        cat_ids=np.array([1]),
        backbone="stub",
        imgsz=32,
    )
    assert match(gal, np.zeros((0, 2))) == []


# --- load_gallery ----------------------------------------------------------------


def test_load_gallery_normalizes_vectors_and_preserves_metadata(tmp_path):
    # A hand-written gallery.npz, exactly the shape build_gallery writes: RAW
    # (non-unit) vectors + parallel cat_ids + the backbone/imgsz that produced them.
    raw = np.array([[2.0, 0.0, 0.0], [0.0, 0.0, 5.0]], dtype=np.float32)
    cat_ids = np.array([7, 9], dtype=np.int64)
    path = tmp_path / "gallery.npz"
    np.savez(path, vectors=raw, cat_ids=cat_ids, backbone="dinov2_vits14", imgsz=224)

    gal = load_gallery(str(path))
    assert isinstance(gal, Gallery)
    assert gal.backbone == "dinov2_vits14"
    assert gal.imgsz == 224
    assert list(gal.cat_ids) == [7, 9]
    # L2-normalised on load — every row has unit norm regardless of the raw magnitude.
    assert np.allclose(np.linalg.norm(gal.vectors, axis=1), 1.0)
    # Direction is preserved: normalising the raw vectors by hand matches the load.
    expected = raw / np.linalg.norm(raw, axis=1, keepdims=True)
    assert np.allclose(gal.vectors, expected)


def test_load_gallery_zero_vector_row_stays_zero_no_nan(tmp_path):
    raw = np.array([[0.0, 0.0], [3.0, 4.0]], dtype=np.float32)
    path = tmp_path / "gallery.npz"
    np.savez(path, vectors=raw, cat_ids=np.array([1, 2]), backbone="stub", imgsz=32)

    gal = load_gallery(str(path))
    assert not np.isnan(gal.vectors).any()
    assert np.allclose(gal.vectors[0], [0.0, 0.0])
    assert np.allclose(gal.vectors[1], [0.6, 0.8])


# --- build_gallery -----------------------------------------------------------------


def test_build_gallery_writes_npz_raw_and_summary(tmp_path, monkeypatch):
    store = _store(tmp_path)
    cat_a = store.create_cat("A")
    cat_b = store.create_cat("B")
    frames = [_add_frame(store, i) for i in range(1, 7)]

    # 3 crops per cat, each mapped to an identical (per-cat) NON-unit raw vector —
    # this makes the same-cat pair distance exactly 0 and the different-cat pair
    # distance exactly 1, so the suggested threshold/balanced-accuracy land on a
    # hand-verifiable value (0.0 / 1.0) rather than a fuzzy noisy one.
    rows = []
    vectors_by_path: "dict[str, list[float]]" = {}
    for i, fid in enumerate(frames[:3]):
        crop_path = f"cat_{cat_a['id']}/{i}.jpg"
        rows.append({"frame_id": fid, "label_kind": "identified", "cat_id": cat_a["id"],
                     "quality": "gallery", "bbox": [0, 0, 5, 5], "crop_path": crop_path})
        vectors_by_path[os.path.join(store.dataset_root, crop_path)] = [2.0, 0.0, 0.0, 0.0]
    for i, fid in enumerate(frames[3:]):
        crop_path = f"cat_{cat_b['id']}/{i}.jpg"
        rows.append({"frame_id": fid, "label_kind": "identified", "cat_id": cat_b["id"],
                     "quality": "ok", "bbox": [0, 0, 5, 5], "crop_path": crop_path})
        vectors_by_path[os.path.join(store.dataset_root, crop_path)] = [0.0, 3.0, 0.0, 0.0]
    store.add_dataset_items(rows)

    monkeypatch.setattr(gallery, "Embedder", _stub_embedder_factory(vectors_by_path))

    out_dir = str(tmp_path / "models" / "v1")
    result = build_gallery(store, out_dir, qualities=None)

    assert result["enough"] is True
    assert result["n_crops"] == 6 and result["n_cats"] == 2 and result["n_vectors"] == 6
    assert result["backbone"] == "stub-backbone" and result["imgsz"] == 32
    assert result["quality"] == "all"
    assert result["threshold"] == pytest.approx(0.0, abs=1e-9)
    assert result["metrics"]["threshold_balanced_acc"] == pytest.approx(1.0)
    assert result["metrics"]["backbone"] == "stub-backbone"
    assert result["metrics"]["imgsz"] == 32
    per_cat = {p["cat_id"]: p for p in result["metrics"]["per_cat"]}
    assert per_cat[cat_a["id"]] == {"cat_id": cat_a["id"], "cat_name": "A", "n": 3}
    assert per_cat[cat_b["id"]] == {"cat_id": cat_b["id"], "cat_name": "B", "n": 3}
    assert result["out_dir"] == out_dir

    npz_path = os.path.join(out_dir, "gallery.npz")
    assert os.path.isfile(npz_path)
    with np.load(npz_path, allow_pickle=False) as data:
        vecs = data["vectors"]
        assert vecs.shape == (6, 4)
        # Stored RAW (un-normalised) — the magnitude-2/3 vectors, not unit ones.
        assert np.allclose(vecs[:3], [2.0, 0.0, 0.0, 0.0])
        assert np.allclose(vecs[3:], [0.0, 3.0, 0.0, 0.0])
        assert data["cat_ids"].tolist() == [cat_a["id"]] * 3 + [cat_b["id"]] * 3
        assert str(data["backbone"]) == "stub-backbone"
        assert int(data["imgsz"]) == 32

    # Round-trip through load_gallery + match: a query along each cat's axis (any
    # magnitude) must resolve to that cat at ~0 distance — the whole build -> load
    # -> identify chain, not just the isolated pieces.
    gal = load_gallery(npz_path)
    assert np.allclose(np.linalg.norm(gal.vectors, axis=1), 1.0)
    matched = match(gal, np.array([[5.0, 0.0, 0.0, 0.0], [0.0, 0.2, 0.0, 0.0]]))
    assert matched[0] == (cat_a["id"], pytest.approx(0.0, abs=1e-6))
    assert matched[1] == (cat_b["id"], pytest.approx(0.0, abs=1e-6))


def test_build_gallery_quality_filter_forwarded(tmp_path, monkeypatch):
    store = _store(tmp_path)
    cat_a = store.create_cat("A")
    cat_b = store.create_cat("B")
    fids = [_add_frame(store, i) for i in range(1, 5)]
    rows = [
        {"frame_id": fids[0], "label_kind": "identified", "cat_id": cat_a["id"], "quality": "gallery",
         "bbox": [0, 0, 5, 5], "crop_path": "a_gallery.jpg"},
        {"frame_id": fids[1], "label_kind": "identified", "cat_id": cat_a["id"], "quality": "ok",
         "bbox": [0, 0, 5, 5], "crop_path": "a_ok.jpg"},
        {"frame_id": fids[2], "label_kind": "identified", "cat_id": cat_b["id"], "quality": "gallery",
         "bbox": [0, 0, 5, 5], "crop_path": "b_gallery.jpg"},
        {"frame_id": fids[3], "label_kind": "identified", "cat_id": cat_b["id"], "quality": "ok",
         "bbox": [0, 0, 5, 5], "crop_path": "b_ok.jpg"},
    ]
    vectors_by_path = {
        os.path.join(store.dataset_root, r["crop_path"]): ([1.0, 0.0] if r["cat_id"] == cat_a["id"] else [0.0, 1.0])
        for r in rows
    }
    store.add_dataset_items(rows)
    monkeypatch.setattr(gallery, "Embedder", _stub_embedder_factory(vectors_by_path))

    out_dir = str(tmp_path / "models" / "gallery-only")
    result = build_gallery(store, out_dir, qualities=("gallery",))

    assert result["enough"] is True
    assert result["quality"] == "gallery"
    # Only the two "gallery"-graded crops (one per cat) were selected — the "ok"
    # ones must never have reached the (stub) embedder.
    assert result["n_crops"] == 2 and result["n_cats"] == 2 and result["n_vectors"] == 2
    # A single crop per cat means no same-cat pair exists — no calibrated cutoff.
    assert result["threshold"] is None
    assert result["metrics"]["threshold_balanced_acc"] is None


def test_build_gallery_insufficient_labels_short_circuits_without_embedder(tmp_path, monkeypatch):
    store = _store(tmp_path)
    cat_a = store.create_cat("A")
    fid = _add_frame(store, 1)
    store.add_dataset_items([
        {"frame_id": fid, "label_kind": "identified", "cat_id": cat_a["id"], "quality": "gallery",
         "bbox": [0, 0, 5, 5], "crop_path": "only.jpg"},
    ])
    # If the guard didn't return first, constructing this stub would raise.
    monkeypatch.setattr(gallery, "Embedder", _NeverConstructed)

    out_dir = str(tmp_path / "models" / "v-cold")
    result = build_gallery(store, out_dir)

    assert result["enough"] is False
    assert result["reason"] == "insufficient_labels"
    assert result["n_crops"] == 1 and result["n_cats"] == 1
    assert result["quality"] == "all"
    assert not os.path.isdir(out_dir)  # nothing written — the guard returns before makedirs


def test_build_gallery_decode_failure_recheck_after_embed(tmp_path, monkeypatch):
    store = _store(tmp_path)
    cat_a = store.create_cat("A")
    cat_b = store.create_cat("B")
    fid_a1 = _add_frame(store, 1)
    fid_a2 = _add_frame(store, 2)
    fid_b1 = _add_frame(store, 3)
    rows = [
        {"frame_id": fid_a1, "label_kind": "identified", "cat_id": cat_a["id"], "quality": "gallery",
         "bbox": [0, 0, 5, 5], "crop_path": "a1.jpg"},
        {"frame_id": fid_a2, "label_kind": "identified", "cat_id": cat_a["id"], "quality": "gallery",
         "bbox": [0, 0, 5, 5], "crop_path": "a2.jpg"},
        {"frame_id": fid_b1, "label_kind": "identified", "cat_id": cat_b["id"], "quality": "gallery",
         "bbox": [0, 0, 5, 5], "crop_path": "b1.jpg"},
    ]
    store.add_dataset_items(rows)
    a1 = os.path.join(store.dataset_root, "a1.jpg")
    a2 = os.path.join(store.dataset_root, "a2.jpg")
    b1 = os.path.join(store.dataset_root, "b1.jpg")
    vectors_by_path = {a1: [1.0, 0.0], a2: [0.9, 0.1], b1: [0.0, 1.0]}
    # The pre-embed count passes (2 crops/2 cats), but cat B's only crop "fails to
    # decode" (skip=), collapsing the DECODED set to 1 cat — the re-check guard.
    monkeypatch.setattr(gallery, "Embedder", _stub_embedder_factory(vectors_by_path, skip={b1}))

    out_dir = str(tmp_path / "models" / "v-decode-fail")
    result = build_gallery(store, out_dir)

    assert result["enough"] is False
    assert result["reason"] == "decode_failure"
    assert result["n_crops"] == 3 and result["n_cats"] == 2  # pre-embed counts (misleadingly ok)
    assert not os.path.isdir(out_dir)


# --- run_identify --------------------------------------------------------------------


def _setup_identify_fixture(tmp_path, monkeypatch):
    """A store with two yolo-serial-detected frames + a promoted 2-cat gallery.

    The gallery's two vectors sit exactly on the [1,0]/[0,1] axes; the stub
    Embedder hands each frame's crop back its own frame's axis vector, so the
    match is a clean, hand-verifiable (cat, distance≈0) per frame.
    """
    store = _store(tmp_path)
    cat_a = store.create_cat("A")
    cat_b = store.create_cat("B")
    f1 = _add_frame(store, 1)
    f2 = _add_frame(store, 2)
    store.write_analysis(f1, "yolo-serial", True, 0.9, {"boxes": [[0, 0, 10, 10, 0.9]]})
    store.write_analysis(f2, "yolo-serial", True, 0.8, {"boxes": [[0, 0, 12, 12, 0.8]]})

    out_dir = os.path.join(store.models_root, "v1")
    os.makedirs(out_dir, exist_ok=True)
    np.savez(
        os.path.join(out_dir, "gallery.npz"),
        vectors=np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32),
        cat_ids=np.array([cat_a["id"], cat_b["id"]], dtype=np.int64),
        backbone="stub-backbone",
        imgsz=32,
    )
    version_id = store.add_model_version(
        status="active",
        kind="gallery",
        backbone="stub-backbone",
        imgsz=32,
        n_cats=2,
        n_vectors=2,
        threshold=0.5,
        quality="all",
        metrics=None,
        gallery_dir="v1",
    )
    model = store.active_model()
    assert model is not None and model["id"] == version_id

    vectors_by_path = {store.path_for(f1): [1.0, 0.0], store.path_for(f2): [0.0, 1.0]}
    monkeypatch.setattr(gallery, "Embedder", _stub_embedder_factory(vectors_by_path))
    return store, model, f1, f2, cat_a, cat_b


def test_run_identify_embeds_crops_and_writes_identifications_via_store(tmp_path, monkeypatch):
    store, model, f1, f2, cat_a, cat_b = _setup_identify_fixture(tmp_path, monkeypatch)

    result = run_identify(store, model, model["gallery_path"], since_id=None, until_id=None)

    assert result == {"n_identified": 2}
    rows = store._conn.execute(
        "SELECT frame_id, cat_id, distance FROM identifications"
        " WHERE model_version_id = ? ORDER BY frame_id",
        (model["id"],),
    ).fetchall()
    assert [(r[0], r[1]) for r in rows] == [(f1, cat_a["id"]), (f2, cat_b["id"])]
    assert rows[0][2] == pytest.approx(0.0, abs=1e-6)
    assert rows[1][2] == pytest.approx(0.0, abs=1e-6)


def test_run_identify_is_idempotent_on_rerun(tmp_path, monkeypatch):
    store, model, f1, f2, cat_a, cat_b = _setup_identify_fixture(tmp_path, monkeypatch)

    first = run_identify(store, model, model["gallery_path"], since_id=None, until_id=None)
    assert first["n_identified"] == 2

    # Nothing left unidentified for this model — a re-run does no new work and
    # writes no duplicate rows (INSERT OR REPLACE on the (frame, model) PK anyway).
    second = run_identify(store, model, model["gallery_path"], since_id=None, until_id=None)
    assert second["n_identified"] == 0

    (count,) = store._conn.execute(
        "SELECT COUNT(*) FROM identifications WHERE model_version_id = ?", (model["id"],)
    ).fetchone()
    assert count == 2


def test_run_identify_uses_injected_embedder_without_rebuild_or_reprepare(tmp_path, monkeypatch):
    # The live worker hands run_identify a RESIDENT embedder it built + prepared once,
    # so the DINOv2 weights aren't torch.hub.load'ed per cluster. run_identify must use
    # it verbatim: never construct a fresh one, never call prepare() again.
    store, model, f1, f2, cat_a, cat_b = _setup_identify_fixture(tmp_path, monkeypatch)

    vectors_by_path = {store.path_for(f1): [1.0, 0.0], store.path_for(f2): [0.0, 1.0]}
    injected = _stub_embedder_factory(vectors_by_path)(model="stub-backbone", imgsz=32)
    assert injected.prepared is False  # caller "prepared" it out-of-band; the stub needs no real load
    # If run_identify tried to REBUILD an embedder, constructing this stand-in would raise.
    monkeypatch.setattr(gallery, "Embedder", _NeverConstructed)

    result = run_identify(
        store, model, model["gallery_path"], since_id=None, until_id=None, embedder=injected
    )

    assert result == {"n_identified": 2}
    assert injected.prepared is False  # never re-prepared — the caller owns the embedder's lifecycle
    rows = store._conn.execute(
        "SELECT frame_id, cat_id FROM identifications WHERE model_version_id = ? ORDER BY frame_id",
        (model["id"],),
    ).fetchall()
    # The injected embedder's vectors produced the matches — proof it was the one used.
    assert [(r[0], r[1]) for r in rows] == [(f1, cat_a["id"]), (f2, cat_b["id"])]


@pytest.mark.parametrize(
    "backbone,imgsz",
    [("other-backbone", 32), ("stub-backbone", 64)],  # mismatch on backbone / on imgsz
)
def test_run_identify_rejects_mismatched_injected_embedder(tmp_path, monkeypatch, backbone, imgsz):
    # A resident embedder whose (backbone, imgsz) drifts from the active model's would
    # embed queries in a different feature space than the gallery — a silent garbage
    # match. run_identify must reject it hard rather than write wrong identities.
    store, model, f1, f2, cat_a, cat_b = _setup_identify_fixture(tmp_path, monkeypatch)

    injected = _stub_embedder_factory({})(model=backbone, imgsz=imgsz)
    # A rebuild would mask the mismatch; _NeverConstructed proves the guard, not a fallback, fires.
    monkeypatch.setattr(gallery, "Embedder", _NeverConstructed)

    with pytest.raises(ValueError):
        run_identify(
            store, model, model["gallery_path"], since_id=None, until_id=None, embedder=injected
        )

    # The guard fires before load_gallery / any write, so no rows leak.
    (count,) = store._conn.execute(
        "SELECT COUNT(*) FROM identifications WHERE model_version_id = ?", (model["id"],)
    ).fetchone()
    assert count == 0
