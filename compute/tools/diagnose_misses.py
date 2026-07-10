"""Characterize the edge MOG2 gate's *misses* against a ground-truth oracle — read-only.

Run this on the compute box (where the frame store lives) AFTER an oracle sweep
(YOLO or BSUV), to answer the question the raw "N missed out of M analyzed" count
can't: *do the misses matter, and if so which MOG2 knob is wrong?*

This is now a THIN CLI wrapper over ``Store.gate_scorecard(source="live", ...)`` —
the same aggregate the dashboard's ``/api/tuning/compare`` route calls (see
``compute/api/app.py``) — so the recall / false-trigger / area-bucket / visit
computation lives in exactly ONE place (``compute/collection/store.py``) and this
tool only builds the ``Store``, gathers thresholds from CLI flags, and prints the
result. It calls no ``Store`` method that writes (``gate_scorecard`` and
``analysis_summary`` are pure reads), so it stays safe to run anytime, including
while the collector keeps writing — even though, unlike the old hand-rolled
``mode=ro`` SQLite connection, the ``Store`` it builds opens its connection in the
normal (writable) mode. The read-only guarantee here is "this tool never calls a
write method," not a DB-enforced one.

A "miss" is a frame where the edge gate stayed still (``frames.motion = 0``) but
the oracle saw the subject (``analysis.verdict = 1`` for that oracle) — exactly
the browse UI's "Missed?" disagreement preset. For each miss the frame row already
carries the *reason* MOG2 stayed quiet: the largest blob ``area`` it measured that
frame. Compared against the live gate thresholds, that area says which of the
three gate conditions rejected the frame (sub-min_area / over-max_area /
debounced), which in turn says which knob to turn. See
``edge/server/grabber.py::_compute_motion``.

IMPORTANT: the thresholds live on the *Pi* (edge settings.json), not here, so pass
the Pi's *current* values as flags if you've tuned them; the defaults below mirror
``edge/config/settings.py``. The script prints the values it used — confirm they
match the Pi before trusting the bucket classification.

Visit clustering's gap/window (how far apart two present frames can be before
they're different "visits", and how close a gate firing must be to count as
catching one) are no longer CLI flags: they're fixed constants inside
``Store.gate_scorecard`` (``_VISIT_GAP_MS`` / ``_VISIT_WINDOW_MS``) now that the
clustering itself moved there — see that module for the rationale.

Usage (from the repo root, in the compute venv or any Python 3.10+):
    python -m compute.tools.diagnose_misses
    python -m compute.tools.diagnose_misses --min-area 0.006 --persistence 2
    python -m compute.tools.diagnose_misses --oracle bsuv --warmup 0
    CAT_COLLECT_DIR=D:/cat/data python -m compute.tools.diagnose_misses
"""
from __future__ import annotations

import argparse
import os

from compute.analysis import ANALYZER_NAMES
from compute.collection.store import Store

# Mirror edge/config/settings.py DEFAULTS — overridable by flag because the Pi's
# live settings.json may have been tuned away from these.
_DEF_MIN_AREA = 0.01
_DEF_MAX_AREA = 0.6
_DEF_PERSISTENCE = 2
_DEF_ORACLE = "yolo"
# Matches Store.gate_scorecard's own default, restated here so --help shows it.
_DEF_WARMUP = 500

# Mirrors compute/api/app.py's _store_from_env — same env vars, same defaults, so
# this tool and the dashboard always point at the same store without a shared
# import (the compute tier has no config module yet; see app.py's own note on
# duplicating the edge's motion defaults by hand for the same reason).
_ENV_DIR = "CAT_COLLECT_DIR"
_ENV_MAX_BYTES = "CAT_COLLECT_MAX_BYTES"
_DEFAULT_DIR = "./data/collection"
_DEFAULT_MAX_BYTES = 5368709120  # 5 GiB


def _resolve_root(root_override: "str | None") -> str:
    """The store root: ``--dir`` if given, else ``$CAT_COLLECT_DIR``, else the default."""
    return root_override if root_override is not None else os.environ.get(_ENV_DIR, _DEFAULT_DIR)


def _build_store(root: str) -> Store:
    """Build the same ``Store`` the dashboard uses, under ``root``.

    Checks the index DB exists FIRST and raises a friendly ``SystemExit`` if not —
    ``Store.__init__`` would otherwise happily create an empty DB/media tree under
    a root that was never actually collected into, silently masking a
    misconfigured ``CAT_COLLECT_DIR``/``--dir``.
    """
    db_path = os.path.join(root, "index.db")
    if not os.path.exists(db_path):
        raise SystemExit(f"no store DB at {db_path!r} (set --dir or {_ENV_DIR} to its parent)")
    try:
        max_bytes = int(os.environ.get(_ENV_MAX_BYTES, _DEFAULT_MAX_BYTES))
    except ValueError:
        max_bytes = _DEFAULT_MAX_BYTES
    return Store(db_path=db_path, media_root=os.path.join(root, "media"), max_bytes=max_bytes)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=None, help=f"store root (default: ${_ENV_DIR} or {_DEFAULT_DIR!r})")
    ap.add_argument("--oracle", default=_DEF_ORACLE, choices=list(ANALYZER_NAMES))
    ap.add_argument("--min-area", type=float, default=_DEF_MIN_AREA)
    ap.add_argument("--max-area", type=float, default=_DEF_MAX_AREA)
    ap.add_argument("--persistence", type=int, default=_DEF_PERSISTENCE)
    ap.add_argument("--warmup", type=int, default=_DEF_WARMUP, help="cold-start prefix (by id) to exclude")
    args = ap.parse_args()

    root = _resolve_root(args.dir)
    store = _build_store(root)

    stats = store.stats()
    raw = store.analysis_summary(args.oracle)
    print(f"DB: {os.path.join(root, 'index.db')}")
    print(f"  frames stored: {stats['count']}  (motion=1: {stats['motion_count']})")
    print(f"  {args.oracle} verdicts (all-time): analyzed={raw['analyzed']}  cat-present={raw['present']}")
    print("thresholds used (CONFIRM against the Pi's settings.json):")
    print(f"  min_area={args.min_area}  max_area_fraction={args.max_area}  persistence={args.persistence}  warmup={args.warmup}")

    if raw["analyzed"] == 0:
        raise SystemExit(f"no {args.oracle} verdicts yet — run a {args.oracle} sweep first.")

    card = store.gate_scorecard(
        "live",
        args.oracle,
        warmup=args.warmup,
        min_area=args.min_area,
        max_area=args.max_area,
        persistence=args.persistence,
    )

    present = card["present"]
    print(f"  scored past warmup: analyzed={card['analyzed']}  cat-present={present}")
    if present == 0:
        raise SystemExit(
            "no cat-present frames past the warmup prefix — lower --warmup or collect/sweep more data."
        )

    recall = card["recall"]
    print("\n== frame-level gate recall on cat-present frames ==")
    print(f"  caught (motion=1): {recall['caught']}")
    print(f"  MISSED (motion=0): {recall['missed']}")
    print(f"  recall = {recall['caught']}/{present} = {recall['rate']:.1%}")
    print(f"  false triggers (motion=1, oracle says no {args.oracle} subject): {card['false_triggers']['count']}")

    conf = card["confidence"]
    print(f"\n== missed-frame {args.oracle} confidence (borderline = maybe oracle noise, not a gate fault) ==")
    print(f"  >=0.50 confident      : {conf['high']}")
    print(f"  0.30-0.50             : {conf['medium']}")
    print(f"  <0.30 / no score      : {conf['low']}")

    buckets = card["area_buckets"]
    print("\n== why the gate stayed still (stored blob area vs thresholds) -> which knob ==")
    print(
        f"  area < min_area ({args.min_area}):           {buckets['below_min']}"
        f"   -> blob too small / MOG2 saw ~nothing => lower min_area; if area~0, lower learning_rate/var_threshold"
    )
    if buckets["below_min"] > 0:
        near_zero_threshold = args.min_area / 10.0
        verdict = (
            "mostly nothing-seen -> sensitivity/learning_rate"
            if buckets["near_zero"] > buckets["below_min"] / 2
            else "mostly small blobs -> min_area"
        )
        print(f"     of those, area~0 (<{near_zero_threshold:g}): {buckets['near_zero']}   ({verdict})")
    print(
        f"  area > max_area ({args.max_area}):            {buckets['above_max']}"
        f"   -> close pass clipped as illumination => raise max_area_fraction"
    )
    print(
        f"  in band ({args.min_area}..{args.max_area}), still: {buckets['in_band']}"
        f"   -> raw motion present but < persistence({args.persistence}) consecutive => lower persistence"
    )

    visits = card["visits"]
    print("\n== visit-level recall (bursts of oracle cat-present frames) -- the misses that actually matter ==")
    print(f"  distinct visits: {visits['total']}")
    print(f"  caught (gate fired at some point during the visit): {visits['caught']}")
    print(f"  WHOLLY MISSED visits (gate never fired -> the real problem): {visits['wholly_missed']}")
    if visits["wholly_missed"] == 0 and visits["total"] > 0:
        print("  => the gate caught every visit; the missed frames are cosmetic. No tuning needed for coverage.")


if __name__ == "__main__":
    main()
