"""Tests for crop GEOMETRY — the letterbox/margin arms and the stamp that keeps them apart.

See docs/specs/2026-08-09-open-set-scoring-and-calibration.md ("Crop geometry"). Four
layers, each testable without a GPU:

- the descriptor string (``embed.geometry_descriptor`` / ``parse_geometry`` /
  ``canonical_geometry``) — pure, and the thing every stamp round-trips through;
- the pixel work (``embed._letterbox_square``, ``crops._expand_box``, ``crop_bytes`` /
  ``materialize`` at a margin) — cv2 + numpy only, no torch;
- ``build_gallery``'s refusal to mix two conventions, against a real temp ``Store`` with
  a stub embedder (the discipline test_gallery.py established);
- the re-cut tool end to end, against a real store with real JPEG frames on disk.

Only ONE test here needs torch (the ``Embedder`` pipeline running letterbox through to a
tensor), and it ``importorskip``s — the dev box has no torch, the compute PC does.
"""
from __future__ import annotations

import os

import numpy as np
import pytest

from compute.collection.store import Store
from compute.dataset import crops
from compute.identification import gallery as gallery_mod
from compute.identification.embed import (
    _IMAGENET_MEAN,
    _IMAGENET_MEAN_255,
    _IMAGENET_STD,
    Embedder,
    _letterbox_square,
    canonical_geometry,
    geometry_descriptor,
    parse_geometry,
)
from compute.identification.gallery import build_gallery, run_identify
from compute.ingest.client import StreamFrame, StreamFrameMeta
from compute.tools import recut_crops

cv2 = pytest.importorskip("cv2", reason="crop geometry is pixel work; cv2 is an analysis extra")


# --- fixtures / helpers ---------------------------------------------------------


def _jpeg(width: int, height: int) -> bytes:
    """A real, decodable JPEG with a horizontal ramp — so a crop's CONTENT is checkable."""
    img = np.zeros((height, width, 3), dtype=np.uint8)
    img[:, :, 0] = np.linspace(0, 255, width, dtype=np.uint8)[None, :]
    ok, buf = cv2.imencode(".jpg", img)
    assert ok
    return buf.tobytes()


def _store(tmp_path) -> Store:
    return Store(
        db_path=str(tmp_path / "index.db"),
        media_root=str(tmp_path / "media"),
        max_bytes=10_000_000,
    )


def _add_frame(store: Store, fid: int, *, width: int = 64, height: int = 48) -> int:
    meta = StreamFrameMeta(frame_id=fid, ts=fid, motion=True, bbox=None, area=0.0)
    return store.add(StreamFrame(meta, _jpeg(width, height)), recv_ts_ms=1000 + fid)


def _stub_embedder(vectors: "dict[str, list[float]]"):
    """An ``Embedder``-shaped stub that returns a fixed vector per crop path.

    Records the geometry kwargs it was CONSTRUCTED with on the class, because
    ``build_gallery`` choosing the wrong ones is a silent feature-space error with no
    other observable effect.
    """
    class _Stub:
        constructed: "list[dict]" = []

        def __init__(self, model=None, imgsz=None, letterbox=False, margin=0.0) -> None:
            self.model_name = model or "stub-backbone"
            self._imgsz = imgsz if imgsz is not None else 32
            self._letterbox = bool(letterbox)
            self._margin = float(margin)
            type(self).constructed.append({"letterbox": self._letterbox, "margin": self._margin})

        backbone = property(lambda self: self.model_name)
        imgsz = property(lambda self: self._imgsz)
        geometry = property(lambda self: geometry_descriptor(self._letterbox, self._margin))

        def prepare(self) -> None:
            pass

        def embed_paths(self, paths, batch_size=32, progress=None):
            vecs, kept = [], []
            for i, p in enumerate(paths):
                if p not in vectors:
                    continue
                vecs.append(vectors[p])
                kept.append(i)
            arr = np.asarray(vecs, dtype=np.float32) if vecs else np.zeros((0, 0), dtype=np.float32)
            return arr, kept

    return _Stub


# --- the descriptor -------------------------------------------------------------


def test_geometry_descriptor_legacy_is_none():
    # None, not the string "legacy": a NULL column and an omitting caller must mean the
    # same thing with no translation step in between.
    assert geometry_descriptor(False, 0.0) is None
    assert parse_geometry(None) == (False, 0.0)
    assert parse_geometry("") == (False, 0.0)
    assert canonical_geometry(None) is None
    assert canonical_geometry("") is None


@pytest.mark.parametrize(
    "letterbox,margin,text",
    [
        (True, 0.0, "letterbox"),
        (False, 0.10, "m10"),
        (True, 0.10, "letterbox+m10"),
        (True, 0.125, "letterbox+m12.5"),
        (False, 0.07, "m7"),
    ],
)
def test_geometry_descriptor_round_trips(letterbox, margin, text):
    assert geometry_descriptor(letterbox, margin) == text
    got_lb, got_margin = parse_geometry(text)
    assert got_lb is letterbox
    assert got_margin == pytest.approx(margin)
    # And the string is a FIXED POINT: re-rendering what was parsed must not drift, or
    # a stamp written by one build would stop matching the same build's own filter.
    assert geometry_descriptor(got_lb, got_margin) == text


def test_canonical_geometry_collapses_equivalent_spellings():
    # `m10.0` and `m10` are one convention; a stamp written either way must match a
    # target written the other way, or a re-cut set silently splits in two.
    assert canonical_geometry("m10.0") == "m10"
    assert canonical_geometry("  letterbox+m10  ") == "letterbox+m10"


def test_canonical_geometry_keeps_an_unparseable_stamp_verbatim():
    # A stamp this build cannot read names a convention it cannot reproduce. Returning
    # it unchanged makes it match NOTHING, which excludes those crops from a build —
    # the safe direction. Raising here would instead take down every reader.
    assert canonical_geometry("hexagon") == "hexagon"


def test_parse_geometry_rejects_unknown_and_negative_tokens():
    with pytest.raises(ValueError, match="unknown geometry token"):
        parse_geometry("squash")
    with pytest.raises(ValueError, match="bad margin token"):
        parse_geometry("mx")
    with pytest.raises(ValueError, match="negative margin"):
        parse_geometry("m-10")


def test_geometry_descriptor_rejects_a_negative_margin():
    # A negative margin SHRINKS the box, clipping the cat — the opposite of the point.
    with pytest.raises(ValueError, match="margin must be >= 0"):
        geometry_descriptor(False, -0.1)


# --- letterbox ------------------------------------------------------------------


def test_letterbox_preserves_aspect_ratio():
    # A 4:1 crop must come out 4:1 inside the square, not squashed to 1:1. The content
    # block is found by "not the pad colour", so this fails the moment a plain resize
    # (which leaves no pad at all) is put back.
    img = np.full((25, 100, 3), 200, dtype=np.uint8)
    out = _letterbox_square(img, 100)
    assert out.shape == (100, 100, 3)
    content = ~np.all(np.isclose(out, np.asarray(_IMAGENET_MEAN_255)), axis=2)
    rows = np.where(content.any(axis=1))[0]
    cols = np.where(content.any(axis=0))[0]
    height = rows[-1] - rows[0] + 1
    width = cols[-1] - cols[0] + 1
    assert width == 100
    assert height == pytest.approx(25, abs=1)
    # ...and it is CENTRED, so the cat sits in the middle of the receptive field rather
    # than jammed against one edge.
    assert rows[0] == pytest.approx(100 - rows[-1] - 1, abs=1)


def test_letterbox_pads_with_the_value_that_normalises_to_zero():
    img = np.full((10, 100, 3), 200, dtype=np.uint8)
    out = _letterbox_square(img, 100)
    # The pad rows are the top ones; take row 0 as a representative.
    pad = out[0, 0]
    assert pad == pytest.approx(np.asarray(_IMAGENET_MEAN_255))
    # The property that MATTERS: after the pipeline's own (x/255 − mean)/std the pad
    # contributes exactly nothing. Black would land near −2 in every channel and inject
    # a constant whose weight varies with the box's aspect — a per-crop bias.
    normalised = (pad / 255.0 - np.asarray(_IMAGENET_MEAN)) / np.asarray(_IMAGENET_STD)
    assert normalised == pytest.approx(np.zeros(3), abs=1e-6)
    assert not np.allclose(
        (np.zeros(3) - np.asarray(_IMAGENET_MEAN)) / np.asarray(_IMAGENET_STD), 0.0
    ), "black must NOT normalise to zero — that is the whole reason for the mean pad"


def test_letterbox_of_a_square_image_pads_nothing():
    img = np.full((40, 40, 3), 123, dtype=np.uint8)
    out = _letterbox_square(img, 60)
    assert not np.any(np.all(np.isclose(out, np.asarray(_IMAGENET_MEAN_255)), axis=2))


def test_letterbox_upscale_interpolates_rather_than_replicating():
    # INTER_AREA — the legacy path's filter, correct for shrinking — degenerates to
    # nearest-neighbour when ENLARGING, so an upscaled small crop would come out blocky
    # with only its source values present. INTER_LINEAR produces values strictly between
    # them. Fails if the filter is not switched by direction.
    img = np.zeros((2, 2, 3), dtype=np.uint8)
    img[:, 0] = 0
    img[:, 1] = 240
    out = _letterbox_square(img, 16)
    values = np.unique(np.round(out[:, :, 0]))
    assert np.any((values > 5) & (values < 235)), f"no interpolated values: {values}"


# --- the context margin ---------------------------------------------------------


def test_expand_box_pushes_every_edge_out_by_the_fraction():
    # margin 0.1 on a 100x50 box: 10px each side, 5px top and bottom — so 20% wider and
    # 20% taller overall. Pinning the numbers, not just "bigger", because the per-edge
    # vs. total reading is exactly what a re-cut would get silently wrong.
    assert crops._expand_box([10, 20, 110, 70], 0.1) == [0.0, 15.0, 120.0, 75.0]


def test_expand_box_is_identity_at_margin_zero():
    # Identity, not a re-rounded copy: the legacy path must stay byte-identical.
    box = [1.5, 2.5, 3.5, 4.5]
    assert crops._expand_box(box, 0.0) is box


def test_expand_box_orders_a_reversed_pair_before_expanding():
    # A reversed pair would otherwise SHRINK under expansion. _clamp_box orders pairs
    # too, but it runs after, so the ordering has to happen here as well.
    assert crops._expand_box([110, 70, 10, 20], 0.1) == [0.0, 15.0, 120.0, 75.0]


def test_expand_box_rejects_a_negative_margin():
    with pytest.raises(ValueError, match="margin must be >= 0"):
        crops._expand_box([0, 0, 10, 10], -0.1)


def test_expand_box_passes_a_malformed_box_through_for_clamp_to_reject():
    # One familiar error for a bad box, raised in one place.
    assert crops._expand_box([1, 2], 0.1) == [1, 2]
    with pytest.raises(ValueError, match=r"box must be \[x1,y1,x2,y2\]"):
        crops._clamp_box(crops._expand_box([1, 2], 0.1), 100, 100)


def test_margin_still_clamps_to_the_image_bounds():
    # A box against the frame edge must not push the crop out of bounds; the clamp trims
    # only the part that fell outside, which is why the expansion happens BEFORE it.
    expanded = crops._expand_box([0, 0, 40, 40], 0.5)
    assert crops._clamp_box(expanded, 64, 48) == (0, 0, 60, 48)


def test_crop_bytes_at_a_margin_returns_a_larger_crop(tmp_path):
    path = tmp_path / "frame.jpg"
    path.write_bytes(_jpeg(200, 200))
    plain = cv2.imdecode(
        np.frombuffer(crops.crop_bytes(str(path), [50, 50, 150, 150]), dtype=np.uint8),
        cv2.IMREAD_COLOR,
    )
    widened = cv2.imdecode(
        np.frombuffer(crops.crop_bytes(str(path), [50, 50, 150, 150], 0.1), dtype=np.uint8),
        cv2.IMREAD_COLOR,
    )
    assert plain.shape[:2] == (100, 100)
    assert widened.shape[:2] == (120, 120)


def test_materialize_forwards_the_margin(tmp_path):
    src = tmp_path / "frame.jpg"
    src.write_bytes(_jpeg(200, 200))
    root = tmp_path / "dataset"
    dest = root / "cat_1" / "a.jpg"
    assert crops.materialize(str(src), [50, 50, 150, 150], str(dest), root=str(root), margin=0.2)
    img = cv2.imread(str(dest))
    assert img.shape[:2] == (140, 140)


# --- Embedder wiring ------------------------------------------------------------


def test_embedder_defaults_to_legacy_geometry():
    # The default is load-bearing: flipping it would silently mismatch every
    # already-promoted gallery against its own queries.
    e = Embedder(model="stub", imgsz=32)
    assert e.letterbox is False
    assert e.margin == 0.0
    assert e.geometry is None


def test_embedder_geometry_property_round_trips():
    e = Embedder(model="stub", imgsz=32, letterbox=True, margin=0.1)
    assert e.geometry == "letterbox+m10"
    assert parse_geometry(e.geometry) == (True, pytest.approx(0.1))


def test_embedder_rejects_a_negative_margin_at_construction():
    with pytest.raises(ValueError, match="margin must be >= 0"):
        Embedder(model="stub", imgsz=32, margin=-0.05)


def test_embed_paths_refuses_a_margin_instead_of_ignoring_it():
    # A stored crop file has no box left to expand — the margin is already in its pixels.
    # Silently dropping it would make one embedder object produce two feature spaces.
    e = Embedder(model="stub", imgsz=32, margin=0.1)
    with pytest.raises(ValueError, match="cannot honour margin"):
        e.embed_paths(["/nonexistent.jpg"])


def test_embedder_letterbox_pads_to_zero_through_the_real_pipeline(tmp_path):
    """The one torch-gated test: letterbox is actually wired into ``_embed_items``.

    Runs the real preprocessing with a trivial stand-in for the backbone (identity over
    the normalised tensor), so it needs torch but no weights, no download and no GPU.
    Skips on a box without the analysis extras — which is where the pure-numpy tests
    above carry the load.
    """
    torch = pytest.importorskip("torch", reason="Embedder's tensor path needs torch")
    path = tmp_path / "wide.jpg"
    path.write_bytes(_jpeg(160, 40))  # 4:1 — guarantees real padding

    e = Embedder(model="stub", imgsz=28, letterbox=True)
    e._device = "cpu"
    e._model = lambda x: x.reshape(x.shape[0], -1)
    emb, kept = e.embed_paths([str(path)])

    assert kept == [0]
    tensor = torch.from_numpy(emb).reshape(1, 3, 28, 28)
    # Top and bottom rows are pad; they must be exactly 0 after normalisation.
    assert float(tensor[0, :, 0, :].abs().max()) == pytest.approx(0.0, abs=1e-5)
    assert float(tensor[0, :, -1, :].abs().max()) == pytest.approx(0.0, abs=1e-5)
    # ...and the middle is not, or "all zero" would pass for the wrong reason.
    assert float(tensor[0, :, 14, :].abs().max()) > 0.1


# --- build_gallery: one convention per gallery ----------------------------------


def _seed_two_geometries(store: Store, tmp_path):
    """Two cats × two crops, at legacy AND at ``letterbox+m10``; returns the vector map.

    Every crop gets a distinct vector so a gallery built from the wrong set is visible in
    the enrolled ``n_vectors`` and in the ``.npz`` contents, not merely in a count.
    """
    cat_a = store.create_cat("A")
    cat_b = store.create_cat("B")
    frames = [_add_frame(store, i) for i in range(1, 9)]
    vectors: "dict[str, list[float]]" = {}
    rows = []
    plan = [
        (cat_a, None, frames[0:1]), (cat_b, None, frames[1:2]),
        (cat_a, None, frames[2:3]), (cat_b, None, frames[3:4]),
        (cat_a, "letterbox+m10", frames[4:5]), (cat_b, "letterbox+m10", frames[5:6]),
        (cat_a, "letterbox+m10", frames[6:7]), (cat_b, "letterbox+m10", frames[7:8]),
    ]
    for cat, geom, fids in plan:
        for fid in fids:
            rel = os.path.join(f"cat_{cat['id']}", (geom or "legacy").replace("+", "-"), f"{fid}.jpg")
            rows.append({
                "frame_id": fid, "label_kind": "identified", "cat_id": cat["id"],
                "quality": "gallery", "bbox": [0, 0, 10, 10], "crop_path": rel,
                "geometry": geom,
            })
            axis = [0.0, 0.0, 0.0, 0.0]
            axis[0 if cat is cat_a else 1] = 1.0
            vectors[os.path.join(store.dataset_root, rel)] = axis
    assert store.add_dataset_items(rows) == len(rows)
    return cat_a, cat_b, vectors


def test_build_gallery_refuses_to_mix_conventions(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _cat_a, _cat_b, vectors = _seed_two_geometries(store, tmp_path)
    stub = _stub_embedder(vectors)
    monkeypatch.setattr(gallery_mod, "Embedder", stub)

    legacy = build_gallery(store, str(tmp_path / "models" / "legacy"))
    assert legacy["enough"] is True
    # 8 labelled crops exist; only the 4 legacy ones may be enrolled.
    assert legacy["n_vectors"] == 4
    assert legacy["geometry"] is None
    assert legacy["metrics"]["geometry"] is None
    assert legacy["metrics"]["n_other_geometry"] == 4

    lb = build_gallery(store, str(tmp_path / "models" / "lb"), geometry="letterbox+m10")
    assert lb["n_vectors"] == 4
    assert lb["geometry"] == "letterbox+m10"
    assert lb["metrics"]["geometry"] == "letterbox+m10"
    assert lb["metrics"]["n_other_geometry"] == 4


def test_build_gallery_matches_the_embedder_to_the_requested_geometry(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _cat_a, _cat_b, vectors = _seed_two_geometries(store, tmp_path)
    stub = _stub_embedder(vectors)
    monkeypatch.setattr(gallery_mod, "Embedder", stub)

    build_gallery(store, str(tmp_path / "models" / "legacy"))
    assert stub.constructed[-1] == {"letterbox": False, "margin": 0.0}

    build_gallery(store, str(tmp_path / "models" / "lb"), geometry="letterbox+m10")
    # letterbox YES, margin NO: the crop FILES already carry the margin, and
    # `embed_paths` refuses to apply it a second time.
    assert stub.constructed[-1] == {"letterbox": True, "margin": 0.0}


def test_build_gallery_names_geometry_when_it_is_why_there_is_nothing(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _seed_two_geometries(store, tmp_path)
    # No crop is cut at m10, so the floor trips with thousands of labels sitting there.
    # "Not enough labelled data" alone would send the operator to label more.
    monkeypatch.setattr(gallery_mod, "Embedder", _stub_embedder({}))
    result = build_gallery(store, str(tmp_path / "models" / "m10"), geometry="m10")
    assert result["enough"] is False
    assert result["geometry"] == "m10"
    assert "different geometry" in result["message"]
    assert "recut_crops" in result["message"]


def test_build_gallery_rejects_an_unreadable_geometry_before_reading_the_store(tmp_path, monkeypatch):
    store = _store(tmp_path)
    monkeypatch.setattr(gallery_mod, "Embedder", _stub_embedder({}))
    with pytest.raises(ValueError, match="unknown geometry token"):
        build_gallery(store, str(tmp_path / "models" / "bad"), geometry="hexagon")


def test_build_gallery_stamped_geometry_survives_a_spelling_difference(tmp_path, monkeypatch):
    store = _store(tmp_path)
    cat_a = store.create_cat("A")
    cat_b = store.create_cat("B")
    frames = [_add_frame(store, i) for i in range(1, 5)]
    vectors, rows = {}, []
    for i, (cat, fid) in enumerate(zip([cat_a, cat_a, cat_b, cat_b], frames)):
        rel = f"cat_{cat['id']}/m10/{fid}.jpg"
        # Stored as `m10.0`; the build asks for `m10`. One convention, two spellings.
        rows.append({"frame_id": fid, "label_kind": "identified", "cat_id": cat["id"],
                     "quality": "gallery", "bbox": [0, 0, 10, 10], "crop_path": rel,
                     "geometry": "m10.0"})
        vectors[os.path.join(store.dataset_root, rel)] = [float(i == 0 or i == 1), 1.0, 0.0]
    store.add_dataset_items(rows)
    monkeypatch.setattr(gallery_mod, "Embedder", _stub_embedder(vectors))

    result = build_gallery(store, str(tmp_path / "models" / "m10"), geometry="m10")
    assert result["enough"] is True and result["n_vectors"] == 4


# --- run_identify: queries land in the gallery's feature space ------------------


def test_run_identify_builds_its_embedder_at_the_models_geometry(tmp_path, monkeypatch):
    store = _store(tmp_path)
    stub = _stub_embedder({})
    monkeypatch.setattr(gallery_mod, "Embedder", stub)
    npz = tmp_path / "gallery.npz"
    np.savez(npz, vectors=np.array([[1.0, 0.0]], dtype=np.float32),
             cat_ids=np.array([1]), backbone="stub-backbone", imgsz=32)

    model = {"id": 1, "backbone": "stub-backbone", "imgsz": 32,
             "metrics": {"geometry": "letterbox+m10"}}
    run_identify(store, model, str(npz), None, None)
    # BOTH halves here, unlike the gallery side: this one cuts from the frame, so the
    # margin is applied rather than already baked in.
    assert stub.constructed[-1] == {"letterbox": True, "margin": pytest.approx(0.1)}


def test_run_identify_rejects_an_injected_embedder_at_another_geometry(tmp_path, monkeypatch):
    store = _store(tmp_path)
    monkeypatch.setattr(gallery_mod, "Embedder", _stub_embedder({}))
    npz = tmp_path / "gallery.npz"
    np.savez(npz, vectors=np.array([[1.0, 0.0]], dtype=np.float32),
             cat_ids=np.array([1]), backbone="stub-backbone", imgsz=32)
    model = {"id": 1, "backbone": "stub-backbone", "imgsz": 32,
             "metrics": {"geometry": "letterbox"}}
    legacy_embedder = _stub_embedder({})(model="stub-backbone", imgsz=32)

    with pytest.raises(ValueError, match="silent garbage-match"):
        run_identify(store, model, str(npz), None, None, embedder=legacy_embedder)


def test_run_identify_reads_a_metrics_less_model_as_legacy(tmp_path, monkeypatch):
    # Every version built before geometry existed has no such key, and legacy is the
    # correct reading — that IS what those galleries were built at.
    store = _store(tmp_path)
    stub = _stub_embedder({})
    monkeypatch.setattr(gallery_mod, "Embedder", stub)
    npz = tmp_path / "gallery.npz"
    np.savez(npz, vectors=np.array([[1.0, 0.0]], dtype=np.float32),
             cat_ids=np.array([1]), backbone="stub-backbone", imgsz=32)

    run_identify(store, {"id": 1, "backbone": "stub-backbone", "imgsz": 32}, str(npz), None, None)
    assert stub.constructed[-1] == {"letterbox": False, "margin": 0.0}


# --- the re-cut tool ------------------------------------------------------------


def _seed_for_recut(tmp_path) -> "tuple[Store, dict, list[int]]":
    """A store with one cat, three frames on disk, and three legacy-stamped crops."""
    store = _store(tmp_path)
    cat = store.create_cat("A")
    frame_ids = [_add_frame(store, i, width=120, height=90) for i in range(1, 4)]
    rows = []
    for fid in frame_ids:
        rel = recut_crops.crop_rel_path(cat["id"], "identified", fid, 1000 + fid, None)
        src = store.path_for(fid)
        assert crops.materialize(src, [20, 20, 60, 50], os.path.join(store.dataset_root, rel),
                                 root=store.dataset_root)
        rows.append({"frame_id": fid, "label_kind": "identified", "cat_id": cat["id"],
                     "quality": "gallery", "bbox": [20, 20, 60, 50], "crop_path": rel})
    assert store.add_dataset_items(rows) == 3
    return store, cat, frame_ids


def test_recut_moves_stamp_and_file_and_removes_the_old_crop(tmp_path):
    store, cat, frame_ids = _seed_for_recut(tmp_path)
    old_paths = [
        os.path.join(store.dataset_root, r["crop_path"]) for r in store.labeled_crops()
    ]
    conn = recut_crops._connect(str(tmp_path / "index.db"))
    try:
        rows = recut_crops.read_rows(conn, str(tmp_path / "media"), "m10")
        assert [r["recuttable"] for r in rows] == [True, True, True]
        summary = recut_crops.recut(store.update_dataset_geometry, rows, "m10", store.dataset_root)
    finally:
        conn.close()

    assert summary == {"recut": 3, "failed": 0, "rows_updated": 3, "old_files_removed": 3}
    labelled = store.labeled_crops()
    assert {r["geometry"] for r in labelled} == {"m10"}
    for row in labelled:
        # Never a row pointing at a file that is not there — the whole ordering rule.
        assert os.path.isfile(row["crop_path"]), row["crop_path"]
        assert f"cat_{cat['id']}{os.sep}m10{os.sep}" in row["crop_path"]
        # And the margin is genuinely in the pixels: 40x30 box + 10% each side.
        assert cv2.imread(row["crop_path"]).shape[:2] == (36, 48)
    for old in old_paths:
        assert not os.path.exists(old)


def test_recut_skips_a_crop_whose_source_frame_is_gone(tmp_path):
    store, _cat, _fids = _seed_for_recut(tmp_path)
    kept_paths = [os.path.join(store.dataset_root, r["crop_path"]) for r in store.labeled_crops()]
    store.clear()  # frames go; dataset_items are the precious survivors

    conn = recut_crops._connect(str(tmp_path / "index.db"))
    try:
        rows = recut_crops.read_rows(conn, str(tmp_path / "media"), "m10")
        assert rows and not any(r["recuttable"] for r in rows)
        summary = recut_crops.recut(store.update_dataset_geometry, [r for r in rows if r["recuttable"]], "m10", store.dataset_root
        )
    finally:
        conn.close()

    assert summary["recut"] == 0
    # It keeps its OLD stamp and its OLD file — excluded from builds at the new
    # geometry, never destroyed and never mis-stamped as something it is not.
    assert {r["geometry"] for r in store.labeled_crops()} == {None}
    assert all(os.path.isfile(p) for p in kept_paths)


def test_recut_leaves_the_row_untouched_when_the_cut_fails(tmp_path):
    store, _cat, frame_ids = _seed_for_recut(tmp_path)
    # Corrupt one source frame's JPEG so its re-cut cannot produce bytes.
    with open(store.path_for(frame_ids[1]), "wb") as fh:
        fh.write(b"not a jpeg")

    conn = recut_crops._connect(str(tmp_path / "index.db"))
    try:
        rows = recut_crops.read_rows(conn, str(tmp_path / "media"), "m10")
        summary = recut_crops.recut(store.update_dataset_geometry, [r for r in rows if r["recuttable"]], "m10", store.dataset_root
        )
    finally:
        conn.close()

    assert summary["recut"] == 2 and summary["failed"] == 1
    by_frame = {r["src_frame_id"]: r for r in store.labeled_crops()}
    assert by_frame[frame_ids[1]]["geometry"] is None
    # Every row still resolves to a real file, moved or not.
    assert all(os.path.isfile(r["crop_path"]) for r in store.labeled_crops())


def test_recut_back_to_legacy_lands_on_the_label_routes_own_path(tmp_path):
    store, cat, _fids = _seed_for_recut(tmp_path)
    conn = recut_crops._connect(str(tmp_path / "index.db"))
    try:
        rows = recut_crops.read_rows(conn, str(tmp_path / "media"), "m10")
        recut_crops.recut(store.update_dataset_geometry, rows, "m10", store.dataset_root)
        back = recut_crops.read_rows(conn, str(tmp_path / "media"), None)
        summary = recut_crops.recut(store.update_dataset_geometry, back, None, store.dataset_root)
    finally:
        conn.close()

    assert summary["recut"] == 3 and summary["failed"] == 0
    labelled = store.labeled_crops()
    assert {r["geometry"] for r in labelled} == {None}
    assert all(os.path.isfile(r["crop_path"]) for r in labelled)
    # Back to the original box, and back onto the flat path `_commit_label` writes — so
    # a later re-label of the same visit overwrites this crop instead of orphaning it.
    assert all(cv2.imread(r["crop_path"]).shape[:2] == (30, 40) for r in labelled)
    for row in labelled:
        assert os.path.dirname(row["crop_path"]).endswith(f"cat_{cat['id']}")


def test_recut_never_deletes_the_crop_it_just_wrote_to_the_same_path(tmp_path):
    # A row can carry a stamp its FILE does not match — a half-applied older run, or a
    # stamp corrected by hand. Re-cutting it to the geometry its path already encodes
    # makes the old and new paths identical, and an unguarded "now delete the old file"
    # would then destroy the crop it had just written, with the row pointing at nothing.
    store, cat, frame_ids = _seed_for_recut(tmp_path)
    conn = recut_crops._connect(str(tmp_path / "index.db"))
    try:
        conn.execute("UPDATE dataset_items SET geometry = 'm10'")
        conn.commit()
        rows = recut_crops.read_rows(conn, str(tmp_path / "media"), None)
        assert all(r["recuttable"] for r in rows)
        summary = recut_crops.recut(store.update_dataset_geometry, rows, None, store.dataset_root)
    finally:
        conn.close()

    assert summary["recut"] == 3
    assert summary["old_files_removed"] == 0  # every "old" path WAS the new one
    labelled = store.labeled_crops()
    assert {r["geometry"] for r in labelled} == {None}
    assert all(os.path.isfile(r["crop_path"]) for r in labelled)
    assert all(cv2.imread(r["crop_path"]).shape[:2] == (30, 40) for r in labelled)
    assert all(os.path.dirname(r["crop_path"]).endswith(f"cat_{cat['id']}") for r in labelled)


def test_recut_dry_run_writes_nothing(tmp_path, monkeypatch, capsys):
    store, _cat, _fids = _seed_for_recut(tmp_path)
    monkeypatch.setenv(recut_crops._ENV_DIR, str(tmp_path))
    assert recut_crops.main(["recut_crops", "--to", "letterbox+m10"]) == 0
    out = capsys.readouterr().out
    assert "Dry run" in out and "to re-cut:" in out
    assert {r["geometry"] for r in store.labeled_crops()} == {None}


def test_recut_apply_from_the_cli(tmp_path, monkeypatch):
    store, _cat, _fids = _seed_for_recut(tmp_path)
    monkeypatch.setenv(recut_crops._ENV_DIR, str(tmp_path))
    assert recut_crops.main(["recut_crops", "--to", "letterbox+m10", "--apply"]) == 0
    assert {r["geometry"] for r in store.labeled_crops()} == {"letterbox+m10"}
    assert all(os.path.isfile(r["crop_path"]) for r in store.labeled_crops())


def test_recut_limit_moves_only_part_of_the_set(tmp_path, monkeypatch):
    store, _cat, _fids = _seed_for_recut(tmp_path)
    monkeypatch.setenv(recut_crops._ENV_DIR, str(tmp_path))
    assert recut_crops.main(["recut_crops", "--to", "m10", "--limit", "1", "--apply"]) == 0
    stamps = sorted((r["geometry"] or "legacy") for r in store.labeled_crops())
    assert stamps == ["legacy", "legacy", "m10"]


def test_recut_rejects_an_unreadable_target(tmp_path, monkeypatch):
    _seed_for_recut(tmp_path)
    monkeypatch.setenv(recut_crops._ENV_DIR, str(tmp_path))
    with pytest.raises(SystemExit):
        recut_crops.main(["recut_crops", "--to", "hexagon", "--apply"])


def test_recut_is_idempotent(tmp_path, monkeypatch):
    store, _cat, _fids = _seed_for_recut(tmp_path)
    monkeypatch.setenv(recut_crops._ENV_DIR, str(tmp_path))
    recut_crops.main(["recut_crops", "--to", "m10", "--apply"])
    before = {r["crop_path"]: os.path.getsize(r["crop_path"]) for r in store.labeled_crops()}
    recut_crops.main(["recut_crops", "--to", "m10", "--apply"])
    after = {r["crop_path"]: os.path.getsize(r["crop_path"]) for r in store.labeled_crops()}
    assert before == after


def test_recut_keeps_the_old_file_when_its_row_vanished_mid_run(tmp_path):
    # `/api/label/relabel` deletes a row and re-commits it, re-materialising a crop at
    # exactly the legacy path the row came from. If that lands between this tool's read
    # and its write, the UPDATE matches nothing — and deleting the "old" file anyway
    # would leave the operator's fresh row pointing at nothing.
    store, _cat, _fids = _seed_for_recut(tmp_path)
    conn = recut_crops._connect(str(tmp_path / "index.db"))
    try:
        rows = recut_crops.read_rows(conn, str(tmp_path / "media"), "m10")
        legacy_paths = [os.path.join(store.dataset_root, r["crop_path"]) for r in rows]
        # Simulate the concurrent re-label: the ids this run holds no longer exist.
        conn.execute("UPDATE dataset_items SET id = id + 1000")
        conn.commit()
        summary = recut_crops.recut(store.update_dataset_geometry, rows, "m10", store.dataset_root)
    finally:
        conn.close()

    assert summary["rows_updated"] == 0
    assert summary["old_files_removed"] == 0
    assert all(os.path.isfile(p) for p in legacy_paths)
    assert all(os.path.isfile(r["crop_path"]) for r in store.labeled_crops())
