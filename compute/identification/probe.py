"""Feasibility-probe orchestrator — labelled crops → embeddings → separability
metrics → a self-contained HTML report, as one reusable library step.

This is the compute+report core that both the CLI tool
(``compute/tools/feasibility.py``) and the Training-page API/manager call, so the
"can we tell our cats apart?" pipeline lives in exactly one place. It reads the
``identified`` crops via ``Store.labeled_crops``, embeds them with the DINOv2
``Embedder`` (first run downloads the backbone), computes the separability
scorecard (``compute.identification.feasibility.run_feasibility``), and writes:

  <out_dir>/feasibility.json   — raw metrics
  <out_dir>/feasibility.html   — self-contained report (charts inlined as base64)

It returns a summary dict and — deliberately — does NOT touch the DB: persisting a
``feasibility_runs`` row is the caller's concern (the manager persists; the CLI
just prints), keeping this a pure compute+report step. It also does not catch
``EmbedCancelled`` — a cancel propagates so the caller records the run as canceled.

Charts follow the dataviz method: cat identity is the validated categorical palette
(fixed slot order, colourblind-safe), the confusion matrix is a single-hue blue
sequential ramp, and the distance histogram is two distinct hues (same vs different)
with the suggested threshold marked. matplotlib is imported lazily inside the chart
helpers, so importing this module stays cheap on the lean collector.
"""
from __future__ import annotations

import base64
import html
import io
import json
import math
import os

import numpy as np

from compute.collection.store import _QUALITIES, Store
from compute.identification.embed import Embedder, canonical_geometry, geometry_descriptor
from compute.identification.feasibility import run_feasibility

# Gap at which a cat's labelled crops are split into separate held-out VISITS. Its own
# constant, deliberately much coarser than the store's `_VISIT_GAP_MS` (2 s), because
# coarse is the fail-safe direction HERE: over-merging two visits removes more of the
# near-duplicate leakage the held-out scoring exists to eliminate, while under-merging
# splits one physical visit across the boundary and lets that leakage straight back in.
# A detection dropout mid-visit (the cat sits still, YOLO drops it for a few seconds)
# would produce exactly such a split at 2 s.
_HELDOUT_GAP_MS = 60_000

# Per-cat caps forecast by default. `None` is the uncapped baseline and is deliberately
# first — every other row is only readable as a difference from it. The values bracket the
# real dataset's skew (the dominant cat is in the low thousands of crops, the thinnest in
# the hundreds), and each is a mask over a matrix the run already holds, so asking for four
# costs numpy rather than a second embed.
_DEFAULT_CAPS: "tuple[int | None, ...]" = (None, 2000, 1000, 500)

# Rows the operating-point table samples out of the (41-point) threshold grid. The whole
# grid lives in feasibility.json; a table long enough to scroll is one nobody reads a
# trade-off off, and the two ends of the grid are degenerate by construction (name
# everything / name nothing).
_OP_ROWS = 15

# Validated light-mode categorical palette (fixed slot order — see the dataviz
# reference palette; worst adjacent CVD ΔE 24.2). Identity is assigned in this
# order, never cycled; a >8-cat run reuses slots with a legend note (see below).
_CAT_PALETTE = ["#2a78d6", "#1baf7a", "#eda100", "#008300", "#4a3aa7", "#e34948", "#e87ba4", "#eb6834"]
_SAME_HUE = "#2a78d6"   # blue — same-cat pair distances
_DIFF_HUE = "#eb6834"   # orange — different-cat pair distances
_INK = "#0b0b0b"
_MUTED = "#898781"


def _quality_slug(quals: "tuple[str, ...]") -> str:
    """Canonical ``gallery+ok`` slug for a quality selection, tier-ordered so the
    dir/name is stable regardless of the order the user typed the grades in."""
    return "+".join(q for q in _QUALITIES if q in quals)


def group_visits(
    labels: "list[dict]", is_night=None, gap_ms: int = _HELDOUT_GAP_MS
) -> "tuple[list[list[int]], list[bool | None]]":
    """Partition ``labels`` (row order == embedding row order) into held-out visits.

    Groups **per cat**: each cat's rows are sorted by ``src_recv_ts`` and split at
    ``gap_ms`` gaps via ``Store._gap_split`` — the same clustering primitive the
    scorecard and visit inbox share, so no second gap-clustering implementation exists to
    drift. Clustering the whole set globally would merge two cats that were at the door
    in the same minute into one group, which has no well-defined true ``cat_id`` to score
    against; ``dataset_items`` is UNIQUE on ``(src_frame_id, src_recv_ts)`` so two cats
    cannot share one frame's label, but adjacent frames seconds apart can carry different
    cats — and tailgating is an expected case at this door.

    Returns ``(groups, night_flags)``: row-index lists, and one regime flag per group,
    bucketed whole by ``is_night`` of the group's FIRST crop (the same first-frame rule
    ``gate_scorecard``'s visit split uses, so the two cannot disagree about which side of
    dusk a visit sits on). ``is_night`` ``None`` → every flag ``None`` (no location
    configured, or astral missing), which disables the split rather than guessing.
    """
    by_cat: "dict[int, list[tuple[int, int]]]" = {}
    for i, row in enumerate(labels):
        cat = row.get("cat_id")
        if cat is None:
            continue  # catless kinds (unknown_cat) carry no identity to score
        by_cat.setdefault(int(cat), []).append((i, int(row["src_recv_ts"])))

    groups: "list[list[int]]" = []
    nights: "list[bool | None]" = []
    for cat in sorted(by_cat):
        rows = sorted(by_cat[cat], key=lambda r: r[1])
        for run in Store._gap_split(rows, gap_ms, ts_of=lambda r: r[1]):
            groups.append([i for i, _ts in run])
            nights.append(bool(is_night(run[0][1])) if is_night is not None else None)
    return groups, nights


def _cap_masks(labels: "list[dict]", caps: "tuple") -> "list[dict] | None":
    """One column mask per per-cat cap, from ``cap_per_cat``'s own selection.

    The forecast has to describe what a BUILD would enrol, so the selection is the build's
    (``gallery.cap_per_cat``) rather than a second policy re-derived here that could
    quietly disagree with it. Imported inside the function because ``gallery`` imports this
    module's ``_quality_slug`` at its top — a module-level import would be circular.

    Rows are located by ``(src_frame_id, src_recv_ts)``, which ``dataset_items`` is UNIQUE
    on, rather than by object identity: the key survives ``cap_per_cat`` copying or
    rebuilding a row, which its contract does not promise either way.
    """
    if not caps:
        return None
    from compute.identification.gallery import cap_per_cat

    index_of = {
        (row["src_frame_id"], row["src_recv_ts"]): i for i, row in enumerate(labels)
    }
    out = []
    for cap in caps:
        mask = np.zeros(len(labels), dtype=bool)
        for row in cap_per_cat(labels, cap):
            mask[index_of[(row["src_frame_id"], row["src_recv_ts"])]] = True
        out.append({"max_per_cat": cap, "mask": mask})
    return out


def _night_classifier(store):
    """The store's configured day/night classifier, or ``None`` when unavailable.

    Same gate as the tuning split and the per-cat regime coverage: a location must be set
    AND astral importable. Missing either reports the split unavailable rather than
    guessing a boundary — a wrong location yields confidently-wrong Day/Night numbers.
    """
    from compute.analysis.suntimes import astral_available, night_classifier

    coords = store.get_location()
    if coords is None or not astral_available():
        return None
    try:
        return night_classifier(coords[0], coords[1])
    except Exception:  # pragma: no cover - a broken astral install must not fail the run
        return None


# Worst-first order for per-cat rows, shared by the TL;DR tile and the chart it sits
# above so the two can never name a different "weakest" cat. Defined once because the
# tie-break is the whole point: two independent min()/sort() calls agreed on the recall
# and disagreed on the tie.
def _WEAKEST_KEY(c: dict) -> "tuple[float, int]":  # noqa: N802 - a constant-like sort key
    return (c["recall"], -(c.get("scored") or 0))


def _wilson(correct: int, n: int, z: float = 1.96) -> "tuple[float, float] | None":
    """95% Wilson score interval for ``correct`` of ``n``, or ``None`` when n == 0.

    Wilson rather than the textbook normal approximation because the interesting cases
    here sit at the ends: a cat with 8 of 8 correct has a normal-approximation interval
    of exactly zero width, which would present "we have seen this cat eight times" as
    certainty. Wilson stays inside [0, 1] and keeps a real width at 100%.

    This is the report's main defence against its own precision. Two runs a day apart
    differ by a handful of visits, and without an interval a 97% -> 95% step reads as a
    regression rather than as the same number measured twice.
    """
    if n <= 0:
        return None
    p = correct / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, centre - half), min(1.0, centre + half)


def _worst_pair(visits: dict, cat_names: "list[str]") -> "dict | None":
    """The largest off-diagonal cell of the visit confusion matrix.

    One pair, not a ranking: a handful of errors spread evenly over five cats is a
    different problem from all of them landing in one cell, and naming the top cell is
    what tells the two apart at a glance. Declined (the final column) is excluded — it
    is not a mistaken identity, and it is reported on its own.
    """
    conf = visits.get("confusion")
    if not conf:
        return None
    best = None
    for i, row in enumerate(conf):
        for j, count in enumerate(row[:len(conf)]):   # drop the trailing 'declined' column
            if i == j or not count:
                continue
            if best is None or count > best["count"]:
                best = {
                    "count": int(count),
                    "true_index": i,
                    "named_index": j,
                    "true": cat_names[i] if i < len(cat_names) else f"#{i}",
                    "named": cat_names[j] if j < len(cat_names) else f"#{j}",
                    # Decided only — the declined column is dropped above, so counting it
                    # here would make "7 of 31" quote a denominator the
                    # numerator was never drawn from.
                    "of": int(sum(row[:len(conf)])),
                }
    return best


def _tldr(metrics: dict, cat_names: "list[str]") -> "dict | None":
    """The handful of numbers the report leads with, derived from what it already has.

    Everything here is a re-reading of the visit block — no new measurement — chosen so
    each one answers a question the reader actually has: is it good (accuracy + its
    interval), did it duck (declined), who is dragging it down (weakest), what is it
    confusing (pair), and is the dark half the problem (night gap).
    """
    v = metrics.get("visits")
    if not v or not v.get("available"):
        return None
    n = v.get("n_scored") or 0
    decided = (v.get("correct") or 0) + (v.get("wrong") or 0)
    ci = _wilson(v.get("correct") or 0, decided)
    per_cat = [c for c in (v.get("per_cat") or []) if c.get("recall") is not None]
    # SAME ordering as _percat_png's sort, tie-break included. Recall is a ratio of small
    # integers, so exact ties are routine (1/2, 2/4, 3/6 all land on 0.5) — and with a
    # plain min() the tile picked the first tied row (per_cat is cat_id order) while the
    # chart accented the largest-sample one, naming two different cats "weakest" in one
    # block. On a tie the bigger sample is the more defensible pick.
    weakest = min(per_cat, key=_WEAKEST_KEY) if per_cat else None

    regimes = v.get("regimes") or {}
    day, night = regimes.get("day"), regimes.get("night")
    gap = None
    if day and night and day.get("accuracy") is not None and night.get("accuracy") is not None:
        gap = night["accuracy"] - day["accuracy"]

    # Unscoreable cats are not a low score — the correct answer was structurally absent
    # from the gallery, so they could only ever have been wrong. Named, because the fix
    # (one more visit) is different from every other row's.
    unscoreable = [
        {"name": cat_names[u["cat_index"]] if u["cat_index"] < len(cat_names) else f"#{u['cat_index']}",
         "n_visits": u["n_visits"]}
        for u in (v.get("unscoreable") or [])
    ]
    return {
        "accuracy": v.get("accuracy"),
        "ci": ci,
        # The smallest move that is not noise, in points — the direct answer to "I
        # labelled all week and the number went DOWN".
        "resolution": (1.0 / decided) if decided else None,
        "n_scored": n,
        "decided": decided,
        "declined": v.get("unknown") or 0,
        "declined_rate": v.get("unknown_rate"),
        "weakest": weakest,
        "pair": _worst_pair(v, cat_names),
        "night_gap": gap,
        "day_acc": (day or {}).get("accuracy"),
        "night_acc": (night or {}).get("accuracy"),
        "unscoreable": unscoreable,
        # The stranger block, verbatim rather than re-read: the lead block's biggest stated
        # limit used to be that nothing here tests a stranger, and when that IS measured the
        # limit has to be replaced by the measurement, not merely sit beside it.
        "strangers": v.get("strangers"),
    }


def _plt():
    """pyplot with the headless Agg backend pinned.

    The charts are rendered inside TrainingManager's WORKER thread, and matplotlib warns
    that a GUI backend outside the main thread "will likely fail". Agg is correct here by
    definition — every figure goes straight to a base64 PNG, never to a window. Set before
    pyplot is imported, which is why the import stays lazy and funnels through here.
    """
    import matplotlib

    if matplotlib.get_backend().lower() != "agg":
        matplotlib.use("Agg", force=True)
    import matplotlib.pyplot as plt

    return plt


def _fig_png(fig) -> str:
    """Render a matplotlib figure to a base64 data-URI PNG and close it."""
    plt = _plt()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, bbox_inches="tight", facecolor="#fcfcfb")
    plt.close(fig)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _percat_png(visits: dict) -> str:
    """Per-cat visit recall, worst-first, with 95% Wilson intervals.

    EMPHASIS, not a categorical palette: the story is one cat, so the weakest bar takes
    the accent and the rest sit in a light step of the same hue. Eight hues here would
    say the cats are the subject; they are not, the recall is.

    The interval is the load-bearing part. Sorted bars invite reading the order as a
    ranking, and with 4 visits against 40 that order is mostly sampling noise — drawn
    whiskers are what stop a cat being 'worse' on the strength of one bad visit.
    """
    plt = _plt()

    rows = [c for c in (visits.get("per_cat") or []) if c.get("recall") is not None]
    if not rows:
        return ""
    rows.sort(key=_WEAKEST_KEY)
    names = [c["cat_name"] or f"#{c['cat_id']}" for c in rows]
    vals = [c["recall"] for c in rows]
    decided = [max(0, (c.get("correct") or 0) + (c.get("wrong") or 0)) for c in rows]
    lo, hi = [], []
    for c, d in zip(rows, decided):
        ci = _wilson(c.get("correct") or 0, d)
        lo.append(c["recall"] - ci[0] if ci else 0.0)
        hi.append(ci[1] - c["recall"] if ci else 0.0)

    fig, ax = plt.subplots(figsize=(6.4, 0.52 * len(rows) + 1.3))
    y = list(range(len(rows)))
    # Accent only on the weakest (index 0 after the sort); the rest recede.
    colors = [_SAME_HUE if i == 0 else "#c3d8f2" for i in y]
    ax.barh(y, vals, height=0.6, color=colors, zorder=2)
    ax.errorbar(vals, y, xerr=[lo, hi], fmt="none", ecolor=_MUTED, elinewidth=1.2,
                capsize=3, zorder=3)
    for i, (val, d) in enumerate(zip(vals, decided)):
        # Label the bar END only — the axis carries the rest, and a number inside every
        # bar would collide with the whisker on the short ones.
        ax.text(min(1.0, val + hi[i]) + 0.02, i, f"{val:.0%}", va="center", fontsize=9,
                color=_INK)
    ax.set_yticks(y)
    # (n) is DECIDED, not scored — the recall and its interval are both computed over
    # correct+wrong, so labelling it "scored" would print a denominator the bar is not
    # drawn from wherever a cat has declines.
    ax.set_yticklabels([f"{n}  ({d})" for n, d in zip(names, decided)], fontsize=9, color=_INK)
    ax.invert_yaxis()
    ax.set_xlim(0, 1.18)
    ax.set_xticks([0, 0.25, 0.5, 0.75, 1.0])
    ax.set_xticklabels(["0", "25%", "50%", "75%", "100%"], fontsize=8, color=_MUTED)
    ax.set_xlabel("visits named correctly, of those decided  ·  (n) = visits decided",
                  color=_MUTED, fontsize=9)
    ax.set_title("Per-cat recall, weakest first — bars are 95% intervals",
                 color=_INK, fontsize=11)
    ax.tick_params(colors=_MUTED, length=0)
    ax.xaxis.grid(True, color="#e8e6e1", linewidth=1, zorder=0)  # solid hairline, recessive
    ax.set_axisbelow(True)
    for spine in ax.spines.values():
        spine.set_visible(False)
    return _fig_png(fig)


def _regime_png(visits: dict) -> str:
    """Day vs night recall per cat, as a dumbbell — two states of one item.

    A dumbbell rather than grouped bars because the reader's question is the GAP, and a
    connector draws the gap directly instead of asking them to subtract two bar lengths.
    Returns "" without a day/night split, so no location means no chart rather than a
    chart of one regime pretending to be both.
    """
    plt = _plt()

    regimes = visits.get("regimes") or {}
    day_rows = {c["cat_id"]: c for c in ((regimes.get("day") or {}).get("per_cat") or [])}
    night_rows = {c["cat_id"]: c for c in ((regimes.get("night") or {}).get("per_cat") or [])}
    pairs = []
    for cid, d in day_rows.items():
        n = night_rows.get(cid)
        # Both sides must be measured: one end missing is not a gap of unknown size, it
        # is no reading at all, and a dot at zero would say the opposite.
        if n and d.get("recall") is not None and n.get("recall") is not None:
            # Each side's decided count rides along: without it a night dot off ONE visit
            # is indistinguishable from one off twenty, and the night side is exactly
            # where the counts are smallest. The sibling bar chart shows (n) for the same
            # reason; a static PNG has no tooltip to hide it in.
            dd = (d.get("correct") or 0) + (d.get("wrong") or 0)
            nd = (n.get("correct") or 0) + (n.get("wrong") or 0)
            pairs.append((d["cat_name"] or f"#{cid}", d["recall"], n["recall"], dd, nd))
    if not pairs:
        return ""
    pairs.sort(key=lambda p: p[2] - p[1])   # biggest night deficit first

    fig, ax = plt.subplots(figsize=(6.4, 0.52 * len(pairs) + 1.3))
    y = list(range(len(pairs)))
    for i, (_name, d, n, _dd, _nd) in enumerate(pairs):
        ax.plot([d, n], [i, i], color="#d8d5cf", linewidth=2, zorder=1, solid_capstyle="round")
    # Warm = day, cool = night. Orange over the palette's amber slot deliberately: amber
    # measures 2.11:1 on this surface, and the validator's relief for that (a table view)
    # does not exist for PER-CAT day/night — the section below tabulates the regimes as
    # wholes. This pair passes every check outright, worst adjacent CVD dE 29.5.
    ax.scatter([p[1] for p in pairs], y, s=64, color=_CAT_PALETTE[7], zorder=3,
               label="day", edgecolors="#fcfcfb", linewidths=2)
    ax.scatter([p[2] for p in pairs], y, s=64, color=_CAT_PALETTE[4], zorder=3,
               label="night", edgecolors="#fcfcfb", linewidths=2)
    ax.set_yticks(y)
    ax.set_yticklabels([f"{p[0]}  ({p[3]}/{p[4]})" for p in pairs], fontsize=9, color=_INK)
    ax.invert_yaxis()
    ax.set_xlim(0, 1.05)
    ax.set_xticks([0, 0.25, 0.5, 0.75, 1.0])
    ax.set_xticklabels(["0", "25%", "50%", "75%", "100%"], fontsize=8, color=_MUTED)
    ax.set_title("Day vs night recall — biggest night deficit first  ·  (day/night visits decided)",
                 color=_INK, fontsize=10.5, pad=22)
    ax.tick_params(colors=_MUTED, length=0)
    ax.xaxis.grid(True, color="#e8e6e1", linewidth=1, zorder=0)
    ax.set_axisbelow(True)
    # ABOVE the axes, not inside them: every dot sits at a high recall against a 0-100%
    # scale, so the lower-right corner a legend defaults to is exactly where the last
    # row's pair lands — measured, it covered them.
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.02), ncol=2, fontsize=8,
              frameon=False, handletextpad=0.4, columnspacing=1.4)
    for spine in ax.spines.values():
        spine.set_visible(False)
    return _fig_png(fig)


def _scatter_png(metrics: dict) -> str:
    plt = _plt()

    fig, ax = plt.subplots(figsize=(6, 5))
    cats = metrics["cats"]
    pts = metrics["projection"]
    for ci, cat in enumerate(cats):
        xs = [p["x"] for p in pts if p["cat_index"] == ci]
        ys = [p["y"] for p in pts if p["cat_index"] == ci]
        ax.scatter(xs, ys, s=28, alpha=0.85, edgecolors="none",
                   color=_CAT_PALETTE[ci % len(_CAT_PALETTE)], label=cat["cat_name"])
    ax.set_title("Crop embeddings (PCA 2D) — do the cats cluster?", color=_INK, fontsize=11)
    ax.set_xlabel("PC1", color=_MUTED, fontsize=9)
    ax.set_ylabel("PC2", color=_MUTED, fontsize=9)
    ax.tick_params(colors=_MUTED, labelsize=8)
    ax.legend(loc="best", fontsize=8, frameon=False)
    for spine in ax.spines.values():
        spine.set_color("#c3c2b7")
    return _fig_png(fig)


def _confusion_png(metrics: dict) -> str:
    import numpy as np

    plt = _plt()

    conf = np.array(metrics["knn"]["confusion"], dtype=float)
    names = [c["cat_name"] for c in metrics["cats"]]
    # Row-normalise for colour (recall per true cat), single-hue blue sequential.
    row_sums = conf.sum(axis=1, keepdims=True)
    norm = np.divide(conf, row_sums, out=np.zeros_like(conf), where=row_sums > 0)
    fig, ax = plt.subplots(figsize=(1.4 + 0.7 * len(names), 1.2 + 0.7 * len(names)))
    ax.imshow(norm, cmap="Blues", vmin=0, vmax=1)
    ax.set_xticks(range(len(names)))
    ax.set_yticks(range(len(names)))
    ax.set_xticklabels(names, rotation=45, ha="right", fontsize=8, color=_INK)
    ax.set_yticklabels(names, fontsize=8, color=_INK)
    ax.set_xlabel("predicted (nearest neighbour)", color=_MUTED, fontsize=9)
    ax.set_ylabel("actual", color=_MUTED, fontsize=9)
    ax.set_title("kNN confusion (counts)", color=_INK, fontsize=11)
    for i in range(len(names)):
        for j in range(len(names)):
            ax.text(j, i, int(conf[i, j]), ha="center", va="center", fontsize=8,
                    color=(_INK if norm[i, j] < 0.6 else "#ffffff"))
    return _fig_png(fig)


def _hist_png(metrics: dict) -> str:
    import numpy as np

    plt = _plt()

    d = metrics["distances"]
    edges = np.array(d["hist"]["edges"])
    centers = (edges[:-1] + edges[1:]) / 2
    width = (edges[1] - edges[0]) if len(edges) > 1 else 1.0
    fig, ax = plt.subplots(figsize=(6, 3.6))
    ax.bar(centers, d["hist"]["same"], width=width, color=_SAME_HUE, alpha=0.7, label="same cat")
    ax.bar(centers, d["hist"]["diff"], width=width, color=_DIFF_HUE, alpha=0.55, label="different cat")
    thr = d.get("suggested_threshold")
    if thr is not None:
        ax.axvline(thr, color=_INK, linestyle="--", linewidth=1.5, label=f"threshold {thr:.3f}")
    ax.set_title("Pair cosine distance — same vs different cat", color=_INK, fontsize=11)
    ax.set_xlabel("cosine distance", color=_MUTED, fontsize=9)
    ax.set_ylabel("pairs", color=_MUTED, fontsize=9)
    ax.tick_params(colors=_MUTED, labelsize=8)
    ax.legend(loc="best", fontsize=8, frameon=False)
    for spine in ax.spines.values():
        spine.set_color("#c3c2b7")
    return _fig_png(fig)


def _curve_png(visits: dict) -> str:
    """Visit accuracy / unknown-rate against the distance threshold, with marked points.

    The circularity answer: the headline threshold is derived from the same crops, so
    rather than state one number and hide that, the curve shows how sensitive the verdict
    is — and marks the crop-level threshold and the active model's promoted one on the
    same axis for comparison.

    When the stranger pass ran, its impersonation rate is drawn on the SAME axes rather
    than in a chart of its own: the trade-off a threshold makes is between these two lines,
    and two figures asking the reader to align two x-axes by eye is exactly how a
    trade-off stops being read as one. Both are shares of their own curve's scored
    visits — different denominators, which the axis label says.
    """
    plt = _plt()

    pts = [p for p in visits.get("curve") or [] if p.get("accuracy") is not None]
    fig, ax = plt.subplots(figsize=(6, 3.6))
    if pts:
        xs = [p["threshold"] for p in pts]
        ax.plot(xs, [p["accuracy"] for p in pts], color=_SAME_HUE, linewidth=2,
                label="visit accuracy")
        n = max(1, visits.get("n_scored") or 1)
        ax.plot(xs, [p["unknown"] / n for p in pts], color=_DIFF_HUE, linewidth=2,
                linestyle=":", label="unknown rate")
    # Points with no denominator are DROPPED rather than plotted at zero: a threshold
    # nothing was scored at is not a threshold with no impersonations.
    op = [
        (r["threshold"], _rate(r["stranger"].get("impersonated"), r["stranger"].get("n_scored")))
        for r in _op_points(visits)
    ]
    op = [(t, rate) for t, rate in op if rate is not None]
    if op:
        ax.plot([t for t, _r in op], [r for _t, r in op], color=_CAT_PALETTE[5],
                linewidth=2, linestyle="--", label="stranger impersonation rate")
    thr = visits.get("threshold")
    if thr is not None:
        ax.axvline(thr, color=_INK, linestyle="--", linewidth=1.5,
                   label=f"visit threshold {thr:.3f}")
    for name, value in sorted((visits.get("marks") or {}).items()):
        if value is None:
            continue
        ax.axvline(float(value), color=_MUTED, linestyle="-.", linewidth=1.2,
                   label=f"{name.replace('_', ' ')} {float(value):.3f}")
    ax.set_ylim(0, 1.02)
    ax.set_title("Visit-level accuracy vs. threshold", color=_INK, fontsize=11)
    ax.set_xlabel("cosine distance threshold", color=_MUTED, fontsize=9)
    ax.set_ylabel("share of each curve's own scored visits", color=_MUTED, fontsize=9)
    ax.tick_params(colors=_MUTED, labelsize=8)
    ax.legend(loc="best", fontsize=8, frameon=False)
    for spine in ax.spines.values():
        spine.set_color("#c3c2b7")
    return _fig_png(fig)


def _pct(v: "float | None") -> str:
    """A share as a percentage, or an em dash — never 0% for an absent measurement."""
    return "—" if v is None else f"{v:.0%}"


def _cell(cell: "dict | None") -> str:
    if cell is None:
        return '<td class="na">n/a</td>'
    return f"<td>{_pct(cell.get('accuracy'))}<span class=\"sub2\"> " \
           f"({cell.get('n_scored', 0)})</span></td>"


def _visit_section(metrics: dict, cat_names: "list[str]") -> str:
    """The report's headline section: the visit-held-out numbers, or why there are none."""
    v = metrics.get("visits")
    if v is None:
        return ""
    if not v.get("available"):
        why = {
            "uncalibrated_threshold": (
                "No cross-visit same-cat pair exists (every cat has a single visit), so "
                "the visit task cannot be calibrated. Nothing was measured — this is not "
                "a score of zero."
            ),
            "too_few_visits": (
                "Fewer than two visits were found, so no visit can be held out against "
                "the others. Nothing was measured."
            ),
        }.get(v.get("reason"), "The visit-level scoring could not run.")
        return (
            '<div class="verdict"><strong>Visit-level scoring unavailable.</strong> '
            f'{html.escape(why)}</div>'
        )

    unsc = v.get("unscoreable") or []
    unsc_txt = ""
    if unsc:
        named = ", ".join(
            f"{html.escape(cat_names[u['cat_index']] if u['cat_index'] < len(cat_names) else '?')}"
            f" ({u['n_visits']})"
            for u in unsc
        )
        unsc_txt = (
            f'<p class="sub">Excluded as unscoreable — only one visit, so the correct '
            f'answer is absent from the gallery and it could only ever be wrong: {named}. '
            f'Counting these as failures would understate the model and hide the cause.</p>'
        )

    regimes = v.get("regimes") or {}
    cross = v.get("cross") or {}
    regime_html = ""
    if regimes.get("day") or regimes.get("night"):
        rows = ""
        for name in ("day", "night"):
            r = regimes.get(name)
            rows += (
                f"<tr><td>{name} visits</td>"
                f"{_cell(cross.get(f'{name}_vs_day'))}"
                f"{_cell(cross.get(f'{name}_vs_night'))}"
                f"{_cell(r)}</tr>"
            )
        regime_html = f"""
  <h2 style="font-size:15px;">Day / night — and can one gallery span both?</h2>
  <table><tr><th></th><th>vs day-only gallery</th><th>vs night-only gallery</th>
    <th>vs mixed gallery</th></tr>{rows}</table>
  <p class="sub">Accuracy (visits scored). If night-vs-night is strong but night-vs-day
     collapses, the two regimes are separate spaces and the answer is separate day/night
     galleries — more night collection would not fix it. If night-vs-night is also weak,
     it is a data problem and collecting is the right response.</p>"""

    # Visit-level confusion: rows actual, columns predicted, final column "declined".
    # The actionable counterpart to the crop-level matrix — it says WHICH cat a visit gets
    # mistaken for, at the unit Run decides on.
    conf_html = ""
    conf = v.get("confusion") or []
    if conf and cat_names:
        head = "".join(f"<th>{html.escape(n)}</th>" for n in cat_names)
        body = ""
        # The zero-cell class is built OUTSIDE the f-string braces: a backslash inside an
        # f-string expression is a SyntaxError before Python 3.12 (PEP 701 lifted it), and
        # that fails at import time for the whole module — compute.ps1 accepts 3.10+.
        na = ' class="na"'
        for i, row in enumerate(conf):
            name = cat_names[i] if i < len(cat_names) else f"#{i}"
            cells = "".join(
                f"<td{na if not c else ''}>{c}</td>" for c in row[:len(cat_names)]
            )
            declined = row[len(cat_names)] if len(row) > len(cat_names) else 0
            body += (f"<tr><td>{html.escape(name)}</td>{cells}"
                     f"<td{na if not declined else ''}>{declined}</td></tr>")
        conf_html = f"""
  <h2 style="font-size:15px;">Which cat gets mistaken for which (visits)</h2>
  <table><tr><th>actual \\ named</th>{head}<th>declined</th></tr>{body}</table>"""

    n_groups = v.get("n_groups")
    lts = v.get("labeled_ts_groups")
    gap_s = int((v.get("gap_ms") or 0) / 1000)
    return f"""
  <div class="tiles">
    <div class="tile"><div class="v">{_pct(v.get('accuracy'))}</div>
      <div class="l">visit accuracy (correct of decided)</div></div>
    <div class="tile"><div class="v">{_pct(v.get('unknown_rate'))}</div>
      <div class="l">declined to name (unknown)</div></div>
    <div class="tile"><div class="v">{v.get('n_scored', 0)}</div>
      <div class="l">visits scored</div></div>
  </div>
  <div class="verdict"><strong>Read:</strong> {html.escape(_visit_verdict(v))}</div>
  <p class="sub">Each visit is hidden WHOLE and matched against the other visits, then
     named by Run's own rule — frames below the threshold vote, plurality wins, otherwise
     <em>unknown</em>. Correct {v.get('correct', 0)} · wrong {v.get('wrong', 0)} ·
     unknown {v.get('unknown', 0)} of {v.get('n_scored', 0)} scored.
     Grouped at a {gap_s}s gap into {n_groups} visits ({lts} distinct label commits —
     these should be close; a large divergence means the gap is wrong for this door).</p>
  {unsc_txt}
  <figure><img alt="visit accuracy vs threshold" src="{{curve}}"></figure>
  {conf_html}
  {regime_html}"""


def _visit_verdict(v: dict) -> str:
    acc = v.get("accuracy")
    if acc is None:
        return (
            "No visit was decided at this threshold — every one declined to name. The "
            "threshold, not the model, is what this reports."
        )
    if acc >= 0.9:
        return "Strong — a held-out visit is named correctly nearly every time."
    if acc >= 0.7:
        return "Usable — most held-out visits are named correctly, with real errors."
    return "Weak — a held-out visit is often named wrongly; this is the number that matters."


def _op_points(visits: dict, limit: "int | None" = None) -> "list[dict]":
    """The operating-point rows: known-cat curve joined to the stranger curve by threshold.

    Joined BY VALUE, not by position, and a point present on only one side is dropped —
    the two sweeps share one grid by construction (``_visits_block`` computes it once and
    hands it to every stranger pass), so an unpaired point means that guarantee broke, and
    lining two such curves up by index would silently quote one threshold's declines
    against another's impersonations.

    ``limit`` samples the result down to that many rows, always keeping the ends and the row
    at the headline threshold — that row is the one the report's other numbers were computed
    at, so a table that omitted it would not contain the reading it is meant to explain. The
    chart passes no limit: a line has room for the whole grid, a table does not.
    """
    strangers = visits.get("strangers") or {}
    if not strangers.get("available"):
        return []
    known = {float(p["threshold"]): p for p in (visits.get("curve") or [])}
    rows = [
        {"threshold": float(s["threshold"]), "known": known[float(s["threshold"])], "stranger": s}
        for s in (strangers.get("curve") or [])
        if float(s["threshold"]) in known
    ]
    if limit is None or len(rows) <= limit or limit < 2:
        return rows
    step = (len(rows) - 1) / float(limit - 1)
    keep = {int(round(i * step)) for i in range(limit)}
    head_t = visits.get("threshold")
    if head_t is not None:
        # Nearest row to the headline threshold — it is a grid mark, so this normally hits
        # it exactly; `min` rather than an equality test keeps the row present even if the
        # mark ever stops landing on a grid point.
        keep.add(min(range(len(rows)), key=lambda i: abs(rows[i]["threshold"] - head_t)))
    return [rows[i] for i in sorted(keep)]


def _stranger_section(metrics: dict) -> str:
    """Stranger rejection: how often an UNENROLLED cat is handed a known cat's name.

    The report's other sections all score cats the gallery contains, so every one of them
    is blind to this by construction — which is why this section states what it measures
    before it states a number, and why an unavailable block still renders a banner rather
    than nothing at all.
    """
    v = metrics.get("visits") or {}
    s = v.get("strangers")
    if not s:
        return ""
    if not s.get("available"):
        why = {
            "single_cat_gallery": (
                f"Holding one cat out of {s.get('n_cats', '?')} leaves a gallery of one, "
                "where every impersonation lands on the only remaining cat — that is a "
                "property of the arithmetic, not of the embedding. Nothing was measured. "
                "Three labelled cats is the floor."
            ),
            "no_cat_index": (
                "The run carried no cat index, so an impersonation could not be attributed "
                "to a cat or split by resident vs neighbour. Nothing was measured."
            ),
            "no_visits": "No held-out cat had a visit to score. Nothing was measured.",
        }.get(s.get("reason"), "The stranger scoring could not run.")
        return (
            '<h2 style="font-size:15px;">Stranger rejection</h2>'
            '<div class="verdict"><strong>Not measured.</strong> '
            f'{html.escape(why)}</div>'
        )

    split = s.get("resident_split")
    res_txt = _pct(s.get("resident_impersonation_rate")) if split else "—"
    rows = "".join(
        f"<tr><td>{html.escape(c.get('cat_name') or '?')}</td>"
        f"<td>{'resident' if c.get('is_resident') else ('neighbour' if c.get('is_resident') is False else '—')}</td>"
        f"<td>{c.get('n_scored', 0)}</td>"
        f"<td>{c.get('rejected', 0)}</td>"
        f"<td>{_pct(c.get('impersonation_rate'))}</td>"
        f"<td>{c.get('as_resident', 0)}</td>"
        f"<td>{html.escape(_named_as(c))}</td></tr>"
        for c in (s.get("per_cat") or [])
    )
    # The row attribute and the sub-span are built OUTSIDE the f-string braces: a
    # backslash inside an f-string expression is a SyntaxError before Python 3.12 (PEP 701
    # lifted it) and it fails at IMPORT for the whole module — compute.ps1 accepts 3.10+.
    op_rows = ""
    for r in _op_points(v, _OP_ROWS):
        head = ' style="font-weight:600;"' if _is_headline(r, v) else ""
        as_res = r["stranger"].get("as_resident", 0)
        op_rows += (
            f"<tr{head}><td>{r['threshold']:.3f}</td>"
            f"<td>{_pct(r['known'].get('accuracy'))}</td>"
            f"<td>{_share(r['known'].get('unknown'), v.get('n_scored'))}</td>"
            f"<td>{_share(r['stranger'].get('impersonated'), r['stranger'].get('n_scored'))}"
            f'<span class="sub2"> ({as_res} as a resident)</span></td></tr>'
        )
    # A cat that could not be held out at all has no reading, and a table that simply
    # omitted it would read as a roster of cats that were all measured.
    skipped = s.get("skipped") or []
    skipped_txt = "" if not skipped else (
        '<p class="sub">Not held out, so no number exists for them: '
        + ", ".join(f"{html.escape(str(k.get('cat_id')))} ({html.escape(str(k.get('reason')))})"
                    for k in skipped)
        + ".</p>"
    )
    op_html = "" if not op_rows else f"""
  <h2 style="font-size:15px;">Operating point — what a tighter threshold buys and costs</h2>
  <table><tr><th>threshold</th><th>known-cat recall</th><th>known-cat declines</th>
    <th>impersonations (resident)</th></tr>{op_rows}</table>
  <p class="sub">Both columns are swept over the SAME grid, so a row reads as one setting:
     at this cutoff, known cats are named correctly this often (of the visits it decided),
     it declines this share of them, and it hands a known cat's name to this share of the
     held-out ones. Recall excludes declines — they are the column beside it — so read the
     two together. The bold row is the threshold every other number in this report was
     computed at; the full grid is in <code>feasibility.json</code>.</p>"""

    return f"""
  <h2 style="font-size:15px;">Stranger rejection — is an unenrolled cat refused?</h2>
  <div class="tiles">
    <div class="tile"><div class="v">{_pct(s.get('impersonation_rate'))}</div>
      <div class="l">held-out visits given a known cat's name</div></div>
    <div class="tile"><div class="v">{res_txt}</div>
      <div class="l">…and given one of OUR cats' names</div></div>
    <div class="tile"><div class="v">{s.get('n_scored', 0)}</div>
      <div class="l">visits scored, over {s.get('n_cats_held_out', 0)} held-out cats</div></div>
  </div>
  <p class="sub">Each cat is removed from the gallery in turn and its own visits are
     re-scored against the rest. The correct answer is then <em>unknown</em> — there is no
     right name available — so declining is success and any name is an impersonation. A
     held-out neighbour stands in for a genuine stranger; a held-out resident stands in for
     one of ours before it has been enrolled. Both figures above are shares of the SAME
     denominator — held-out visits — so the second is not a fraction of the first.
     Rejected {s.get('rejected', 0)} · impersonated {s.get('impersonated', 0)}
     ({s.get('as_resident', 0)} as a resident, {s.get('as_neighbour', 0)} as a neighbour)
     of {s.get('n_scored', 0)}.
     {'' if split else 'The resident / neighbour split is UNAVAILABLE for this run — the roster flag did not reach the probe, so "named as one of ours" could not be counted and is shown as not measured rather than as zero.'}</p>
  <table><tr><th>held-out cat</th><th>kind</th><th>visits</th><th>rejected</th>
    <th>impersonation rate</th><th>as a resident</th><th>named as</th></tr>{rows}</table>
  {skipped_txt}
  {op_html}"""


def _is_headline(row: dict, visits: dict) -> bool:
    """Is this operating-point row the one at the run's own threshold?"""
    head = visits.get("threshold")
    return head is not None and abs(row["threshold"] - float(head)) < 1e-12


def _rate(part: "int | None", whole: "int | None") -> "float | None":
    """``part / whole``, or ``None`` when nothing was counted — never a zero standing in."""
    if part is None or not whole:
        return None
    return part / whole


def _share(part: "int | None", whole: "int | None") -> str:
    """``part`` of ``whole`` as a percentage — an em dash when the whole is unmeasured."""
    return _pct(_rate(part, whole))


def _named_as(row: dict) -> str:
    """The cats a held-out cat was mistaken for, biggest first — the actionable half.

    An impersonation RATE says how leaky the gallery is; WHO it leaked into is what a
    reader acts on (a lookalike pair is a different problem from a cat that attracts
    everything)."""
    named = sorted(row.get("named") or [], key=lambda n: -int(n["n"]))
    if not named:
        return "—"
    return ", ".join(f"{n.get('cat_name') or ('#' + str(n.get('cat_id')))} ×{n['n']}"
                     for n in named)


def _caps_section(metrics: dict) -> str:
    """The capped-gallery forecast: what a per-cat cap would do, without building one.

    Two columns per cap on purpose. ``cap_per_cat`` claims to fix two biases — the dominant
    cat's vectors blanketing the embedding space, and the threshold being calibrated from a
    distance distribution those cats' pairs dominate — so a forecast that recalibrated only,
    or held the threshold only, would answer half the question and read as the whole.
    """
    caps = (metrics.get("visits") or {}).get("caps")
    if not caps:
        return ""
    rows = ""
    for c in caps:
        recal, fixed = c.get("recalibrated"), c.get("fixed") or {}
        thr = c.get("threshold")
        # The DENOMINATOR, beside every rate read off it. Both columns score the same
        # visits, so one cell serves both — and a cap that made visits unscoreable is
        # exactly when its recall is not comparable to the row above, so the `+N` is not
        # decoration. Matches the runs table's `N +M` shape for the same distinction.
        col = recal or fixed
        n_scored, n_uns = col.get("n_scored"), col.get("n_unscoreable") or 0
        # Built OUTSIDE the f-string: the fragment needs an escaped double quote, and a
        # backslash inside `{}` is a SyntaxError before Python 3.12 (PEP 701 lifted it) —
        # which fails at IMPORT for the whole module, so `compute.api.app` will not start.
        # Entry 325, second instance: invisible on a 3.13 dev box, fatal on the 3.11
        # compute PC. `compute.ps1` accepts 3.10+, so this is the constraint, not 3.13.
        uns_html = f' <span class="sub">+{n_uns}</span>' if n_uns else ""
        rows += (
            f"<tr><td>{'uncapped' if c.get('max_per_cat') is None else c['max_per_cat']}</td>"
            f"<td>{c.get('n_vectors', 0)}</td>"
            f"<td>{c.get('n_cats', 0)}</td>"
            f"<td>{'—' if n_scored is None else n_scored}{uns_html}</td>"
            f"<td>{'—' if thr is None else f'{thr:.3f}'}</td>"
            f"<td>{_pct((recal or {}).get('accuracy'))}</td>"
            f"<td>{_pct((recal or {}).get('unknown_rate'))}</td>"
            f"<td>{_pct(fixed.get('accuracy'))}</td>"
            f"<td>{_pct(fixed.get('unknown_rate'))}</td></tr>"
        )
    return f"""
  <h2 style="font-size:15px;">If the gallery were capped per cat</h2>
  <table><tr><th>cap</th><th>vectors</th><th>cats</th><th>visits</th><th>threshold</th>
    <th colspan="2">recalibrated under the cap</th>
    <th colspan="2">at the uncapped threshold</th></tr>
  <tr><th></th><th></th><th></th><th></th><th></th><th>recall</th><th>declines</th>
    <th>recall</th><th>declines</th></tr>{rows}</table>
  <p class="sub">Each cap is the SAME selection a build would make (best grades first,
     spread over time), applied as a gallery mask over this run's own distances — so this
     forecasts the gallery you would get, not a second policy that could disagree with one.
     A dash under threshold means the survivors held no cross-visit same-cat pair to
     calibrate from, and only the fixed-threshold column is a reading.
     <b>Compare recall only between rows at the same visit count.</b> Two caveats pull
     opposite ways: where a cap happens to select crops from the visit being held out, that
     cat's gallery for that fold sits just below the cap, understating it — but a tight cap
     can also leave a cat's every surviving vector inside the held-out visit, which makes
     that visit unscoreable (the dim <span class="sub">+N</span>) and drops it from the
     denominator rather than counting it wrong. A recall that rose while the visit count
     fell has measured an easier set, not a better gallery.</p>"""


def _tldr_section(t: "dict | None", charts: dict) -> str:
    """The lead block: five numbers, what they cannot tell you, and two charts.

    Written because the report had grown five sections and a reader had no way to know
    which of them to act on — the honest number and an inflated one sat under similar
    headings, and nothing said what the whole probe structurally cannot measure. Every
    figure here is a re-reading of the visit block below, never a second measurement, so
    the summary can never disagree with the section it summarises.
    """
    if not t:
        return ""
    pct = lambda x: "—" if x is None else f"{x:.0%}"  # noqa: E731
    acc = pct(t["accuracy"])
    ci = t["ci"]
    # The interval is shown ON the headline, not beside it: the number's own width is
    # the thing a reader comparing two runs most needs and is least likely to seek out.
    # "pts", not "%": these are differences BETWEEN percentages, and "±7%" beside "88%"
    # invites reading it as 7% of 88. The unit is percentage points; saying so is cheap.
    ci_pts = (max(t["accuracy"] - ci[0], ci[1] - t["accuracy"]) * 100
              if ci and t["accuracy"] is not None else None)
    ci_txt = f"±{ci_pts:.0f} pts" if ci_pts is not None else ""
    weak = t["weakest"]
    pair = t["pair"]
    gap = t["night_gap"]

    tiles = [
        (f"{acc} <span style='font-size:15px;color:{_MUTED}'>{ci_txt}</span>",
         f"visits named correctly ({t['decided']} decided)"),
        (pct(t["declined_rate"]), "declined to name — not wrong, unnamed"),
    ]
    tiles.append(
        (f"{pct(weak['recall'])}", f"weakest: {html.escape(weak['cat_name'] or '?')}"
         f" ({weak['scored']} visits)") if weak else ("—", "weakest cat")
    )
    tiles.append(
        (f"{pair['count']}", f"worst mix-up: {html.escape(pair['true'])} named "
         f"{html.escape(pair['named'])}") if pair
        else ("0", "no cat was ever named as another")
    )
    if gap is not None:
        # Round BEFORE signing: a gap of -0.004 formats as "-0 pts", which reads as a
        # deficit that measured zero.
        gp = round(gap * 100)
        tiles.append((f"{gp:+d} pts" if gp else "0 pts", "night recall vs day"))

    tile_html = "".join(
        f'<div class="tile"><div class="v">{v}</div><div class="l">{l}</div></div>'
        for v, l in tiles
    )

    # What the numbers above structurally cannot say. Each line is a real limit of this
    # probe, not a disclaimer — the first one is the big one, and it is invisible in
    # every figure on the page.
    strangers = t.get("strangers")
    if strangers and strangers.get("available"):
        # Measured, so it stops being a limit and becomes a finding. Still stated here
        # rather than only in its own section: this block is where a reader takes their
        # numbers, and "the door refuses strangers this often" belongs beside them.
        # Both rates are over the SAME denominator — held-out visits — so the sentence says
        # so rather than writing "20% of them", which reads as a share of the 30% above it.
        as_res = (
            f", and {pct(strangers.get('resident_impersonation_rate'))} of them were given "
            "one of OUR cats' names — the direction that lets a stranger through the door"
            if strangers.get("resident_split") else
            ", though the resident / neighbour split could not be counted for this run"
        )
        limits = [
            f"<b>Strangers WERE tested: of {strangers.get('n_scored', 0)} held-out visits, "
            f"{pct(strangers.get('impersonation_rate'))} were given a known cat's name"
            f"{as_res}.</b> These are still real cats we have labelled, standing in "
            "for one we have not — a true stranger has never been seen by this door at all.",
        ]
    else:
        limits = [
            "<b>No strangers were tested.</b> Every crop here belongs to a labelled cat, so "
            "nothing measures whether a foreign cat is correctly refused — the half of the "
            "job the door actually needs. Read these as an optimistic bound."
            + (
                " The held-out-cat pass ran but could not be scored on this run "
                f"({html.escape(str((strangers or {}).get('reason') or 'unknown'))}); see "
                "the stranger-rejection section below."
                if strangers else ""
            ),
        ]
    if t["resolution"]:
        limits.append(
            f"<b>One visit is {t['resolution'] * 100:.1f} points.</b> With {t['decided']} "
            f"visits decided, a change smaller than the {ci_txt or '—'} above is sampling "
            "noise, not progress or regression — two runs a day apart differ by a handful "
            "of visits."
        )
    if t["unscoreable"]:
        names = ", ".join(html.escape(u["name"]) for u in t["unscoreable"])
        limits.append(
            f"<b>{names} could not be scored at all</b> — only one visit, so the correct "
            "answer was absent from the gallery it was matched against. Excluded from "
            "every number above; the fix is another visit, not more crops of that one."
        )
    limits.append(
        "<b>The crop-level numbers further down read high by construction</b> and are kept "
        "only to compare with runs recorded before visit scoring existed. The per-cat table "
        "down there is the crop-level one — not the same number as this section's."
    )

    figs = "".join(
        f'<figure><img alt="{alt}" src="{charts[key]}"></figure>'
        for key, alt in (("percat", "per-cat recall"), ("regime", "day vs night recall"))
        if charts.get(key)
    )
    return f"""
  <div class="tldr">
    <h2 style="font-size:15px;margin:0 0 10px;">In short</h2>
    <div class="tiles">{tile_html}</div>
    {figs}
    <div class="limits"><div class="l" style="margin-bottom:6px;">What this cannot tell you</div>
      <ul>{''.join(f'<li>{x}</li>' for x in limits)}</ul>
    </div>
  </div>
"""


def _render_html(metrics: dict, charts: dict, quality_label: str) -> str:
    knn = metrics["knn"]
    dist = metrics["distances"]
    auc = dist.get("auc")
    rows = "".join(
        f"<tr><td>{html.escape(c['cat_name'])}</td><td>{c['n']}</td>"
        f"<td>{knn['per_cat_recall'][i]:.0%}</td></tr>"
        for i, c in enumerate(metrics["cats"])
    )
    auc_txt = f"{auc:.3f}" if auc is not None else "—"
    thr = dist.get("suggested_threshold")
    thr_txt = f"{thr:.3f}" if thr is not None else "—"
    # Only shown when there is no visit-level section to lead with; a crop-level verdict
    # printed above the honest number is exactly the misreading this report now avoids.
    verdict = (
        "Strong separation — identification looks feasible." if knn["accuracy"] >= 0.85
        else "Partial separation — usable but needs better crops / more data / a stronger backbone."
        if knn["accuracy"] >= 0.6
        else "Weak separation — the cats are hard to tell apart in this embedding space."
    )
    cat_names = [c["cat_name"] for c in metrics["cats"]]
    tldr_html = _tldr_section(_tldr(metrics, cat_names), charts)
    visit_html = _visit_section(metrics, cat_names)
    if visit_html and "{curve}" in visit_html:
        visit_html = visit_html.replace("{curve}", charts.get("curve", ""))
    # Both sit directly under the visit block and above the demoted crop-level one: they
    # answer the questions that block cannot (a cat it never saw; a gallery it was not
    # built from), and both return "" when their feature was not requested.
    stranger_html = _stranger_section(metrics)
    caps_html = _caps_section(metrics)
    # With visit-held-out scoring present the crop-level block is DEMOTED: it stays (it is
    # the one number comparable with runs recorded before this existed) but it stops being
    # what a reader sees first, and it carries the explanation of why it reads high.
    #
    # Keyed on the scoring having actually SUCCEEDED, not on `_visit_section` returning any
    # HTML — it also returns a banner when the scoring was unavailable, and demoting on that
    # suppressed the crop-level verdict while telling the reader to compare against "the
    # visit-level number above", which in that case was never computed.
    visits_block = metrics.get("visits")
    demoted = bool(visits_block and visits_block.get("available"))
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Cat identification — feasibility</title>
<style>
  body {{ font: 14px system-ui, -apple-system, "Segoe UI", sans-serif; color: {_INK};
         background: #f9f9f7; margin: 0; padding: 24px; }}
  .wrap {{ max-width: 820px; margin: 0 auto; }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  .sub {{ color: {_MUTED}; margin: 0 0 20px; }}
  .tiles {{ display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 20px; }}
  .tile {{ background: #fcfcfb; border: 1px solid rgba(11,11,11,0.10); border-radius: 8px;
           padding: 14px 18px; min-width: 150px; }}
  .tile .v {{ font-size: 26px; font-weight: 600; }}
  .tile .l {{ color: {_MUTED}; font-size: 12px; }}
  .verdict {{ background: #fcfcfb; border: 1px solid rgba(11,11,11,0.10); border-radius: 8px;
              padding: 12px 16px; margin-bottom: 20px; }}
  figure {{ margin: 0 0 20px; }} img {{ max-width: 100%; height: auto; border-radius: 6px; }}
  table {{ border-collapse: collapse; font-size: 13px; }}
  th, td {{ text-align: left; padding: 4px 14px 4px 0; }}
  th {{ color: {_MUTED}; font-weight: 500; }}
  td {{ font-variant-numeric: tabular-nums; }}
  td.na {{ color: {_MUTED}; }}
  .sub2 {{ color: {_MUTED}; font-size: 11px; }}
  .demoted {{ border-top: 1px solid rgba(11,11,11,0.10); margin-top: 32px; padding-top: 20px; }}
  /* The lead block reads as one card so the eye takes it before the sections below,
     which are the detail behind it rather than five peers competing for attention. */
  .tldr {{ background: #ffffff; border: 1px solid rgba(11,11,11,0.14); border-radius: 10px;
           padding: 18px 20px 6px; margin-bottom: 28px; }}
  .tldr .tiles {{ margin-bottom: 18px; }}
  .tldr .tile {{ min-width: 128px; }}
  .limits {{ border-top: 1px solid rgba(11,11,11,0.08); padding: 12px 0 14px; }}
  .limits .l {{ color: {_MUTED}; font-size: 12px; }}
  .limits ul {{ margin: 0; padding-left: 18px; }}
  .limits li {{ margin-bottom: 6px; font-size: 13px; line-height: 1.45; }}
</style></head><body><div class="wrap">
  <h1>Can we tell our cats apart?</h1>
  <p class="sub">{metrics['n_crops']} labelled crops · {metrics['n_cats']} cats · quality: {html.escape(quality_label)} · DINOv2 embeddings</p>
  {tldr_html}
  {visit_html}
  {stranger_html}
  {caps_html}
  <div class="{'demoted' if demoted else ''}">
  <h2 style="font-size:15px;">{'Crop-level scoring (for comparison)' if demoted else 'Crop-level scoring'}</h2>
  {(
    '<p class="sub">These read HIGH and are not a forecast of recognition. Every crop is '
    'scored against all other crops with only itself excluded — and one door crossing '
    'yields tens of frames a tenth of a second apart, so a crop&rsquo;s nearest neighbour is '
    'almost always the adjacent frame of its own visit. That measures near-duplicate '
    'detection. The visit-level number above is the comparable one; these are kept because '
    'they are what earlier runs recorded.</p>'
  ) if demoted else ''}
  <div class="tiles">
    <div class="tile"><div class="v">{knn['accuracy']:.0%}</div><div class="l">kNN accuracy (leave-one-CROP-out, k={knn['k']})</div></div>
    <div class="tile"><div class="v">{auc_txt}</div><div class="l">separation AUC (1.0 = perfect)</div></div>
    <div class="tile"><div class="v">{thr_txt}</div><div class="l">suggested distance threshold</div></div>
  </div>
  {'' if demoted else f'<div class="verdict"><strong>Read:</strong> {html.escape(verdict)}</div>'}
  <figure><img alt="PCA scatter" src="{charts['scatter']}"></figure>
  <figure><img alt="kNN confusion matrix" src="{charts['confusion']}"></figure>
  <figure><img alt="distance histogram" src="{charts['hist']}"></figure>
  <h2 style="font-size:15px;">Per-cat</h2>
  <table><tr><th>cat</th><th>crops</th><th>recall (crop-level)</th></tr>{rows}</table>
  </div>
  <p class="sub" style="margin-top:20px;">Read-only diagnostic. No model was trained; DINOv2 is a pretrained,
     never-fine-tuned baseline. Weak results ≠ hopeless — they point at crop quality, data volume, or a fine-tune.</p>
</div></body></html>"""


def run_feasibility_probe(
    store,
    out_dir: str,
    qualities: "tuple[str, ...] | None" = None,
    exclude_cat_ids: "tuple[int, ...] | None" = None,
    progress: "object | None" = None,
    max_per_cat: "list[int | None] | tuple | None" = _DEFAULT_CAPS,
    strangers: bool = True,
    letterbox: bool = False,
    margin: float = 0.0,
) -> dict:
    """Run the probe over the store's ``identified`` crops → summary dict + report.

    ``qualities`` restricts which crop grades feed the probe (``None`` = every
    grade); ``progress`` is forwarded to ``Embedder.embed_paths`` as its
    ``progress(done, total)`` callback (which also carries the cancel signal — a
    falsy return raises ``EmbedCancelled``, left to propagate here).

    ``exclude_cat_ids`` leaves the named roster cats out of the scored set — the same
    per-build selection ``build_gallery`` takes, offered here because a validation run
    forecasts the gallery you would build at those grades: an exclusion applied only to
    the build is invisible to the number, and the errors it removes still appear. Echoed
    back in the summary so the caller can record WHICH cat set a run scored (a run over a
    different set is not comparable with one over the whole roster).

    ``strangers`` (on) adds the held-out-CAT pass: how often a cat the gallery has never
    seen is handed a known cat's name. On by default because a run without it measures only
    the half of the door's job the labelled set can see, which is the blind spot this whole
    scoring exists to close — it costs one extra scoring pass over a matrix already in
    memory, not a second embed.

    ``max_per_cat`` is a LIST of per-cat caps to forecast — ``(None, 2000, 1000, 500)`` by
    default, ``None``/``[]`` for no forecast. Each is applied through
    ``gallery.cap_per_cat`` (the build's own selection, not a second policy) and scored as
    a gallery mask over the same distances, with the threshold recalibrated under it.

    ``letterbox`` / ``margin`` select the crop GEOMETRY this run scores. Only crops whose
    ``geometry`` stamp matches are read — a run at a geometry embeds the pixels cut at that
    geometry, so a margin arm over legacy crops would report a margin it never applied
    (see ``build_gallery``, which filters the same way and for the same reason). The margin
    itself is NOT passed to the embedder: it is already baked into the crop file's pixels,
    and ``embed_paths`` rejects it outright rather than applying it twice.

    Guards the too-little-data cases instead of raising, returning a structured
    ``{'enough': False, 'reason': ..., 'message': ...}`` so the endpoint can surface
    an empty-state. ``reason`` distinguishes a benign cold-start
    (``'insufficient_labels'`` — fewer than 2 labelled crops/cats) from a genuine
    fault (``'decode_failure'`` — enough labels existed but too few crops decoded);
    the CLI maps the latter to a non-zero exit. On success it writes
    ``feasibility.{json,html}`` plus ``visit_outcomes.json`` under ``out_dir`` and returns
    ``{'enough': True, ...}`` with the headline metrics. Does NOT touch the DB.
    """
    quality_label = "all" if qualities is None else _quality_slug(qualities)
    excluded = tuple(sorted({int(c) for c in exclude_cat_ids})) if exclude_cat_ids else ()
    # Canonicalise + validate BEFORE the store read, as `build_gallery` does: a negative
    # margin is rejected here rather than silently shrinking a box, and the descriptor is
    # what the row stamps are compared against below.
    geometry = geometry_descriptor(bool(letterbox), float(margin))
    caps = tuple(max_per_cat) if max_per_cat else ()
    # active_only: score the CURRENT household. A retired cat is one we no longer
    # need to tell apart, and leaving it in would move the separability numbers
    # away from the gallery that actually gets built (which excludes it too).
    labels = store.labeled_crops(
        ("identified",), qualities, active_only=True, exclude_cat_ids=excluded or None
    )
    # Geometry filter in Python, not SQL: `labeled_crops` returns the stamp but has no
    # filter for it, and the canonical comparison (`m10` == `m10.0`, an unknown stamp
    # never matching) is not a `WHERE geometry = ?`. Like the exclusion it can drop whole
    # CATS, so it runs before the floor below, not after.
    n_all_geometries = len(labels)
    labels = [row for row in labels if canonical_geometry(row.get("geometry")) == geometry]
    n_other_geometry = n_all_geometries - len(labels)
    n_crops = len(labels)
    n_cats = len({row["cat_id"] for row in labels})
    if n_crops < 2 or n_cats < 2:
        return {
            "enough": False,
            "reason": "insufficient_labels",  # benign cold-start — nothing labelled yet
            "n_crops": n_crops,
            "n_cats": n_cats,
            "quality": quality_label,
            "geometry": geometry,
            "message": (
                f"Not enough labelled data yet: {n_crops} crops across {n_cats} cat(s). "
                + (
                    f"Label at least two cats, or re-include one of the {len(excluded)} "
                    "excluded cat(s)."
                    if excluded
                    else "Label at least two cats."
                )
                # Named separately, because "not enough labelled data" is a lie when the
                # operator has thousands of crops and merely asked for a geometry almost
                # none of them are cut at.
                + (
                    f" {n_other_geometry} labelled crop(s) are cut at a different geometry "
                    f"and were left out of this {geometry or 'legacy'} run."
                    if n_other_geometry
                    else ""
                )
            ),
        }

    # Letterbox only, never the margin: `embed_paths` reads whole crop FILES, whose pixels
    # already carry whatever margin they were cut at, and it rejects a non-zero margin
    # outright rather than applying it a second time. The geometry FILTER above is what
    # makes `margin` mean something here. `_geometry_kwargs` is imported from the gallery
    # (not re-derived) so a build and a probe at one geometry construct one embedder — and
    # imported inside the function because `gallery` imports this module at its top.
    from compute.identification.gallery import _geometry_kwargs

    embedder = Embedder(**_geometry_kwargs(bool(letterbox), 0.0))
    embedder.prepare()
    emb, kept = embedder.embed_paths([row["crop_path"] for row in labels], progress=progress)
    kept_labels = [labels[i] for i in kept]
    # Decode failures can collapse the *surviving* crops below the separability
    # floor even though the pre-embed counts passed — e.g. every crop of one cat is
    # corrupt. Re-check both floors on the decoded set (not just crop count) so
    # ``run_feasibility`` never raises: too few crops OR fewer than 2 distinct cats
    # among the decoded crops both degrade to a graceful ``enough: False``.
    n_decoded = int(emb.shape[0])
    n_decoded_cats = len({row["cat_id"] for row in kept_labels})
    if n_decoded < 2 or n_decoded_cats < 2:
        return {
            "enough": False,
            "reason": "decode_failure",  # had enough labels but crops wouldn't decode — a real fault
            "n_crops": n_crops,
            "n_cats": n_cats,
            "quality": quality_label,
            "geometry": geometry,
            "message": (
                f"Only {n_decoded} crops across {n_decoded_cats} cat(s) decoded — "
                "cannot measure separability."
            ),
        }

    cat_ids = [row["cat_id"] for row in kept_labels]
    cat_names = {row["cat_id"]: (row["cat_name"] or f"cat #{row['cat_id']}") for row in kept_labels}
    # The roster flag, carried per cat so an impersonation can be split by whether the
    # impersonated cat is one of OURS. Read off the same rows the crops came from, so it
    # cannot disagree with the crop it describes.
    cat_residents = {row["cat_id"]: row.get("is_resident") for row in kept_labels}
    # Visit-held-out scoring rides the SAME embeddings and the same distance matrix — the
    # expensive part is the embed above, so the honest number costs numpy, not GPU.
    is_night = _night_classifier(store)
    groups, nights = group_visits(kept_labels, is_night)
    # `group_visits` sorts each cat's rows by `src_recv_ts` before splitting, so a group's
    # FIRST index is its earliest crop. Half of the `(cat_id, first_src_recv_ts)` key two
    # runs' per-visit outcomes are joined on; it has to come from here because
    # `feasibility.py` is deliberately pure-numpy and holds no timestamps.
    visit_meta = [{"first_src_recv_ts": int(kept_labels[G[0]]["src_recv_ts"])} for G in groups]
    active = store.active_model()
    marks = {}
    if active and active.get("threshold") is not None:
        marks["active_model"] = float(active["threshold"])
    metrics = run_feasibility(
        cat_ids,
        cat_names,
        emb,
        visit_groups=groups,
        visit_night=nights,
        # Run's own verdict rule, injected rather than imported so the metrics layer keeps
        # no dependency on the store — and so the probe cannot drift from what Run does.
        aggregate=Store._aggregate_identity,
        extra_thresholds=marks,
        labeled_ts_groups=len({row["labeled_ts"] for row in kept_labels}),
        gap_ms=_HELDOUT_GAP_MS,
        cat_residents=cat_residents,
        visit_meta=visit_meta,
        strangers=strangers,
        cap_masks=_cap_masks(kept_labels, caps),
    )
    # OUT of the metrics dict before anything persists it. `feasibility_runs.metrics` rows
    # are kept indefinitely and the Model page parses 100 of them on every load, so a
    # per-visit list — which grows with the door's traffic — belongs in the run dir, which
    # `prune_feasibility_reports` bounds at 25.
    outcomes = (metrics.get("visits") or {}).pop("outcomes", None)

    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "feasibility.json"), "w", encoding="utf-8") as fh:
        json.dump(metrics, fh, indent=2)
    if outcomes is not None:
        # Self-describing: a paired comparison reads two of these and has to know which arm
        # each one is, and the run dir name alone does not say.
        with open(os.path.join(out_dir, "visit_outcomes.json"), "w", encoding="utf-8") as fh:
            json.dump({
                "geometry": geometry,
                "quality": quality_label,
                "excluded_cat_ids": list(excluded) or None,
                "gap_ms": _HELDOUT_GAP_MS,
                "threshold": (metrics.get("visits") or {}).get("threshold"),
                "n": len(outcomes),
                "visits": outcomes,
            }, fh, indent=2)
    charts = {"scatter": _scatter_png(metrics), "confusion": _confusion_png(metrics), "hist": _hist_png(metrics)}
    visits_block = metrics.get("visits")
    if visits_block and visits_block.get("available"):
        charts["curve"] = _curve_png(visits_block)
        # Both return "" when their data isn't there (no per_cat on an older shape, no
        # day/night without a location), and _tldr_section skips a chart it has no PNG
        # for — so the lead block shrinks rather than showing an empty frame.
        charts["percat"] = _percat_png(visits_block)
        charts["regime"] = _regime_png(visits_block)
    with open(os.path.join(out_dir, "feasibility.html"), "w", encoding="utf-8") as fh:
        fh.write(_render_html(metrics, charts, quality_label))

    dist = metrics["distances"]
    return {
        "enough": True,
        "n_crops": metrics["n_crops"],
        "n_cats": metrics["n_cats"],
        "knn_accuracy": metrics["knn"]["accuracy"],
        "auc": dist.get("auc"),
        "threshold": dist.get("suggested_threshold"),
        "quality": quality_label,
        "report_dir": out_dir,
        # The honest headline, lifted for the caller to persist as the run's `metrics`
        # (see TrainingManager._run_feasibility) and to show in the runs table.
        "visits": metrics.get("visits"),
        # WHICH cat set this run scored, persisted alongside `visits` — a run that
        # excluded a cat is not comparable with one over the whole roster, and that has
        # to be visible in the runs row rather than only in the request that made it.
        "excluded_cat_ids": list(excluded) or None,
        # And WHICH crop convention it scored. Same reason, one level sharper: two
        # geometry arms are compared against each other, so a run that does not name its
        # own geometry is not auditable after the fact. `None` = legacy.
        "geometry": geometry,
        "n_other_geometry": n_other_geometry,
    }
