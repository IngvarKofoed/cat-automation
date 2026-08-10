"""Open-set scoring: stranger rejection, the capped-gallery forecast, and the
per-visit outcome list (docs/specs/2026-08-09-open-set-scoring-and-calibration.md).

Pure numpy over synthetic vectors, like ``test_visit_held_out.py`` — the three additions
are all masks and re-votes over the SAME distance matrix that file already exercises, so
no torch, no model and no GPU is involved in pinning them.

What each half is protecting, in one line, because the failure modes are quiet ones:

- **Stranger mode** inverts ``_score_visits``' unscoreable branch. Under
  ``gal_mask = (y != C)`` every one of C's groups has an empty ``others``, so the branch
  as written counts them all unscoreable and the pass scores NOTHING — a regression to it
  yields a clean-looking "0 impersonations" rather than an error.
- **The shared threshold grid** is the only thing making the two curves one table. A
  masked pass's own distance range differs, so a grid derived per pass would pair the
  known-cat declines at one threshold with the impersonations at another.
- **Per-visit outcomes** carry REAL cat ids. Inside the scoring they are positional
  indices over the cats present in this run, so excluding one shifts every index above it
  and two runs' "cat 3" are different cats (entry 357).
"""
from __future__ import annotations

import numpy as np
import pytest

from compute.collection.store import Store
from compute.identification.feasibility import (
    _score_visits, _stranger_block, run_feasibility,
)

_AGG = Store._aggregate_identity


def _visit_data(rng, cats, visits_per_cat, crops_per_visit, cat_sep=1.0, visit_sep=0.25,
                dup=0.005, dim=8):
    """Synthetic crops laid out as cat → visits → near-duplicate crops.

    Same generator as ``test_visit_held_out.py``: ``cat_sep`` separates cats, ``visit_sep``
    scatters a cat's visits around its own centre, ``dup`` is the spread inside one visit.
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


def _run(ids, vecs, groups, residents=None, **kw):
    names = {c: f"cat{c}" for c in set(ids)}
    return run_feasibility(ids, names, vecs, visit_groups=groups, aggregate=_AGG,
                           cat_residents=residents, **kw)


# --- Stranger mode: the inverted branch -------------------------------------------------

def test_stranger_mode_scores_the_held_out_cats_visits_the_plain_mode_skips():
    """The load-bearing inversion, pinned against the exact regression.

    The SAME call without ``stranger=True`` is the pre-fix behaviour: every group counted
    unscoreable and nothing scored. Both are asserted here so a revert cannot pass by
    reporting a plausible zero.
    """
    rng = np.random.default_rng(101)
    ids, vecs, groups = _visit_data(rng, cats=3, visits_per_cat=3, crops_per_visit=4,
                                    cat_sep=2.0, visit_sep=0.2)
    m = _run(ids, vecs, groups)
    dist, y = _matrix(vecs), _index(ids)
    held = 0                                     # hold cat index 0 out of the gallery
    sel = [G for G, t in zip(groups, _group_true(y, groups)) if t == held]
    gal = y != held
    thr = m["visits"]["threshold"]

    plain = _score_visits(dist, y, sel, [held] * len(sel), gal, thr, [], _AGG, 3)
    assert plain["n_scored"] == 0, "the plain branch skips these — that IS the defect"
    assert plain["unscoreable"] == [{"cat_index": held, "n_visits": len(sel)}]

    strange = _score_visits(dist, y, sel, [held] * len(sel), gal, thr, [], _AGG, 3,
                            _cats(3), stranger=True)
    assert strange["n_scored"] == len(sel) == 3
    assert strange["rejected"] + strange["impersonated"] == strange["n_scored"]


def test_unknown_is_the_correct_answer_and_any_name_is_an_impersonation():
    """Declining a cat with no right answer available is success, not a failure.

    With well-separated cats and a tight threshold every held-out visit lands far from the
    remaining gallery, so the whole pass rejects — 0% impersonation is the GOOD reading
    here, and the counts must say so rather than reading as 100% wrong.
    """
    rng = np.random.default_rng(102)
    ids, vecs, groups = _visit_data(rng, cats=4, visits_per_cat=4, crops_per_visit=5,
                                    cat_sep=3.0, visit_sep=0.1)
    s = _run(ids, vecs, groups, strangers=True)["visits"]["strangers"]
    assert s["available"] is True
    assert s["impersonated"] == 0 and s["impersonation_rate"] == 0.0
    assert s["rejected"] == s["n_scored"] == 16

    # And the opposite pole: cats that overlap heavily DO get named, and the block says so.
    rng2 = np.random.default_rng(103)
    ids2, vecs2, groups2 = _visit_data(rng2, cats=4, visits_per_cat=4, crops_per_visit=5,
                                       cat_sep=0.3, visit_sep=0.2)
    s2 = _run(ids2, vecs2, groups2, strangers=True)["visits"]["strangers"]
    assert s2["impersonated"] > 0
    assert s2["impersonation_rate"] == pytest.approx(s2["impersonated"] / s2["n_scored"])
    # Every impersonation names a cat, and those names are real ids from `cats`.
    for row in s2["per_cat"]:
        assert sum(n["n"] for n in row["named"]) == row["impersonated"]
        assert all(n["cat_id"] in set(ids2) for n in row["named"])


def test_impersonations_split_by_resident_using_the_roster_flag():
    rng = np.random.default_rng(104)
    ids, vecs, groups = _visit_data(rng, cats=4, visits_per_cat=4, crops_per_visit=5,
                                    cat_sep=0.3, visit_sep=0.2)
    residents = {1: True, 2: True, 3: False, 4: False}
    s = _run(ids, vecs, groups, residents=residents, strangers=True)["visits"]["strangers"]
    assert s["resident_split"] is True
    assert s["as_unknown"] == 0
    assert s["as_resident"] + s["as_neighbour"] == s["impersonated"]
    assert s["resident_impersonation_rate"] == pytest.approx(s["as_resident"] / s["n_scored"])
    # The split is over the IMPERSONATED cat, not the held-out one: a neighbour named as a
    # resident is the dangerous direction and must land in as_resident.
    by_id = {c["cat_id"]: c for c in s["per_cat"]}
    for cid, row in by_id.items():
        named_res = sum(n["n"] for n in row["named"] if residents[n["cat_id"]])
        assert row["as_resident"] == named_res, cid


def test_an_absent_roster_flag_reports_the_split_unmeasured_not_zero():
    """No `cat_residents` → "named as one of ours" is unknown, and must not read as none."""
    rng = np.random.default_rng(105)
    ids, vecs, groups = _visit_data(rng, cats=4, visits_per_cat=4, crops_per_visit=5,
                                    cat_sep=0.3, visit_sep=0.2)
    s = _run(ids, vecs, groups, strangers=True)["visits"]["strangers"]
    assert s["impersonated"] > 0
    assert s["resident_split"] is False
    assert s["resident_impersonation_rate"] is None, "unmeasured, never 0.0"
    assert s["as_unknown"] == s["impersonated"]
    assert s["as_resident"] == 0 and s["as_neighbour"] == 0


def test_the_headline_is_micro_averaged_over_visits_not_over_cats():
    """A three-visit cat must not weigh the same as a two-hundred-visit one.

    Built so the two averages genuinely differ: one cat has many visits and a low
    impersonation rate, another has few and a high one.
    """
    rng = np.random.default_rng(106)
    ids, vecs, groups = [], [], []

    def add(cat, n_visits, centre_axis, spread):
        for _ in range(n_visits):
            c = np.zeros(8)
            c[centre_axis] = 1.0
            c = c + rng.normal(0, spread, size=8)
            g = []
            for _ in range(4):
                g.append(len(ids))
                ids.append(cat)
                vecs.append(c + rng.normal(0, 0.005, size=8))
            groups.append(g)

    add(1, 12, 0, 0.05)     # tight, many visits
    add(2, 3, 0, 0.05)      # tight, few visits — sits on cat 1, so it gets named
    add(3, 4, 3, 0.05)      # far away
    s = _run(ids, np.array(vecs), groups, strangers=True)["visits"]["strangers"]
    assert s["available"] is True
    micro = s["impersonated"] / s["n_scored"]
    macro = sum(c["impersonation_rate"] for c in s["per_cat"]) / len(s["per_cat"])
    assert s["impersonation_rate"] == pytest.approx(micro)
    assert s["impersonation_rate"] != pytest.approx(macro), \
        "this fixture exists to separate the two averages; if they coincide it proves nothing"
    # Totals are the per-cat rows summed, so the headline and the table cannot disagree.
    assert s["n_scored"] == sum(c["n_scored"] for c in s["per_cat"])
    assert s["impersonated"] == sum(c["impersonated"] for c in s["per_cat"])
    assert s["rejected"] == sum(c["rejected"] for c in s["per_cat"])


def test_both_residents_and_neighbours_are_held_out():
    """A held-out neighbour simulates a stranger; a held-out resident, an unenrolled cat."""
    rng = np.random.default_rng(107)
    ids, vecs, groups = _visit_data(rng, cats=4, visits_per_cat=3, crops_per_visit=4,
                                    cat_sep=2.0, visit_sep=0.2)
    residents = {1: True, 2: True, 3: False, 4: False}
    s = _run(ids, vecs, groups, residents=residents, strangers=True)["visits"]["strangers"]
    assert s["n_cats_held_out"] == 4
    assert {c["is_resident"] for c in s["per_cat"]} == {True, False}


# --- Stranger mode: the degenerate cases ------------------------------------------------

def test_two_cats_leave_a_one_cat_gallery_and_report_unavailable():
    """Holding one cat out of two leaves a gallery of one.

    Every impersonation then lands on the only remaining cat, so a 100% readout would be a
    property of the arithmetic — the exact "catastrophe where nothing was measured" the
    block's ``available: false`` convention exists for.
    """
    rng = np.random.default_rng(108)
    ids, vecs, groups = _visit_data(rng, cats=2, visits_per_cat=3, crops_per_visit=4,
                                    cat_sep=2.0)
    s = _run(ids, vecs, groups, strangers=True)["visits"]["strangers"]
    assert s["available"] is False
    assert s["reason"] == "single_cat_gallery"
    assert "impersonation_rate" not in s, "an unavailable block carries no measurements"
    assert "per_cat" not in s


def test_without_a_cat_index_the_block_declines_to_measure():
    """No `cats` → an impersonation cannot be attributed, so the split cannot exist."""
    rng = np.random.default_rng(109)
    ids, vecs, groups = _visit_data(rng, cats=3, visits_per_cat=3, crops_per_visit=4,
                                    cat_sep=2.0)
    dist, y = _matrix(vecs), _index(ids)
    s = _stranger_block(dist, y, groups, _group_true(y, groups), 0.5, [], _AGG, 3, None)
    assert s == {"available": False, "reason": "no_cat_index"}


def test_an_unavailable_visit_block_carries_no_stranger_block_either():
    """Nothing to hold out against when the visit scoring itself could not run."""
    rng = np.random.default_rng(110)
    ids, vecs, groups = _visit_data(rng, cats=3, visits_per_cat=2, crops_per_visit=4,
                                    cat_sep=2.0)
    v = _run(ids, vecs, [groups[0]], strangers=True)["visits"]
    assert v["available"] is False and v["reason"] == "too_few_visits"
    assert "strangers" not in v


def test_strangers_are_opt_in_and_absent_by_default():
    """The cross-regime callers pass no mode and are unchanged; so is a plain run."""
    rng = np.random.default_rng(111)
    ids, vecs, groups = _visit_data(rng, cats=3, visits_per_cat=3, crops_per_visit=4,
                                    cat_sep=2.0)
    v = _run(ids, vecs, groups)["visits"]
    assert "strangers" not in v
    for cell in (v["cross"] or {}).values():
        assert cell is None or "rejected" not in cell


# --- The shared threshold grid ----------------------------------------------------------

def test_the_stranger_curve_shares_the_known_cat_grid_point_for_point():
    """One grid, computed once on the UNMASKED pass and handed to every stranger pass.

    A masked pass's own distance range is different, so a grid derived per pass would
    still produce a plausible-looking curve — one whose rows cannot be read across.
    """
    rng = np.random.default_rng(112)
    ids, vecs, groups = _visit_data(rng, cats=4, visits_per_cat=4, crops_per_visit=4,
                                    cat_sep=0.5, visit_sep=0.2)
    v = _run(ids, vecs, groups, strangers=True, n_curve=13)["visits"]
    known = [p["threshold"] for p in v["curve"]]
    stranger = [p["threshold"] for p in v["strangers"]["curve"]]
    assert stranger == known
    assert len(known) >= 13
    # Each stranger point is over the SAME held-out visits at every threshold, which is
    # what makes a row a single operating point rather than two samples.
    assert {p["n_scored"] for p in v["strangers"]["curve"]} == {v["strangers"]["n_scored"]}
    for p in v["strangers"]["curve"]:
        assert p["rejected"] + p["impersonated"] == p["n_scored"]
        assert p["as_resident"] + p["as_neighbour"] + p["as_unknown"] == p["impersonated"]


def test_the_curve_moves_the_way_a_threshold_must():
    """Sanity on direction: a tighter cutoff rejects more strangers and declines more cats."""
    rng = np.random.default_rng(113)
    ids, vecs, groups = _visit_data(rng, cats=4, visits_per_cat=4, crops_per_visit=4,
                                    cat_sep=0.5, visit_sep=0.2)
    v = _run(ids, vecs, groups, strangers=True)["visits"]
    s_curve = sorted(v["strangers"]["curve"], key=lambda p: p["threshold"])
    assert s_curve[0]["impersonated"] <= s_curve[-1]["impersonated"]
    assert s_curve[0]["rejected"] >= s_curve[-1]["rejected"]


# --- Per-visit outcomes -----------------------------------------------------------------

def test_outcomes_carry_real_cat_ids_not_positional_indices():
    """Entry 357's trap: the index shifts, the id does not.

    The ids here are gappy and out of order, so a row that leaked the positional index
    would be a *valid-looking* small integer — which is exactly why this is asserted
    against the id set rather than against a shape.
    """
    rng = np.random.default_rng(114)
    ids, vecs, groups = _visit_data(rng, cats=3, visits_per_cat=3, crops_per_visit=4,
                                    cat_sep=3.0)
    remap = {1: 11, 2: 3, 3: 7}
    ids = [remap[i] for i in ids]
    meta = [{"first_src_recv_ts": 1000 * i} for i in range(len(groups))]
    v = _run(ids, vecs, groups, visit_meta=meta)["visits"]

    outcomes = v["outcomes"]
    assert len(outcomes) == len(groups)
    assert {o["cat_id"] for o in outcomes} == {3, 7, 11}
    assert {o["named"] for o in outcomes} <= {3, 7, 11, None}
    # The positional indices are 0/1/2 — none of which is a real id here, so a regression
    # to emitting them would be caught by the set above rather than by luck.
    assert 0 not in {o["cat_id"] for o in outcomes}


def test_outcomes_carry_the_callers_timestamp_and_regime():
    rng = np.random.default_rng(115)
    ids, vecs, groups = _visit_data(rng, cats=3, visits_per_cat=3, crops_per_visit=4,
                                    cat_sep=3.0)
    meta = [{"first_src_recv_ts": 5_000 + 7 * i} for i in range(len(groups))]
    nights = [i % 2 == 1 for i in range(len(groups))]
    v = _run(ids, vecs, groups, visit_meta=meta, visit_night=nights)["visits"]
    assert [o["first_src_recv_ts"] for o in v["outcomes"]] == \
        [m["first_src_recv_ts"] for m in meta]
    assert [o["night"] for o in v["outcomes"]] == nights
    assert {o["outcome"] for o in v["outcomes"]} <= {"correct", "wrong", "unknown",
                                                     "unscoreable"}


def test_an_unscoreable_visit_is_recorded_rather_than_dropped():
    """A missing row would read as "this arm never saw the visit" in a paired join."""
    rng = np.random.default_rng(116)
    ids, vecs, groups = _visit_data(rng, cats=3, visits_per_cat=3, crops_per_visit=4,
                                    cat_sep=3.0)
    lone = [len(ids) + i for i in range(4)]
    centre = np.zeros(8)
    centre[5] = 9.0
    vecs = np.vstack([vecs, np.array([centre + rng.normal(0, 0.005, size=8) for _ in range(4)])])
    ids = list(ids) + [4] * 4
    groups = groups + [lone]
    meta = [{"first_src_recv_ts": i} for i in range(len(groups))]
    v = _run(ids, vecs, groups, visit_meta=meta)["visits"]
    assert len(v["outcomes"]) == len(groups)
    lonely = [o for o in v["outcomes"] if o["cat_id"] == 4]
    assert lonely and [o["outcome"] for o in lonely] == ["unscoreable"]
    assert lonely[0]["named"] is None


def test_outcomes_are_absent_unless_asked_for():
    rng = np.random.default_rng(117)
    ids, vecs, groups = _visit_data(rng, cats=3, visits_per_cat=3, crops_per_visit=4,
                                    cat_sep=3.0)
    assert "outcomes" not in _run(ids, vecs, groups)["visits"]


def test_a_misaligned_meta_list_fails_loudly():
    """Silently stamping one visit's timestamp onto another is undetectable downstream."""
    rng = np.random.default_rng(118)
    ids, vecs, groups = _visit_data(rng, cats=3, visits_per_cat=3, crops_per_visit=4,
                                    cat_sep=3.0)
    with pytest.raises(ValueError, match="visit_meta"):
        _run(ids, vecs, groups, visit_meta=[{"first_src_recv_ts": 1}])


def test_outcomes_reconcile_with_the_headline_counts():
    rng = np.random.default_rng(119)
    ids, vecs, groups = _visit_data(rng, cats=4, visits_per_cat=4, crops_per_visit=4,
                                    cat_sep=0.6, visit_sep=0.25)
    meta = [{"first_src_recv_ts": i} for i in range(len(groups))]
    v = _run(ids, vecs, groups, visit_meta=meta)["visits"]
    tally = {k: 0 for k in ("correct", "wrong", "unknown", "unscoreable")}
    for o in v["outcomes"]:
        tally[o["outcome"]] += 1
    assert (tally["correct"], tally["wrong"], tally["unknown"]) == \
        (v["correct"], v["wrong"], v["unknown"])
    assert tally["correct"] + tally["wrong"] + tally["unknown"] == v["n_scored"]


def test_the_regime_cells_do_not_re_emit_outcomes():
    """They re-score the same visits under other galleries — one visit, one row."""
    rng = np.random.default_rng(120)
    ids, vecs, groups = _visit_data(rng, cats=3, visits_per_cat=4, crops_per_visit=4,
                                    cat_sep=3.0)
    meta = [{"first_src_recv_ts": i} for i in range(len(groups))]
    nights = [i % 2 == 1 for i in range(len(groups))]
    v = _run(ids, vecs, groups, visit_meta=meta, visit_night=nights)["visits"]
    assert len(v["outcomes"]) == len(groups)
    for regime in v["regimes"].values():
        assert regime is None or "outcomes" not in regime
    for cell in v["cross"].values():
        assert cell is None or "outcomes" not in cell


# --- Capped-gallery forecast ------------------------------------------------------------

def _mask_all(n):
    return np.ones(n, dtype=bool)


def test_the_uncapped_baseline_reproduces_the_runs_own_numbers():
    """The `None` row is the reference every other row is read as a difference from.

    An all-true mask is the same gallery and the same pair set as the main pass, so its
    recalibrated threshold must BE the run's threshold and both columns must coincide —
    which is what makes the row usable as a baseline rather than a fourth measurement.
    """
    rng = np.random.default_rng(121)
    ids, vecs, groups = _visit_data(rng, cats=4, visits_per_cat=4, crops_per_visit=5,
                                    cat_sep=0.8, visit_sep=0.25)
    v = _run(ids, vecs, groups,
             cap_masks=[{"max_per_cat": None, "mask": _mask_all(len(ids))}])["visits"]
    row = v["caps"][0]
    assert row["max_per_cat"] is None
    assert row["n_vectors"] == len(ids) and row["n_cats"] == 4
    assert row["threshold"] == pytest.approx(v["threshold"])
    assert row["recalibrated"] == row["fixed"]
    assert row["recalibrated"]["accuracy"] == pytest.approx(v["accuracy"])
    assert row["recalibrated"]["n_scored"] == v["n_scored"]


def test_a_cap_reports_both_the_recalibrated_and_the_fixed_column():
    """Two columns, because `cap_per_cat` claims to fix two biases.

    A forecast that only recalibrated (or only held the threshold) would answer half the
    question while reading as the whole of it.
    """
    rng = np.random.default_rng(122)
    ids, vecs, groups = _visit_data(rng, cats=4, visits_per_cat=4, crops_per_visit=6,
                                    cat_sep=0.8, visit_sep=0.25)
    n = len(ids)
    capped = np.array([i % 3 != 0 for i in range(n)])
    v = _run(ids, vecs, groups, cap_masks=[
        {"max_per_cat": None, "mask": _mask_all(n)},
        {"max_per_cat": 4, "mask": capped},
    ])["visits"]
    base, cap = v["caps"]
    assert cap["n_vectors"] == int(capped.sum()) < base["n_vectors"]
    assert cap["recalibrated"] is not None and cap["fixed"] is not None
    assert cap["reason"] is None
    # RECALIBRATED, not inherited: the threshold is fitted to the surviving columns' own
    # pair distribution, which is the half of the bias a cap is meant to fix. A forecast
    # that reused the uncapped value would be indistinguishable from this except here.
    assert cap["threshold"] != pytest.approx(base["threshold"])
    assert cap["threshold"] != pytest.approx(v["threshold"])
    # The fixed column is scored at the UNCAPPED threshold, so it is what changes when only
    # the density moves; the recalibrated one also absorbs the calibration shift.
    assert set(cap["fixed"]) == set(cap["recalibrated"]) == {
        "n_scored", "n_unscoreable", "correct", "wrong", "unknown", "accuracy",
        "unknown_rate"}
    for col in (cap["fixed"], cap["recalibrated"]):
        assert col["correct"] + col["wrong"] + col["unknown"] == col["n_scored"]
        # The denominator every rate on the row is read off, carried so a cap whose recall
        # rose only because visits left the denominator is distinguishable from one that
        # improved. Both columns score the same visits, so they must agree on it.
        assert col["n_unscoreable"] == cap["fixed"]["n_unscoreable"]


def test_a_cap_that_cannot_be_calibrated_says_so_and_still_reports_the_fixed_column():
    """One surviving crop per cat leaves no cross-visit same-cat pair to calibrate from."""
    rng = np.random.default_rng(123)
    ids, vecs, groups = _visit_data(rng, cats=3, visits_per_cat=3, crops_per_visit=4,
                                    cat_sep=2.0, visit_sep=0.2)
    n = len(ids)
    one_per_cat = np.zeros(n, dtype=bool)
    for cat in (1, 2, 3):
        one_per_cat[[i for i, c in enumerate(ids) if c == cat][0]] = True
    v = _run(ids, vecs, groups,
             cap_masks=[{"max_per_cat": 1, "mask": one_per_cat}])["visits"]
    row = v["caps"][0]
    assert row["threshold"] is None
    assert row["reason"] == "uncalibrated_threshold"
    assert row["recalibrated"] is None, "no threshold, no recalibrated reading"
    assert row["fixed"]["n_scored"] > 0, "the fixed-threshold column is still a reading"


def test_caps_are_absent_unless_asked_for_and_skipped_when_nothing_was_measured():
    rng = np.random.default_rng(124)
    ids, vecs, groups = _visit_data(rng, cats=3, visits_per_cat=3, crops_per_visit=4,
                                    cat_sep=2.0)
    assert "caps" not in _run(ids, vecs, groups)["visits"]
    v = _run(ids, vecs, [groups[0]],
             cap_masks=[{"max_per_cat": None, "mask": _mask_all(len(ids))}])["visits"]
    assert v["available"] is False and "caps" not in v


# --- Backwards compatibility ------------------------------------------------------------

def test_the_result_is_byte_identical_when_nothing_new_is_requested():
    """Every addition is additive: the existing report, `_run_metrics` and the runs table
    all read this shape, and the old keys must not move."""
    rng = np.random.default_rng(125)
    ids, vecs, groups = _visit_data(rng, cats=3, visits_per_cat=3, crops_per_visit=4,
                                    cat_sep=1.5, visit_sep=0.3)
    plain = _run(ids, vecs, groups)
    rich = _run(ids, vecs, groups, residents={1: True, 2: False, 3: True},
                visit_meta=[{"first_src_recv_ts": i} for i in range(len(groups))],
                strangers=True,
                cap_masks=[{"max_per_cat": None, "mask": _mask_all(len(ids))}])
    assert set(rich["visits"]) - set(plain["visits"]) == {"strangers", "outcomes", "caps"}
    for key in plain["visits"]:
        assert rich["visits"][key] == plain["visits"][key], key
    # `cats` gains is_resident ONLY when the flags were supplied.
    assert all("is_resident" not in c for c in plain["cats"])
    assert [c["is_resident"] for c in rich["cats"]] == [True, False, True]
    for key in plain:
        if key not in ("visits", "cats"):
            assert rich[key] == plain[key], key


def test_the_known_cat_curve_keeps_its_own_shape_under_stranger_mode():
    """The impersonation buckets share one accumulator; they must not leak into the
    known-cat curve points, which are persisted on every run ever recorded."""
    rng = np.random.default_rng(126)
    ids, vecs, groups = _visit_data(rng, cats=4, visits_per_cat=3, crops_per_visit=4,
                                    cat_sep=0.5)
    v = _run(ids, vecs, groups, strangers=True)["visits"]
    for point in v["curve"]:
        assert set(point) == {"threshold", "correct", "wrong", "unknown", "accuracy"}


# --- helpers ---------------------------------------------------------------------------
# Small re-derivations of what `run_feasibility` computes internally, so the two
# `_score_visits` / `_stranger_block` unit tests above can call them directly.

def _matrix(vecs):
    e = np.asarray(vecs, dtype=np.float64)
    e = e / np.clip(np.linalg.norm(e, axis=1, keepdims=True), 1e-12, None)
    return 1.0 - (e @ e.T)


def _index(ids):
    uniq = sorted(set(int(c) for c in ids))
    idx = {c: i for i, c in enumerate(uniq)}
    return np.array([idx[int(c)] for c in ids])


def _group_true(y, groups):
    return [int(y[G[0]]) for G in groups]


def _cats(n):
    return [{"cat_id": i + 1, "cat_name": f"cat{i + 1}", "is_resident": None} for i in range(n)]
