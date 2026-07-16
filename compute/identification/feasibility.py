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
) -> dict:
    """Separability scorecard over ``embeddings`` (N,D) labelled by ``cat_ids`` (len N).

    ``cat_names`` maps a ``cat_id`` → display name (missing names fall back to
    ``"cat #<id>"``). ``k`` is the kNN vote size (clamped to ``[1, N-1]``).
    Returns the JSON-serialisable dict documented in the module header. Raises
    ``ValueError`` if there are fewer than 2 crops or fewer than 2 distinct cats —
    separability is undefined with one class or one point.
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
    return {
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
