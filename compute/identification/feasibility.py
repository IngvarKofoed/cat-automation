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

- **Stranger rejection** (opt-in, ``strangers``) — the half of the door's job every
  measurement above is structurally blind to: each crop scored there belongs to a cat we
  have labelled, so nothing says how often a cat we have NEVER labelled is handed a known
  cat's name. Holding a whole CAT out of the gallery turns each of its visits into a
  trial whose only correct answer is *declined*. Split by whether the impersonated cat is
  a resident or a neighbour, because "stranger let in as one of ours" is the outcome the
  door cares about and "stranger named as another neighbour" is not.

- **Capped-gallery forecast** (opt-in, ``cap_masks``) — what a per-cat cap would do,
  without building the gallery to find out. The cap's own selection becomes a gallery
  mask over this same matrix, and the threshold is RECALIBRATED under it, because a cap
  is meant to fix the calibration bias as well as the density one (see
  ``gallery.cap_per_cat``); both columns are reported so which half moved is visible.

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


def _per_cat(
    conf: np.ndarray,
    unscoreable: "dict[int, int]",
    cats: "list[dict] | None",
    n_cats: int,
) -> "list[dict] | None":
    """Per-cat rows off the visit confusion matrix, or ``None`` without ``cats``.

    ``conf`` row ``i`` is cat ``i``'s held-out visits, its final column *declined*, so
    ``correct``/``wrong``/``declined`` come straight off the row and ``scored`` is its
    sum. ``recall`` is ``correct / (correct + wrong)`` — the SAME convention as the
    block's ``accuracy``, deliberately: declined is reported beside it, never folded in,
    because for a resident at the door "named the wrong cat" and "declined to name" mean
    opposite things.

    ``recall`` is ``None`` when nothing was decided (every visit declined), which is not
    a recall of zero — the caller renders the two differently. An entirely-unscoreable
    cat has an all-zero row and its count only in ``unscoreable``; both read as "no
    number can exist for this cat yet", which is why the tally is folded onto the cat's
    own row here instead of being left as a parallel index-keyed list.
    """
    if not cats:
        return None
    out = []
    for i, cat in enumerate(cats[:n_cats]):
        row = conf[i]
        declined = int(row[n_cats])
        correct = int(row[i])
        scored = int(row.sum())
        wrong = scored - correct - declined
        decided = correct + wrong
        out.append({
            "cat_id": cat.get("cat_id"),
            "cat_name": cat.get("cat_name"),
            "scored": scored,
            "correct": correct,
            "wrong": wrong,
            "declined": declined,
            "recall": (correct / decided) if decided else None,
            "declined_rate": (declined / scored) if scored else None,
            "unscoreable": int(unscoreable.get(i, 0)),
        })
    return out


def _cat_id_of(cats: "list[dict] | None", index: "int | None") -> "int | None":
    """The REAL ``cat_id`` behind a positional cat index, or ``None``.

    The index is positional over ``sorted(set(cat_ids))`` — the cats present in THIS run —
    so it shifts the moment a cat is excluded or retired, and two runs' "cat 3" are then
    different cats (entry 357's trap). Anything leaving this module keyed by cat has to
    come through here."""
    if not cats or index is None or index < 0 or index >= len(cats):
        return None
    return cats[index].get("cat_id")


def _resident_bucket(cats: "list[dict] | None", index: int) -> str:
    """Which impersonation bucket naming cat ``index`` falls in.

    Three, not two: ``is_resident`` is ``None`` when the roster flag was never plumbed
    through (no ``cat_residents``), and counting an unknown flag as *neighbour* would
    report the dangerous direction as measured-and-zero. The block reports the split as
    unavailable when any cat lands in ``as_unknown``."""
    flag = cats[index].get("is_resident") if cats and 0 <= index < len(cats) else None
    if flag is True:
        return "as_resident"
    return "as_neighbour" if flag is False else "as_unknown"


def _outcome_row(
    cats: "list[dict] | None",
    gi: int,
    true_cat: int,
    outcome: str,
    named: "int | None",
    group_meta: "list[dict]",
    group_night: "list[bool | None] | None",
) -> dict:
    """One row of the per-visit outcome list two arms are compared over.

    ``(cat_id, first_src_recv_ts)`` is the join key, which is why both halves are the
    caller's data rather than this module's: the timestamp comes from ``group_meta`` (no
    timestamps live here) and the ids come through ``cats`` (a positional index is only
    meaningful inside the run that produced it)."""
    meta = group_meta[gi] if gi < len(group_meta) else {}
    night = group_night[gi] if group_night is not None and gi < len(group_night) else None
    return {
        "cat_id": _cat_id_of(cats, true_cat),
        "first_src_recv_ts": meta.get("first_src_recv_ts"),
        "outcome": outcome,
        "named": _cat_id_of(cats, named),
        "night": None if night is None else bool(night),
    }


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
    cats: "list[dict] | None" = None,
    *,
    stranger: bool = False,
    group_meta: "list[dict] | None" = None,
    group_night: "list[bool | None] | None" = None,
) -> dict:
    """Score every held-out group against the rest of the matrix.

    For each group the group's OWN columns are masked to ``+inf`` — that exclusion is
    the whole point, and it is what the crop-level leave-one-out (diagonal only) fails
    to do. ``gal_mask`` optionally restricts the gallery side to a subset of columns
    (used for the day-only / night-only cross-regime cells); ``None`` = the full mixed
    gallery.

    ``cats`` (``[{cat_id, cat_name, n}]``, positionally aligned with the confusion
    index) adds a ``per_cat`` breakdown of the same counts, each row self-identifying by
    ``cat_id``. Derived HERE rather than by a reader of ``confusion`` so it cannot drift
    from ``accuracy`` below, and so no consumer has to know that the index is positional
    over the cats present in this run — an index that shifts whenever a cat is excluded.

    A group whose true cat has NO crop left on the gallery side is *unscoreable*: the
    correct answer is structurally absent, so it could only ever be wrong. Those are
    excluded from the denominator and returned as their own tally rather than counted
    as failures (which would understate the model) or dropped silently.

    The nearest-neighbour span is computed ONCE per group and then re-voted at every
    threshold in ``curve_ts``, since the threshold enters only through
    ``_aggregate_identity``'s below-threshold filter — so the sweep costs votes, not
    distance maths.

    **``stranger`` mode** inverts the unscoreable branch above rather than inheriting it.
    Called with ``gal_mask = (y != C)`` and only ``C``'s groups, every group's true cat is
    absent from the gallery BY DESIGN — so the branch that skips them would score nothing
    at all, which is why this is an explicit mode and not a consequence of the mask. Here
    *unknown* is the CORRECT answer (the stranger was rejected) and any name is an
    impersonation, recorded by which cat was impersonated and whether that cat is a
    resident. The returned shape is deliberately different: no ``accuracy``, because
    ``correct/(correct+wrong)`` over a pass whose correct answer is "no name" reads as a
    flat zero.

    ``group_meta`` (parallel to ``groups``, carrying ``first_src_recv_ts``) switches on the
    per-visit ``outcomes`` list two runs are compared over. It comes from the caller
    because this module holds no timestamps, and its ids are resolved through ``cats`` —
    a positional index means nothing outside the run that produced it.
    """
    counts = {"correct": 0, "wrong": 0, "unknown": 0}
    unscoreable: "dict[int, int]" = {}
    # rows = true cat index, cols = predicted cat index, final column = unknown.
    conf = np.zeros((n_cats, n_cats + 1), dtype=int)
    buckets = ("as_resident", "as_neighbour", "as_unknown")
    curve = {
        float(t): {"correct": 0, "wrong": 0, "unknown": 0, **{b: 0 for b in buckets}}
        for t in curve_ts
    }
    named_counts: "dict[int, int]" = {}
    impersonated_as = {b: 0 for b in buckets}
    outcomes: "list[dict] | None" = [] if group_meta is not None else None
    n_scored = 0

    for gi, (G, true_cat) in enumerate(zip(groups, group_true)):
        if not stranger:
            # Is the true cat represented on the gallery side, outside this group?
            others = y == true_cat
            others[G] = False
            if gal_mask is not None:
                others &= gal_mask
            if not others.any():
                unscoreable[true_cat] = unscoreable.get(true_cat, 0) + 1
                if outcomes is not None:
                    # Recorded, not dropped: a paired comparison joins two runs' visits, and
                    # a visit missing from one side would read as "this arm never saw it"
                    # rather than "no number can exist for it".
                    outcomes.append(_outcome_row(
                        cats, gi, true_cat, "unscoreable", None, group_meta, group_night))
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
        if named is not None:
            named_counts[named] = named_counts.get(named, 0) + 1
            impersonated_as[_resident_bucket(cats, named)] += 1
        if outcomes is not None:
            outcomes.append(_outcome_row(
                cats, gi, true_cat, outcome, named, group_meta, group_night))
        for t in curve_ts:
            oc, nm = _visit_outcome(span, float(t), true_cat, aggregate)
            curve[float(t)][oc] += 1
            # Only in stranger mode: this is the inner loop (every visit x every grid
            # point) and the buckets are read out nowhere else.
            if stranger and nm is not None:
                curve[float(t)][_resident_bucket(cats, nm)] += 1

    if stranger:
        # Everything not rejected is an impersonation — derived by subtraction rather than
        # read off `wrong`, so a caller who handed in a gal_mask that does NOT exclude the
        # held-out cat cannot make an impersonation vanish into `correct`.
        rejected = counts["unknown"]
        return {
            "n_scored": n_scored,
            "rejected": rejected,
            "impersonated": n_scored - rejected,
            "impersonation_rate": ((n_scored - rejected) / n_scored) if n_scored else None,
            **impersonated_as,
            # WHO it leaked into, by id and name only — no positional index leaves here.
            # An impersonation rate says how leaky the gallery is; the name is what a
            # reader acts on, and a stored index would mean a different cat in the next run.
            "named": [
                {
                    "cat_id": _cat_id_of(cats, i),
                    "cat_name": (cats[i].get("cat_name") if cats and i < len(cats) else None),
                    "is_resident": (cats[i].get("is_resident") if cats and i < len(cats) else None),
                    "n": int(n),
                }
                for i, n in sorted(named_counts.items())
            ],
            "curve": [
                {
                    "threshold": float(t),
                    "n_scored": n_scored,
                    "rejected": curve[float(t)]["unknown"],
                    "impersonated": n_scored - curve[float(t)]["unknown"],
                    **{b: curve[float(t)][b] for b in buckets},
                }
                for t in curve_ts
            ],
        }

    decided = counts["correct"] + counts["wrong"]
    return {
        "per_cat": _per_cat(conf, unscoreable, cats, n_cats),
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
        # Spelled out rather than splatted from `curve[t]`: the accumulator also carries the
        # impersonation buckets the stranger mode fills, and splatting would leak three
        # always-zero keys into every known-cat curve point ever recorded.
        "curve": [
            {
                "threshold": float(t),
                "correct": curve[float(t)]["correct"],
                "wrong": curve[float(t)]["wrong"],
                "unknown": curve[float(t)]["unknown"],
                "accuracy": (
                    curve[float(t)]["correct"]
                    / (curve[float(t)]["correct"] + curve[float(t)]["wrong"])
                    if (curve[float(t)]["correct"] + curve[float(t)]["wrong"])
                    else None
                ),
            }
            for t in curve_ts
        ],
        # Last, and only when asked for: this is the one field that leaves the metrics dict
        # for a file of its own (see run_feasibility_probe) because it grows with the visit
        # count, and `feasibility_runs.metrics` is kept indefinitely.
        **({"outcomes": outcomes} if outcomes is not None else {}),
    }


# Below three cats, holding one out leaves a ONE-CAT gallery: every impersonation lands on
# the single remaining cat and the readout is a property of the arithmetic, not of the
# embedding. Reported as unavailable, because "100% impersonation" reads as catastrophe
# where the honest reading is that nothing was measured.
_STRANGER_MIN_CATS = 3


def _stranger_block(
    dist: np.ndarray,
    y: np.ndarray,
    groups: "list[list[int]]",
    group_true: "list[int]",
    headline_t: float,
    curve_ts: "list[float]",
    aggregate,
    n_cats: int,
    cats: "list[dict] | None",
) -> dict:
    """Hold each cat out of the gallery in turn and score its visits as strangers.

    One pass per cat ``C`` with ``gal_mask = (y != C)``, over ``C``'s groups only. Every
    such visit is a trial with no correct name available: *unknown* is the fail-safe answer
    (**rejected**) and any name is an **impersonation**. Held-out residents and held-out
    neighbours are both scored — a neighbour simulates a genuine stranger, a resident
    simulates a cat that exists but has not been enrolled yet, and both are real situations
    at this door.

    The headline is MICRO-averaged over held-out visits, with per-cat rows beside it — the
    same shape and convention ``_per_cat`` and the block's own ``accuracy`` use, so the
    curve and the rows cannot disagree. Macro-averaging over cats would weight a
    three-visit cat equally with a two-hundred-visit one; the per-cat rows are where a thin
    cat stays visible.

    ``curve_ts`` is the KNOWN-cat pass's grid, handed in rather than derived here: a masked
    pass's own distance range differs, so the two curves would not pair up point for point
    by themselves — and pairing them is the whole operating-point table.
    """
    if not cats:
        # Without the index there is no way to say WHICH cat was impersonated, and an
        # impersonation count with no resident/neighbour split answers a different question
        # from the one this block exists for.
        return {"available": False, "reason": "no_cat_index"}
    if n_cats < _STRANGER_MIN_CATS:
        return {"available": False, "reason": "single_cat_gallery", "n_cats": int(n_cats)}

    buckets = ("as_resident", "as_neighbour", "as_unknown")
    totals = {"n_scored": 0, "rejected": 0, "impersonated": 0, **{b: 0 for b in buckets}}
    curve_acc = {
        float(t): {"n_scored": 0, "rejected": 0, "impersonated": 0, **{b: 0 for b in buckets}}
        for t in curve_ts
    }
    per_cat: "list[dict]" = []
    skipped: "list[dict]" = []

    for ci in range(n_cats):
        sel = [G for G, t in zip(groups, group_true) if t == ci]
        if not sel:
            # A cat with crops but no group can only arise from a caller that grouped a
            # different label set than it labelled; named rather than silently absent.
            skipped.append({"cat_id": _cat_id_of(cats, ci), "reason": "no_visits"})
            continue
        gal = y != ci
        scored = _score_visits(
            dist, y, sel, [ci] * len(sel), gal, headline_t, curve_ts, aggregate, n_cats,
            cats, stranger=True,
        )
        row = {
            "cat_id": _cat_id_of(cats, ci),
            "cat_name": cats[ci].get("cat_name"),
            "is_resident": cats[ci].get("is_resident"),
            **{k: v for k, v in scored.items() if k != "curve"},
        }
        per_cat.append(row)
        for key in ("n_scored", "rejected", "impersonated", *buckets):
            totals[key] += int(scored[key])
        for point in scored["curve"]:
            acc = curve_acc[float(point["threshold"])]
            for key in ("n_scored", "rejected", "impersonated", *buckets):
                acc[key] += int(point[key])

    if not per_cat:
        return {"available": False, "reason": "no_visits"}

    n = totals["n_scored"]
    # The split is only claimed when EVERY named cat could be classified. A missing roster
    # flag would otherwise be reported as a measured zero in the dangerous direction.
    split_known = totals["as_unknown"] == 0
    return {
        "available": True,
        "reason": None,
        "n_cats_held_out": len(per_cat),
        "n_scored": n,
        "rejected": totals["rejected"],
        "impersonated": totals["impersonated"],
        "impersonation_rate": (totals["impersonated"] / n) if n else None,
        "as_resident": totals["as_resident"],
        "as_neighbour": totals["as_neighbour"],
        "as_unknown": totals["as_unknown"],
        "resident_impersonation_rate": (
            (totals["as_resident"] / n) if (n and split_known) else None
        ),
        "resident_split": split_known,
        "per_cat": per_cat,
        # Rates are left to the reader: the row already carries its own denominator, and
        # this curve is stored per run on a table that is parsed 100 rows at a time.
        "curve": [curve_acc[float(t)] | {"threshold": float(t)} for t in curve_ts],
        "skipped": skipped,
    }


def _caps_block(
    dist: np.ndarray,
    y: np.ndarray,
    groups: "list[list[int]]",
    group_true: "list[int]",
    iu: np.ndarray,
    ju: np.ndarray,
    pair_d: np.ndarray,
    same_pair: np.ndarray,
    cross_pair: np.ndarray,
    cap_masks: "list[dict]",
    base_t: float,
    aggregate,
    n_cats: int,
) -> "list[dict]":
    """Forecast a per-cat-capped gallery: one row per cap, scored over this same matrix.

    Each cap's own selection (``gallery.cap_per_cat``, run by the caller — the policy must
    be the build's, not a second one that could disagree with it) arrives as a column mask.
    The threshold is RECALIBRATED under that mask, over cross-visit same-cat pairs drawn
    from the surviving columns, because ``cap_per_cat`` names two biases and a forecast
    reusing the uncapped threshold answers only the first. Both columns are reported —
    ``recalibrated`` and ``fixed`` (the uncapped threshold, unchanged) — so which half of
    the bias moved is visible rather than inferred.

    One subtlety, in the fail-safe direction: under leave-one-visit-out the held-out visit's
    columns are masked anyway, so where a cap happened to select crops from the held-out
    visit that cat's effective gallery for that fold sits slightly BELOW the cap. That
    under-represents the true cat, so on the visits it scores a cap is understated here.

    That is NOT a guarantee the row cannot flatter, and the other direction is why
    ``n_scored``/``n_unscoreable`` are reported per cap rather than left implicit: a tight
    cap can leave a cat's every surviving vector inside the visit being held out, which
    makes that visit *unscoreable* and drops it from the DENOMINATOR instead of counting it
    wrong. Measured on a 4-cat fixture, capping to 1 moved 20 scored visits to 16 and lifted
    reported recall 40% -> 56% with no gallery improvement whatever. So a cap's accuracy is
    only comparable to the row above it at the same ``n_scored``.
    """
    out: "list[dict]" = []
    for entry in cap_masks:
        mask = np.asarray(entry["mask"], dtype=bool)
        # Transient and freed per iteration: at 12k crops each pair-length array is ~72 MB
        # (bool) / ~576 MB (float64), which is why the caps are scored one at a time rather
        # than by materialising every mask's pair set up front.
        surviving = mask[iu] & mask[ju]
        same_cross = pair_d[same_pair & cross_pair & surviving]
        diff_surv = pair_d[(~same_pair) & surviving]
        recal_t, recal_bal = _best_threshold(same_cross, diff_surv)
        del surviving, same_cross, diff_surv

        # The sweep re-votes an already-computed span, so asking for the fixed threshold as
        # a curve point costs one vote per visit instead of a second pass over the matrix.
        head_t = base_t if recal_t is None else recal_t
        scored = _score_visits(
            dist, y, groups, group_true, mask, head_t,
            [] if recal_t is None else [float(base_t)], aggregate, n_cats, None,
        )
        # Carried, not inferable: a cap can push a visit out of the denominator entirely
        # (its cat's every surviving vector sits inside the held-out visit), so an accuracy
        # read without the count it was measured over can rise purely by shrinkage.
        n_unscoreable = sum(int(u["n_visits"]) for u in scored["unscoreable"])
        head = {
            "n_scored": scored["n_scored"],
            "n_unscoreable": n_unscoreable,
            "correct": scored["correct"],
            "wrong": scored["wrong"],
            "unknown": scored["unknown"],
            "accuracy": scored["accuracy"],
            "unknown_rate": scored["unknown_rate"],
        }
        if recal_t is None:
            recalibrated, fixed = None, head
        else:
            point = scored["curve"][0]
            recalibrated = head
            fixed = {
                "n_scored": scored["n_scored"],
                "n_unscoreable": n_unscoreable,
                "correct": point["correct"],
                "wrong": point["wrong"],
                "unknown": point["unknown"],
                "accuracy": point["accuracy"],
                "unknown_rate": (
                    (point["unknown"] / scored["n_scored"]) if scored["n_scored"] else None
                ),
            }
        out.append({
            "max_per_cat": entry.get("max_per_cat"),
            "n_vectors": int(mask.sum()),
            "n_cats": int(len(set(int(c) for c in y[mask]))),
            "threshold": recal_t,
            "threshold_balanced_acc": recal_bal,
            # Named, not implied: a cap that leaves no cross-visit same-cat pair among the
            # survivors cannot be calibrated, and a recalibrated column of nulls beside a
            # populated fixed one would read as a measured collapse.
            "reason": None if recal_t is not None else "uncalibrated_threshold",
            "recalibrated": recalibrated,
            "fixed": fixed,
        })
    return out


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
    cats: "list[dict] | None" = None,
    *,
    visit_meta: "list[dict] | None" = None,
    strangers: bool = False,
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

    ``strangers`` adds the held-out-CAT block, which shares this function's ``curve_ts``
    (see ``_stranger_block``) — the only place it is computed. ``visit_meta`` switches on
    the per-visit outcome list, and applies to the MAIN pass only: the regime and
    cross-regime cells re-score subsets of the same visits, so emitting there would put
    each visit in the list several times under different galleries.
    """
    if aggregate is None:
        raise ValueError("visit_groups requires an aggregate (Store._aggregate_identity)")
    if visit_meta is not None and len(visit_meta) != len(groups):
        # A misaligned meta list silently stamps one visit's timestamp onto another, which a
        # paired comparison then joins on. Loud, because nothing downstream could detect it.
        raise ValueError(
            f"visit_meta has {len(visit_meta)} entries for {len(groups)} groups"
        )
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
        dist, y, groups, group_true, None, threshold, curve_ts, aggregate, n_cats, cats,
        group_meta=visit_meta, group_night=visit_night,
    )
    out = {**base, "available": True, "reason": None, **scored,
           "marks": dict(extra_thresholds or {})}

    # Held-out CATS, on the SAME grid as the held-out visits above — that shared grid is
    # what lets the two curves be read as one operating-point table instead of two
    # unrelated sweeps.
    if strangers:
        out["strangers"] = _stranger_block(
            dist, y, groups, group_true, threshold, curve_ts, aggregate, n_cats, cats
        )

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
        # `cats` is passed here too, so each regime carries its own per-cat rows — that
        # is the whole day/night column, free. NOT to the cross cells below: per-cat
        # cross-regime is a non-goal, and computing rows nothing reads would put a
        # plausible-looking but unsurfaced number into every stored run.
        regimes[name] = _score_visits(
            dist, y, g_sel, t_sel, None, threshold, [], aggregate, n_cats, cats
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
    cat_residents: "dict[int, bool | None] | None" = None,
    visit_meta: "list[dict] | None" = None,
    strangers: bool = False,
    cap_masks: "list[dict] | None" = None,
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

    **Open-set additions**, each also additive and each riding the same distance matrix:

    - ``strangers`` adds ``visits.strangers`` — every cat held out of the gallery in turn,
      so an unenrolled cat's visits are scored with *declined* as the correct answer. Needs
      ``cat_residents`` (``cat_id`` → roster flag) to split an impersonation by whether the
      impersonated cat is one of ours; without it the split reports itself unmeasured.
    - ``cap_masks`` (``[{max_per_cat, mask}]``) adds ``visits.caps`` — a per-cap forecast
      under the caller's own ``cap_per_cat`` selection, recalibrated under each mask.
    - ``visit_meta`` (one dict per group, carrying ``first_src_recv_ts``) adds
      ``visits.outcomes``, the per-visit list two runs are compared over.

    ``cat_residents`` is the only one that touches an existing key: each ``cats`` entry
    gains ``is_resident`` when it is supplied, and is left exactly as before when it is not.
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
        {"cat_id": c, "cat_name": cat_names.get(c) or f"cat #{c}", "n": int((ids == c).sum()),
         # Only when the roster flag was actually supplied. Defaulting it to None instead
         # would put an unmeasured-looking field on every run ever recorded, and the
         # stranger block's split reads exactly this key to decide whether it can claim one.
         **({"is_resident": cat_residents.get(c)} if cat_residents is not None else {})}
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
            threshold_cross, n_curve, marks, labeled_ts_groups, gap_ms, cats,
            visit_meta=visit_meta, strangers=strangers,
        )
        # Only on an AVAILABLE block. `_visits_block` deliberately returns none of its
        # computed fields when it reports "nothing was measured", and attaching a real
        # cross-visit AUC beside `available: False` would contradict exactly that — the
        # distinction this whole scoring exists to keep honest.
        if result["visits"].get("available"):
            result["visits"]["auc"] = auc_cross
            result["visits"]["threshold_balanced_acc"] = bal_cross
            # Same gate for the cap forecast: it re-scores the same held-out visits, so
            # where those could not be scored at all there is nothing for a cap to move.
            # Computed here rather than in `_visits_block` because the recalibration reads
            # the pair arrays this function already materialised.
            if cap_masks:
                group_true = [int(y[G[0]]) for G in visit_groups]
                result["visits"]["caps"] = _caps_block(
                    dist, y, visit_groups, group_true, iu, ju, pair_d, same_pair,
                    cross_pair, cap_masks, float(threshold_cross), aggregate, n_cats,
                )
    return result
