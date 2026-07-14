"""Diff the ``yolo`` (batched) vs ``yolo-serial`` oracle verdicts — read-only diagnostic.

Root-causes the tuning matrix's disagreement between the two YOLO oracles (gate visit
recall vs ``yolo`` ≈ 75% but vs ``yolo-serial`` ≈ 90% on one bucket): the gate is
identical in both columns, so the gap is a pure verdict/visit-set difference between
the two oracles. Run this on the box that holds the verdicts (the Windows collection
PC); on a store with an empty ``analysis`` table it reports that and exits cleanly.

PART 1 (default — stdlib sqlite3 only, no ML deps):
  1. coverage parity   — frames analyzed by each oracle in the window (a partial
                         serial sweep would fake the gap: confound #1);
  2. regime blend      — per-oracle (model, device, half) regimes from ``detail`` +
                         ran_at spans, exposing verdicts accumulated across sessions
                         under different env (imgsz/conf are NOT recorded in detail —
                         a drift there is invisible here, only rerun can catch it);
  3. verdict diff      — over frames analyzed by BOTH: agreements and disagreements
                         split by direction (which side over-detects);
  4. score analysis    — the detecting side's score distribution on disagreements
                         (clustered at the conf≈0.15 floor = noise; well above =
                         systematic) + the score shift on both-present frames;
  5. image dims        — JPEG dimensions of disagreeing vs agreeing frames
                         (letterbox-drift suspicion: shape-dependent divergence);
  6. visits            — the store's OWN clustering (``Store._cluster_visits`` /
                         ``_split_into_visits`` / ``_visit_caught``, gap/window
                         constants imported, never reimplemented) over the
                         both-analyzed set: per-oracle visit recall, visits unique
                         to each oracle, and whether the gate caught them —
                         reconstructing the matrix gap;
  7. matrix repro      — ``Store.gate_scorecard("live", oracle, ...)`` per oracle,
                         the exact numbers the dashboard's Live row shows.

PART 2 (``--rerun`` — needs compute/requirements-analysis.txt + the JPEG files):
  For a sample of the disagreeing frames, decode each JPEG and run BOTH the real
  ``YoloAnalyzer(serial=True)`` per-frame path and the real batched path (same
  shape-boundary chunking the runner uses) over the same pixels, printing per-frame
  (frame_id, dims, serial verdict/score, batched verdict/score) against the stored
  verdicts. Separates intrinsic batching divergence from stored-regime drift from
  a result-ordering bug. Caveat: rerun batches are composed of the sampled frames,
  not the original sweep's neighbors — batch size and shape chunking match, batch
  membership doesn't.

Usage (repo root; Part 1 needs only stdlib):
    python compute/tools/diff_yolo_batch_serial.py --db data/collection/index.db --group 1
    python compute/tools/diff_yolo_batch_serial.py --db D:/cat/data/collection/index.db --since-id 100 --until-id 9000
    python compute/tools/diff_yolo_batch_serial.py --db ... --group 3 --rerun --limit 64
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
import sys
from datetime import datetime

# Make ``compute.*`` importable when run as a bare script (python compute/tools/...):
# sys.path[0] is then compute/tools, not the repo root that ``-m`` would provide.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from compute.collection.store import (  # noqa: E402  (path bootstrap above)
    _VISIT_GAP_MS,
    _VISIT_WINDOW_MS,
    Store,
)

_BATCHED = "yolo"
_SERIAL = "yolo-serial"

# The recall-first conf floor (compute/analysis/yolo.py::_DEFAULT_CONF) — the bin
# edge that separates "flip at the floor = last-bit noise" from "systematic".
_CONF_FLOOR = 0.15

# Mirrors compute/api/app.py::_WARMUP_FRAMES — used to derive the dashboard-equivalent
# warmup for a scoped window so the matrix numbers reproduce (see --warmup).
_WARMUP_FRAMES = 500

# Scorecard area-bucket thresholds (only affect the miss->knob buckets, which this
# tool does not print; visits/recall never read them). Mirror diagnose_misses.py.
_DEF_MIN_AREA = 0.01
_DEF_MAX_AREA = 0.6
_DEF_PERSISTENCE = 2


def _fmt_ts(ms: "int | None") -> str:
    if ms is None:
        return "?"
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")


def _connect(db_path: str) -> sqlite3.Connection:
    """Open the index DB for reading; ``query_only`` makes read-only enforced, not policy."""
    if not os.path.exists(db_path):
        raise SystemExit(f"no store DB at {db_path!r}")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _resolve_window(conn: sqlite3.Connection, args) -> "tuple[int | None, int | None, str]":
    """(since_id, until_id, label) from --group or --since-id/--until-id."""
    if args.group is not None:
        row = conn.execute(
            "SELECT name, start_id, end_id FROM groups WHERE id = ?", (args.group,)
        ).fetchone()
        if row is None:
            known = conn.execute("SELECT id, name, start_id, end_id FROM groups ORDER BY id").fetchall()
            listing = "; ".join(f"id={i} {n!r} [{s}..{e}]" for i, n, s, e in known) or "(none)"
            raise SystemExit(f"no group with id {args.group}; known groups: {listing}")
        name, start_id, end_id = row
        return int(start_id), int(end_id), f"group {args.group} {name!r}"
    if args.since_id is not None or args.until_id is not None:
        return args.since_id, args.until_id, "explicit id range"
    return None, None, "whole store"


def _range_sql(since_id: "int | None", until_id: "int | None", col: str = "f.id") -> "tuple[str, list]":
    frags, params = [], []
    if since_id is not None:
        frags.append(f"{col} >= ?")
        params.append(int(since_id))
    if until_id is not None:
        frags.append(f"{col} <= ?")
        params.append(int(until_id))
    return (" AND ".join(frags) or "1=1"), params


def _auto_warmup(conn: sqlite3.Connection, since_id: "int | None", until_id: "int | None") -> int:
    """The warmup /api/tuning/compare would pass for this scope (compute/api/app.py)."""
    scoped = since_id is not None or until_id is not None
    if not scoped or since_id is None:
        return _WARMUP_FRAMES
    (pre_window,) = conn.execute("SELECT COUNT(*) FROM frames WHERE id <= ?", (since_id - 1,)).fetchone()
    return max(0, _WARMUP_FRAMES - int(pre_window))


def _jpeg_dims(path: str) -> "tuple[int, int] | None":
    """(width, height) from a JPEG's SOF marker; None when unreadable. Pure stdlib."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None
    i, n = 2, len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker == 0xFF:  # fill byte: only one 0xFF consumed, the next may be the marker
            i += 1
            continue
        if marker == 0xD9:  # EOI: no SOF found
            return None
        if marker == 0x01 or 0xD0 <= marker <= 0xD8:
            i += 2
            continue
        seg_len = int.from_bytes(data[i + 2 : i + 4], "big")
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height = int.from_bytes(data[i + 5 : i + 7], "big")
            width = int.from_bytes(data[i + 7 : i + 9], "big")
            return (width, height)
        i += 2 + seg_len
    return None


def _score_hist(scores: "list[float]") -> "list[tuple[str, int]]":
    """Bins keyed to the 0.15 conf floor: at-floor flips are noise, higher is systematic."""
    edges = [
        (f"~{_CONF_FLOOR} floor (<{_CONF_FLOOR + 0.005:.3f})", lambda s: s < _CONF_FLOOR + 0.005),
        ("0.155-0.20", lambda s: s < 0.20),
        ("0.20-0.30", lambda s: s < 0.30),
        ("0.30-0.50", lambda s: s < 0.50),
        (">=0.50", lambda s: True),
    ]
    out = []
    for label, _pred in edges:
        out.append([label, 0])
    for s in scores:
        for idx, (_label, pred) in enumerate(edges):
            if pred(s):
                out[idx][1] += 1
                break
    return [(label, count) for label, count in out]


def _regime_breakdown(conn: sqlite3.Connection, oracle: str, where: str, params: list) -> None:
    """Per-(model, device, half) verdict counts + ran_at spans — exposes session blending."""
    try:
        rows = conn.execute(
            "SELECT COALESCE(json_extract(a.detail, '$.model'), '?'),"
            " COALESCE(json_extract(a.detail, '$.device'), '?'),"
            " COALESCE(json_extract(a.detail, '$.half'), '?'),"
            " COUNT(*), MIN(a.ran_at), MAX(a.ran_at)"
            " FROM analysis a JOIN frames f ON f.id = a.frame_id"
            f" WHERE a.analyzer = ? AND {where}"
            " GROUP BY 1, 2, 3 ORDER BY 4 DESC",
            [oracle] + params,
        ).fetchall()
    except sqlite3.OperationalError as exc:  # sqlite built without JSON1
        print(f"  {oracle}: regime breakdown unavailable ({exc})")
        return
    if not rows:
        print(f"  {oracle}: no verdicts in window")
        return
    for model, device, half, count, lo, hi in rows:
        print(
            f"  {oracle}: model={model} device={device} half={half}  "
            f"verdicts={count}  ran {_fmt_ts(lo)} .. {_fmt_ts(hi)}"
        )
    if len(rows) > 1:
        print(f"  !! {oracle} verdicts span {len(rows)} regimes — cross-session blend, a confound")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True, help="path to the store's index.db")
    ap.add_argument("--group", type=int, default=None, help="bucket id in the groups table")
    ap.add_argument("--since-id", type=int, default=None, help="window floor (inclusive frame id)")
    ap.add_argument("--until-id", type=int, default=None, help="window ceiling (inclusive frame id)")
    ap.add_argument(
        "--media-root", default=None, help="media dir for frame JPEGs (default: <db dir>/media)"
    )
    ap.add_argument(
        "--warmup",
        type=int,
        default=None,
        help="scored-set warmup prefix; default: the dashboard-equivalent value for this scope",
    )
    ap.add_argument("--max-probe", type=int, default=1500, help="cap on JPEG dimension probes")
    ap.add_argument("--rerun", action="store_true", help="PART 2: re-run both YOLO paths on disagreeing frames")
    ap.add_argument("--limit", type=int, default=64, help="--rerun sample size")
    args = ap.parse_args()
    if args.group is not None and (args.since_id is not None or args.until_id is not None):
        ap.error("--group and --since-id/--until-id are mutually exclusive")

    db_path = os.path.abspath(args.db)
    media_root = args.media_root or os.path.join(os.path.dirname(db_path), "media")
    conn = _connect(db_path)
    since_id, until_id, scope_label = _resolve_window(conn, args)
    where, wparams = _range_sql(since_id, until_id)
    warmup = args.warmup if args.warmup is not None else _auto_warmup(conn, since_id, until_id)

    (total_frames,) = conn.execute(f"SELECT COUNT(*) FROM frames f WHERE {where}", wparams).fetchone()
    span = conn.execute(f"SELECT MIN(f.recv_ts), MAX(f.recv_ts) FROM frames f WHERE {where}", wparams).fetchone()

    print(f"DB: {db_path}")
    print(f"window: {scope_label}  [{since_id if since_id is not None else '-inf'} .. "
          f"{until_id if until_id is not None else '+inf'}]  frames={total_frames}")
    print(f"        {_fmt_ts(span[0])} .. {_fmt_ts(span[1])}")
    print(f"warmup used: {warmup} (dashboard-equivalent for this scope"
          f"{'' if args.warmup is None else ' OVERRIDDEN by --warmup'})")

    # ---- 1. Coverage parity (confound #1: a partial sweep fakes the gap) -------------
    cov = conn.execute(
        "SELECT"
        " SUM(CASE WHEN y.frame_id IS NOT NULL THEN 1 ELSE 0 END),"
        " SUM(CASE WHEN y.verdict = 1 THEN 1 ELSE 0 END),"
        " SUM(CASE WHEN s.frame_id IS NOT NULL THEN 1 ELSE 0 END),"
        " SUM(CASE WHEN s.verdict = 1 THEN 1 ELSE 0 END),"
        " SUM(CASE WHEN y.frame_id IS NOT NULL AND s.frame_id IS NOT NULL THEN 1 ELSE 0 END),"
        " SUM(CASE WHEN y.frame_id IS NOT NULL AND s.frame_id IS NULL THEN 1 ELSE 0 END),"
        " SUM(CASE WHEN y.frame_id IS NULL AND s.frame_id IS NOT NULL THEN 1 ELSE 0 END)"
        " FROM frames f"
        " LEFT JOIN analysis y ON y.frame_id = f.id AND y.analyzer = ?"
        " LEFT JOIN analysis s ON s.frame_id = f.id AND s.analyzer = ?"
        f" WHERE {where}",
        [_BATCHED, _SERIAL] + wparams,
    ).fetchone()
    y_n, y_present, s_n, s_present, both_n, only_y, only_s = (int(x or 0) for x in cov)

    print("\n== 1. coverage parity ==")
    print(f"  {_BATCHED:<12} analyzed {y_n}/{total_frames}  present={y_present}")
    print(f"  {_SERIAL:<12} analyzed {s_n}/{total_frames}  present={s_present}")
    print(f"  analyzed by both: {both_n}   only-{_BATCHED}: {only_y}   only-{_SERIAL}: {only_s}")
    if y_n == 0 and s_n == 0:
        print(
            "\n0 verdicts for both oracles in this window — the analysis table has no data here.\n"
            "Run this script on the machine that ran the sweeps (the Windows collection PC),\n"
            "pointing --db at its store."
        )
        return
    if only_y or only_s or y_n < total_frames or s_n < total_frames:
        print("  !! coverage is NOT identical/full — the matrix columns scored different frame sets;")
        print("     fix coverage (sweep the missing frames) before trusting any verdict-level conclusion.")
    else:
        print("  coverage identical and full — the gap is in the verdicts, not the frame sets.")

    # ---- 2. Regime blend (detail carries model/device/half; NOT imgsz/conf) ----------
    print("\n== 2. per-oracle verdict regimes (from analysis.detail; imgsz/conf are NOT recorded) ==")
    _regime_breakdown(conn, _BATCHED, where, wparams)
    _regime_breakdown(conn, _SERIAL, where, wparams)

    if both_n == 0:
        print("\nno frames analyzed by BOTH oracles — nothing to diff; sweep both over this window first.")
        return

    # ---- 3. Verdict diff over both-analyzed -------------------------------------------
    both_from = (
        " FROM frames f"
        " JOIN analysis y ON y.frame_id = f.id AND y.analyzer = ?"
        " JOIN analysis s ON s.frame_id = f.id AND s.analyzer = ?"
    )
    both_params = [_BATCHED, _SERIAL] + wparams
    agree11, agree00, y1s0, y0s1 = (
        int(x or 0)
        for x in conn.execute(
            "SELECT"
            " SUM(CASE WHEN y.verdict = 1 AND s.verdict = 1 THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN y.verdict = 0 AND s.verdict = 0 THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN y.verdict = 1 AND s.verdict = 0 THEN 1 ELSE 0 END),"
            " SUM(CASE WHEN y.verdict = 0 AND s.verdict = 1 THEN 1 ELSE 0 END)"
            + both_from
            + f" WHERE {where}",
            both_params,
        ).fetchone()
    )
    print("\n== 3. verdict diff (frames analyzed by both) ==")
    print(f"  agree present (1,1): {agree11}")
    print(f"  agree absent  (0,0): {agree00}")
    print(f"  DISAGREE {_BATCHED}=1 / {_SERIAL}=0: {y1s0}   <- batched over-detects (or serial under-)")
    print(f"  DISAGREE {_BATCHED}=0 / {_SERIAL}=1: {y0s1}   <- serial over-detects (or batched under-)")
    disagree_n = y1s0 + y0s1
    if disagree_n == 0:
        print("  verdicts agree on every shared frame — the matrix gap must come from coverage/warmup, not verdicts.")

    # ---- 4. Score analysis --------------------------------------------------------------
    if disagree_n:
        print("\n== 4. disagreement scores (the DETECTING side's max-conf; floor = conf 0.15) ==")
        for label, cond, col in (
            (f"{_BATCHED}=1/{_SERIAL}=0", "y.verdict = 1 AND s.verdict = 0", "y.score"),
            (f"{_BATCHED}=0/{_SERIAL}=1", "y.verdict = 0 AND s.verdict = 1", "s.score"),
        ):
            scores = [
                float(r[0])
                for r in conn.execute(
                    f"SELECT COALESCE({col}, 0.0)" + both_from + f" WHERE {where} AND {cond}",
                    both_params,
                ).fetchall()
            ]
            if not scores:
                print(f"  {label}: none")
                continue
            med = statistics.median(scores)
            print(f"  {label}: n={len(scores)}  median={med:.3f}  max={max(scores):.3f}")
            for bin_label, count in _score_hist(scores):
                print(f"      {bin_label:<24} {count}")
        shift = conn.execute(
            "SELECT COUNT(*), AVG(COALESCE(y.score,0) - COALESCE(s.score,0)),"
            " SUM(CASE WHEN ABS(COALESCE(y.score,0) - COALESCE(s.score,0)) > 0.05 THEN 1 ELSE 0 END)"
            + both_from
            + f" WHERE {where} AND y.verdict = 1 AND s.verdict = 1",
            both_params,
        ).fetchone()
        if shift and int(shift[0] or 0) > 0:
            print(
                f"  both-present score shift: n={int(shift[0])}  avg({_BATCHED}-{_SERIAL})={float(shift[1] or 0):+.4f}"
                f"  |shift|>0.05 on {int(shift[2] or 0)} frames"
            )
            print("  (a near-zero avg shift + at-floor flips = numeric noise; a large shift = systematic)")

    # ---- 5. Dimensions of disagreeing vs agreeing frames --------------------------------
    if disagree_n:
        print("\n== 5. image dimensions (letterbox-drift check; probed from JPEG headers) ==")
        probe_rows = conn.execute(
            "SELECT f.id, f.path, y.verdict, s.verdict" + both_from
            + f" WHERE {where} AND y.verdict != s.verdict ORDER BY f.id LIMIT ?",
            both_params + [args.max_probe],
        ).fetchall()
        agree_rows = conn.execute(
            "SELECT f.id, f.path, y.verdict, s.verdict" + both_from
            + f" WHERE {where} AND y.verdict = s.verdict ORDER BY f.id LIMIT 200",
            both_params,
        ).fetchall()
        dims_count: dict = {}
        unreadable = 0
        for rows, tag in ((probe_rows, "disagree"), (agree_rows, "agree-sample")):
            for fid, rel_path, yv, sv in rows:
                dims = _jpeg_dims(os.path.join(media_root, rel_path))
                if dims is None:
                    unreadable += 1
                    continue
                key = (dims, tag if tag == "agree-sample" else f"disagree {_BATCHED}={yv}/{_SERIAL}={sv}")
                dims_count[key] = dims_count.get(key, 0) + 1
        if not dims_count:
            print(f"  no JPEGs readable under {media_root!r} — pass --media-root (files live on the sweep box)")
        else:
            for (dims, tag), count in sorted(dims_count.items(), key=lambda kv: (kv[0][1], kv[0][0])):
                print(f"  {dims[0]}x{dims[1]:<6} {tag:<28} {count}")
            if unreadable:
                print(f"  ({unreadable} files unreadable/missing)")
            print("  (disagreements clustering in a dims class the agreements lack = letterbox suspicion)")

    # ---- 6. Visit-level reconstruction (the store's own clustering, both-analyzed set) --
    print(f"\n== 6. visits over the both-analyzed set (gap={_VISIT_GAP_MS}ms window=+/-{_VISIT_WINDOW_MS}ms,"
          f" Store's own clustering) ==")
    threshold_row = conn.execute(
        "SELECT f.id" + both_from + f" WHERE {where} ORDER BY f.id ASC LIMIT 1 OFFSET ?",
        both_params + [warmup],
    ).fetchone()
    if threshold_row is None:
        print(f"  nothing past the warmup prefix ({warmup}) in the both-analyzed set")
    else:
        threshold_id = int(threshold_row[0])
        rows = conn.execute(
            "SELECT f.id, f.recv_ts, f.motion, y.verdict, COALESCE(y.score, 0), s.verdict,"
            " COALESCE(s.score, 0)" + both_from
            + f" WHERE {where} AND f.id >= ? AND (f.motion = 1 OR y.verdict = 1 OR s.verdict = 1)"
            " ORDER BY f.recv_ts ASC, f.id ASC",
            both_params + [threshold_id],
        ).fetchall()
        motion_ts = sorted(r[1] for r in rows if r[2] == 1)
        per_oracle = {}
        for name, v_idx in ((_BATCHED, 3), (_SERIAL, 5)):
            interesting = [(r[1], r[2], r[v_idx]) for r in rows if r[2] == 1 or r[v_idx] == 1]
            total, caught = Store._cluster_visits(interesting)
            present_ts = sorted(r[1] for r in rows if r[v_idx] == 1)
            spans = Store._split_into_visits(present_ts)
            per_oracle[name] = (total, caught, spans, present_ts)
            rate = (caught / total) if total else 0.0
            print(f"  vs {name:<12} visits={total}  gate-caught={caught}  visit recall={rate:.1%}")
        y_total, y_caught, y_spans, y_present_ts = per_oracle[_BATCHED]
        s_total, s_caught, s_spans, s_present_ts = per_oracle[_SERIAL]
        if y_total and s_total:
            gap = (y_caught / y_total) - (s_caught / s_total)
            print(f"  recall gap ({_BATCHED} - {_SERIAL}): {gap:+.1%}")

        for name, spans, other_ts in (
            (_BATCHED, y_spans, s_present_ts),
            (_SERIAL, s_spans, y_present_ts),
        ):
            # A span is "shared" when the OTHER oracle has a present frame within the
            # span +/- the same catch window the scorecard uses (Store._visit_caught).
            unique = [sp for sp in spans if not Store._visit_caught(sp[0], sp[1], other_ts)]
            unique_caught = sum(1 for sp in unique if Store._visit_caught(sp[0], sp[1], motion_ts))
            print(f"  visits unique to {name}: {len(unique)}  (gate caught {unique_caught} of them)")
            v_idx, score_idx = (3, 4) if name == _BATCHED else (5, 6)
            for lo, hi in unique[:15]:
                members = [r for r in rows if r[v_idx] == 1 and lo <= r[1] <= hi]
                ids = [r[0] for r in members]
                max_score = max((r[score_idx] for r in members), default=0.0)
                caught_flag = "caught" if Store._visit_caught(lo, hi, motion_ts) else "MISSED by gate"
                print(
                    f"      {_fmt_ts(lo)} .. {_fmt_ts(hi)}  frames {min(ids)}-{max(ids)} (n={len(ids)})"
                    f"  max_score={max_score:.2f}  {caught_flag}"
                )
            if len(unique) > 15:
                print(f"      ... and {len(unique) - 15} more")
        print("  (many low-score visits unique to one oracle, gate-MISSED => that oracle's extra marginal")
        print("   detections are what drag its recall column down)")

    # ---- 7. Matrix reproduction via the real gate_scorecard -----------------------------
    print("\n== 7. dashboard Live-row reproduction (Store.gate_scorecard, per-oracle scored set) ==")
    try:
        store = Store(db_path=db_path, media_root=media_root, max_bytes=1)
    except Exception as exc:  # e.g. read-only filesystem: Store opens writable
        store = None
        print(f"  skipped: could not open Store ({exc})")
    if store is not None:
        for oracle in (_BATCHED, _SERIAL):
            try:
                card = store.gate_scorecard(
                    "live",
                    oracle,
                    warmup=warmup,
                    min_area=_DEF_MIN_AREA,
                    max_area=_DEF_MAX_AREA,
                    persistence=_DEF_PERSISTENCE,
                    since_id=since_id,
                    until_id=until_id,
                )
            except Exception as exc:  # e.g. a lock timeout against a live-collecting DB
                print(f"  vs {oracle:<12} scorecard unavailable ({exc})")
                continue
            v = card["visits"]
            rate = (v["caught"] / v["total"]) if v["total"] else 0.0
            print(
                f"  vs {oracle:<12} visit recall={rate:.1%} ({v['caught']}/{v['total']})"
                f"  frame recall={card['recall']['rate']:.1%}"
                f"  analyzed={card['analyzed']}  present={card['present']}"
            )
        print("  (these are the matrix's Live-row cells; each column's scored set is that oracle's")
        print("   own analyzed frames, so coverage differences shift them — see section 1)")

    # ---- PART 2: definitive rerun --------------------------------------------------------
    if args.rerun:
        _rerun(conn, media_root, where, wparams, both_from, both_params, args.limit)


def _rerun(conn, media_root, where, wparams, both_from, both_params, limit) -> None:
    """PART 2: run BOTH real YOLO paths over the disagreeing frames' pixels."""
    print(f"\n== PART 2: re-run both paths on up to {limit} disagreeing frames ==")
    try:
        import cv2
        import numpy as np

        from compute.analysis.yolo import YoloAnalyzer
    except ImportError as exc:
        raise SystemExit(
            f"--rerun needs cv2/numpy (+ torch/ultralytics): {exc}\n"
            "install: pip install -r compute/requirements-analysis.txt"
        )

    sample = conn.execute(
        "SELECT f.id, f.path, y.verdict, COALESCE(y.score, 0), s.verdict, COALESCE(s.score, 0)"
        + both_from
        + f" WHERE {where} AND y.verdict != s.verdict ORDER BY f.id LIMIT ?",
        both_params + [limit],
    ).fetchall()
    if not sample:
        print("  no disagreeing frames — sampling both-analyzed frames instead")
        sample = conn.execute(
            "SELECT f.id, f.path, y.verdict, COALESCE(y.score, 0), s.verdict, COALESCE(s.score, 0)"
            + both_from
            + f" WHERE {where} ORDER BY f.id LIMIT ?",
            both_params + [limit],
        ).fetchall()
    if not sample:
        print("  nothing to rerun")
        return

    serial = YoloAnalyzer(serial=True)
    batched = YoloAnalyzer()
    try:
        serial.prepare(None)   # store/since_id unused for the stateless backend
        batched.prepare(None)
    except ImportError as exc:
        raise SystemExit(str(exc))
    print(
        f"  params: weights={serial._weights} imgsz={serial._imgsz} conf={serial._conf}"
        f" half={serial._half} device={serial._device} batch_size={batched.batch_size}"
    )
    print("  caveat: batches here are built from the SAMPLED frames (same batch size + shape-")
    print("  boundary chunking as the runner), not the original sweep's neighboring frames.")

    frames = []  # (fid, dims, image, stored_y, stored_ys, stored_s, stored_ss)
    for fid, rel_path, yv, ys, sv, ss in sample:
        abs_path = os.path.join(media_root, rel_path)
        try:
            with open(abs_path, "rb") as fh:
                buf = fh.read()
            image = cv2.imdecode(np.frombuffer(buf, dtype=np.uint8), cv2.IMREAD_COLOR)
        except OSError:
            image = None
        if image is None:
            print(f"  frame {fid}: unreadable at {abs_path!r} — skipped")
            continue
        frames.append((int(fid), image.shape, image, int(yv), float(ys), int(sv), float(ss)))
    if not frames:
        print("  no frames decodable — check --media-root")
        return

    # Serial pass: the real one-bare-image-per-frame path.
    serial_results = [serial.analyze(img) for (_f, _sh, img, *_rest) in frames]

    # Batched pass: mimic the runner's chunking — same-shape runs up to batch_size
    # (compute/analysis/runner.py shape-boundary flush), one analyze_batch per chunk.
    batched_results = [None] * len(frames)
    start = 0
    while start < len(frames):
        end = start + 1
        shape = frames[start][1]
        while (
            end < len(frames)
            and frames[end][1] == shape
            and (end - start) < batched.batch_size
        ):
            end += 1
        chunk = [frames[i][2] for i in range(start, end)]
        for offset, res in enumerate(batched.analyze_batch(chunk)):
            batched_results[start + offset] = res
        start = end

    print(f"\n  {'frame':>8}  {'dims':>10}  {'stored y/sc':>13}  {'stored ser':>13}"
          f"  {'rerun batch':>13}  {'rerun serial':>13}")
    n_rb_ne_rs = n_rs_ne_stored_s = n_rb_ne_stored_y = 0
    for i, (fid, shape, _img, yv, ys, sv, ss) in enumerate(frames):
        rb, rs = batched_results[i], serial_results[i]
        rb_v, rb_s = int(rb.verdict), rb.score or 0.0
        rs_v, rs_s = int(rs.verdict), rs.score or 0.0
        n_rb_ne_rs += rb_v != rs_v
        n_rs_ne_stored_s += rs_v != sv
        n_rb_ne_stored_y += rb_v != yv
        dims = f"{shape[1]}x{shape[0]}"
        flag = "  <-- batch!=serial NOW" if rb_v != rs_v else ""
        print(
            f"  {fid:>8}  {dims:>10}  {yv}/{ys:>10.3f}  {sv}/{ss:>10.3f}"
            f"  {rb_v}/{rb_s:>10.3f}  {rs_v}/{rs_s:>10.3f}{flag}"
        )

    n = len(frames)
    print(f"\n  rerun batched != rerun serial : {n_rb_ne_rs}/{n}   <- INTRINSIC batching divergence, today")
    print(f"  rerun serial  != stored serial: {n_rs_ne_stored_s}/{n}   <- env/model drift since the stored sweep")
    print(f"  rerun batched != stored batched: {n_rb_ne_stored_y}/{n}")
    print("  reading: rerun batch==serial everywhere while the STORED verdicts disagree =>")
    print("  the stored gap is not intrinsic batching under current env — suspect cross-session")
    print("  regime drift (section 2) or misassignment during the original sweep. Large intrinsic")
    print("  divergence with scores well above the 0.15 floor => a real batching bug (per the")
    print("  sweep-throughput spec's validation gate).")


if __name__ == "__main__":
    main()
