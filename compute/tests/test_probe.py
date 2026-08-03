"""Unit tests for the feasibility-probe orchestrator's pure/guard paths.

These exercise ``_quality_slug`` and the cold-start guard of
``run_feasibility_probe`` with a stub store, so they run on the lean dev box with
NO heavy deps: the guard returns before any ``Embedder`` is constructed, so torch /
matplotlib are never touched. (The embedding + chart path lives on the compute PC.)
"""
from __future__ import annotations

from compute.identification.probe import _quality_slug, run_feasibility_probe


class _StubStore:
    """Minimal stand-in exposing only what the probe's guard path calls."""

    def __init__(self, rows: "list[dict]") -> None:
        self._rows = rows

    def labeled_crops(self, kinds, qualities, active_only=False, exclude_cat_ids=None):
        # Signature mirrors Store.labeled_crops. The probe passes active_only=True (and an
        # exclusion when one was requested); the stub's rows stand in for the
        # already-filtered result, so both filters are the real store's business.
        return list(self._rows)


def test_quality_slug_is_tier_ordered():
    # Order the user typed the grades in must not change the canonical slug.
    assert _quality_slug(("ok", "gallery")) == "gallery+ok"
    assert _quality_slug(("poor",)) == "poor"
    assert _quality_slug(("poor", "gallery", "ok")) == "gallery+ok+poor"
    assert _quality_slug(("gallery",)) == "gallery"


def test_cold_start_no_crops_does_not_embed(tmp_path):
    store = _StubStore([])
    result = run_feasibility_probe(store, str(tmp_path / "out"))
    assert result["enough"] is False
    assert result["reason"] == "insufficient_labels"  # benign cold-start (CLI exits 0)
    assert result["n_crops"] == 0
    assert result["n_cats"] == 0
    assert result["quality"] == "all"
    assert "Label at least two cats" in result["message"]
    # Guard returns before writing anything — no report dir created.
    assert not (tmp_path / "out").exists()


def test_cold_start_single_cat_does_not_embed(tmp_path):
    rows = [
        {"cat_id": 1, "cat_name": "Mittens", "crop_path": "/x/1.jpg"},
        {"cat_id": 1, "cat_name": "Mittens", "crop_path": "/x/2.jpg"},
    ]
    store = _StubStore(rows)
    result = run_feasibility_probe(store, str(tmp_path / "out"), qualities=("gallery", "ok"))
    assert result["enough"] is False
    assert result["n_crops"] == 2
    assert result["n_cats"] == 1  # two crops but a single distinct cat
    assert result["quality"] == "gallery+ok"
    assert "Label at least two cats" in result["message"]


# --- Visit grouping (docs/specs/2026-08-03-visit-held-out-validation.md) ---------------
# Pure functions over label rows: no embedder, no torch, no matplotlib.

from compute.identification.probe import (  # noqa: E402
    _HELDOUT_GAP_MS, _night_classifier, _render_html, _visit_section, group_visits,
)


def _row(cat_id, ts, labeled_ts=None):
    return {"cat_id": cat_id, "src_recv_ts": ts, "labeled_ts": labeled_ts or ts,
            "cat_name": f"cat{cat_id}", "quality": "gallery", "crop_path": "x.jpg"}


def test_groups_split_at_the_gap_within_one_cat():
    rows = [_row(1, 0), _row(1, 100), _row(1, 200),          # visit A
            _row(1, 200 + _HELDOUT_GAP_MS + 1), _row(1, 200 + _HELDOUT_GAP_MS + 50)]  # visit B
    groups, nights = group_visits(rows)
    assert groups == [[0, 1, 2], [3, 4]]
    assert nights == [None, None]


def test_two_cats_in_the_same_minute_are_never_one_group():
    """The defect a global time-sort would introduce: a group with no true cat_id.

    Tailgating is expected at this door, and dataset_items' UNIQUE (src_frame_id,
    src_recv_ts) only stops two cats sharing ONE frame's label — not adjacent frames.
    """
    rows = [_row(1, 0), _row(2, 50), _row(1, 100), _row(2, 150)]
    groups, _nights = group_visits(rows)
    assert groups == [[0, 2], [1, 3]], "one group per cat, interleaved in time"
    for g in groups:
        assert len({rows[i]["cat_id"] for i in g}) == 1


def test_a_dropout_shorter_than_the_gap_keeps_one_visit():
    """A few seconds with no detections must not split a physical visit."""
    rows = [_row(1, 0), _row(1, 8_000), _row(1, 16_000)]  # 8s dropouts
    groups, _ = group_visits(rows)
    assert groups == [[0, 1, 2]]


def test_night_flag_comes_from_the_groups_first_crop():
    """Bucketed WHOLE by the first crop — the same rule gate_scorecard's split uses."""
    rows = [_row(1, 10), _row(1, 20), _row(2, 10_000_000)]
    groups, nights = group_visits(rows, is_night=lambda ts: ts > 1_000)
    assert groups == [[0, 1], [2]]
    assert nights == [False, True], "first crop decides, not a per-crop vote"


def test_catless_rows_are_skipped():
    """unknown_cat crops carry no identity to score against."""
    rows = [_row(1, 0), {"cat_id": None, "src_recv_ts": 5, "labeled_ts": 5}, _row(1, 10)]
    groups, _ = group_visits(rows)
    assert groups == [[0, 2]]


def test_empty_labels_group_to_nothing():
    assert group_visits([]) == ([], [])


class _LocStore:
    def __init__(self, coords):
        self._coords = coords

    def get_location(self):
        return self._coords


def test_night_classifier_is_none_without_a_location():
    """No location → the split is reported unavailable, never guessed at (0,0)."""
    assert _night_classifier(_LocStore(None)) is None


# --- Report rendering -----------------------------------------------------------------

_CHARTS = {"scatter": "s", "confusion": "c", "hist": "h", "curve": "u"}


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


def test_report_without_visits_keeps_the_crop_level_verdict():
    html_out = _render_html(_metrics(), _CHARTS, "gallery")
    assert "Strong separation" in html_out
    assert "for comparison" not in html_out


def test_report_with_visits_demotes_crop_level_and_explains_it():
    visits = {"available": True, "accuracy": 0.82, "unknown_rate": 0.1, "n_scored": 40,
              "correct": 29, "wrong": 6, "unknown": 5, "n_groups": 41,
              "labeled_ts_groups": 41, "gap_ms": 60_000, "threshold": 0.5,
              "curve": [], "marks": {}, "unscoreable": [], "regimes": None, "cross": None}
    html_out = _render_html(_metrics(visits), _CHARTS, "gallery")
    assert "Crop-level scoring (for comparison)" in html_out
    assert "near-duplicate detection" in html_out
    assert "Strong separation" not in html_out, "no crop-level verdict above the real one"
    assert "82%" in html_out and "visit accuracy" in html_out
    assert "leave-one-CROP-out" in html_out


def test_unavailable_visit_section_says_nothing_was_measured():
    for reason, phrase in (("uncalibrated_threshold", "cannot be calibrated"),
                           ("too_few_visits", "Fewer than two visits")):
        out = _visit_section(_metrics({"available": False, "reason": reason}), ["a", "b"])
        assert phrase in out
        assert "not a score of zero" in out or "Nothing was measured" in out


def test_unscoreable_cats_are_named_in_the_report():
    visits = {"available": True, "accuracy": 1.0, "unknown_rate": 0.0, "n_scored": 4,
              "correct": 4, "wrong": 0, "unknown": 0, "n_groups": 5,
              "labeled_ts_groups": 5, "gap_ms": 60_000, "threshold": 0.5, "curve": [],
              "marks": {}, "unscoreable": [{"cat_index": 1, "n_visits": 1}],
              "regimes": None, "cross": None}
    out = _visit_section(_metrics(visits), ["Sultan", "Store Kali"])
    assert "Store Kali (1)" in out
    assert "only one visit" in out


def test_regime_table_renders_na_for_an_empty_cross_cell():
    visits = {"available": True, "accuracy": 0.9, "unknown_rate": 0.0, "n_scored": 10,
              "correct": 9, "wrong": 1, "unknown": 0, "n_groups": 10,
              "labeled_ts_groups": 10, "gap_ms": 60_000, "threshold": 0.5, "curve": [],
              "marks": {}, "unscoreable": [],
              "regimes": {"day": {"accuracy": 1.0, "n_scored": 6}, "night": None},
              "cross": {"day_vs_day": {"accuracy": 1.0, "n_scored": 6},
                        "day_vs_night": None, "night_vs_day": None,
                        "night_vs_night": None}}
    out = _visit_section(_metrics(visits), ["Sultan", "Kali"])
    assert "n/a" in out, "an empty gallery side is n/a, never 0%"
    assert "vs night-only gallery" in out


def test_visit_confusion_table_names_the_mistake():
    """The actionable matrix: WHICH cat a visit is mistaken for, plus a declined column."""
    visits = {"available": True, "accuracy": 0.5, "unknown_rate": 0.0, "n_scored": 4,
              "correct": 2, "wrong": 2, "unknown": 0, "n_groups": 4,
              "labeled_ts_groups": 4, "gap_ms": 60_000, "threshold": 0.5, "curve": [],
              "marks": {}, "unscoreable": [], "regimes": None, "cross": None,
              # Sultan twice named Store Sultan; Store Sultan clean, one declined.
              "confusion": [[0, 2, 0], [0, 1, 1]]}
    out = _visit_section(_metrics(visits), ["Sultan", "Store Sultan"])
    assert "Which cat gets mistaken for which" in out
    assert "actual \\ named" in out
    assert "declined" in out
    # A zero cell is muted rather than shouting a bold 0 across a sparse matrix.
    assert 'class="na">0<' in out


def test_unavailable_visits_do_not_demote_the_crop_level_verdict():
    """An unavailable visit score must not suppress the only verdict the report has.

    `demoted` keys on the scoring having SUCCEEDED, not on `_visit_section` returning any
    HTML — it also returns a banner when scoring was unavailable, and demoting on that both
    hid the crop-level verdict and told the reader to compare against "the visit-level
    number above", which in that case was never computed.
    """
    out = _render_html(_metrics({"available": False, "reason": "too_few_visits"}),
                       _CHARTS, "gallery")
    assert "Strong separation" in out, "the crop-level verdict is all this report has"
    assert "visit-level number above" not in out, "no pointer to a number never computed"
    assert "unavailable" in out.lower(), "the banner still explains why"
