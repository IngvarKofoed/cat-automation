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

    def labeled_crops(self, kinds, qualities):  # signature mirrors Store.labeled_crops
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
