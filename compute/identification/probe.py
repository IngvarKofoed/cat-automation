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
import os

from compute.collection.store import _QUALITIES, Store
from compute.identification.embed import Embedder
from compute.identification.feasibility import run_feasibility

# Gap at which a cat's labelled crops are split into separate held-out VISITS. Its own
# constant, deliberately much coarser than the store's `_VISIT_GAP_MS` (2 s), because
# coarse is the fail-safe direction HERE: over-merging two visits removes more of the
# near-duplicate leakage the held-out scoring exists to eliminate, while under-merging
# splits one physical visit across the boundary and lets that leakage straight back in.
# A detection dropout mid-visit (the cat sits still, YOLO drops it for a few seconds)
# would produce exactly such a split at 2 s.
_HELDOUT_GAP_MS = 60_000

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
    ax.set_ylabel("share of scored visits", color=_MUTED, fontsize=9)
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
    visit_html = _visit_section(metrics, cat_names)
    if visit_html and "{curve}" in visit_html:
        visit_html = visit_html.replace("{curve}", charts.get("curve", ""))
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
</style></head><body><div class="wrap">
  <h1>Can we tell our cats apart?</h1>
  <p class="sub">{metrics['n_crops']} labelled crops · {metrics['n_cats']} cats · quality: {html.escape(quality_label)} · DINOv2 embeddings</p>
  {visit_html}
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
    progress: "object | None" = None,
) -> dict:
    """Run the probe over the store's ``identified`` crops → summary dict + report.

    ``qualities`` restricts which crop grades feed the probe (``None`` = every
    grade); ``progress`` is forwarded to ``Embedder.embed_paths`` as its
    ``progress(done, total)`` callback (which also carries the cancel signal — a
    falsy return raises ``EmbedCancelled``, left to propagate here).

    Guards the too-little-data cases instead of raising, returning a structured
    ``{'enough': False, 'reason': ..., 'message': ...}`` so the endpoint can surface
    an empty-state. ``reason`` distinguishes a benign cold-start
    (``'insufficient_labels'`` — fewer than 2 labelled crops/cats) from a genuine
    fault (``'decode_failure'`` — enough labels existed but too few crops decoded);
    the CLI maps the latter to a non-zero exit. On success it writes
    ``feasibility.{json,html}`` under ``out_dir`` and returns ``{'enough': True, ...}``
    with the headline metrics. Does NOT touch the DB.
    """
    quality_label = "all" if qualities is None else _quality_slug(qualities)
    # active_only: score the CURRENT household. A retired cat is one we no longer
    # need to tell apart, and leaving it in would move the separability numbers
    # away from the gallery that actually gets built (which excludes it too).
    labels = store.labeled_crops(("identified",), qualities, active_only=True)
    n_crops = len(labels)
    n_cats = len({row["cat_id"] for row in labels})
    if n_crops < 2 or n_cats < 2:
        return {
            "enough": False,
            "reason": "insufficient_labels",  # benign cold-start — nothing labelled yet
            "n_crops": n_crops,
            "n_cats": n_cats,
            "quality": quality_label,
            "message": (
                f"Not enough labelled data yet: {n_crops} crops across {n_cats} cat(s). "
                "Label at least two cats."
            ),
        }

    embedder = Embedder()
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
            "message": (
                f"Only {n_decoded} crops across {n_decoded_cats} cat(s) decoded — "
                "cannot measure separability."
            ),
        }

    cat_ids = [row["cat_id"] for row in kept_labels]
    cat_names = {row["cat_id"]: (row["cat_name"] or f"cat #{row['cat_id']}") for row in kept_labels}
    # Visit-held-out scoring rides the SAME embeddings and the same distance matrix — the
    # expensive part is the embed above, so the honest number costs numpy, not GPU.
    is_night = _night_classifier(store)
    groups, nights = group_visits(kept_labels, is_night)
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
    )

    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "feasibility.json"), "w", encoding="utf-8") as fh:
        json.dump(metrics, fh, indent=2)
    charts = {"scatter": _scatter_png(metrics), "confusion": _confusion_png(metrics), "hist": _hist_png(metrics)}
    visits_block = metrics.get("visits")
    if visits_block and visits_block.get("available"):
        charts["curve"] = _curve_png(visits_block)
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
    }
