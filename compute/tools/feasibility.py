"""Run the Phase-1 feasibility probe over labelled crops and write a report.

    python -m compute.tools.feasibility [OUT_DIR]

Offline, read-only against the store (it never writes frames/labels). It reads the
``identified`` crops via ``Store.labeled_crops``, embeds them with the DINOv2
``Embedder`` (first run downloads the backbone), computes the separability
scorecard (``compute.identification.feasibility.run_feasibility``), and writes:

  <OUT_DIR>/feasibility.json   — raw metrics
  <OUT_DIR>/feasibility.html   — self-contained report (charts inlined as base64)

and prints a one-screen summary. OUT_DIR defaults to ``<CAT_COLLECT_DIR>/feasibility``.

Charts follow the dataviz method: cat identity is the validated categorical palette
(fixed slot order, colourblind-safe), the confusion matrix is a single-hue blue
sequential ramp, and the distance histogram is two distinct hues (same vs different)
with the suggested threshold marked. Runs on the compute PC (labels + GPU live
there); the dev box has no real labelled data.
"""
from __future__ import annotations

import base64
import html
import io
import json
import os
import sys

from compute.collection.store import Store
from compute.identification.embed import Embedder
from compute.identification.feasibility import run_feasibility

# Validated light-mode categorical palette (fixed slot order — see the dataviz
# reference palette; worst adjacent CVD ΔE 24.2). Identity is assigned in this
# order, never cycled; a >8-cat run reuses slots with a legend note (see below).
_CAT_PALETTE = ["#2a78d6", "#1baf7a", "#eda100", "#008300", "#4a3aa7", "#e34948", "#e87ba4", "#eb6834"]
_SAME_HUE = "#2a78d6"   # blue — same-cat pair distances
_DIFF_HUE = "#eb6834"   # orange — different-cat pair distances
_INK = "#0b0b0b"
_MUTED = "#898781"


def _store_from_env() -> Store:
    root = os.environ.get("CAT_COLLECT_DIR", "data/collection")
    # max_bytes is irrelevant to a read-only pass (no inserts → no eviction); pass
    # a large cap. dataset_root defaults to <root>/dataset, matching the app.
    return Store(
        db_path=os.path.join(root, "index.db"),
        media_root=os.path.join(root, "media"),
        max_bytes=1 << 60,
    )


def _fig_png(fig) -> str:
    """Render a matplotlib figure to a base64 data-URI PNG and close it."""
    import matplotlib.pyplot as plt

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, bbox_inches="tight", facecolor="#fcfcfb")
    plt.close(fig)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _scatter_png(metrics: dict) -> str:
    import matplotlib.pyplot as plt

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
    import matplotlib.pyplot as plt
    import numpy as np

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
    import matplotlib.pyplot as plt
    import numpy as np

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


def _render_html(metrics: dict, charts: dict) -> str:
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
    verdict = (
        "Strong separation — identification looks feasible." if knn["accuracy"] >= 0.85
        else "Partial separation — usable but needs better crops / more data / a stronger backbone."
        if knn["accuracy"] >= 0.6
        else "Weak separation — the cats are hard to tell apart in this embedding space."
    )
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
</style></head><body><div class="wrap">
  <h1>Can we tell our cats apart?</h1>
  <p class="sub">{metrics['n_crops']} labelled crops · {metrics['n_cats']} cats · DINOv2 embeddings · leave-one-out kNN (k={knn['k']})</p>
  <div class="tiles">
    <div class="tile"><div class="v">{knn['accuracy']:.0%}</div><div class="l">kNN accuracy</div></div>
    <div class="tile"><div class="v">{auc_txt}</div><div class="l">separation AUC (1.0 = perfect)</div></div>
    <div class="tile"><div class="v">{thr_txt}</div><div class="l">suggested distance threshold</div></div>
  </div>
  <div class="verdict"><strong>Read:</strong> {html.escape(verdict)}</div>
  <figure><img alt="PCA scatter" src="{charts['scatter']}"></figure>
  <figure><img alt="kNN confusion matrix" src="{charts['confusion']}"></figure>
  <figure><img alt="distance histogram" src="{charts['hist']}"></figure>
  <h2 style="font-size:15px;">Per-cat</h2>
  <table><tr><th>cat</th><th>crops</th><th>recall</th></tr>{rows}</table>
  <p class="sub" style="margin-top:20px;">Read-only diagnostic. No model was trained; DINOv2 is a pretrained,
     never-fine-tuned baseline. Weak results ≠ hopeless — they point at crop quality, data volume, or a fine-tune.</p>
</div></body></html>"""


def main(argv: "list[str]") -> int:
    store = _store_from_env()
    out_dir = argv[1] if len(argv) > 1 else os.path.join(store.dataset_root, os.pardir, "feasibility")
    out_dir = os.path.abspath(out_dir)

    labels = store.labeled_crops(("identified",))
    n_cats = len({row["cat_id"] for row in labels})
    if len(labels) < 2 or n_cats < 2:
        print(
            f"Not enough labelled data yet: {len(labels)} identified crops across {n_cats} cat(s).\n"
            "Label at least two cats (a handful of crops each) on the Annotate page, then re-run."
        )
        return 0

    print(f"Embedding {len(labels)} crops across {n_cats} cats … (first run downloads DINOv2)")
    embedder = Embedder()
    embedder.prepare()
    emb, kept = embedder.embed_paths([row["crop_path"] for row in labels])
    if emb.shape[0] < 2:
        print(f"Only {emb.shape[0]} crops decoded — cannot measure separability.")
        return 1
    kept_labels = [labels[i] for i in kept]
    cat_ids = [row["cat_id"] for row in kept_labels]
    cat_names = {row["cat_id"]: (row["cat_name"] or f"cat #{row['cat_id']}") for row in kept_labels}

    metrics = run_feasibility(cat_ids, cat_names, emb)

    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "feasibility.json"), "w", encoding="utf-8") as fh:
        json.dump(metrics, fh, indent=2)
    charts = {"scatter": _scatter_png(metrics), "confusion": _confusion_png(metrics), "hist": _hist_png(metrics)}
    html_path = os.path.join(out_dir, "feasibility.html")
    with open(html_path, "w", encoding="utf-8") as fh:
        fh.write(_render_html(metrics, charts))

    knn, dist = metrics["knn"], metrics["distances"]
    auc = dist.get("auc")
    print(f"\nkNN accuracy: {knn['accuracy']:.1%}   separation AUC: {auc:.3f}" if auc is not None
          else f"\nkNN accuracy: {knn['accuracy']:.1%}")
    for i, c in enumerate(metrics["cats"]):
        print(f"  {c['cat_name']:<16} {c['n']:>4} crops   recall {knn['per_cat_recall'][i]:.0%}")
    print(f"\nReport: {html_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
