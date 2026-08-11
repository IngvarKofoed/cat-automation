"""Re-cut stored labelled crops at a different crop GEOMETRY, and stamp each row.

    python -m compute.tools.recut_crops --to letterbox+m10          # dry run (default)
    python -m compute.tools.recut_crops --to letterbox+m10 --apply
    python -m compute.tools.recut_crops --to legacy --apply         # back to squash/margin-0

A crop's *geometry* is the preprocessing convention its pixels follow — the context
margin baked in when it was cut, plus the resize (letterbox vs. squash) it must be
embedded under. ``build_gallery`` builds from ONE convention and drops the rest, because
1-NN over a blend of two feature spaces is silently worse matching with no symptom to
notice. So changing geometry means re-cutting the labelled set, which is what this does.

Three ways a crop moves, cheapest first
---------------------------------------
Cutting is the LAST resort, because a move needs a source frame only when it needs new
pixels. A geometry is ``(letterbox, margin)`` and only ``margin`` reaches the stored
pixels — ``letterbox`` is a resize applied at embed time — so for any given margin there
are exactly TWO geometries (letterbox on/off) whose files are pixel-interchangeable:

1. **relink** — a file already sits at the target's path. Move the stamp; touch no pixels.
2. **copy** — the row's OTHER margin-equal file exists (e.g. its legacy crop when the
   target is ``letterbox``). Copy it into place; no bbox and no source frame needed.
3. **recut** — no margin-equal file exists. Decode the source frame, expand the box,
   re-encode. The only branch a frame eviction can block.

That is what makes a ``letterbox``-only flip lossless: it alters no pixel, so it never
needs the frame, and a crop whose frame aged out moves anyway. It also makes RETURNING to
a geometry already visited free, which is what turns an A/B of several crop shapes into
quick hops rather than a full re-cut per switch.

Superseded crops are KEPT
-------------------------
A move never deletes the file it supersedes, so every geometry a crop has been cut at
stays on disk and any move can be walked back — including long after the source frame has
evicted. The cost is a copy of the labelled set per geometry visited; nothing reclaims it
automatically (removing a stale convention's subdirectory is a manual, deliberate act).

This makes crop files outlive a geometry move, but they must NEVER outlive their ROW:
``relink`` trusts that a file at the target path is *this* row's crop there, which holds
only while nothing survives the row that produced it. The label delete/relabel paths are
what enforce that (they remove every geometry variant, not just the current one).

Labelled crops are this repo's precious output — they survive frame eviction and
``clear()``, and a human's attention is what produced them — so the ordering is strict:

1. write the new crop (cut or copied) to a **temp file**, then ``os.replace`` it into
   its new path;
2. only then move the row (``crop_path`` + ``geometry``) in one committed batch.

Every crash window in that sequence leaves a HARMLESS orphan file, never a row pointing
at a file that is not there, and never a destroyed crop that has no replacement. A
re-run is idempotent: rows already at the target are skipped, and a leftover orphan is
overwritten by the next write of the same crop.

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
import shutil
import sqlite3
import sys

from compute.dataset import crops
# `crop_rel_path` lives in `dataset.crops` (see there): the label-commit path needs the
# same rule, and the API importing this CLI tool to get it would be backwards layering.
# Re-exported under this module's own name so existing callers keep resolving.
from compute.dataset.crops import crop_rel_path
from compute.identification.embed import (
    canonical_geometry,
    geometry_descriptor,
    parse_geometry,
)

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


def margin_twins(margin: float) -> "tuple[str | None, str | None]":
    """The two geometries sharing ``margin`` — ``(squash, letterbox)``.

    A geometry is ``(letterbox, margin)`` and only ``margin`` reaches the pixels, so for a
    fixed margin exactly two stamps name the SAME bytes: letterbox off and on. That is why
    finding a pixel-interchangeable file is two ``stat`` calls rather than a directory
    walk — and why a ``letterbox``-only flip never needs a source frame.
    """
    return geometry_descriptor(False, margin), geometry_descriptor(True, margin)


def _route(row: dict, target: "str | None", target_margin: float, dataset_root: str) -> None:
    """Fill ``row``'s ``move`` / ``blocked`` / ``copy_from`` in place. See the module docstring.

    Order is cheapest-first — relink, copy, recut — and each step is a plain existence
    test, so the routing is a pure function of what is on disk right now. A row already at
    the target moves nowhere; its file is checked anyway, because a stamp whose file is
    missing reads as "done" everywhere else and would let a build silently under-enrol.
    """
    if row["at_target"]:
        row["at_target_missing"] = not os.path.isfile(
            os.path.join(dataset_root, row["crop_path"])
        )
        return
    # relink, then copy: both twins are candidates, and the target itself is one of them.
    squash, letterbox = margin_twins(target_margin)
    for cand in (target, squash, letterbox):
        rel = crop_rel_path(
            row["cat_id"], row["label_kind"], row["src_frame_id"], row["src_recv_ts"], cand
        )
        if not os.path.isfile(os.path.join(dataset_root, rel)):
            continue
        if cand == target:
            row["move"], row["new_rel"] = "relink", rel
        else:
            row["move"], row["copy_from"] = "copy", rel
        return
    # Nothing pixel-equal exists, so the pixels must be made: this is the only branch a
    # frame eviction can block, and the only one needing a box.
    if row["bbox"] is None:
        row["blocked"] = "no_box"
    elif row["frame_path"] is None:
        row["blocked"] = "frame_gone"
    else:
        row["move"] = "recut"


def read_rows(
    conn: sqlite3.Connection, media_root: str, target: "str | None", dataset_root: str
) -> "list[dict]":
    """Every labelled crop row, annotated with HOW a move to ``target`` would happen.

    One LEFT JOIN to ``frames`` on the ``(id, recv_ts)`` PAIR — the ``clear()``-safe
    linkage ``labeled_cat_motion_floor`` uses, since frame ids restart at 1 and a bare id
    match could cross-link a reused rowid to some other frame's pixels. Per row::

        {id, cat_id, label_kind, bbox, bbox_text, crop_path, src_frame_id, src_recv_ts,
         geometry (canonical), at_target, at_target_missing, frame_path (abs|None),
         move ('relink'|'copy'|'recut'|None), blocked ('frame_gone'|'no_box'|None),
         copy_from (rel|None), new_rel (rel|None)}

    ``bbox_text`` is the RAW stored string, carried because
    ``Store.update_dataset_geometry`` compares it: a relabel to the same cat re-commits at
    the identical path, so path alone cannot tell a replaced row from the one that was
    read, while a moved box can.

    An UNPARSEABLE stored stamp (a convention some other build wrote — ``canonical_geometry``
    passes those through untouched) yields an unknown margin, so it is never treated as
    pixel-equal to anything; the row simply routes to ``recut``. It must not raise: a
    single foreign stamp would otherwise fail the whole plan.
    """
    rows = conn.execute(
        "SELECT d.id, d.cat_id, d.label_kind, d.bbox, d.crop_path, d.src_frame_id,"
        " d.src_recv_ts, d.geometry, f.path"
        " FROM dataset_items d"
        " LEFT JOIN frames f ON f.id = d.src_frame_id AND f.recv_ts = d.src_recv_ts"
        " WHERE d.crop_path IS NOT NULL"
        " ORDER BY d.id"
    ).fetchall()
    _, target_margin = parse_geometry(target)
    out: "list[dict]" = []
    for (rid, cat_id, label_kind, bbox_text, crop_path, src_frame_id, src_recv_ts,
         geometry, frame_rel) in rows:
        geom = canonical_geometry(geometry)
        frame_path = os.path.join(media_root, frame_rel) if frame_rel else None
        frame_live = bool(frame_path) and os.path.isfile(frame_path)
        row = {
            "id": int(rid),
            "cat_id": cat_id,
            "label_kind": label_kind,
            "bbox": _parse_bbox(bbox_text),
            "bbox_text": bbox_text,
            "crop_path": crop_path,
            "src_frame_id": int(src_frame_id),
            "src_recv_ts": int(src_recv_ts),
            "geometry": geom,
            "at_target": geom == target,
            "at_target_missing": False,
            "frame_path": frame_path if frame_live else None,
            "move": None,
            "blocked": None,
            "copy_from": None,
            "new_rel": None,
        }
        _route(row, target, target_margin, dataset_root)
        out.append(row)
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


def _copy_one(row: dict, target: "str | None", dataset_root: str) -> "str | None":
    """Copy ``row``'s margin-equal file into the target's path; return the new rel path.

    Same tmp-then-``os.replace`` discipline as ``_cut_one``, for the same reason — the
    destination is never a half-written file — but the bytes come from a crop already on
    disk rather than from the source frame, which is what lets an evicted-frame row move.
    """
    rel = crop_rel_path(
        row["cat_id"], row["label_kind"], row["src_frame_id"], row["src_recv_ts"], target
    )
    dest_abs = os.path.join(dataset_root, rel)
    tmp_abs = dest_abs + ".recut-tmp"
    try:
        os.makedirs(os.path.dirname(dest_abs) or ".", exist_ok=True)
        shutil.copyfile(os.path.join(dataset_root, row["copy_from"]), tmp_abs)
        os.replace(tmp_abs, dest_abs)
    except OSError:
        try:
            os.remove(tmp_abs)
        except OSError:
            pass
        return None
    return rel


def recut(
    write_moves,
    rows: "list[dict]",
    target: "str | None",
    dataset_root: str,
    batch: int = _BATCH,
    on_progress=None,
) -> dict:
    """Move ``rows`` (all carrying a ``move``) to ``target``; return a counts summary.

    ``write_moves`` is ``Store.update_dataset_geometry`` — ``[(item_id, new_rel_path,
    geometry, expected_old_crop_path, expected_bbox_text)] -> [ids actually updated]``.
    Injected rather than imported so this function is testable against a plain fake and
    so the tool holds no store lock of its own.

    Per batch: put every crop's pixels in place (relink does nothing, copy duplicates a
    margin-equal file, recut cuts from the frame), then ONE transaction moving those
    rows. Nothing is ever deleted — see *Superseded crops are KEPT* in the module
    docstring — and the batch is small so the write lock is held briefly against a live
    collector. A write that fails leaves its row completely untouched (old stamp, old
    file) and is counted in ``failed``; the run continues, because one unreadable frame
    is not a reason to abandon the other thousands.

    ``on_progress(done, total)`` is BOTH the progress report and the cancel signal: a
    FALSY return stops the run at the batch boundary (the convention ``embed_paths``
    defines and the managers produce). Unlike that one it does not raise — it breaks and
    returns the summary earned so far, because a canceled run still has to say what it
    moved, and an exception would discard exactly that.

    Returns ``{recut, copied, relinked, failed, rows_updated, old_files_kept}``.
    ``rows_updated`` counts the ids the UPDATE actually matched, not an assumption — a row
    replaced by a concurrent re-label between the read and the write simply does not
    update.
    """
    summary = {
        "recut": 0, "copied": 0, "relinked": 0,
        "failed": 0, "rows_updated": 0, "old_files_kept": 0,
    }
    _letterbox, margin = parse_geometry(target)
    canceled = False
    for start in range(0, len(rows), batch):
        chunk = rows[start:start + batch]
        moved: "list[tuple[dict, str]]" = []
        for row in chunk:
            # Branch on what `read_rows` routed this row to. Relink writes NO file — the
            # target's crop is already there — so it cannot fail before the row write.
            if row["move"] == "relink":
                # Re-check the file is STILL there. `_route` decided this at read time,
                # and a run spans minutes across many batches — long enough for a manual
                # reclaim of a stale convention's directory (which this module documents
                # as the normal way to free the disk) or a concurrent relabel's
                # variant-delete to remove it. Unlike copy/recut, relink does no I/O, so
                # nothing else would notice: the write would stamp the row onto a missing
                # file and report it as moved, which is the one outcome this module
                # promises cannot happen. Narrows the window to the gap before the write
                # rather than closing it, but that is microseconds against minutes.
                new_rel = row["new_rel"]
                if not os.path.isfile(os.path.join(dataset_root, new_rel)):
                    new_rel = None
            elif row["move"] == "copy":
                new_rel = _copy_one(row, target, dataset_root)
            else:
                new_rel = _cut_one(row, target, margin, dataset_root)
            if new_rel is None:
                summary["failed"] += 1
                continue
            moved.append((row, new_rel))
        if moved:
            # The new files are already on disk. If the write raises, no row names them:
            # harmless orphans a re-run overwrites. Nothing was destroyed, so it
            # propagates loudly rather than continuing against a DB that would not take
            # writes. (`update_dataset_geometry` rolls its own batch back.)
            #
            # The row's crop_path AND bbox AS READ are passed, so the write is a
            # compare-and-swap: rowids are reused (no AUTOINCREMENT) and
            # `/api/label/relabel` deletes and re-commits the same frames, so this id may
            # belong to a different, freshly labelled row by now. Path alone is not enough
            # — a relabel to the SAME cat re-commits at the identical legacy path — so the
            # box is compared too: it is exactly what must be unchanged for these pixels to
            # still belong to this row, and a relabel that moved the box fails the swap.
            updated_ids = set(
                write_moves([
                    (row["id"], new_rel, target, row["crop_path"], row["bbox_text"])
                    for row, new_rel in moved
                ])
            )
            summary["rows_updated"] += len(updated_ids)
            for row, new_rel in moved:
                summary["relinked" if row["move"] == "relink"
                        else "copied" if row["move"] == "copy" else "recut"] += 1
                # The superseded file is KEPT, always — that is what makes a move
                # reversible without a source frame. Counted so the card can say what the
                # disk bought; a relink supersedes nothing new, its old file simply stays.
                if row["id"] in updated_ids and row["crop_path"] != new_rel:
                    summary["old_files_kept"] += 1
        if on_progress is not None:
            # Falsy return = cancel. BREAK and return what was earned, rather than raising
            # as `embed_paths` does: a canceled run still has to report what it moved.
            # Called exactly once per batch — it is a progress REPORT as well as a signal,
            # so calling it twice would double-count in any consumer that accumulates.
            if not on_progress(min(start + batch, len(rows)), len(rows)):
                canceled = True
                break
    summary["canceled"] = canceled
    return summary


def census(rows: "list[dict]") -> "list[tuple[str | None, int]]":
    """``[(geometry, count), ...]`` over every labelled crop, commonest first.

    Legacy keeps its canonical spelling of ``None`` rather than the CLI's display word, so
    the API renders one value for it and callers need no translation step. Ties break on
    the rendered name so the order is stable between calls.
    """
    counts: "dict[str | None, int]" = {}
    for row in rows:
        counts[row["geometry"]] = counts.get(row["geometry"], 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0] or ""))


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
        rows = read_rows(conn, media_root, target, dataset_root)
        todo = [r for r in rows if r["move"]]
        blocked = [r for r in rows if r["blocked"]]
        if ns.limit is not None:
            todo = todo[:max(0, ns.limit)]

        print(f"Store:  {db_path}")
        print(f"Target: {target or _LEGACY_WORD}")
        print(f"Labelled crops with a file: {len(rows)}")
        for geom, count in census(rows):
            print(f"  {geom or _LEGACY_WORD:<24} {count}")
        # Split by BRANCH, not one total: a recut decodes a frame apiece (minutes across
        # thousands) while a relink is one UPDATE, so the recut count is the wait.
        by_move = {m: sum(1 for r in todo if r["move"] == m) for m in ("recut", "copy", "relink")}
        print(f"  -> to move:              {len(todo)}"
              f"  ({by_move['recut']} re-cut, {by_move['copy']} copy, "
              f"{by_move['relink']} relink)")
        missing = sum(1 for r in rows if r["at_target_missing"])
        if missing:
            print(f"  -> at target, FILE GONE: {missing}"
                  "  (a build would silently skip these)")
        if blocked:
            # These are the rows that stay behind, and WHY matters: an evicted frame is
            # the expected, permanent case (nothing can re-cut it), a missing box is a
            # data fault worth looking at.
            no_box = sum(1 for r in blocked if r["blocked"] == "no_box")
            gone = len(blocked) - no_box
            print(f"  -> cannot move:          {len(blocked)}"
                  f"  ({gone} source frame gone, {no_box} no usable box)")
            print("     They keep their current stamp and are excluded from builds at "
                  f"{target or _LEGACY_WORD}.")

        if not ns.apply:
            print("\nDry run — nothing written. Re-run with --apply to perform it.")
            return 0
        if not todo:
            print("\nNothing to do.")
            return 0

        def progress(done: int, total: int) -> bool:
            # MUST return truthy: `recut` reads a falsy return as cancel, so the bare
            # `print` this used to be would stop the CLI after its first batch and report
            # it as a clean success. The CLI has nothing to cancel it, so: always continue.
            print(f"  … {done}/{total}", flush=True)
            return True

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
        print(f"re-cut {summary['recut']}, copied {summary['copied']}, "
              f"relinked {summary['relinked']}, failed {summary['failed']}, "
              f"rows updated {summary['rows_updated']}, "
              f"superseded crops kept {summary['old_files_kept']}")
        # A failure left its row untouched, so the store is consistent either way — but
        # exit non-zero so a wrapper notices that the set did not fully move.
        return 1 if summary["failed"] else 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
