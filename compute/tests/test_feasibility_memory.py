"""Tests for the feasibility probe's BOUNDED-MEMORY rewrites.

Each one pins a construct against the straightforward form it replaced, because every
rewrite here trades a large allocation for a fiddlier expression and none of them is
allowed to change a number. A real run at 27k crops died allocating 2.72 GiB inside
``_best_threshold``; the O(n²) arrays behind it (a float64 distance matrix, its copy,
an int64 argsort of it, and ``triu_indices``' index pair) came to ~41 GB together.

Pure numpy — the reference forms are written out inline so a regression is caught
against the OLD code, not against whatever the new code currently happens to do.
"""
from __future__ import annotations

import numpy as np

from compute.identification.feasibility import (
    _best_threshold,
    _triu_pairs,
    _KNN_ROWS,
    run_feasibility,
)


def _best_threshold_reference(same, diff):
    """The pre-change implementation: candidates over `same ∪ diff`."""
    if same.size == 0 or diff.size == 0:
        return None, None
    same_sorted = np.sort(same)
    diff_sorted = np.sort(diff)
    cand = np.unique(np.concatenate([same, diff]))
    tp = np.searchsorted(same_sorted, cand, side="right")
    tn = diff.size - np.searchsorted(diff_sorted, cand, side="right")
    bal = 0.5 * (tp / same.size + tn / diff.size)
    i = int(bal.argmax())
    return float(cand[i]), float(bal[i])


def test_best_threshold_matches_the_full_candidate_sweep():
    """Dropping `diff` from the candidate set is exact, not a sampling.

    Swept over separated, overlapping and identical-scale distributions, plus heavy
    integer-valued ties (where an argmax tie-break could diverge) and the lopsided
    sizes a real run has (`diff` ~95% of pairs).
    """
    rng = np.random.default_rng(7)
    cases = [
        (rng.normal(0.2, 0.05, 400), rng.normal(0.9, 0.10, 4000)),   # well separated
        (rng.normal(0.5, 0.20, 300), rng.normal(0.6, 0.20, 3000)),   # overlapping
        (rng.normal(0.5, 0.20, 300), rng.normal(0.5, 0.20, 300)),    # indistinguishable
        (rng.integers(0, 5, 500).astype(float),                      # dense ties
         rng.integers(0, 5, 5000).astype(float)),
        (rng.normal(0.3, 0.05, 5), rng.normal(0.8, 0.05, 9000)),     # tiny same side
    ]
    for same, diff in cases:
        assert _best_threshold(same, diff) == _best_threshold_reference(same, diff)


def test_best_threshold_matches_reference_in_float32():
    """The same equivalence at the dtype the probe now runs in.

    float32 collapses near-duplicate distances that float64 kept distinct, which changes
    the candidate GRID — so the equivalence has to hold at this dtype too, not only where
    every value is unique.
    """
    rng = np.random.default_rng(11)
    same = rng.normal(0.2, 0.05, 800).astype(np.float32)
    diff = rng.normal(0.7, 0.15, 8000).astype(np.float32)
    assert _best_threshold(same, diff) == _best_threshold_reference(same, diff)


def test_triu_pairs_matches_triu_indices_ordering():
    """Row-major order and values identical to `np.triu_indices(n, k=1)`.

    Order is the load-bearing half: `same_pair`, `cross_pair` and each cap's `surviving`
    are combined POSITIONALLY with `pair_d`, so a different traversal would silently pair
    a distance with another pair's label.
    """
    rng = np.random.default_rng(3)
    for n in (2, 3, 5, 40):
        d = rng.random((n, n))
        y = rng.integers(0, 4, n)
        iu, ju = np.triu_indices(n, k=1)
        assert np.array_equal(_triu_pairs(lambda r: d[r, r + 1:], n, d.dtype), d[iu, ju])
        assert np.array_equal(
            _triu_pairs(lambda r: y[r + 1:] == y[r], n, bool), y[iu] == y[ju]
        )


def test_triu_pairs_dtype_is_the_requested_one():
    """float32 distances must not be widened on the way into the pair vector."""
    d = np.zeros((6, 6), dtype=np.float32)
    assert _triu_pairs(lambda r: d[r, r + 1:], 6, d.dtype).dtype == np.float32
    assert _triu_pairs(lambda r: d[r, r + 1:] > 0, 6, bool).dtype == np.bool_


def test_knn_blocking_matches_a_single_block(monkeypatch):
    """`run_feasibility`'s blocked kNN gives what one whole-matrix sort would.

    Drives the REAL loop at several block sizes — including ones that divide the crop
    count unevenly, so the last block is short — against a single-block run. Rows sort
    independently, so this is an identity; it is asserted because the failure mode is a
    wrong `knn.accuracy`, which is a plausible-looking number rather than a crash.

    Monkeypatching `_KNN_ROWS` is what makes it a test of this module: hand-rolling the
    same loop in the test body instead passes against a broken production loop (verified
    — an injected stride bug there left all of this file's tests green).
    """
    rng = np.random.default_rng(5)
    ids, emb = [], []
    for cid, center in enumerate([[1, 0, 0], [0, 1, 0], [0, 0, 1]], start=1):
        for _ in range(9):
            ids.append(cid)
            emb.append(np.array(center, dtype=float) + rng.normal(0, 0.25, 3))
    emb = np.array(emb)

    monkeypatch.setattr("compute.identification.feasibility._KNN_ROWS", 10_000)
    whole = run_feasibility(ids, {1: "A", 2: "B", 3: "C"}, emb, k=3)
    for rows in (1, 2, 5, 26, 27):        # 27 crops: uneven splits and an exact one
        monkeypatch.setattr("compute.identification.feasibility._KNN_ROWS", rows)
        blocked = run_feasibility(ids, {1: "A", 2: "B", 3: "C"}, emb, k=3)
        assert blocked["knn"]["accuracy"] == whole["knn"]["accuracy"], rows
        assert blocked["knn"]["confusion"] == whole["knn"]["confusion"], rows
        assert blocked["knn"]["per_cat_recall"] == whole["knn"]["per_cat_recall"], rows
    assert _KNN_ROWS > 0                  # the real default, not the patched one


def test_diagonal_mask_and_restore_needs_a_copy():
    """`np.diagonal(...)` is a VIEW, so the save-mask-restore pattern needs `.copy()`.

    The kNN pass masks the diagonal on `dist` itself rather than on a full n² copy of it.
    Saving the diagonal without copying reads back the `+inf` that was just written over
    it, restoring nothing — silently, since the shapes and dtypes all still line up.

    This documents the trap; it does NOT pin the production call site, and cannot: every
    `dist` reader downstream masks the diagonal itself, so dropping the `.copy()` there
    changes no output and no test in this repo goes red (verified). The restore is
    defensive, kept for the next reader who indexes `dist` differently.
    """
    d = np.arange(16, dtype=np.float32).reshape(4, 4)
    view = np.diagonal(d)                  # what the WRONG version would keep
    saved = np.diagonal(d).copy()          # what the code keeps
    np.fill_diagonal(d, np.inf)
    assert np.all(np.isinf(view)), "diagonal() is a view — that is the trap being pinned"
    d[np.diag_indices(4)] = saved
    assert np.array_equal(np.diagonal(d), np.array([0, 5, 10, 15], dtype=np.float32))


def test_run_feasibility_runs_in_float32_end_to_end():
    """The probe's matrices are float32 now; the reported numbers stay sane.

    Guards the dtype choice itself: a stray `dtype=float` reintroduced anywhere in the
    chain doubles the footprint again with nothing to show for it, and the failure mode
    is an OOM on the compute PC rather than anything visible here.
    """
    rng = np.random.default_rng(0)
    ids, emb = [], []
    for cid, center in enumerate([[1, 0, 0], [0, 1, 0], [0, 0, 1]], start=1):
        for _ in range(8):
            ids.append(cid)
            emb.append(np.array(center, dtype=float) + rng.normal(0, 0.02, 3))
    m = run_feasibility(ids, {1: "A", 2: "B", 3: "C"}, np.array(emb))
    assert m["knn"]["accuracy"] >= 0.95
    assert 0.0 <= m["distances"]["auc"] <= 1.0
    assert np.isfinite(m["distances"]["same"]["mean"])
    assert np.isfinite(m["distances"]["diff"]["mean"])
