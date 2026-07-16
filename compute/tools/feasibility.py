"""Run the Phase-1 feasibility probe over labelled crops and write a report.

    python -m compute.tools.feasibility [OUT_DIR] [--quality gallery[,ok[,poor]]]

Offline, read-only against the store (it never writes frames/labels). It is a thin
CLI wrapper over ``compute.identification.probe.run_feasibility_probe`` — the same
orchestrator the Training-page API calls — which reads the ``identified`` crops via
``Store.labeled_crops``, embeds them with the DINOv2 ``Embedder`` (first run
downloads the backbone), computes the separability scorecard, and writes:

  <OUT_DIR>/feasibility.json   — raw metrics
  <OUT_DIR>/feasibility.html   — self-contained report (charts inlined as base64)

This tool then prints a one-screen summary. OUT_DIR defaults to
``<CAT_COLLECT_DIR>/feasibility`` (latest-only — the API's timestamped per-run dirs
are the manager's job, not this diagnostic's).

Charts follow the dataviz method: cat identity is the validated categorical palette
(fixed slot order, colourblind-safe), the confusion matrix is a single-hue blue
sequential ramp, and the distance histogram is two distinct hues (same vs different)
with the suggested threshold marked. Runs on the compute PC (labels + GPU live
there); the dev box has no real labelled data.

``--quality`` restricts which crop grades feed the probe, so you can A/B whether
crop quality is the separability bottleneck: ``--quality gallery`` embeds only the
clean gallery crops (what a real gallery build would use), the default (flag
omitted) embeds every ``identified`` crop regardless of grade. A quality-filtered
run writes to ``feasibility-<slug>`` (e.g. ``feasibility-gallery``) so it sits
beside the all-crops report instead of overwriting it; an explicit OUT_DIR wins.
"""
from __future__ import annotations

import json
import os
import sys

from compute.collection.store import Store, _QUALITIES
from compute.identification.probe import _quality_slug, run_feasibility_probe


def _store_from_env() -> Store:
    root = os.environ.get("CAT_COLLECT_DIR", "data/collection")
    # max_bytes is irrelevant to a read-only pass (no inserts → no eviction); pass
    # a large cap. dataset_root defaults to <root>/dataset, matching the app.
    return Store(
        db_path=os.path.join(root, "index.db"),
        media_root=os.path.join(root, "media"),
        max_bytes=1 << 60,
    )


def main(argv: "list[str]") -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="feasibility", description="Phase-1 cat-identity separability probe (read-only)."
    )
    parser.add_argument(
        "out_dir", nargs="?", default=None,
        help="output dir (default <CAT_COLLECT_DIR>/feasibility, or …-<quality> when --quality is set)",
    )
    parser.add_argument(
        "--quality", default=None, metavar="GRADES",
        help=f"comma-separated crop grades to include ({'/'.join(_QUALITIES)}); "
             "default all. e.g. --quality gallery embeds only the clean gallery crops",
    )
    ns = parser.parse_args(argv[1:])

    qualities: "tuple[str, ...] | None" = None
    if ns.quality is not None:
        qualities = tuple(q.strip() for q in ns.quality.split(",") if q.strip())
        bad = [q for q in qualities if q not in _QUALITIES]
        if bad or not qualities:
            parser.error(f"--quality must be a comma-separated subset of {_QUALITIES}, got {ns.quality!r}")
    quality_label = "all" if qualities is None else _quality_slug(qualities)

    store = _store_from_env()
    if ns.out_dir is not None:
        out_dir = ns.out_dir
    else:
        name = "feasibility" if qualities is None else f"feasibility-{quality_label}"
        out_dir = os.path.join(store.dataset_root, os.pardir, name)
    out_dir = os.path.abspath(out_dir)

    # Cheap pre-count so the console shows the "Embedding N crops …" download hint
    # only when there is actually enough to embed; the probe re-reads authoritatively
    # and owns the cold-start verdict (its structured result, printed below).
    n_crops, n_cats = store.count_identified_crops(qualities)
    if n_crops >= 2 and n_cats >= 2:
        print(f"Embedding {n_crops} crops across {n_cats} cats (quality: {quality_label}) … "
              "(first run downloads DINOv2)")

    result = run_feasibility_probe(store, out_dir, qualities=qualities)
    if not result["enough"]:
        print(result["message"])
        # A genuine fault — enough labels existed but too few crops decoded — keeps the
        # pre-refactor non-zero exit so a CI/cron wrapper keying on $? still detects a
        # broken dataset; a benign cold-start (nothing labelled yet) stays 0.
        return 1 if result.get("reason") == "decode_failure" else 0

    with open(os.path.join(out_dir, "feasibility.json"), encoding="utf-8") as fh:
        metrics = json.load(fh)
    html_path = os.path.join(out_dir, "feasibility.html")

    knn, dist = metrics["knn"], metrics["distances"]
    auc = dist.get("auc")
    print(f"\nquality: {quality_label}")
    print(f"kNN accuracy: {knn['accuracy']:.1%}   separation AUC: {auc:.3f}" if auc is not None
          else f"kNN accuracy: {knn['accuracy']:.1%}")
    for i, c in enumerate(metrics["cats"]):
        print(f"  {c['cat_name']:<16} {c['n']:>4} crops   recall {knn['per_cat_recall'][i]:.0%}")
    print(f"\nReport: {html_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
