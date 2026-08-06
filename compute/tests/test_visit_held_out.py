"""Tests for the visit-held-out scoring (compute.identification.feasibility).

Pure numpy with synthetic vectors — no torch, no model, no GPU — which is what lets
the load-bearing part be pinned exactly: that hiding a whole visit removes the
near-duplicate shortcut the crop-level leave-one-out leaves open.

The synthetic setup mirrors the real failure mode. Each cat gets several VISITS, and
the crops within one visit are near-duplicates of each other (tight cluster) while the
same cat's *other* visits sit further away. Under crop-level LOO that scores ~100%
regardless of whether the cats are separable at all; under visit-held-out it only scores
well when the cat's visits genuinely resemble each other.
"""
from __future__ import annotations

import numpy as np
import pytest

from compute.collection.store import Store
from compute.identification.feasibility import _per_cat, run_feasibility
from compute.identification.probe import _tldr, _wilson, _worst_pair

_AGG = Store._aggregate_identity


def _visit_data(rng, cats, visits_per_cat, crops_per_visit, cat_sep=1.0, visit_sep=0.25,
                dup=0.005, dim=8):
    """Synthetic crops laid out as cat → visits → near-duplicate crops.

    ``cat_sep`` separates cats, ``visit_sep`` scatters a cat's visits around its own
    centre, and ``dup`` is the (tiny) spread within one visit. Returns
    ``(cat_ids, vectors, groups)`` where ``groups`` are row-index lists, one per visit.
    """
    ids, vecs, groups = [], [], []
    for ci in range(cats):
        cat_centre = np.zeros(dim)
        cat_centre[ci % dim] = cat_sep * (1 + ci // dim)
        for _ in range(visits_per_cat):
            visit_centre = cat_centre + rng.normal(0, visit_sep, size=dim)
            g = []
            for _ in range(crops_per_visit):
                g.append(len(ids))
                ids.append(ci + 1)
                vecs.append(visit_centre + rng.normal(0, dup, size=dim))
            groups.append(g)
    return ids, np.array(vecs), groups


def test_absent_visit_groups_leaves_result_byte_identical():
    """The feature is additive: no visit_groups → no 'visits' key, nothing else moves."""
    rng = np.random.default_rng(1)
    ids, vecs, _groups = _visit_data(rng, cats=3, visits_per_cat=3, crops_per_visit=5)
    plain = run_feasibility(ids, {}, vecs)
    assert "visits" not in plain
    # And the same call WITH groups changes only that one key.
    _ids2, _v2, groups = _visit_data(np.random.default_rng(1), 3, 3, 5)
    withv = run_feasibility(ids, {}, vecs, visit_groups=groups, aggregate=_AGG)
    assert set(withv) - set(plain) == {"visits"}
    for key in plain:
        assert withv[key] == plain[key], key


def test_held_out_visit_cannot_match_its_own_crops():
    """The core property: a visit's own near-duplicates are excluded from its gallery.

    Crops within a visit are 50x closer to each other than the cat's visits are to each
    other, so crop-level LOO is trivially perfect. Visit-held-out must NOT inherit that.
    """
    rng = np.random.default_rng(2)
    # Cats deliberately NOT separable: every cat shares one centre, so identity is
    # unlearnable and only the same-visit shortcut could produce a good score.
    ids, vecs, groups = _visit_data(rng, cats=4, visits_per_cat=4, crops_per_visit=6,
                                    cat_sep=0.0, visit_sep=0.4, dup=0.005)
    res = run_feasibility(ids, {}, vecs, visit_groups=groups, aggregate=_AGG)
    assert res["knn"]["accuracy"] > 0.95, "crop LOO should be fooled by near-duplicates"
    v = res["visits"]
    assert v["available"] is True
    # With no real signal, visit-level accuracy must collapse toward chance (4 cats).
    assert v["accuracy"] is not None and v["accuracy"] < 0.6


def test_separable_cats_score_well_at_visit_level():
    """Sanity counterpart: when cats really are distinct, visit-held-out agrees."""
    rng = np.random.default_rng(3)
    ids, vecs, groups = _visit_data(rng, cats=4, visits_per_cat=4, crops_per_visit=6,
                                    cat_sep=3.0, visit_sep=0.1, dup=0.005)
    v = run_feasibility(ids, {}, vecs, visit_groups=groups, aggregate=_AGG)["visits"]
    assert v["accuracy"] == 1.0
    assert v["n_scored"] == 16
    assert v["correct"] + v["wrong"] + v["unknown"] == v["n_scored"]


def test_single_visit_cat_is_unscoreable_not_wrong():
    """A cat with one visit has no gallery entry of its own — excluded, and named.

    Counting it as a failure would understate the model and hide the real cause (this
    is Store Kali: 17 crops, one visit).
    """
    rng = np.random.default_rng(4)
    ids, vecs, groups = _visit_data(rng, cats=3, visits_per_cat=3, crops_per_visit=4,
                                    cat_sep=3.0)
    # Append a 4th cat with exactly ONE visit.
    lone = [len(ids) + i for i in range(4)]
    centre = np.zeros(8)
    centre[5] = 9.0
    extra = [centre + rng.normal(0, 0.005, size=8) for _ in range(4)]
    ids = list(ids) + [4] * 4
    vecs = np.vstack([vecs, np.array(extra)])
    groups = groups + [lone]

    v = run_feasibility(ids, {}, vecs, visit_groups=groups, aggregate=_AGG)["visits"]
    assert v["n_groups"] == 10
    assert v["n_scored"] == 9, "the lone visit must be excluded from the denominator"
    assert v["unscoreable"] == [{"cat_index": 3, "n_visits": 1}]  # 0-based cat index


def test_uncalibrated_threshold_reports_unavailable_not_all_unknown():
    """threshold=None would make _aggregate_identity resolve EVERY visit to unknown.

    That renders as 0 correct / 0 wrong / 100% unknown — a catastrophic-looking result
    where the honest reading is that nothing was measured.
    """
    rng = np.random.default_rng(5)
    # One crop per cat → no same-cat pair exists → _best_threshold returns None.
    ids = [1, 2, 3]
    vecs = rng.normal(0, 1, size=(3, 8))
    groups = [[0], [1], [2]]
    res = run_feasibility(ids, {}, vecs, visit_groups=groups, aggregate=_AGG)
    assert res["distances"]["suggested_threshold"] is None
    v = res["visits"]
    assert v["available"] is False
    assert v["reason"] == "uncalibrated_threshold"
    assert "accuracy" not in v


def test_too_few_visits_reports_unavailable():
    rng = np.random.default_rng(6)
    # Two cats (run_feasibility needs >= 2) but only ONE group handed in, so there is
    # nothing to hold a visit out against.
    ids, vecs, groups = _visit_data(rng, cats=2, visits_per_cat=2, crops_per_visit=4,
                                    cat_sep=3.0)
    v = run_feasibility(ids, {}, vecs, visit_groups=[groups[0]], aggregate=_AGG)["visits"]
    assert v["available"] is False and v["reason"] == "too_few_visits"


def test_mixed_cat_group_raises():
    """A group must hold one cat's crops — otherwise there is no true label to score."""
    rng = np.random.default_rng(7)
    ids, vecs, groups = _visit_data(rng, cats=2, visits_per_cat=2, crops_per_visit=3)
    bad = [groups[0] + groups[2], groups[1], groups[3]]  # cat 1 + cat 2 in one group
    with pytest.raises(ValueError, match="mixes cats"):
        run_feasibility(ids, {}, vecs, visit_groups=bad, aggregate=_AGG)


def test_aggregate_is_required():
    rng = np.random.default_rng(8)
    ids, vecs, groups = _visit_data(rng, cats=2, visits_per_cat=2, crops_per_visit=3)
    with pytest.raises(ValueError, match="aggregate"):
        run_feasibility(ids, {}, vecs, visit_groups=groups)


def test_unknown_is_reported_beside_accuracy_not_folded_in():
    """A far-away threshold forces unknowns; accuracy stays correct/(correct+wrong)."""
    rng = np.random.default_rng(9)
    ids, vecs, groups = _visit_data(rng, cats=3, visits_per_cat=3, crops_per_visit=4,
                                    cat_sep=3.0, visit_sep=0.1)
    v = run_feasibility(ids, {}, vecs, visit_groups=groups, aggregate=_AGG,
                        extra_thresholds={"active_model": 0.0})["visits"]
    # The curve includes threshold 0.0, where nothing can be below → all unknown.
    at_zero = [p for p in v["curve"] if p["threshold"] == 0.0]
    assert at_zero and at_zero[0]["unknown"] == v["n_scored"]
    assert at_zero[0]["accuracy"] is None, "no decisions → no accuracy, not 0%"
    assert v["marks"]["active_model"] == 0.0
    assert "crop_level" in v["marks"], "the crop threshold is marked for comparison"


def test_confusion_rows_sum_to_scored_visits_with_unknown_column():
    rng = np.random.default_rng(10)
    ids, vecs, groups = _visit_data(rng, cats=3, visits_per_cat=3, crops_per_visit=4,
                                    cat_sep=1.5, visit_sep=0.3)
    v = run_feasibility(ids, {}, vecs, visit_groups=groups, aggregate=_AGG)["visits"]
    conf = np.array(v["confusion"])
    assert conf.shape == (3, 4), "n_cats x (n_cats + unknown column)"
    assert int(conf.sum()) == v["n_scored"]
    assert int(np.trace(conf[:, :3])) == v["correct"]


def test_day_night_split_partitions_the_scored_visits():
    """Day + night must account for every scored visit — neither can drift from All."""
    rng = np.random.default_rng(11)
    ids, vecs, groups = _visit_data(rng, cats=3, visits_per_cat=4, crops_per_visit=4,
                                    cat_sep=3.0, visit_sep=0.1)
    night = [i % 2 == 1 for i in range(len(groups))]
    v = run_feasibility(ids, {}, vecs, visit_groups=groups, visit_night=night,
                        aggregate=_AGG)["visits"]
    day, ni = v["regimes"]["day"], v["regimes"]["night"]
    assert day["n_scored"] + ni["n_scored"] == v["n_scored"]
    assert day["correct"] + ni["correct"] == v["correct"]
    assert day["wrong"] + ni["wrong"] == v["wrong"]
    assert day["unknown"] + ni["unknown"] == v["unknown"]


def test_cross_regime_detects_a_regime_split_embedding():
    """The one-gallery-or-two question: same cats, but day and night look different.

    Under IR a cat can resemble a DIFFERENT cat's daylight appearance, so the night
    identity axes are permuted relative to day: night-vs-night is consistent and works,
    while night-vs-day-gallery systematically names the wrong cat. That is the signal
    arguing for separate day/night galleries rather than more night data.

    Note a mere large offset would NOT model this: distances are cosine over
    L2-normalised vectors, so a shared extra axis rescales every vector alike and the
    identity component survives it. The confusion has to be in the identity itself.
    """
    rng = np.random.default_rng(12)
    n_cats = 3
    ids, vecs, groups, night = [], [], [], []
    for ci in range(n_cats):
        for is_night in (False, True):
            for _ in range(3):  # 3 visits per cat per regime
                centre = np.zeros(8)
                # Night identity is the NEXT cat's day identity — a lookalike collapse.
                centre[(ci + 1) % n_cats if is_night else ci] = 3.0
                g = []
                for _ in range(4):
                    g.append(len(ids))
                    ids.append(ci + 1)
                    vecs.append(centre + rng.normal(0, 0.01, size=8))
                groups.append(g)
                night.append(is_night)
    v = run_feasibility(ids, {}, np.array(vecs), visit_groups=groups,
                        visit_night=night, aggregate=_AGG)["visits"]
    cross = v["cross"]
    assert cross["night_vs_night"]["accuracy"] == 1.0
    assert cross["day_vs_day"]["accuracy"] == 1.0
    # Cross-regime must fail or decline — never quietly report the same-regime number.
    assert (cross["night_vs_day"]["accuracy"] or 0.0) < 0.5
    assert (cross["day_vs_night"]["accuracy"] or 0.0) < 0.5


def test_regime_split_matches_cross_vs_mixed_gallery():
    """regimes[x] is 'x visits vs the mixed gallery' — the cross table's implicit column."""
    rng = np.random.default_rng(13)
    ids, vecs, groups = _visit_data(rng, cats=3, visits_per_cat=4, crops_per_visit=3,
                                    cat_sep=2.0, visit_sep=0.3)
    night = [i < 6 for i in range(len(groups))]
    v = run_feasibility(ids, {}, vecs, visit_groups=groups, visit_night=night,
                        aggregate=_AGG)["visits"]
    for name in ("day", "night"):
        assert v["regimes"][name]["n_scored"] >= 1
    # A visit unscoreable against the mixed gallery is unscoreable against any subset.
    for name in ("day", "night"):
        mixed = v["regimes"][name]["n_scored"]
        for gal in ("day", "night"):
            cell = v["cross"][f"{name}_vs_{gal}"]
            if cell is not None:
                assert cell["n_scored"] <= mixed


def test_missing_regime_flags_disable_the_split_without_failing():
    rng = np.random.default_rng(14)
    ids, vecs, groups = _visit_data(rng, cats=2, visits_per_cat=3, crops_per_visit=3,
                                    cat_sep=3.0)
    v = run_feasibility(ids, {}, vecs, visit_groups=groups,
                        visit_night=[None] * len(groups), aggregate=_AGG)["visits"]
    assert v["available"] is True
    assert v["regimes"] is None and v["cross"] is None


def test_grouping_crosscheck_fields_are_recorded_verbatim():
    """The block records the gap + labeled_ts counts; it does no clustering itself."""
    rng = np.random.default_rng(15)
    ids, vecs, groups = _visit_data(rng, cats=2, visits_per_cat=3, crops_per_visit=3,
                                    cat_sep=3.0)
    v = run_feasibility(ids, {}, vecs, visit_groups=groups, aggregate=_AGG,
                        gap_ms=60_000, labeled_ts_groups=7)["visits"]
    assert v["gap_ms"] == 60_000
    assert v["labeled_ts_groups"] == 7
    assert v["n_groups"] == 6


def test_visit_threshold_is_calibrated_on_cross_visit_pairs():
    """The threshold behind the visit headline must NOT be the crop-level one.

    Same-visit crops are near-duplicates, so the crop-level threshold lands at a tiny
    distance. Using it for the visit task declines nearly every visit — the defect this
    separate calibration exists to remove.

    The size of the gap is data-dependent — with cleanly separable cats both
    calibrations coincide, because the threshold is then set by the largest same-cat
    distance either way. It bites when the same-cat distances OVERLAP the
    different-cat ones, which is the real-data case: the near-zero same-visit mass
    drags the balanced-accuracy optimum down toward the duplicates. On this fixture
    it is a ~1500x difference.
    """
    rng = np.random.default_rng(2)
    ids, vecs, groups = _visit_data(rng, cats=4, visits_per_cat=4, crops_per_visit=6,
                                    cat_sep=0.0, visit_sep=0.4, dup=0.005)
    res = run_feasibility(ids, {}, vecs, visit_groups=groups, aggregate=_AGG)
    crop_t = res["distances"]["suggested_threshold"]
    v = res["visits"]
    assert crop_t < 0.01, "same-visit duplicates pull the crop threshold to ~zero"
    assert v["threshold"] > crop_t * 100, (crop_t, v["threshold"])
    # The regression this guards: at the crop-level threshold every visit is DECLINED,
    # which renders as 0 correct / 0 wrong / 100% unknown — a catastrophic-looking
    # report where the honest reading is "the threshold was wrong for this question".
    assert v["correct"] + v["wrong"] == v["n_scored"], "must decide, not decline"
    # The cross-visit AUC is reported too, and is its own number (not the crop one).
    assert v["auc"] is not None and v["auc"] != res["distances"]["auc"]


def test_single_visit_per_cat_cannot_calibrate_the_visit_threshold():
    """No cross-visit same-cat pair exists → uncalibrated, reported as such.

    Distinct from the too-few-visits case: there are plenty of visits here, just never
    two of the same cat.
    """
    rng = np.random.default_rng(21)
    ids, vecs, groups = _visit_data(rng, cats=4, visits_per_cat=1, crops_per_visit=5,
                                    cat_sep=3.0)
    v = run_feasibility(ids, {}, vecs, visit_groups=groups, aggregate=_AGG)["visits"]
    assert v["available"] is False and v["reason"] == "uncalibrated_threshold"


def test_unavailable_block_carries_no_measurements():
    """`available: False` means nothing was measured — so no auc/threshold rides along.

    The cross-visit AUC is computed before the availability branch, so it has to be
    gated on the way out; attaching a real number beside "nothing was measured" would
    contradict the distinction this whole scoring exists to keep honest.
    """
    rng = np.random.default_rng(6)
    ids, vecs, groups = _visit_data(rng, cats=2, visits_per_cat=2, crops_per_visit=4,
                                    cat_sep=3.0)
    v = run_feasibility(ids, {}, vecs, visit_groups=[groups[0]], aggregate=_AGG)["visits"]
    assert v["available"] is False
    assert "auc" not in v and "threshold_balanced_acc" not in v
    # An available block still reports both.
    ok = run_feasibility(ids, {}, vecs, visit_groups=groups, aggregate=_AGG)["visits"]
    assert ok["auc"] is not None and ok["threshold_balanced_acc"] is not None


# --- per-cat breakdown -----------------------------------------------------------------
# The rows the "Cats to enrol" table reads. Derived in `_score_visits` (not by a reader
# of `confusion`) precisely so these invariants hold by construction.

def test_per_cat_rows_self_identify_by_cat_id_not_position():
    """The load-bearing property: a row names its cat, so a shifting index can't mislabel.

    `confusion`'s index is positional over `sorted(set(cat_ids))`, so with a
    non-contiguous id set (a cat retired, or excluded from this build) row 1 is NOT
    cat 1. Reading per-cat data off the matrix without a map is what made this
    unbuildable on stored runs; these rows carry the id themselves.
    """
    rng = np.random.default_rng(31)
    ids, vecs, groups = _visit_data(rng, cats=3, visits_per_cat=3, crops_per_visit=4,
                                    cat_sep=3.0)
    # _visit_data labels cats 1..3; remap to a gappy, out-of-order set.
    remap = {1: 11, 2: 3, 3: 7}
    ids = [remap[i] for i in ids]
    names = {11: "Mittens", 3: "Sultan", 7: "Store Jihn"}
    v = run_feasibility(ids, names, vecs, visit_groups=groups, aggregate=_AGG)["visits"]

    per_cat = v["per_cat"]
    # Ordered by the same sorted-unique ids the confusion index uses.
    assert [c["cat_id"] for c in per_cat] == [3, 7, 11]
    assert [c["cat_name"] for c in per_cat] == ["Sultan", "Store Jihn", "Mittens"]


def test_per_cat_counts_reconcile_with_the_confusion_matrix_and_headline():
    """Rows are the same counts the block's own accuracy is built from — not a re-derivation."""
    rng = np.random.default_rng(32)
    ids, vecs, groups = _visit_data(rng, cats=3, visits_per_cat=4, crops_per_visit=4,
                                    cat_sep=3.0)
    v = run_feasibility(ids, {}, vecs, visit_groups=groups, aggregate=_AGG)["visits"]
    per_cat, conf = v["per_cat"], v["confusion"]
    n_cats = len(per_cat)

    for i, c in enumerate(per_cat):
        assert c["scored"] == sum(conf[i]), "scored is the confusion row sum"
        assert c["correct"] == conf[i][i]
        assert c["declined"] == conf[i][n_cats], "the final column is 'declined'"
        assert c["correct"] + c["wrong"] + c["declined"] == c["scored"]

    # And they roll up to the headline exactly — the per-cat column can never contradict
    # the run's own number.
    assert sum(c["correct"] for c in per_cat) == v["correct"]
    assert sum(c["wrong"] for c in per_cat) == v["wrong"]
    assert sum(c["declined"] for c in per_cat) == v["unknown"]
    assert sum(c["scored"] for c in per_cat) == v["n_scored"]


def test_per_cat_recall_excludes_declined_like_the_headline_accuracy():
    """recall = correct/(correct+wrong): declined is reported beside it, never folded in.

    For a resident at the door "named the wrong cat" and "declined to name" mean
    opposite things, and the block's own `accuracy` already draws that line.
    """
    conf = np.array([
        [6, 2, 0, 2],   # cat 0: 6 correct, 2 wrong, 2 declined  -> recall 6/8
        [0, 0, 0, 5],   # cat 1: everything declined             -> recall None, not 0.0
        [0, 0, 4, 0],   # cat 2: clean sweep (diagonal is col 2) -> recall 1.0
    ], dtype=int)
    rows = _per_cat(conf, {}, [{"cat_id": i + 1, "cat_name": f"c{i}"} for i in range(3)], 3)

    assert rows[0]["recall"] == pytest.approx(6 / 8)
    assert rows[0]["declined_rate"] == pytest.approx(2 / 10)
    # Nothing decided is NOT a recall of zero — the reader renders the two differently.
    assert rows[1]["recall"] is None and rows[1]["declined"] == 5
    assert rows[1]["declined_rate"] == pytest.approx(1.0)
    assert rows[2]["recall"] == pytest.approx(1.0)


def test_per_cat_folds_the_unscoreable_tally_onto_the_cats_own_row():
    """A single-visit cat has an all-zero row; its count survives on the row, not beside it."""
    conf = np.zeros((2, 3), dtype=int)
    conf[0] = [3, 0, 0]
    rows = _per_cat(conf, {1: 2}, [{"cat_id": 10, "cat_name": "a"},
                                   {"cat_id": 20, "cat_name": "b"}], 2)
    assert rows[0]["unscoreable"] == 0 and rows[0]["scored"] == 3
    # Zero scored AND unscoreable > 0 is "no number can exist yet", not a measured zero.
    assert rows[1]["scored"] == 0 and rows[1]["recall"] is None
    assert rows[1]["unscoreable"] == 2


def test_each_regime_carries_per_cat_but_the_cross_cells_do_not():
    """Day/night per-cat is free from the same addition; cross-regime is a non-goal.

    Computing rows nothing surfaces would put a plausible-looking but unread number
    into every stored run.
    """
    rng = np.random.default_rng(33)
    ids, vecs, groups = _visit_data(rng, cats=3, visits_per_cat=4, crops_per_visit=4,
                                    cat_sep=3.0)
    nights = [i % 2 == 1 for i in range(len(groups))]
    v = run_feasibility(ids, {}, vecs, visit_groups=groups, visit_night=nights,
                        aggregate=_AGG)["visits"]

    for regime in ("day", "night"):
        rows = v["regimes"][regime]["per_cat"]
        assert rows and all(r["cat_id"] is not None for r in rows)
        # Each regime's rows sum to that regime's own scored count — the number the
        # Day/Night tooltip has to show, so a 100% off one visit can't read as solid.
        assert sum(r["scored"] for r in rows) == v["regimes"][regime]["n_scored"]

    for cell in v["cross"].values():
        if cell is not None:
            assert cell["per_cat"] is None


# --- report TL;DR: derived numbers -------------------------------------------------
# Pure re-readings of the visit block. Tested here because the report's lead block is
# the one place a reader takes a number without reading the section it came from.

def test_wilson_keeps_width_at_a_perfect_score():
    """The reason it is Wilson and not the normal approximation.

    8-of-8 has a normal-approximation interval of exactly zero width, which would print
    "we saw this cat eight times" as certainty — precisely the over-reading the interval
    exists to prevent.
    """
    lo, hi = _wilson(8, 8)
    assert hi == 1.0
    assert lo < 0.75, "a perfect 8/8 must still admit real uncertainty"
    # And it stays inside [0, 1] at the other end.
    lo0, hi0 = _wilson(0, 5)
    assert lo0 == 0.0 and 0 < hi0 < 1
    assert _wilson(0, 0) is None, "no visits is not a 0% interval"
    # More data narrows it.
    assert (_wilson(90, 100)[1] - _wilson(90, 100)[0]) < (_wilson(9, 10)[1] - _wilson(9, 10)[0])


def test_worst_pair_names_the_biggest_off_diagonal_cell_only():
    """One pair, and never the diagonal or the declined column.

    Declined is the final column and is not a mistaken identity — counting it would
    make the report's "worst mix-up" the cat it most often stayed silent about.
    """
    #        ->A  ->B  ->C  declined
    conf = [[10,   1,   0,   4],      # A: mostly right, 4 declined (must be ignored)
            [ 3,   5,   7,   0],      # B: named C seven times  <- the answer
            [ 0,   2,   9,   0]]
    pair = _worst_pair({"confusion": conf}, ["A", "B", "C"])
    assert (pair["true"], pair["named"], pair["count"]) == ("B", "C", 7)
    assert pair["of"] == 15
    # `of` is DECIDED visits — the declined column is excluded, matching the cell the
    # count came from. Row A above has 4 declines, so a row-sum would read 15 here.
    a_pair = _worst_pair({"confusion": [[10, 5, 0, 4], [0, 9, 0, 0], [0, 0, 9, 0]]},
                         ["A", "B", "C"])
    assert (a_pair["true"], a_pair["count"], a_pair["of"]) == ("A", 5, 15)
    # A clean run has no pair at all rather than a zero-count one.
    clean = [[4, 0, 0, 1], [0, 4, 0, 0], [0, 0, 4, 0]]
    assert _worst_pair({"confusion": clean}, ["A", "B", "C"]) is None


def test_tldr_derives_the_lead_numbers_and_stays_none_when_unavailable():
    rng = np.random.default_rng(41)
    ids, vecs, groups = _visit_data(rng, cats=3, visits_per_cat=4, crops_per_visit=4,
                                    cat_sep=3.0)
    m = run_feasibility(ids, {1: "A", 2: "B", 3: "C"}, vecs,
                        visit_groups=groups, aggregate=_AGG)
    t = _tldr(m, ["A", "B", "C"])
    v = m["visits"]
    assert t["accuracy"] == v["accuracy"], "the summary must not recompute the headline"
    assert t["decided"] == v["correct"] + v["wrong"]
    # Resolution is one visit as a share of what was DECIDED — the smallest real move.
    assert t["resolution"] == pytest.approx(1 / t["decided"])
    assert t["ci"][0] <= v["accuracy"] <= t["ci"][1]
    # The weakest cat is a real per_cat row, not a recomputation.
    assert t["weakest"] in [c for c in v["per_cat"] if c["recall"] is not None]
    assert t["weakest"]["recall"] == min(
        c["recall"] for c in v["per_cat"] if c["recall"] is not None)

    # An unavailable block yields no summary at all, rather than a row of dashes that
    # would read as a measured result.
    single = run_feasibility(ids, {}, vecs, visit_groups=[groups[0]], aggregate=_AGG)
    assert single["visits"]["available"] is False
    assert _tldr(single, ["A", "B", "C"]) is None


# --- report TL;DR: the two charts -----------------------------------------------------
# Asserted because a regression to "" (or to a raised exception swallowed upstream) would
# otherwise remove a chart from every report silently.

def _pc_row(cid, name, scored, correct, wrong, declined=0):
    dec = correct + wrong
    return {"cat_id": cid, "cat_name": name, "scored": scored, "correct": correct,
            "wrong": wrong, "declined": declined,
            "recall": (correct / dec) if dec else None,
            "declined_rate": (declined / scored) if scored else None, "unscoreable": 0}


def test_percat_chart_renders_and_is_empty_only_when_it_has_nothing():
    pytest.importorskip("matplotlib")
    from compute.identification.probe import _percat_png

    rows = [_pc_row(1, "A", 20, 19, 1), _pc_row(2, "B", 9, 5, 4)]
    png = _percat_png({"per_cat": rows})
    assert png.startswith("data:image/png;base64,") and len(png) > 2000
    # One row is a legitimate chart; no rows, or only unscoreable rows, is not.
    assert _percat_png({"per_cat": [rows[0]]}).startswith("data:image/png;base64,")
    assert _percat_png({"per_cat": []}) == ""
    assert _percat_png({}) == ""
    assert _percat_png({"per_cat": [_pc_row(3, "C", 0, 0, 0)]}) == "", \
        "a cat with nothing decided has no bar to draw"


def test_regime_chart_needs_both_sides_of_a_pair():
    pytest.importorskip("matplotlib")
    from compute.identification.probe import _regime_png

    day = [_pc_row(1, "A", 12, 12, 0), _pc_row(2, "B", 8, 6, 2)]
    night = [_pc_row(1, "A", 5, 3, 2), _pc_row(2, "B", 4, 1, 3)]
    png = _regime_png({"regimes": {"day": {"per_cat": day}, "night": {"per_cat": night}}})
    assert png.startswith("data:image/png;base64,") and len(png) > 2000
    # No location -> no regimes -> no chart, rather than one regime drawn as if it were both.
    assert _regime_png({"regimes": None}) == ""
    assert _regime_png({}) == ""
    # A cat present on only ONE side is not a gap of unknown size — it is no reading.
    only_day = {"regimes": {"day": {"per_cat": day}, "night": {"per_cat": []}}}
    assert _regime_png(only_day) == ""
    # Mismatched cat_ids across the regimes must not pair up by position.
    mismatched = {"regimes": {"day": {"per_cat": [_pc_row(1, "A", 4, 4, 0)]},
                              "night": {"per_cat": [_pc_row(9, "Z", 4, 2, 2)]}}}
    assert _regime_png(mismatched) == ""


def test_the_weakest_cat_tile_and_the_chart_cannot_disagree_on_a_tie():
    """One shared ordering key, because two independent ones disagreed.

    Recall is a ratio of small integers, so exact ties are routine — 1/2, 2/4 and 3/6 all
    land on 0.5. A plain `min()` took the first tied row (per_cat is cat_id order) while
    the chart's sort preferred the largest sample, so one report block named two different
    cats "weakest".
    """
    from compute.identification.probe import _WEAKEST_KEY, _tldr

    tied = [_pc_row(2, "CatB", 3, 1, 1), _pc_row(1, "CatA", 5, 2, 2)]   # both recall 0.5
    assert tied[0]["recall"] == tied[1]["recall"]
    t = _tldr({"visits": {"available": True, "per_cat": tied, "accuracy": 0.5,
                          "correct": 3, "wrong": 3, "unknown": 0, "n_scored": 8,
                          "unknown_rate": 0.0, "confusion": None, "unscoreable": []}},
              ["CatA", "CatB"])
    chart_first = sorted([c for c in tied if c["recall"] is not None], key=_WEAKEST_KEY)[0]
    assert t["weakest"]["cat_id"] == chart_first["cat_id"], \
        "the tile and the chart's accented bar must be the same cat"
    assert t["weakest"]["cat_name"] == "CatA", "on a tie, the larger sample is the pick"
