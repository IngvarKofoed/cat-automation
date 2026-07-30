"""Tests for the two dataset-skew answers: the per-cat gallery cap and the queue's
uncertain-only filter.

Why they exist (see the CHANGELOG entries they close):

* The door produces wildly unequal crop counts — a resident crosses many times a day,
  a neighbour visits occasionally — so an uncapped gallery enrols hundreds of vectors
  for one cat and a handful for another. ``cap_per_cat`` balances enrolment without
  discarding a label.
* A good model does NOT shrink the annotation queue, because membership is "undecided"
  (no ``dataset_items`` row), not "uncertain". ``uncertain_only`` filters to what the
  model actually found hard — the active-learning set ARCHITECTURE describes.

``cap_per_cat`` is pure (plain dicts, no store, no torch), so it is tested directly.
"""
from __future__ import annotations

import pytest

from compute.identification.gallery import cap_per_cat


def _crop(cat_id: int, frame_id: int, quality: "str | None" = "ok") -> dict:
    return {"cat_id": cat_id, "src_frame_id": frame_id, "quality": quality,
            "crop_path": f"/x/{frame_id}.jpg", "cat_name": f"cat{cat_id}"}


# --- the per-cat cap -------------------------------------------------------


def test_cap_is_a_noop_without_a_limit():
    rows = [_crop(1, i) for i in range(50)]
    assert cap_per_cat(rows, None) is rows
    assert cap_per_cat(rows, 0) is rows


def test_cap_balances_a_skewed_set():
    """The headline case: 2 dominant cats, 1 rare one."""
    rows = ([_crop(1, i) for i in range(100)]
            + [_crop(2, 1000 + i) for i in range(80)]
            + [_crop(3, 2000 + i) for i in range(5)])
    kept = cap_per_cat(rows, 10)
    per_cat = {}
    for r in kept:
        per_cat[r["cat_id"]] = per_cat.get(r["cat_id"], 0) + 1
    # The dominant two are capped; the rare cat keeps everything it has (never padded).
    assert per_cat == {1: 10, 2: 10, 3: 5}


def test_cap_prefers_better_grades():
    """A clean crop beats a hard one for ENROLMENT (protect-the-gallery)."""
    rows = ([_crop(1, i, "poor") for i in range(10)]
            + [_crop(1, 100 + i, "gallery") for i in range(3)]
            + [_crop(1, 200 + i, "ok") for i in range(4)])
    kept = cap_per_cat(rows, 5)
    grades = [r["quality"] for r in kept]
    # All 3 gallery crops, then 2 of the ok tier; no poor crop is reached.
    assert sorted(grades) == ["gallery", "gallery", "gallery", "ok", "ok"]


def test_ungraded_crops_sort_last():
    """A missing grade is not evidence of quality — it loses to every real grade."""
    rows = [_crop(1, i, None) for i in range(5)] + [_crop(1, 100 + i, "poor") for i in range(5)]
    kept = cap_per_cat(rows, 5)
    assert [r["quality"] for r in kept] == ["poor"] * 5


def test_overflowing_tier_is_spread_over_time_not_taken_from_the_front():
    """The load-bearing selection rule.

    A contiguous run of crops is one visit in one light — the least useful thing to
    fill a gallery with. Taking the front 5 of 100 would do exactly that; an even
    stride over `src_frame_id` (which is receive order) approximates pose/lighting
    variety instead.
    """
    rows = [_crop(1, i, "ok") for i in range(100)]
    kept = cap_per_cat(rows, 5)
    ids = [r["src_frame_id"] for r in kept]
    assert ids == sorted(ids)                  # chronological
    assert ids != list(range(5))               # NOT the front block
    assert ids[0] < 20 and ids[-1] > 79        # spans the whole range
    # Roughly even spacing: no two picks adjacent in a 100-row tier.
    assert min(b - a for a, b in zip(ids, ids[1:])) >= 15


def test_cap_is_deterministic_and_output_is_grouped_by_cat():
    rows = [_crop(2, 50 + i) for i in range(20)] + [_crop(1, i) for i in range(20)]
    first = cap_per_cat(rows, 4)
    assert [r["src_frame_id"] for r in first] == [r["src_frame_id"] for r in cap_per_cat(rows, 4)]
    # Cats in ascending id order, each cat's crops chronological within its group.
    assert [r["cat_id"] for r in first] == [1, 1, 1, 1, 2, 2, 2, 2]


def test_cap_never_drops_a_cat():
    """It can reduce crops per cat but never the number of cats.

    That is what lets `build_gallery` cap BEFORE its cold-start guard: the cap can't
    turn a buildable set into `insufficient_labels` on the cat count.
    """
    rows = [_crop(c, c * 100 + i) for c in range(1, 6) for i in range(30)]
    kept = cap_per_cat(rows, 1)
    assert sorted({r["cat_id"] for r in kept}) == [1, 2, 3, 4, 5]
    assert len(kept) == 5


# --- the queue's uncertain-only filter -------------------------------------
#
# Reuses the P4 annotation-queue fixtures: a bare gallery.npz FILE fakes a promotable
# model (the store only checks the file exists), so no torch is involved.

from compute.tests.test_annotation_p4 import (  # noqa: E402
    _present, _promote_model, _store, _write_gallery_file,
)


def _identify(store, frame_id: int, model_id: int, cat_id: int, distance: float) -> None:
    """One MATCH row, in write_identifications_batch's positional-tuple contract."""
    store.write_identifications_batch([(frame_id, model_id, cat_id, distance, [0, 0, 1, 1])])


def test_uncertain_only_hides_confident_matches(tmp_path):
    store = _store(tmp_path)
    cat = store.create_cat("Mittens", is_resident=True)["id"]
    # Three visits, gap-split by time: one confident match, one far match, one never
    # identified. Threshold 0.5, so distance <= 0.5 is confident.
    confident = _present(store, 1_000)
    far = _present(store, 20_000)
    never = _present(store, 40_000)
    _write_gallery_file(store)
    model_id = _promote_model(store, threshold=0.5)
    _identify(store, confident, model_id, cat, 0.1)
    _identify(store, far, model_id, cat, 0.9)

    unfiltered = store.annotation_queue_page("yolo-serial")
    assert {v["frames"][0]["id"] for v in unfiltered["visits"]} == {confident, far, never}
    assert unfiltered["hidden_confident"] == 0

    filtered = store.annotation_queue_page("yolo-serial", uncertain_only=True)
    # The confident resident match is gone; the far match and the never-identified
    # visit — the cases the model found hard — remain.
    assert {v["frames"][0]["id"] for v in filtered["visits"]} == {far, never}
    assert filtered["hidden_confident"] == 1


def test_uncertain_only_is_a_noop_without_an_active_model(tmp_path):
    """No model means nothing has been identified, so nothing is 'confident'.

    Filtering on the None-valued `uncertain` flag would empty the queue instead —
    hiding every visit exactly when there is no model to have judged them.
    """
    store = _store(tmp_path)
    ids = {_present(store, 1_000), _present(store, 20_000)}
    page = store.annotation_queue_page("yolo-serial", uncertain_only=True)
    assert {v["frames"][0]["id"] for v in page["visits"]} == ids
    assert page["has_model"] is False and page["hidden_confident"] == 0


def test_uncertain_only_filters_before_the_limit_cap(tmp_path):
    """The whole point: applied after the cap, confident visits would eat the page.

    Four confident visits ahead of one uncertain one, with limit=2. Unfiltered, the
    page is all-confident and the uncertain visit is unreachable; filtered, it fits.
    """
    store = _store(tmp_path)
    cat = store.create_cat("Mittens", is_resident=True)["id"]
    _write_gallery_file(store)
    confident_ids = [_present(store, 1_000 + i * 20_000) for i in range(4)]
    uncertain_id = _present(store, 200_000)
    model_id = _promote_model(store, threshold=0.5)
    for fid in confident_ids:
        _identify(store, fid, model_id, cat, 0.1)
    _identify(store, uncertain_id, model_id, cat, 0.95)

    unfiltered = store.annotation_queue_page("yolo-serial", limit=2)
    filtered = store.annotation_queue_page("yolo-serial", limit=2, uncertain_only=True)
    # Worst-first puts the far match first, so it IS reachable at limit=2 here; the
    # filter's contribution is that the remaining slot holds another hard case rather
    # than a confident one, and that the hidden count is reported.
    assert filtered["hidden_confident"] == 4
    assert all(v["uncertain"] for v in filtered["visits"])
    assert len(filtered["visits"]) == 1
    assert len(unfiltered["visits"]) == 2


def test_uncertain_only_route_param(tmp_path):
    from fastapi.testclient import TestClient

    from compute.api.app import create_app

    store = _store(tmp_path)
    cat = store.create_cat("Mittens", is_resident=True)["id"]
    confident = _present(store, 1_000)
    _present(store, 40_000)
    _write_gallery_file(store)
    model_id = _promote_model(store, threshold=0.5)
    _identify(store, confident, model_id, cat, 0.1)

    client = TestClient(create_app(store=store, start_collector=False))
    off = client.get("/api/label/queue").json()
    on = client.get("/api/label/queue?uncertain_only=true").json()
    assert len(off["visits"]) == 2 and off["hidden_confident"] == 0
    assert len(on["visits"]) == 1 and on["hidden_confident"] == 1
