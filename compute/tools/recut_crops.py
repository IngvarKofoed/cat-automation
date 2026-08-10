"""Re-cut stored labelled crops at a different crop GEOMETRY, and stamp each row.

    python -m compute.tools.recut_crops --to letterbox+m10          # dry run (default)
    python -m compute.tools.recut_crops --to letterbox+m10 --apply
    python -m compute.tools.recut_crops --to legacy --apply         # back to squash/margin-0

A crop's *geometry* is the preprocessing convention its pixels follow — the context
margin baked in when it was cut, plus the resize (letterbox vs. squash) it must be
embedded under. ``build_gallery`` builds from ONE convention and drops the rest, because
1-NN over a blend of two feature spaces is silently worse matching with no symptom to
notice. So changing geometry means re-cutting the labelled set, which is what this does.

What it can and cannot move
---------------------------
A crop is re-cut FROM ITS SOURCE FRAME, so only a row whose frame is still live in the
rolling buffer can move. Anything else keeps its old stamp and its old file, and is
simply excluded from builds at the new geometry — never deleted, never mis-stamped. (At
the time of writing 100% of labelled crops still resolve to a live frame; that shrinks as
the ring turns over, which is the whole reason the stamp is per-row rather than global.)

Labelled crops are this repo's precious output — they survive frame eviction and
``clear()``, and a human's attention is what produced them — so the ordering is strict:

1. cut the new crop to a **temp file**, then ``os.replace`` it into its new path;
2. only then move the row (``crop_path`` + ``geometry``) in one committed batch;
3. only then delete the OLD crop file, and only when it is a different path.

Every crash window in that sequence leaves a HARMLESS orphan file, never a row pointing
at a file that is not there, and never a destroyed crop that has no replacement. A
re-run is idempotent: rows already at the target are skipped, and a leftover orphan is
overwritten by the next cut of the same crop.

Dry run is the default. It prints the census and the plan and writes nothing; ``--apply``
is what performs it.

Reads off its own connection, writes through ``Store``
------------------------------------------------------
The READ runs on its own short-lived SQLite connection, like ``ir_lamp_timeline`` /
``Store.lighting_histogram`` / ``tuning_calendar`` do: the compute PC collects
continuously and ``Store`` funnels every query through one shared write-locked
connection, which is the collector starvation changelog 102-105 removed.

The WRITE goes through ``Store.update_dataset_geometry``, which holds the store lock and
rolls a failed batch back. It is an UPDATE rather than the public delete-then-re-insert
path on purpose: that path destroys the label for the duration of the gap, resets
``labeled_ts`` (entry 323 relies on it identifying one keypress) and drops ``source``,
all on the one artifact that must not be lost. A re-cut changes only which PIXELS a label
points at — nothing about the label itself moves.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

from compute.dataset import crops
from compute.identification.embed import canonical_geometry, parse_geometry

# Same env var/default as compute/api/app.py's _store_from_env and every other tool, so
# all entry points point at one store without a shared config module.
_ENV_DIR = "CAT_COLLECT_DIR"
_DEFAULT_DIR = "./data/collection"

# Rows per committed transaction. Small on purpose: this runs against a LIVE store, and a
# long write transaction is exactly the lock hold that starves the collector. It also
# bounds what a crash loses — the batch in flight, whose worst outcome is orphan files.
_BATCH = 200

# What `--to` accepts for "the geometry with no stamp". The column stores NULL and
# `geometry_descriptor` renders None, but a CLI cannot pass None, so this word does.
_LEGACY_WORD = "legacy"


def _connect(db_path: str) -> sqlite3.Connection:
    """A short-lived connection with the store's own concurrency pragmas.

    ``busy_timeout`` matches ``Store``'s 5 s so a write here queues behind the
    collector's insert rather than failing outright; WAL is already on the DB file (it
    persists), so this only has to not fight it.
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _parse_bbox(text: "str | None") -> "list[float] | None":
    """``"x1,y1,x2,y2"`` → floats, or ``None`` when absent/unparseable.

    Mirrors what ``Store._bbox_text`` wrote. Unparseable degrades to ``None`` — that row
    is then reported as un-re-cuttable rather than crashing a whole run over one bad
    value, and its crop stays exactly where it is.
    """
    if not text:
        return None
    try:
        parts = [float(v) for v in str(text).split(",")]
    except ValueError:
        return None
    return parts if len(parts) >= 4 else None


def crop_rel_path(
    cat_id: "int | None", label_kind: str, src_frame_id: int, src_recv_ts: int,
    geometry: "str | None",
) -> str:
    """The dataset-root-relative path a crop of this row at ``geometry`` lives at.

    The LEGACY form is byte-for-byte what the label route writes
    (``cat_<id>/<frame_id>_<recv_ts>.jpg``, or ``cat_unknown_cat/…`` for a catless
    kind), so re-cutting back to legacy lands on exactly the path a fresh label would
    have used and the two conventions cannot diverge. A non-legacy geometry gets its own
    subdirectory named by the descriptor, which keeps each convention's files together
    and — more importantly — means a re-cut NEVER writes over the crop it is replacing
    until its row has moved.
    """
    subdir = f"cat_{cat_id}" if label_kind == "identified" else "cat_unknown_cat"
    base = f"{src_frame_id}_{src_recv_ts}.jpg"
    return os.path.join(subdir, base) if geometry is None else os.path.join(subdir, geometry, base)


def read_rows(conn: sqlite3.Connection, media_root: str, target: "str | None") -> "list[dict]":
    """Every labelled crop row, annotated with what a re-cut to ``target`` could do.

    One LEFT JOIN to ``frames`` on the ``(id, recv_ts)`` PAIR — the ``clear()``-safe
    linkage ``labeled_cat_motion_floor`` uses, since frame ids restart at 1 and a bare id
    match could cross-link a reused rowid to some other frame's pixels. Per row::

        {id, cat_id, label_kind, bbox, crop_path, src_frame_id, src_recv_ts,
         geometry (canonical), at_target, frame_path (abs|None), recuttable}

    ``recuttable`` is the conjunction the plan is built from: not already at the target,
    a parseable box, and a source frame that is both a live row AND still on disk.
    Everything else is reported and left alone.
    """
    rows = conn.execute(
        "SELECT d.id, d.cat_id, d.label_kind, d.bbox, d.crop_path, d.src_frame_id,"
        " d.src_recv_ts, d.geometry, f.path"
        " FROM dataset_items d"
        " LEFT JOIN frames f ON f.id = d.src_frame_id AND f.recv_ts = d.src_recv_ts"
        " WHERE d.crop_path IS NOT NULL"
        " ORDER BY d.id"
    ).fetchall()
    out: "list[dict]" = []
    for (rid, cat_id, label_kind, bbox_text, crop_path, src_frame_id, src_recv_ts,
         geometry, frame_rel) in rows:
        geom = canonical_geometry(geometry)
        bbox = _parse_bbox(bbox_text)
        frame_path = os.path.join(media_root, frame_rel) if frame_rel else None
        frame_live = bool(frame_path) and os.path.isfile(frame_path)
        at_target = geom == target
        out.append({
            "id": int(rid),
            "cat_id": cat_id,
            "label_kind": label_kind,
            "bbox": bbox,
            "crop_path": crop_path,
            "src_frame_id": int(src_frame_id),
            "src_recv_ts": int(src_recv_ts),
            "geometry": geom,
            "at_target": at_target,
            "frame_path": frame_path if frame_live else None,
            "recuttable": (not at_target) and bbox is not None and frame_live,
        })
    return out


def _cut_one(row: dict, target: "str | None", margin: float, dataset_root: str) -> "str | None":
    """Cut ``row``'s crop at ``target`` into place; return its new relative path or ``None``.

    Writes to ``<dest>.recut-tmp`` first and ``os.replace``s onto the destination, so the
    destination is never a half-written file even when it is the SAME path (which happens
    re-cutting back to legacy). The row is not touched here — the caller moves it only
    after this returned a path, which is what guarantees no row ever names a missing file.
    """
    rel = crop_rel_path(
        row["cat_id"], row["label_kind"], row["src_frame_id"], row["src_recv_ts"], target
    )
    dest_abs = os.path.join(dataset_root, rel)
    tmp_abs = dest_abs + ".recut-tmp"
    if not crops.materialize(
        row["frame_path"], row["bbox"], tmp_abs, root=dataset_root, margin=margin
    ):
        return None
    try:
        os.replace(tmp_abs, dest_abs)
    except OSError:
        # Leave nothing behind on a failed swap; the row still points at its old crop.
        try:
            os.remove(tmp_abs)
        except OSError:
            pass
        return None
    return rel


def _remove_old_crop(old_rel: str, new_rel: str, dataset_root: str) -> bool:
    """Delete the superseded crop file — never when the new path IS the old one.

    Realpath-contained under ``dataset_root`` before unlinking (defence in depth, the
    same check the API's ``_delete_crop_files`` makes on its own DB values), and the
    same-path test is what stops a re-cut back to legacy from deleting the file it just
    wrote. An already-gone file counts as removed; nothing here should ever raise.
    """
    root = os.path.realpath(dataset_root)
    old_abs = os.path.realpath(os.path.join(dataset_root, old_rel))
    new_abs = os.path.realpath(os.path.join(dataset_root, new_rel))
    if old_abs == new_abs:
        return False
    if old_abs != root and not old_abs.startswith(root + os.sep):
        return False
    try:
        os.remove(old_abs)
    except OSError:
        return False
    return True


def recut(
    write_moves,
    rows: "list[dict]",
    target: "str | None",
    dataset_root: str,
    batch: int = _BATCH,
    on_progress=None,
) -> dict:
    """Re-cut ``rows`` (all ``recuttable``) to ``target``; return a counts summary.

    ``write_moves`` is ``Store.update_dataset_geometry`` — ``[(item_id, new_rel_path,
    geometry)] -> [ids actually updated]``. Injected rather than imported so this
    function is testable against a plain fake and so the tool holds no store lock of its
    own.

    Per batch: cut every crop into place, then ONE transaction moving those rows, then
    delete the superseded files. That order is the safety property — see the module
    docstring — and the batch is small so the write lock is held briefly against a live
    collector. A cut that fails leaves its row completely untouched (old stamp, old
    file) and is counted in ``failed``; the run continues, because one unreadable frame
    is not a reason to abandon the other thousands.

    Returns ``{recut, failed, rows_updated, old_files_removed}``. ``rows_updated`` counts
    the ids the UPDATE actually matched, not an assumption — a row deleted by a
    concurrent re-label between the read and the write simply does not update, and only
    rows that DID update give up their old file.
    """
    summary = {"recut": 0, "failed": 0, "rows_updated": 0, "old_files_removed": 0}
    _letterbox, margin = parse_geometry(target)
    for start in range(0, len(rows), batch):
        chunk = rows[start:start + batch]
        moved: "list[tuple[dict, str]]" = []
        for row in chunk:
            new_rel = _cut_one(row, target, margin, dataset_root)
            if new_rel is None:
                summary["failed"] += 1
                continue
            moved.append((row, new_rel))
        if moved:
            # The new files are already on disk. If the write raises, no row names them:
            # harmless orphans a re-run overwrites. Nothing was destroyed, so it
            # propagates loudly rather than continuing to cut against a DB that would not
            # take writes. (`update_dataset_geometry` rolls its own batch back.)
            # The row's crop_path AS READ is passed so the write is a compare-and-swap:
            # rowids are reused (no AUTOINCREMENT) and `/api/label/relabel` deletes and
            # re-commits the same frames, so this id may belong to a different, freshly
            # labelled row by now. A row whose path moved under us fails to match, so it
            # is never reported moved and its file is never unlinked below.
            updated_ids = set(
                write_moves([
                    (row["id"], new_rel, target, row["crop_path"]) for row, new_rel in moved
                ])
            )
            summary["rows_updated"] += len(updated_ids)
            updated = [(row, new_rel) for row, new_rel in moved if row["id"] in updated_ids]
            summary["recut"] += len(moved)
            # ONLY rows the UPDATE actually matched give up their old file. A row that
            # vanished mid-run — `/api/label/relabel` deletes and re-commits, which
            # re-materialises a crop at exactly the legacy path this row came from — no
            # longer owns that path, so deleting it would leave the operator's fresh row
            # pointing at nothing. Small window, but it is the one outcome this tool
            # promises can't happen.
            for row, new_rel in updated:
                if _remove_old_crop(row["crop_path"], new_rel, dataset_root):
                    summary["old_files_removed"] += 1
        if on_progress is not None:
            on_progress(min(start + batch, len(rows)), len(rows))
    return summary


def _census(rows: "list[dict]") -> "list[tuple[str, int]]":
    """``[(geometry-or-'legacy', count), ...]`` over every labelled crop, commonest first."""
    counts: "dict[str, int]" = {}
    for row in rows:
        key = row["geometry"] or _LEGACY_WORD
        counts[key] = counts.get(key, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def main(argv: "list[str]") -> int:
    parser = argparse.ArgumentParser(
        prog="recut_crops",
        description="Re-cut stored labelled crops at a different geometry (dry run by default).",
    )
    parser.add_argument(
        "--to", required=True, metavar="GEOMETRY",
        help=f"target geometry: '{_LEGACY_WORD}' (squash, no margin), 'letterbox', "
             "'m10', 'letterbox+m10', …",
    )
    parser.add_argument(
        "--limit", type=int, default=None, metavar="N",
        help="re-cut at most N crops (the rest keep their current stamp) — for a "
             "cautious first pass on a live store",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="actually re-cut; without it nothing is written and only the plan is printed",
    )
    ns = parser.parse_args(argv[1:])

    raw_target = None if ns.to.strip().lower() == _LEGACY_WORD else ns.to
    try:
        # Parse for validation + the margin; canonicalise so `m10.0` and `m10` are one
        # target and the stamp written matches what `build_gallery` will filter on.
        parse_geometry(raw_target)
        target = canonical_geometry(raw_target)
    except ValueError as exc:
        parser.error(str(exc))
        return 2  # unreachable (parser.error exits) — keeps the type checker honest

    root = os.environ.get(_ENV_DIR, _DEFAULT_DIR)
    db_path = os.path.join(root, "index.db")
    media_root = os.path.join(root, "media")
    dataset_root = os.path.join(root, "dataset")
    if not os.path.isfile(db_path):
        print(f"no store at {db_path} (set {_ENV_DIR})")
        return 1

    conn = _connect(db_path)
    try:
        # `dataset_items.geometry` is added by `Store._migrate_schema`, which runs only in
        # `Store.__init__` — and this tool constructs a Store solely on the `--apply` path,
        # below. So on a store no process has yet opened with this build (the ordinary state
        # right after deploying, before the API restarts) the census `read_rows` performs
        # would die with a bare `no such column: d.geometry`, on the very first command this
        # module's docstring gives. Probed rather than migrated here so a dry run keeps its
        # write-nothing contract — the answer is an instruction, not a traceback.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(dataset_items)")}
        if "geometry" not in cols:
            print(f"{db_path} predates the dataset_items.geometry column.")
            print("Start the API once (it migrates the schema on open), then re-run this.")
            return 1
        rows = read_rows(conn, media_root, target)
        todo = [r for r in rows if r["recuttable"]]
        blocked = [r for r in rows if not r["recuttable"] and not r["at_target"]]
        if ns.limit is not None:
            todo = todo[:max(0, ns.limit)]

        print(f"Store:  {db_path}")
        print(f"Target: {target or _LEGACY_WORD}")
        print(f"Labelled crops with a file: {len(rows)}")
        for name, count in _census(rows):
            print(f"  {name:<24} {count}")
        print(f"  -> to re-cut:            {len(todo)}")
        if blocked:
            # These are the rows that stay behind, and WHY matters: an evicted frame is
            # the expected, permanent case (nothing can re-cut it), a missing box is a
            # data fault worth looking at.
            no_box = sum(1 for r in blocked if r["bbox"] is None)
            gone = len(blocked) - no_box
            print(f"  -> cannot re-cut:        {len(blocked)}"
                  f"  ({gone} source frame gone, {no_box} no usable box)")
            print("     They keep their current stamp and are excluded from builds at "
                  f"{target or _LEGACY_WORD}.")

        if not ns.apply:
            print("\nDry run — nothing written. Re-run with --apply to perform it.")
            return 0
        if not todo:
            print("\nNothing to do.")
            return 0

        def progress(done: int, total: int) -> None:
            print(f"  … {done}/{total}", flush=True)

        print()
        from compute.collection.store import Store

        # `max_bytes` is inert here: it is only consulted by `_evict_locked`, which runs
        # from `add()`, and this tool never adds a frame. `Store.__init__` itself neither
        # evicts nor sweeps orphans (the launch sweep is the API's, and it walks media/,
        # not the dataset/ tree this tool writes), so constructing one is side-effect-free
        # beyond the schema check. Passing the real env value anyway rather than a made-up
        # number, so nothing here depends on that staying true.
        store = Store(
            db_path,
            media_root,
            int(os.environ.get("CAT_COLLECT_MAX_BYTES", 5368709120)),
            dataset_root=dataset_root,
        )
        summary = recut(
            store.update_dataset_geometry, todo, target, dataset_root, on_progress=progress
        )
        print(f"re-cut {summary['recut']}, failed {summary['failed']}, "
              f"rows updated {summary['rows_updated']}, "
              f"old crop files removed {summary['old_files_removed']}")
        # A failure left its row untouched, so the store is consistent either way — but
        # exit non-zero so a wrapper notices that the set did not fully move.
        return 1 if summary["failed"] else 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
