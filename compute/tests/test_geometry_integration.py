"""Tests for the geometry/threshold INTEGRATION seams — the wiring between the three
streams of docs/specs/2026-08-09-open-set-scoring-and-calibration.md.

The streams' own tests cover their internals (letterbox pixels, the stranger inversion,
the threshold endpoint). What is checked here is only what no single stream could see:

- ``Store.labeled_crops`` hands out ``is_resident``/``geometry`` (the columns the probe
  and the build read but the store owns).
- ``Store.update_dataset_geometry`` returns the ids it actually MOVED, which is what
  licenses the re-cut tool to delete a superseded file.
- ``count_identified_crops`` applies the geometry filter, so the endpoint pre-check
  counts exactly what the build will embed — the same contract the cat exclusion has.
- Geometry rides the job ``params`` for BOTH kinds, so two arms are two jobs rather than
  one deduped press, and lands in the artifact dir name.

Torch-free throughout: nothing here embeds.
"""
from __future__ import annotations

import os
import time

from compute.collection.store import Store
from compute.ingest.client import StreamFrame, StreamFrameMeta
from compute.learning.runner import TrainingManager

_JPEG = b"\xff\xd8\xff\xd9"


def _store(tmp_path) -> Store:
    return Store(
        db_path=str(tmp_path / "index.db"),
        media_root=str(tmp_path / "media"),
        max_bytes=10_000_000,
    )


def _label(store: Store, cat_id: "int | None", fid: int, geometry: "str | None" = None) -> int:
    meta = StreamFrameMeta(frame_id=fid, ts=fid, motion=False, bbox=None, area=0.0)
    row_id = store.add(StreamFrame(meta, _JPEG), recv_ts_ms=1000 + fid)
    store.add_dataset_items([{
        "frame_id": row_id,
        "label_kind": "identified" if cat_id is not None else "unknown_cat",
        "cat_id": cat_id,
        "quality": "gallery",
        "bbox": [0, 0, 10, 10],
        "crop_path": f"cat_{cat_id}/{fid}.jpg",
        "geometry": geometry,
    }])
    return row_id


def _wait(pred, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.005)
    return bool(pred())


# --- The columns the store hands out --------------------------------------------------


def test_labeled_crops_reports_is_resident_and_geometry(tmp_path):
    # The open-set probe splits an impersonation by whether the impersonated cat is OURS,
    # so the flag has to arrive with the crop rather than from a second query that could
    # disagree with it.
    store = _store(tmp_path)
    ours = store.create_cat("Mittens")
    store.update_cat(ours["id"], {"is_resident": True})
    neighbour = store.create_cat("Store Sultan")
    _label(store, ours["id"], 1, geometry="letterbox+m10")
    _label(store, neighbour["id"], 2)

    rows = {r["cat_name"]: r for r in store.labeled_crops(("identified",))}
    assert rows["Mittens"]["is_resident"] is True
    assert rows["Store Sultan"]["is_resident"] is False
    assert rows["Mittens"]["geometry"] == "letterbox+m10"
    # NULL geometry is LEGACY, not "unknown" — every crop cut before the column means it.
    assert rows["Store Sultan"]["geometry"] is None


def test_labeled_crops_is_resident_is_none_for_a_catless_kind(tmp_path):
    # The join is a LEFT JOIN: an `unknown_cat` crop has no roster cat at all, which is
    # "no flag", not "not a resident". Collapsing the two would let a catless crop be
    # counted as a neighbour in the impersonation split.
    store = _store(tmp_path)
    _label(store, None, 1)
    row = store.labeled_crops(("unknown_cat",))[0]
    assert row["is_resident"] is None


# --- update_dataset_geometry ----------------------------------------------------------


def test_update_dataset_geometry_returns_only_the_ids_it_moved(tmp_path):
    # The re-cut tool deletes a crop's OLD file only for rows this call actually matched.
    # A count cannot express which those were, and deleting on a count would destroy the
    # file belonging to a row a concurrent relabel had already replaced.
    store = _store(tmp_path)
    cat = store.create_cat("A")
    _label(store, cat["id"], 1)
    _label(store, cat["id"], 2)
    items = store.labeled_crops(("identified",))
    ids = [r["src_frame_id"] for r in items]
    rows = store._conn.execute(
        "SELECT id FROM dataset_items WHERE src_frame_id IN (?, ?) ORDER BY id", ids
    ).fetchall()
    real = [r[0] for r in rows]

    old0 = f"cat_{cat['id']}/{ids[0]}.jpg"
    box = "0,0,10,10"  # what `_label` stored, and what the swap compares
    moved = store.update_dataset_geometry([
        (real[0], "cat_1/new-1.jpg", "letterbox", old0, box),
        (999_999, "cat_1/ghost.jpg", "letterbox", old0, box),  # no such row — not reported
    ])
    assert moved == [real[0]]

    after = {r["src_frame_id"]: r for r in store.labeled_crops(("identified",))}
    assert after[ids[0]]["geometry"] == "letterbox"
    assert after[ids[0]]["crop_path"].endswith("new-1.jpg")
    assert after[ids[1]]["geometry"] is None          # untouched row keeps its stamp


def test_update_dataset_geometry_empty_is_a_no_op(tmp_path):
    store = _store(tmp_path)
    assert store.update_dataset_geometry([]) == []


def test_update_dataset_geometry_refuses_a_row_whose_path_moved(tmp_path):
    # Rowids are REUSED (INTEGER PRIMARY KEY, no AUTOINCREMENT) and `/api/label/relabel`
    # deletes a visit's rows and re-commits the same frames — so the id the re-cut tool
    # read can belong to a different, freshly labelled row by the time it writes. Matching
    # the path it actually read is what stops the tool reporting that row as moved.
    store = _store(tmp_path)
    cat = store.create_cat("A")
    _label(store, cat["id"], 1)
    row_id = store._conn.execute("SELECT id FROM dataset_items").fetchone()[0]

    stale = store.update_dataset_geometry([
        (row_id, "cat_1/new.jpg", "letterbox", "cat_1/some-other-path.jpg", "0,0,10,10"),
    ])
    assert stale == []
    after = store.labeled_crops(("identified",))[0]
    assert after["geometry"] is None                     # untouched
    assert after["crop_path"].endswith("1.jpg")          # still its own file


def test_update_dataset_geometry_refuses_a_row_whose_BOX_moved(tmp_path):
    # The case the path alone cannot catch: a relabel to the SAME cat re-commits at the
    # identical path, so id and path both still match a row this run never read. Only the
    # box separates them — and it is the exact predicate, being what must be unchanged for
    # the cut pixels to still belong to this row.
    store = _store(tmp_path)
    cat = store.create_cat("A")
    _label(store, cat["id"], 1)
    row_id = store._conn.execute("SELECT id FROM dataset_items").fetchone()[0]
    path = store.labeled_crops(("identified",))[0]["crop_path"]
    rel = f"cat_{cat['id']}/1.jpg"
    assert path.endswith("1.jpg")

    stale = store.update_dataset_geometry([
        (row_id, "cat_1/new.jpg", "letterbox", rel, "9,9,90,90"),  # box as READ, now stale
    ])
    assert stale == []
    after = store.labeled_crops(("identified",))[0]
    assert after["geometry"] is None
    assert after["crop_path"].endswith("1.jpg")


# --- The pre-check counts what the build embeds ---------------------------------------


def test_count_identified_crops_geometry_filter_matches_labeled_crops(tmp_path):
    # Same contract the cat exclusion has: the endpoint's guard must count EXACTLY the set
    # the build will embed, or it waves through a build that then finds nothing.
    store = _store(tmp_path)
    a, b = store.create_cat("A"), store.create_cat("B")
    _label(store, a["id"], 1, geometry="letterbox")
    _label(store, b["id"], 2, geometry="letterbox")
    _label(store, a["id"], 3)          # legacy
    _label(store, b["id"], 4)          # legacy

    for geom in ("letterbox", None):
        counted = store.count_identified_crops(
            None, active_only=True, geometry=geom, geometry_filter=True
        )
        embedded = [
            r for r in store.labeled_crops(("identified",), None, active_only=True)
            if r["geometry"] == geom
        ]
        assert counted == (len(embedded), len({r["cat_id"] for r in embedded}))
        assert counted == (2, 2)

    # A geometry nothing is cut at yet is (0, 0) — the state right after switching
    # convention, which the endpoint must name rather than call "not enough labels".
    assert store.count_identified_crops(
        None, active_only=True, geometry="letterbox+m25", geometry_filter=True
    ) == (0, 0)


def test_count_identified_crops_without_the_flag_is_unfiltered(tmp_path):
    # `geometry=None` MEANS legacy, so "no filter" needs its own flag. Every existing
    # caller omits both and must keep counting every crop regardless of convention.
    store = _store(tmp_path)
    a = store.create_cat("A")
    _label(store, a["id"], 1, geometry="letterbox")
    _label(store, a["id"], 2)

    assert store.count_identified_crops(None, active_only=True) == (2, 1)
    assert store.count_identified_crops(
        None, active_only=True, geometry=None, geometry_filter=True
    ) == (1, 1)


# --- Geometry is part of a job's identity ---------------------------------------------


class _RecordingBuilder:
    def __init__(self) -> None:
        self.calls: "list[dict]" = []

    def __call__(self, store, out_dir, qualities=None, max_per_cat=None,
                 exclude_cat_ids=None, geometry=None, progress=None):
        self.calls.append({"out_dir": out_dir, "geometry": geometry})
        return {"enough": False, "message": "no", "n_crops": 0, "n_cats": 0, "quality": "all"}


class _RecordingProbe:
    def __init__(self) -> None:
        self.calls: "list[dict]" = []

    def __call__(self, store, out_dir, qualities=None, exclude_cat_ids=None,
                 letterbox=False, margin=0.0, progress=None, **kwargs):
        self.calls.append({"out_dir": out_dir, "letterbox": letterbox, "margin": margin})
        return {"enough": False, "message": "no", "n_crops": 0, "n_cats": 0, "quality": "all"}


def test_gallery_build_geometry_reaches_the_builder_and_the_dir_name(tmp_path):
    store = _store(tmp_path)
    builder = _RecordingBuilder()
    manager = TrainingManager(gallery_builder=builder)

    manager.enqueue_gallery_build(store, ["gallery"], None, None, "letterbox+m10")
    assert _wait(lambda: not manager.running)

    assert builder.calls[0]["geometry"] == "letterbox+m10"
    # `+` is sanitised out — the slug reaches the filesystem.
    assert os.path.basename(builder.calls[0]["out_dir"]).endswith("-gallery-letterbox_m10")


def test_feasibility_geometry_becomes_the_embedders_letterbox_and_margin(tmp_path):
    # The job carries a geometry STRING; the probe takes the two halves. Parsing here is
    # what keeps one vocabulary between the dedup key, the dir slug and the embedder.
    store = _store(tmp_path)
    probe = _RecordingProbe()
    manager = TrainingManager(probe_runner=probe)

    manager.enqueue_feasibility(store, ["gallery"], None, "letterbox+m10")
    assert _wait(lambda: not manager.running)

    assert probe.calls[0]["letterbox"] is True
    assert probe.calls[0]["margin"] == 0.1
    assert os.path.basename(probe.calls[0]["out_dir"]).endswith("-gallery-letterbox_m10")


def test_legacy_geometry_leaves_the_job_byte_identical(tmp_path):
    # Legacy is the default and every historical dir name means it, so stamping it would
    # make two identical builds' artifacts look like different work.
    store = _store(tmp_path)
    builder = _RecordingBuilder()
    manager = TrainingManager(gallery_builder=builder)

    manager.enqueue_gallery_build(store, ["gallery"], None, None, None)
    assert _wait(lambda: not manager.running)

    assert builder.calls[0]["geometry"] is None
    assert os.path.basename(builder.calls[0]["out_dir"]).endswith("-gallery")


class _GatedBuilder:
    """Blocks inside the build so the first job is still RUNNING for the second enqueue.

    Load-bearing: dedup only ever guards against the running job, so a fake that returns
    immediately lets every second press through regardless of the params — a test built on
    one passes with geometry removed from the key and asserts nothing.
    """

    def __init__(self) -> None:
        self.entered = __import__("threading").Event()
        self.release = __import__("threading").Event()

    def __call__(self, store, out_dir, qualities=None, max_per_cat=None,
                 exclude_cat_ids=None, geometry=None, progress=None):
        self.entered.set()
        self.release.wait(timeout=5)
        return {"enough": False, "message": "no", "n_crops": 0, "n_cats": 0, "quality": "all"}


def test_two_geometries_are_two_jobs_not_one_deduped_press(tmp_path):
    # The whole point of the A/B is running both arms; without geometry in the params the
    # second press dedups onto the running first and silently never runs.
    store = _store(tmp_path)
    gated = _GatedBuilder()
    manager = TrainingManager(gallery_builder=gated)
    try:
        manager.enqueue_gallery_build(store, ["gallery"], None, None, None)
        assert gated.entered.wait(timeout=5), "head job never started"

        same = manager.enqueue_gallery_build(store, ["gallery"], None, None, None)
        assert same["deduped"] is True          # a genuine double-click still collapses
        other = manager.enqueue_gallery_build(store, ["gallery"], None, None, "letterbox")
        assert other["deduped"] is False        # a different convention is real new work
        assert [j["params"][3] for j in manager.status()["queue"]] == ["letterbox"]
    finally:
        gated.release.set()
    assert _wait(lambda: not manager.running)


def test_geometry_spellings_canonicalise_to_one_job(tmp_path):
    # `m10` and `m10.0` are the same convention; if they deduped apart they would also
    # claim two artifact dirs for one crop set.
    store = _store(tmp_path)
    gated = _GatedBuilder()
    manager = TrainingManager(gallery_builder=gated)
    try:
        manager.enqueue_gallery_build(store, ["gallery"], None, None, "letterbox+m10")
        assert gated.entered.wait(timeout=5), "head job never started"

        dup = manager.enqueue_gallery_build(store, ["gallery"], None, None, "letterbox+m10.0")
        assert dup["deduped"] is True
        assert manager.status()["queue"] == []
    finally:
        gated.release.set()
    assert _wait(lambda: not manager.running)
