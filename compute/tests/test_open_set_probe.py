"""The open-set probe end to end, and the report sections it added
(docs/specs/2026-08-09-open-set-scoring-and-calibration.md).

Runs against a REAL store with a stubbed Embedder — real ``labeled_crops`` SQL carrying
``is_resident``/``geometry``, real visit grouping, real ``cap_per_cat`` selection, and the
real files landing in the run dir. The four chart helpers are stubbed to ``""`` so the
whole orchestration is exercised on a lean box: matplotlib is an opt-in analysis extra, and
gating this file on it would leave the parts that actually changed (the cap masks, the
geometry filter, the outcomes file) untested everywhere but the compute PC. The charts
themselves keep their own matplotlib-gated test at the bottom.

The report half needs no store at all — ``_render_html`` takes a metrics dict — so those
tests build one by hand and assert on the HTML.
"""
from __future__ import annotations

import json
import os

import numpy as np
import pytest

from compute.collection.store import Store
from compute.identification import probe as probe_mod
from compute.identification.probe import (
    _caps_section, _cap_masks, _op_points, _render_html, _stranger_section, _tldr,
)
from compute.ingest import StreamFrame
from shared.wire import StreamFrameMeta

_JPEG = b"\xff\xd8\xff\xe0" + b"fake" + b"\xff\xd9"
_HOUR_MS = 3_600_000
_CROPS_PER_VISIT = 5


def _store(tmp_path) -> Store:
    return Store(db_path=str(tmp_path / "index.db"), media_root=str(tmp_path / "media"),
                 max_bytes=50_000_000)


def _populate(store: Store, cats=3, visits=4, geometry=None) -> None:
    """``cats`` cats x ``visits`` visits x 5 crops, visits hours apart.

    Three cats minimum: the stranger pass needs a gallery of at least two after one is
    held out, which is the block's own floor.
    """
    for i in range(cats):
        store.create_cat(f"Cat{i + 1}", is_resident=(i % 2 == 0))
    fid = 0
    for visit in range(visits):
        for cat_id in range(1, cats + 1):
            base = (visit * 8 + cat_id) * _HOUR_MS
            for c in range(_CROPS_PER_VISIT):
                fid += 1
                ts = base + c * 200
                row_id = store.add(
                    StreamFrame(StreamFrameMeta(frame_id=fid, ts=ts, motion=True,
                                                bbox=None, area=1.0), _JPEG),
                    recv_ts_ms=ts,
                )
                store.add_dataset_items([{
                    "frame_id": row_id, "label_kind": "identified", "cat_id": cat_id,
                    "quality": "gallery", "bbox": [0, 0, 10, 10],
                    "crop_path": f"c{fid}.jpg", "geometry": geometry,
                }])


class _StubEmbedder:
    """Separable synthetic vectors: one cluster per cat, tight within a visit."""

    def __init__(self, labels, **_kw):
        self._labels = labels
        self.kwargs = _kw

    def prepare(self):
        return None

    def embed_paths(self, paths, progress=None):
        rng = np.random.default_rng(7)
        vecs = []
        by_path = {row["crop_path"]: row for row in self._labels}
        for path in paths:
            row = by_path[path]
            centre = np.zeros(8)
            centre[int(row["cat_id"])] = 3.0
            visit_key = int(row["src_recv_ts"]) // _HOUR_MS
            centre = centre + np.random.default_rng(visit_key).normal(0, 0.15, size=8)
            vecs.append(centre + rng.normal(0, 0.004, size=8))
        if progress is not None:
            progress(len(vecs), len(vecs))
        return np.array(vecs), list(range(len(vecs)))


@pytest.fixture()
def probe_env(tmp_path, monkeypatch):
    """A populated store with the Embedder and the four chart helpers stubbed."""
    store = _store(tmp_path)
    _populate(store)
    labels = store.labeled_crops(("identified",), ("gallery",), active_only=True)
    constructed: "list[dict]" = []

    def _embedder(**kwargs):
        constructed.append(kwargs)
        return _StubEmbedder(labels, **kwargs)

    monkeypatch.setattr(probe_mod, "Embedder", _embedder)
    for name in ("_scatter_png", "_confusion_png", "_hist_png", "_curve_png",
                 "_percat_png", "_regime_png"):
        monkeypatch.setattr(probe_mod, name, lambda *_a, **_kw: "")
    yield store, constructed
    store.close()


# --- The orchestrator -------------------------------------------------------------------

def test_the_probe_measures_strangers_by_default(probe_env, tmp_path):
    """A run that does not measure stranger rejection measures only half the door's job."""
    store, _c = probe_env
    result = probe_mod.run_feasibility_probe(store, str(tmp_path / "r"), qualities=("gallery",))
    s = result["visits"]["strangers"]
    assert s["available"] is True
    assert s["n_cats_held_out"] == 3
    assert s["n_scored"] == 12, "every visit of every held-out cat is a trial"
    # The roster flag reached the probe off `labeled_crops`, so the dangerous direction is
    # counted rather than reported unmeasured.
    assert s["resident_split"] is True
    assert s["resident_impersonation_rate"] is not None


def test_the_probe_forecasts_the_default_cap_ladder(probe_env, tmp_path):
    store, _c = probe_env
    result = probe_mod.run_feasibility_probe(store, str(tmp_path / "r"), qualities=("gallery",))
    caps = result["visits"]["caps"]
    assert [c["max_per_cat"] for c in caps] == [None, 2000, 1000, 500]
    # Every cap here is far above the fixture's 20 crops/cat, so all four enrol everything
    # — which is the point: the ladder describes the gallery, and an uncapped-equivalent
    # row must say so rather than silently differing.
    assert {c["n_vectors"] for c in caps} == {60}
    assert all(c["n_cats"] == 3 for c in caps)


def test_a_cap_that_bites_shows_up_as_fewer_vectors(probe_env, tmp_path):
    store, _c = probe_env
    result = probe_mod.run_feasibility_probe(
        store, str(tmp_path / "r"), qualities=("gallery",), max_per_cat=[None, 6],
    )
    base, capped = result["visits"]["caps"]
    assert base["n_vectors"] == 60
    assert capped["n_vectors"] == 18, "6 per cat x 3 cats"
    assert capped["n_cats"] == 3, "a cap can never drop a cat"


def test_the_cap_ladder_is_skippable(probe_env, tmp_path):
    store, _c = probe_env
    result = probe_mod.run_feasibility_probe(
        store, str(tmp_path / "r"), qualities=("gallery",), max_per_cat=None, strangers=False,
    )
    assert "caps" not in result["visits"]
    assert "strangers" not in result["visits"]


def test_cap_masks_select_exactly_what_cap_per_cat_selects(probe_env):
    """The forecast must describe what a BUILD would enrol, not a re-derived selection."""
    from compute.identification.gallery import cap_per_cat

    store, _c = probe_env
    labels = store.labeled_crops(("identified",), ("gallery",), active_only=True)
    masks = _cap_masks(labels, (None, 6))
    for entry in masks:
        chosen = cap_per_cat(labels, entry["max_per_cat"])
        want = {(r["src_frame_id"], r["src_recv_ts"]) for r in chosen}
        got = {(labels[i]["src_frame_id"], labels[i]["src_recv_ts"])
               for i in np.flatnonzero(entry["mask"])}
        assert got == want
    assert _cap_masks(labels, ()) is None


# --- The per-visit outcome list ---------------------------------------------------------

def test_outcomes_go_to_the_run_dir_and_never_into_the_persisted_metrics(probe_env, tmp_path):
    """`feasibility_runs.metrics` is kept indefinitely and 100 rows are parsed per page
    load; a list that grows with the door's traffic belongs in the bounded run dir."""
    store, _c = probe_env
    out = str(tmp_path / "r")
    result = probe_mod.run_feasibility_probe(store, out, qualities=("gallery",))

    assert "outcomes" not in result["visits"], "not on what the caller persists"
    with open(os.path.join(out, "feasibility.json"), encoding="utf-8") as fh:
        assert "outcomes" not in json.load(fh)["visits"]

    with open(os.path.join(out, "visit_outcomes.json"), encoding="utf-8") as fh:
        doc = json.load(fh)
    assert doc["n"] == len(doc["visits"]) == result["visits"]["n_groups"]
    assert doc["geometry"] is None and doc["quality"] == "gallery"
    # The join key both halves of a paired comparison need, in REAL cat ids.
    roster = {c["id"] for c in store.list_cats()}
    for row in doc["visits"]:
        assert row["cat_id"] in roster
        assert row["named"] is None or row["named"] in roster
        assert isinstance(row["first_src_recv_ts"], int)
    keys = {(r["cat_id"], r["first_src_recv_ts"]) for r in doc["visits"]}
    assert len(keys) == len(doc["visits"]), "the join key must be unique per visit"


def test_the_outcome_timestamp_is_the_visits_first_crop(probe_env, tmp_path):
    store, _c = probe_env
    out = str(tmp_path / "r")
    probe_mod.run_feasibility_probe(store, out, qualities=("gallery",))
    with open(os.path.join(out, "visit_outcomes.json"), encoding="utf-8") as fh:
        rows = json.load(fh)["visits"]
    labels = store.labeled_crops(("identified",), ("gallery",), active_only=True)
    firsts = set()
    for cat in {r["cat_id"] for r in labels}:
        ts = sorted(int(r["src_recv_ts"]) for r in labels if r["cat_id"] == cat)
        firsts |= {t for t in ts if not any(0 < t - o <= 60_000 for o in ts)}
    assert {r["first_src_recv_ts"] for r in rows} == firsts


# --- Crop geometry ----------------------------------------------------------------------

def test_a_run_scores_one_geometry_and_names_it(probe_env, tmp_path):
    """A margin arm over legacy pixels would report a margin it never applied."""
    store, constructed = probe_env
    result = probe_mod.run_feasibility_probe(store, str(tmp_path / "r"), qualities=("gallery",))
    assert result["geometry"] is None, "legacy is the default and is spelled None"
    assert result["n_other_geometry"] == 0
    assert constructed == [{}], "a legacy run constructs the embedder exactly as before"


def test_letterbox_reaches_the_embedder_and_stamps_the_run(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _populate(store, geometry="letterbox")
    labels = store.labeled_crops(("identified",), ("gallery",), active_only=True)
    constructed: "list[dict]" = []
    monkeypatch.setattr(probe_mod, "Embedder",
                        lambda **kw: (constructed.append(kw), _StubEmbedder(labels, **kw))[1])
    for name in ("_scatter_png", "_confusion_png", "_hist_png", "_curve_png",
                 "_percat_png", "_regime_png"):
        monkeypatch.setattr(probe_mod, name, lambda *_a, **_kw: "")

    result = probe_mod.run_feasibility_probe(
        store, str(tmp_path / "r"), qualities=("gallery",), letterbox=True,
    )
    assert result["geometry"] == "letterbox"
    assert constructed == [{"letterbox": True}]
    assert result["n_crops"] == 60
    store.close()


def test_crops_at_another_geometry_are_left_out_and_the_cold_start_says_so(tmp_path,
                                                                          monkeypatch):
    """Not "you have no labels": the operator has plenty, cut at another convention."""
    store = _store(tmp_path)
    _populate(store, geometry="letterbox")
    monkeypatch.setattr(probe_mod, "Embedder",
                        lambda **kw: pytest.fail("must not embed — the guard fires first"))
    result = probe_mod.run_feasibility_probe(store, str(tmp_path / "r"), qualities=("gallery",))
    assert result["enough"] is False and result["reason"] == "insufficient_labels"
    assert result["geometry"] is None
    assert "cut at a different geometry" in result["message"]
    assert "60 labelled crop(s)" in result["message"]
    assert not (tmp_path / "r").exists()
    store.close()


def test_the_margin_never_reaches_the_embedder(tmp_path, monkeypatch):
    """`embed_paths` rejects a margin outright — the pixels already carry it. The margin's
    job here is selecting which stored crops the run reads."""
    store = _store(tmp_path)
    _populate(store, geometry="m10")
    labels = store.labeled_crops(("identified",), ("gallery",), active_only=True)
    constructed: "list[dict]" = []
    monkeypatch.setattr(probe_mod, "Embedder",
                        lambda **kw: (constructed.append(kw), _StubEmbedder(labels, **kw))[1])
    for name in ("_scatter_png", "_confusion_png", "_hist_png", "_curve_png",
                 "_percat_png", "_regime_png"):
        monkeypatch.setattr(probe_mod, name, lambda *_a, **_kw: "")

    result = probe_mod.run_feasibility_probe(
        store, str(tmp_path / "r"), qualities=("gallery",), margin=0.1,
    )
    assert result["geometry"] == "m10"
    assert constructed == [{}], "no margin kwarg — embed_paths would raise on it"
    assert result["n_crops"] == 60, "the m10 crops are the ones it read"
    store.close()


def test_a_negative_margin_is_rejected_before_anything_is_read(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="margin"):
        probe_mod.run_feasibility_probe(store, str(tmp_path / "r"), margin=-0.1)
    store.close()


# --- The report -------------------------------------------------------------------------

_CHARTS = {"scatter": "s", "confusion": "c", "hist": "h", "curve": "u"}


def _stranger(**over):
    block = {
        "available": True, "reason": None, "n_cats_held_out": 2, "n_scored": 10,
        "rejected": 7, "impersonated": 3, "impersonation_rate": 0.3,
        "as_resident": 2, "as_neighbour": 1, "as_unknown": 0,
        "resident_impersonation_rate": 0.2, "resident_split": True,
        "per_cat": [
            {"cat_id": 1, "cat_name": "Sultan", "is_resident": True, "n_scored": 6,
             "rejected": 5, "impersonated": 1, "impersonation_rate": 1 / 6,
             "as_resident": 0, "as_neighbour": 1, "as_unknown": 0,
             "named": [{"cat_id": 2, "cat_name": "Store Sultan", "is_resident": False, "n": 1}]},
            {"cat_id": 2, "cat_name": "Store Sultan", "is_resident": False, "n_scored": 4,
             "rejected": 2, "impersonated": 2, "impersonation_rate": 0.5,
             "as_resident": 2, "as_neighbour": 0, "as_unknown": 0,
             "named": [{"cat_id": 1, "cat_name": "Sultan", "is_resident": True, "n": 2}]},
        ],
        "curve": [
            {"threshold": 0.2, "n_scored": 10, "rejected": 10, "impersonated": 0,
             "as_resident": 0, "as_neighbour": 0, "as_unknown": 0},
            {"threshold": 0.6, "n_scored": 10, "rejected": 7, "impersonated": 3,
             "as_resident": 2, "as_neighbour": 1, "as_unknown": 0},
            {"threshold": 0.9, "n_scored": 10, "rejected": 1, "impersonated": 9,
             "as_resident": 6, "as_neighbour": 3, "as_unknown": 0},
        ],
        "skipped": [],
    }
    block.update(over)
    return block


def _visits(**over):
    v = {
        "available": True, "reason": None, "accuracy": 0.9, "unknown_rate": 0.1,
        "n_scored": 40, "correct": 36, "wrong": 0, "unknown": 4, "n_groups": 40,
        "labeled_ts_groups": 40, "gap_ms": 60_000, "threshold": 0.6, "marks": {},
        "unscoreable": [], "regimes": None, "cross": None, "per_cat": [],
        "confusion": [[36, 0, 4], [0, 0, 0]],
        "curve": [
            {"threshold": 0.2, "correct": 0, "wrong": 0, "unknown": 40, "accuracy": None},
            {"threshold": 0.6, "correct": 36, "wrong": 0, "unknown": 4, "accuracy": 0.9},
            {"threshold": 0.9, "correct": 30, "wrong": 10, "unknown": 0, "accuracy": 0.75},
        ],
    }
    v.update(over)
    return v


def _metrics(visits=None):
    m = {
        "n_crops": 20, "n_cats": 2,
        "cats": [{"cat_id": 1, "cat_name": "Sultan", "n": 10},
                 {"cat_id": 2, "cat_name": "Store Sultan", "n": 10}],
        "knn": {"k": 1, "accuracy": 1.0, "confusion": [[10, 0], [0, 10]],
                "per_cat_recall": [1.0, 1.0]},
        "distances": {"auc": 0.878, "suggested_threshold": 0.44,
                      "hist": {"edges": [0, 1], "same": [1], "diff": [1]},
                      "same": {}, "diff": {}, "threshold_balanced_acc": 0.8},
        "projection": [],
    }
    if visits is not None:
        m["visits"] = visits
    return m


def test_the_stranger_section_leads_with_the_two_rates_and_the_operating_table():
    out = _stranger_section(_metrics(_visits(strangers=_stranger())))
    assert "Stranger rejection" in out
    assert "30%" in out and "20%" in out
    assert "…and given one of OUR cats' names" in out
    # The two tiles share a denominator; the section says so, because "20%" under "30%"
    # otherwise reads as a fraction of it.
    assert "shares of the SAME" in out
    # The operating-point table the spec specifies, with both curves on one row.
    assert "known-cat recall" in out and "known-cat declines" in out
    assert "impersonations (resident)" in out
    assert "(2 as a resident)" in out
    # Per-cat rows name who was impersonated, which is the actionable half.
    assert "Store Sultan" in out and "Sultan ×2" in out


def test_an_unmeasurable_stranger_pass_says_nothing_was_measured():
    """Never a clean-looking zero: a one-cat gallery impersonates 100% by arithmetic."""
    out = _stranger_section(_metrics(_visits(
        strangers={"available": False, "reason": "single_cat_gallery", "n_cats": 2})))
    assert "Not measured" in out
    assert "property of the arithmetic" in out
    assert "%" not in out.split("Not measured")[1].split("</div>")[0]


def test_a_cat_that_could_not_be_held_out_is_named_rather_than_omitted():
    """A roster the table silently shortens reads as a roster that was all measured."""
    out = _stranger_section(_metrics(_visits(strangers=_stranger(
        skipped=[{"cat_id": 9, "reason": "no_visits"}]))))
    assert "Not held out, so no number exists" in out
    assert "9 (no_visits)" in out


def test_the_stranger_section_is_absent_when_the_pass_was_not_requested():
    assert _stranger_section(_metrics(_visits())) == ""
    assert _stranger_section(_metrics()) == ""


def test_an_uncounted_resident_split_is_shown_as_unmeasured_not_zero():
    out = _stranger_section(_metrics(_visits(strangers=_stranger(
        resident_split=False, resident_impersonation_rate=None,
        as_resident=0, as_neighbour=0, as_unknown=3))))
    assert "split is UNAVAILABLE" in out
    assert "<div class=\"v\">—</div>" in out, "an em dash, never 0%"


def test_the_operating_table_pairs_the_two_curves_by_threshold_and_drops_the_unpaired():
    v = _visits(strangers=_stranger())
    rows = _op_points(v)
    assert [r["threshold"] for r in rows] == [0.2, 0.6, 0.9]
    assert rows[1]["known"]["accuracy"] == 0.9 and rows[1]["stranger"]["impersonated"] == 3

    # A stranger point with no known-cat twin is DROPPED rather than lined up by index —
    # that pairing would quote one threshold's declines against another's impersonations.
    s = _stranger()
    s["curve"] = [{"threshold": 0.55, "n_scored": 10, "rejected": 8, "impersonated": 2,
                   "as_resident": 1, "as_neighbour": 1, "as_unknown": 0}] + s["curve"]
    assert [r["threshold"] for r in _op_points(_visits(strangers=s))] == [0.2, 0.6, 0.9]


def test_the_operating_table_samples_but_always_keeps_the_headline_row():
    """The bold row is the threshold every other number in the report was computed at."""
    ts = [round(i / 40, 4) for i in range(41)]
    s = _stranger()
    s["curve"] = [{"threshold": t, "n_scored": 10, "rejected": 10, "impersonated": 0,
                   "as_resident": 0, "as_neighbour": 0, "as_unknown": 0} for t in ts]
    v = _visits(threshold=0.575, strangers=s)
    v["curve"] = [{"threshold": t, "correct": 9, "wrong": 1, "unknown": 0,
                   "accuracy": 0.9} for t in ts]
    sampled = _op_points(v, 15)
    assert len(sampled) <= 16
    assert 0.575 in [r["threshold"] for r in sampled]
    assert len(_op_points(v)) == 41, "the chart gets the whole grid"


def test_the_caps_section_shows_both_columns_and_flags_an_uncalibrated_cap():
    caps = [
        {"max_per_cat": None, "n_vectors": 900, "n_cats": 3, "threshold": 0.6,
         "threshold_balanced_acc": 0.8, "reason": None,
         "recalibrated": {"accuracy": 0.9, "unknown_rate": 0.1},
         "fixed": {"accuracy": 0.9, "unknown_rate": 0.1}},
        {"max_per_cat": 500, "n_vectors": 700, "n_cats": 3, "threshold": None,
         "threshold_balanced_acc": None, "reason": "uncalibrated_threshold",
         "recalibrated": None, "fixed": {"accuracy": 0.88, "unknown_rate": 0.12}},
    ]
    out = _caps_section(_metrics(_visits(caps=caps)))
    assert "If the gallery were capped per cat" in out
    assert "uncapped" in out and "500" in out
    assert "recalibrated under the cap" in out and "at the uncapped threshold" in out
    assert "88%" in out, "the fixed column is still a reading without a threshold"
    assert _caps_section(_metrics(_visits())) == ""


def test_the_lead_block_replaces_its_biggest_limit_once_strangers_are_measured():
    """"No strangers were tested" is the report's load-bearing caveat — it must not
    survive a run that tested them."""
    measured = _render_html(_metrics(_visits(strangers=_stranger())), _CHARTS, "gallery")
    assert "No strangers were tested" not in measured
    assert "Strangers WERE tested" in measured
    assert "one of OUR cats" in measured

    plain = _render_html(_metrics(_visits()), _CHARTS, "gallery")
    assert "No strangers were tested" in plain


def test_an_unavailable_stranger_pass_keeps_the_caveat_and_says_it_ran():
    out = _render_html(_metrics(_visits(
        strangers={"available": False, "reason": "single_cat_gallery", "n_cats": 2})),
        _CHARTS, "gallery")
    assert "No strangers were tested" in out
    assert "could not be scored on this run" in out
    t = _tldr(_metrics(_visits(strangers=_stranger())), ["Sultan", "Store Sultan"])
    assert t["strangers"]["impersonation_rate"] == 0.3


def test_both_new_sections_render_into_the_page_once():
    page = _render_html(_metrics(_visits(strangers=_stranger(), caps=[
        {"max_per_cat": None, "n_vectors": 60, "n_cats": 3, "threshold": 0.6,
         "threshold_balanced_acc": 0.8, "reason": None,
         "recalibrated": {"accuracy": 0.9, "unknown_rate": 0.1},
         "fixed": {"accuracy": 0.9, "unknown_rate": 0.1}}])), _CHARTS, "gallery")
    assert page.count("Stranger rejection") == 1
    assert page.count("If the gallery were capped per cat") == 1
    # Still above the demoted crop-level block, which answers a different question.
    assert page.index("Stranger rejection") < page.index("Crop-level scoring")


# --- The chart, which is the one part that needs matplotlib -----------------------------

def test_the_threshold_chart_draws_the_impersonation_line_when_it_has_one():
    """One chart, not two: the trade-off IS between these lines, and two figures would ask
    the reader to align two x-axes by eye."""
    pytest.importorskip("matplotlib")
    from compute.identification.probe import _curve_png

    without = _curve_png(_visits())
    with_s = _curve_png(_visits(strangers=_stranger()))
    assert without.startswith("data:image/png;base64,")
    assert with_s.startswith("data:image/png;base64,")
    assert with_s != without, "the extra series has to change the figure"
