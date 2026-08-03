"""Separability metrics for the Phase-1 feasibility question — "can we tell our
cats apart?" — computed over embeddings of labelled crops.

Pure numpy, no torch/cv2: it takes an ALREADY-computed embedding matrix plus the
per-crop cat labels and returns a JSON-serialisable results dict. The heavy part —
turning crops into embeddings — is the ``Embedder``'s job (``embed.py``, torch-
gated). Keeping the metrics dependency-light is exactly what lets the test suite
exercise them with synthetic vectors and no model download.

The three things it measures, matching the three views the report draws:

- **kNN leave-one-out** — for each crop, does its nearest OTHER crop share its
  identity? Accuracy + a per-cat confusion matrix. This is the headline "can we
  identify a cat from its gallery neighbours" number.
- **Same-cat vs different-cat distance separation** — an AUC (P(a same-cat pair is
  closer than a different-cat pair)) and the distance threshold that best splits
  them, i.e. the calibrated confidence signal the concept relies on (name it when
  close, "unknown" when far). 0.5 AUC = no separation, 1.0 = perfect.
- **PCA 2D projection** — per-crop x/y for the scatter, so clustering is visible.

- **Visit-held-out scoring** (opt-in, ``visit_groups``) — the honest counterpart to the
  kNN number above. Crops arrive in bursts: one door crossing yields tens of frames a
  tenth of a second apart, so a crop's nearest OTHER crop is nearly always the adjacent
  frame of its own visit — a near-duplicate. The kNN leave-one-out masks only the
  diagonal, so it measures near-duplicate detection, not recognition, and reads ~100%.
  This instead hides a whole VISIT and matches it against the others, scored at the
  visit level by Run's own threshold-and-vote rule. Day and night are scored separately,
  plus a cross-regime matrix (can a day-only gallery name a night visit?) that answers
  whether one gallery can span both regimes.

Distances are COSINE distance (1 − cosine similarity) over L2-normalised
embeddings — the standard metric for re-ID embedding spaces.
"""
from __future__ import annotations

import numpy as np


def _l2_normalize(e: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(e, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return e / norms


def _stats(a: np.ndarray) -> dict:
    a = np.asarray(a, dtype=float)
    if a.size == 0:
        return {"n": 0, "mean": None, "std": None}
    return {"n": int(a.size), "mean": float(a.mean()), "std": float(a.std())}


def _pairwise_auc(same: np.ndarray, diff: np.ndarray) -> "float | None":
    """P(a same-cat pair distance < a different-cat pair distance), ties at 0.5.

    The Mann–Whitney interpretation of AUC, computed by rank rather than the O(n·m)
    all-pairs loop: for each same-cat distance, count how many different-cat
    distances are strictly greater (+ half the ties)."""
    if same.size == 0 or diff.size == 0:
        return None
    diff_sorted = np.sort(diff)
    right = np.searchsorted(diff_sorted, same, side="right")
    left = np.searchsorted(diff_sorted, same, side="left")
    greater = diff.size - right
    ties = right - left
    return float((greater + 0.5 * ties).sum() / (same.size * diff.size))


def _best_threshold(same: np.ndarray, diff: np.ndarray) -> "tuple[float | None, float | None]":
    """Distance threshold maximising balanced accuracy (call a pair 'same' if d ≤ t).

    Vectorised sweep over every candidate distance: TPR = same ≤ t, TNR = diff > t,
    balanced accuracy = ½(TPR + TNR). Returns (threshold, balanced_accuracy)."""
    if same.size == 0 or diff.size == 0:
        return None, None
    same_sorted = np.sort(same)
    diff_sorted = np.sort(diff)
    cand = np.unique(np.concatenate([same, diff]))
    tp = np.searchsorted(same_sorted, cand, side="right")  # same distances ≤ t
    tn = diff.size - np.searchsorted(diff_sorted, cand, side="right")  # diff distances > t
    bal = 0.5 * (tp / same.size + tn / diff.size)
    i = int(bal.argmax())
    return float(cand[i]), float(bal[i])


def _visit_outcome(
    span: "list[tuple[int, float]]",
    threshold: "float | None",
    true_cat: int,
    aggregate,
) -> "tuple[str, int | None]":
    """One held-out visit's outcome under ``threshold``: ``('correct'|'wrong'|'unknown', named)``.

    ``aggregate`` is Run's own verdict rule — ``Store._aggregate_identity`` — injected
    rather than imported so this module stays a pure-numpy metrics layer with no
    dependency on the persistence tier (the same seam ``is_night`` uses elsewhere).
    Injecting it (rather than re-implementing the vote here) is what stops the probe
    drifting from what Run actually decides.

    ``named`` is the cat the vote landed on, or ``None`` for *unknown* — the caller
    needs it for the visit-level confusion matrix.
    """
    agg = aggregate(span, threshold, {}, {})
    if agg is None or agg.get("cat_id") is None:
        return "unknown", None
    named = int(agg["cat_id"])
    return ("correct" if named == true_cat else "wrong"), named


def _score_visits(
    dist: np.ndarray,
    y: np.ndarray,
    groups: "list[list[int]]",
    group_true: "list[int]",
    gal_mask: "np.ndarray | None",
    headline_t: "float | None",
    curve_ts: "list[float]",
    aggregate,
    n_cats: int,
) -> dict:
    """Score every held-out group against the rest of the matrix.

    For each group the group's OWN columns are masked to ``+inf`` — that exclusion is
    the whole point, and it is what the crop-level leave-one-out (diagonal only) fails
    to do. ``gal_mask`` optionally restricts the gallery side to a subset of columns
    (used for the day-only / night-only cross-regime cells); ``None`` = the full mixed
    gallery.

    A group whose true cat has NO crop left on the gallery side is *unscoreable*: the
    correct answer is structurally absent, so it could only ever be wrong. Those are
    excluded from the denominator and returned as their own tally rather than counted
    as failures (which would understate the model) or dropped silently.

    The nearest-neighbour span is computed ONCE per group and then re-voted at every
    threshold in ``curve_ts``, since the threshold enters only through
    ``_aggregate_identity``'s below-threshold filter — so the sweep costs votes, not
    distance maths.
    """
    counts = {"correct": 0, "wrong": 0, "unknown": 0}
    unscoreable: "dict[int, int]" = {}
    # rows = true cat index, cols = predicted cat index, final column = unknown.
    conf = np.zeros((n_cats, n_cats + 1), dtype=int)
    curve = {float(t): {"correct": 0, "wrong": 0, "unknown": 0} for t in curve_ts}
    n_scored = 0

    for G, true_cat in zip(groups, group_true):
        # Is the true cat represented on the gallery side, outside this group?
        others = y == true_cat
        others[G] = False
        if gal_mask is not None:
            others &= gal_mask
        if not others.any():
            unscoreable[true_cat] = unscoreable.get(true_cat, 0) + 1
            continue

        d = dist[G, :]  # advanced indexing already copies
        if gal_mask is not None:
            d[:, ~gal_mask] = np.inf
        d[:, G] = np.inf
        nn = d.argmin(axis=1)
        span = [(int(y[j]), float(d[i, j])) for i, j in enumerate(nn)]

        n_scored += 1
        outcome, named = _visit_outcome(span, headline_t, true_cat, aggregate)
        counts[outcome] += 1
        conf[true_cat, n_cats if named is None else named] += 1
        for t in curve_ts:
            curve[float(t)][_visit_outcome(span, float(t), true_cat, aggregate)[0]] += 1

    decided = counts["correct"] + counts["wrong"]
    return {
        "n_scored": n_scored,
        "correct": counts["correct"],
        "wrong": counts["wrong"],
        "unknown": counts["unknown"],
        # correct / (correct + wrong): 'declined to name' is reported beside this, never
        # folded into it — for a resident at the door the two mean opposite things.
        "accuracy": (counts["correct"] / decided) if decided else None,
        "unknown_rate": (counts["unknown"] / n_scored) if n_scored else None,
        "unscoreable": [
            {"cat_index": int(c), "n_visits": int(n)} for c, n in sorted(unscoreable.items())
        ],
        "confusion": conf.tolist(),
        "curve": [
            {
                "threshold": float(t),
                **curve[float(t)],
                "accuracy": (
                    curve[float(t)]["correct"]
                    / (curve[float(t)]["correct"] + curve[float(t)]["wrong"])
                    if (curve[float(t)]["correct"] + curve[float(t)]["wrong"])
                    else None
                ),
            }
            for t in curve_ts
        ],
    }


def _visits_block(
    dist: np.ndarray,
    y: np.ndarray,
    n_cats: int,
    groups: "list[list[int]]",
    visit_night: "list[bool | None] | None",
    aggregate,
    threshold: "float | None",
    n_curve: int,
    extra_thresholds: "dict[str, float] | None",
    labeled_ts_groups: "int | None",
    gap_ms: "int | None",
) -> dict:
    """Assemble the ``visits`` block: headline, sweep curve, day/night, cross-regime.

    Degenerate inputs report ``available: False`` with a ``reason`` rather than
    producing numbers. Two cases matter, and both would otherwise render as a
    catastrophic-looking result where the honest reading is "nothing was measured":

    - ``threshold is None`` — no CROSS-VISIT same-cat pair exists (every cat has a
      single visit), or no different-cat pair does, so the visit task cannot be
      calibrated. ``_aggregate_identity`` treats a ``None`` threshold as the
      uncalibrated fail-safe and resolves EVERY visit to *unknown*, i.e. 0 correct /
      0 wrong / 100% unknown.
    - fewer than 2 groups — nothing can be held out against anything.

    ``threshold`` is the cross-visit-calibrated value (see ``run_feasibility``), NOT the
    crop-level one — that one is calibrated on same-visit near-duplicates and is far too
    tight here. It is passed in ``extra_thresholds`` as a curve reference instead.
    """
    if aggregate is None:
        raise ValueError("visit_groups requires an aggregate (Store._aggregate_identity)")
    for gi, G in enumerate(groups):
        if not G:
            raise ValueError(f"visit group {gi} is empty")
        if len({int(y[i]) for i in G}) != 1:
            # The caller partitions per cat (probe.group_visits); a mixed group has no
            # well-defined true cat to score against, so fail loudly rather than guess.
            raise ValueError(f"visit group {gi} mixes cats — group per cat before scoring")
    group_true = [int(y[G[0]]) for G in groups]

    base = {
        "n_groups": len(groups),
        "gap_ms": gap_ms,
        "labeled_ts_groups": labeled_ts_groups,
        "threshold": threshold,
    }
    if threshold is None:
        return {**base, "available": False, "reason": "uncalibrated_threshold"}
    if len(groups) < 2:
        return {**base, "available": False, "reason": "too_few_visits"}

    # Sweep points: linear over the observed pair-distance range, plus the headline and
    # any named reference (the active model's promoted threshold), so the reader can see
    # how sensitive the verdict is instead of trusting one circularly-derived number.
    # Reduced in place rather than via `dist[np.isfinite(dist)]`, which boolean-indexes a
    # COPY of the whole N x N matrix — ~1.16 GB at 12k crops, the single largest allocation
    # the visit path was adding. `dist` is 1 - (unit @ unit.T), so every entry is finite and
    # the min/max are identical; the mask only ever removed nothing.
    lo, hi = (float(dist.min()), float(dist.max())) if dist.size else (0.0, 1.0)
    marks = {float(threshold)} | {
        float(v) for v in (extra_thresholds or {}).values() if v is not None
    }
    curve_ts = sorted(set(np.linspace(lo, hi, max(2, int(n_curve))).tolist()) | marks)

    scored = _score_visits(
        dist, y, groups, group_true, None, threshold, curve_ts, aggregate, n_cats
    )
    out = {**base, "available": True, "reason": None, **scored,
           "marks": dict(extra_thresholds or {})}

    # --- Day/night and the cross-regime matrix -----------------------------------------
    # Regime is a per-GROUP property (each visit is bucketed whole by its first crop), so
    # the gallery side of a cross cell is the union of that regime's groups. Restricting
    # the gallery by regime is what turns "does night work" into "can a DAY-only gallery
    # recognise a night visit" — the one-gallery-or-two question.
    if visit_night is None or all(v is None for v in visit_night):
        out["regimes"] = None
        out["cross"] = None
        return out

    col_regime = np.full(dist.shape[0], -1, dtype=int)
    for G, night in zip(groups, visit_night):
        if night is not None:
            col_regime[G] = 1 if night else 0
    day_mask = col_regime == 0
    night_mask = col_regime == 1

    regimes: "dict[str, dict | None]" = {}
    cross: "dict[str, dict | None]" = {}
    for name, want_night in (("day", False), ("night", True)):
        sel = [
            (G, t) for G, t, nb in zip(groups, group_true, visit_night)
            if nb is not None and bool(nb) == want_night
        ]
        if not sel:
            regimes[name] = None
            cross[f"{name}_vs_day"] = None
            cross[f"{name}_vs_night"] = None
            continue
        g_sel = [G for G, _t in sel]
        t_sel = [t for _G, t in sel]
        # vs the MIXED gallery — the plain day/night split. Identical to the "vs mixed"
        # column of the cross table, so the two can never disagree.
        regimes[name] = _score_visits(
            dist, y, g_sel, t_sel, None, threshold, [], aggregate, n_cats
        )
        for gal_name, gal in (("day", day_mask), ("night", night_mask)):
            cross[f"{name}_vs_{gal_name}"] = (
                _score_visits(dist, y, g_sel, t_sel, gal, threshold, [], aggregate, n_cats)
                if gal.any()
                else None
            )
    out["regimes"] = regimes
    out["cross"] = cross
    return out


def _pca_2d(e: np.ndarray) -> np.ndarray:
    """Project (N,D) to (N,2) via PCA (SVD of the centred matrix). Zeros if N < 2."""
    if e.shape[0] < 2:
        return np.zeros((e.shape[0], 2))
    x = e - e.mean(axis=0, keepdims=True)
    try:
        _u, _s, vt = np.linalg.svd(x, full_matrices=False)
    except np.linalg.LinAlgError:
        return np.zeros((e.shape[0], 2))
    return x @ vt[:2].T


def run_feasibility(
    cat_ids: "list[int]",
    cat_names: "dict[int, str]",
    embeddings: np.ndarray,
    *,
    k: int = 1,
    n_bins: int = 30,
    visit_groups: "list[list[int]] | None" = None,
    visit_night: "list[bool | None] | None" = None,
    aggregate=None,
    n_curve: int = 41,
    extra_thresholds: "dict[str, float] | None" = None,
    labeled_ts_groups: "int | None" = None,
    gap_ms: "int | None" = None,
) -> dict:
    """Separability scorecard over ``embeddings`` (N,D) labelled by ``cat_ids`` (len N).

    ``cat_names`` maps a ``cat_id`` → display name (missing names fall back to
    ``"cat #<id>"``). ``k`` is the kNN vote size (clamped to ``[1, N-1]``).
    Returns the JSON-serialisable dict documented in the module header. Raises
    ``ValueError`` if there are fewer than 2 crops or fewer than 2 distinct cats —
    separability is undefined with one class or one point.

    **Visit-held-out scoring (additive).** Pass ``visit_groups`` — a partition of the
    row indices into visits, one list per visit, each holding ONE cat's crops (see
    ``probe.group_visits``) — and ``aggregate`` (``Store._aggregate_identity``) to add a
    ``"visits"`` key holding the honest number: each visit is hidden whole and matched
    against the others, scored at the visit level by Run's own vote rule. Without
    ``visit_groups`` the returned dict is byte-identical to before, so every existing
    caller and report is untouched.

    ``visit_night`` (one flag per group, ``None`` where the regime is unknown) adds the
    day/night split and the cross-regime matrix. ``extra_thresholds`` marks named
    reference points on the sweep curve (e.g. ``{"active_model": 0.44}``).
    ``labeled_ts_groups`` and ``gap_ms`` are recorded verbatim for the report's grouping
    cross-check — this function does no clustering of its own.
    """
    e = _l2_normalize(np.asarray(embeddings, dtype=np.float64))
    n = e.shape[0]
    ids = np.asarray([int(c) for c in cat_ids])
    if n < 2 or ids.shape[0] != n:
        raise ValueError(f"need >= 2 crops with matching labels, got {n} crops / {ids.shape[0]} labels")
    uniq = sorted(set(int(x) for x in ids))
    if len(uniq) < 2:
        raise ValueError(f"need >= 2 distinct cats to measure separability, got {len(uniq)}")
    idx_of = {c: i for i, c in enumerate(uniq)}
    y = np.array([idx_of[int(c)] for c in ids])
    n_cats = len(uniq)

    sim = e @ e.T
    dist = 1.0 - sim

    # kNN leave-one-out: exclude self by masking the diagonal to +inf.
    d_knn = dist.copy()
    np.fill_diagonal(d_knn, np.inf)
    kk = max(1, min(int(k), n - 1))
    nn = np.argsort(d_knn, axis=1)[:, :kk]
    pred = np.array([np.bincount(y[nn[i]], minlength=n_cats).argmax() for i in range(n)])
    accuracy = float((pred == y).mean())

    conf = np.zeros((n_cats, n_cats), dtype=int)
    for t, p in zip(y, pred):
        conf[t, p] += 1
    per_cat_recall = [
        float(conf[i, i] / conf[i].sum()) if conf[i].sum() else 0.0 for i in range(n_cats)
    ]

    # Same-cat vs different-cat pair distances over the upper triangle (real diagonal 0).
    iu, ju = np.triu_indices(n, k=1)
    pair_d = dist[iu, ju]
    same_pair = y[iu] == y[ju]
    same = pair_d[same_pair]
    diff = pair_d[~same_pair]
    auc = _pairwise_auc(same, diff)
    threshold, bal_acc = _best_threshold(same, diff)

    # A threshold for the VISIT task must be calibrated on the visit task's geometry.
    # `same` above is dominated by SAME-VISIT pairs — near-duplicate frames a tenth of a
    # second apart, sitting at near-zero distance — so the threshold it yields is far too
    # tight for matching a visit against OTHER visits: at that value the held-out scoring
    # declines almost everything and reads as a catastrophe rather than a measurement.
    # So recompute it over cross-visit same-cat pairs only. Deliberately reuses the
    # `pair_d`/`same_pair` arrays already materialised above rather than re-deriving the
    # upper triangle, which at 12k crops is a ~580 MB allocation.
    threshold_cross = auc_cross = bal_cross = None
    if visit_groups is not None:
        gid = np.full(n, -1, dtype=np.int64)
        for g_index, G in enumerate(visit_groups):
            gid[G] = g_index
        cross_pair = gid[iu] != gid[ju]
        same_cross = pair_d[same_pair & cross_pair]
        auc_cross = _pairwise_auc(same_cross, diff)
        threshold_cross, bal_cross = _best_threshold(same_cross, diff)

    lo = float(max(0.0, pair_d.min())) if pair_d.size else 0.0
    hi = float(pair_d.max()) if pair_d.size else 1.0
    if hi <= lo:
        hi = lo + 1.0
    edges = np.linspace(lo, hi, n_bins + 1)
    same_h = np.histogram(same, bins=edges)[0].tolist() if same.size else [0] * n_bins
    diff_h = np.histogram(diff, bins=edges)[0].tolist() if diff.size else [0] * n_bins

    proj = _pca_2d(e)
    cats = [
        {"cat_id": c, "cat_name": cat_names.get(c) or f"cat #{c}", "n": int((ids == c).sum())}
        for c in uniq
    ]
    result = {
        "n_crops": int(n),
        "n_cats": int(n_cats),
        "cats": cats,
        "knn": {
            "k": kk,
            "accuracy": accuracy,
            "confusion": conf.tolist(),
            "per_cat_recall": per_cat_recall,
        },
        "distances": {
            "same": _stats(same),
            "diff": _stats(diff),
            "hist": {"edges": edges.tolist(), "same": same_h, "diff": diff_h},
            "auc": auc,
            "suggested_threshold": threshold,
            "threshold_balanced_acc": bal_acc,
        },
        "projection": [
            {"x": float(proj[i, 0]), "y": float(proj[i, 1]), "cat_index": int(y[i])}
            for i in range(n)
        ],
    }
    # Additive: absent visit_groups the dict above is byte-identical to the pre-feature
    # shape, so existing reports/consumers are untouched.
    if visit_groups is not None:
        marks = dict(extra_thresholds or {})
        # The crop-level threshold is shown on the curve as a reference point, so the
        # gap between the two calibrations is visible rather than merely asserted.
        if threshold is not None:
            marks.setdefault("crop_level", threshold)
        result["visits"] = _visits_block(
            dist, y, n_cats, visit_groups, visit_night, aggregate,
            threshold_cross, n_curve, marks, labeled_ts_groups, gap_ms,
        )
        # Only on an AVAILABLE block. `_visits_block` deliberately returns none of its
        # computed fields when it reports "nothing was measured", and attaching a real
        # cross-visit AUC beside `available: False` would contradict exactly that — the
        # distinction this whole scoring exists to keep honest.
        if result["visits"].get("available"):
            result["visits"]["auc"] = auc_cross
            result["visits"]["threshold_balanced_acc"] = bal_cross
    return result
