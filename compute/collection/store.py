"""The bounded frame store: a SQLite index over JPEGs on the filesystem.

One row per collected frame in ``index.db`` (stdlib ``sqlite3`` — no new
dependency), each pointing at a JPEG written verbatim to a date/hour-bucketed
media dir. SQLite buys fast time-ordered paging and motion/area filtering at
10 fps volume — the exact operations the tuning workflow leans on — which a flat
folder or a JSONL manifest can't (see the spec's *Alternatives considered*).

Retention is a rolling cap on total media bytes: after each insert, the oldest
rows (and their files) are evicted until the running total is back under the cap.
The total is kept in memory and recomputed from ``SUM(bytes)`` on startup, so it
never has to walk the disk per-add.

Concurrency (per the spec): the collector thread is the sole writer; the API
handlers read, and ``clear`` writes. There is ONE connection opened
``check_same_thread=False`` and a SINGLE ``threading.Lock`` held around *every*
operation — including ``add``'s file write — so the collector and a racing
``clear`` are fully serialized and can never leave a row without its file or a
file without its row. Writes are tiny (~67 KB) and browse reads are human-paced,
so the coarse lock costs nothing in practice.
"""
from __future__ import annotations

import bisect
import json
import math
import os
import shutil
import sqlite3
import threading
import time
from datetime import datetime

from compute.analysis import ANALYZER_NAMES  # import-light registry (no ML); single source of oracle ids

# Oldest-first eviction batch size: eviction selects and deletes rows in chunks
# rather than one round-trip per row, so freeing space after a burst stays cheap.
_EVICT_BATCH = 64

# The columns every query selects, in this order, so _row_to_dict can unpack a
# fetched tuple positionally without re-stating the layout at each call site.
_ROW_COLUMNS = "id, recv_ts, edge_ts, frame_id, motion, area, bbox"

# The same columns qualified to the ``frames`` alias ``f``, for the disagreement
# query that JOINs the analysis table (where bare "id"/"motion" would be
# ambiguous). Derived from _ROW_COLUMNS so the two lists can never drift.
_ROW_COLUMNS_F = ", ".join("f." + c for c in _ROW_COLUMNS.split(", "))

_ALLOWED_MOTION = ("all", "motion", "still")
_ALLOWED_ORDER = ("time", "area_desc", "area_asc")
# Disagreement modes for query_disagreements: which side of an oracle verdict vs.
# the edge's MOG2 motion flag we want to eyeball. "missed" = MOG2 saw no motion
# but the oracle says the subject is present (a genuine gate miss); "false" =
# MOG2 fired but the oracle sees nothing (a false trigger — leaf/shadow/person).
_ALLOWED_DISAGREE = ("missed", "false")

# Sentinel distinguishing "no score column was selected" from a genuine NULL
# score, so _row_to_dict adds the "score" key only for disagreement rows and
# leaves the plain browse feed's row shape exactly as it was.
_NO_SCORE = object()

# Oracles gate_scorecard scores a motion source against — the ground-truth
# analyzers (see the motion-gate-oracles layer). A `source` is either the live
# gate ("live") or an analysis slot name (e.g. "mog2:candidate"). Derived from the
# registry's ANALYZER_NAMES — the single source of truth — so a newly registered
# oracle (e.g. "yolo-serial") is scoreable without a second list to keep in sync;
# hardcoding these once let a new oracle 500 the scorecard endpoint.
_SCORECARD_ORACLES = ANALYZER_NAMES

# Visit clustering (gate_scorecard): consecutive oracle-present frames whose
# recv_ts gap is within _VISIT_GAP_MS belong to the same visit; a visit counts
# as caught if any source-motion frame falls inside its recv_ts span expanded by
# _VISIT_WINDOW_MS on each side (the gate may fire just before/after the oracle
# sees the cat — on approach, or in the tail — so an exact-frame match is too
# strict for "did this visit cost a GPU trigger?"). Both are in milliseconds,
# matching recv_ts.
_VISIT_GAP_MS = 2000
_VISIT_WINDOW_MS = 3000

# Server-side cap on how many frames ``sample_frames`` returns, so a wide window
# can never request tens of thousands of thumbnails: the density viewer computes
# ``count = X × span-minutes`` and this clamps it into ``[1, _MAX_SAMPLE]``.
_MAX_SAMPLE = 4000

# Server-side cap on how many activity events ``events`` returns. Clustering is
# cheap (it runs over the sparse motion frames only), so this bounds the response
# and the client's DOM/JSON size, NOT compute: a busy multi-day store could yield
# thousands of clusters, and the activity grid renders one card each. When the cap
# bites, ``events`` flags ``truncated`` so the UI can prompt for a narrower date.
_MAX_EVENTS = 500

# The visit-inbox error modes ``visits`` clusters and ranks (see the
# motion-detection-workflow spec). "missed"/"false" judge the LIVE edge gate
# (``frames.motion``) against one oracle; "conflict" compares the two oracles
# (YOLO vs BSUV) and ignores the ``oracle`` argument.
_VISIT_MODES = ("missed", "false", "conflict")

# Annotation-tool enums for the durable ``dataset_items`` table (see the
# cat-identity annotation-tool spec). ``label_kind`` is the NOT NULL discriminator
# (``cat_id`` set only for ``identified``); ``quality`` is the per-crop signal a
# future gallery build filters on (NULL for a ``not_cat`` row, which has no crop).
# Both are validated in ``add_dataset_items`` so a typo can never silently poison
# the precious, eviction-surviving label set.
_LABEL_KINDS = ("identified", "unknown_cat", "not_cat")
_QUALITIES = ("gallery", "ok", "poor")

# Minimum yolo-serial detection confidence for a frame to enter the annotation
# queue (see ``_present_frames``). The oracle runs recall-first at conf 0.15 and
# hallucinates cats on empty frames (a bare tile floor reads as a low-conf "cat"),
# so without a floor the queue fills with empty-scene phantoms the annotator would
# just mark "not a cat" — pure noise, since an empty scene isn't a useful negative.
# Mirrors the gate scorecard's default oracle floor; fixed (not per-request) because
# the queue's clean-crop purpose wants a stable, meaningful universe, not a knob.
_ANNOTATE_MIN_CONF = 0.3


def _parse_id_cursor(cursor: str) -> int:
    """Decode a time-order cursor (an id). Raises ValueError if malformed."""
    try:
        return int(cursor)
    except (TypeError, ValueError):
        raise ValueError(f"invalid time cursor: {cursor!r}")


def _parse_area_cursor(cursor: str) -> "tuple[float, int]":
    """Decode an area-order cursor, formatted ``"<area>:<id>"``.

    The compound (area, id) key is what lets the area/triage views page beyond a
    single top-N slice (id breaks ties within an equal area). ``rsplit`` on the
    last ``:`` so a float like ``"0.05"`` is unambiguous. Raises ValueError if
    malformed — since the value only ever comes from our own ``next_cursor``,
    that signals a bug/tamper, surfaced as a 400 rather than silently ignored.
    """
    try:
        area_str, id_str = str(cursor).rsplit(":", 1)
        return float(area_str), int(id_str)
    except (ValueError, AttributeError):
        raise ValueError(f"invalid area cursor: {cursor!r}")


def _range_bounds(col: str, since_id: "int | None", until_id: "int | None") -> "tuple[list, list]":
    """Inclusive id-range SQL fragments + bind params for one column.

    The single owner of the ``since_id`` / ``until_id`` scope semantics every
    scoped read shares (see the frame-range-groups spec): returns
    ``(fragments, params)`` where ``fragments`` is 0–2 of ``"<col> >= ?"`` /
    ``"<col> <= ?"`` and ``params`` the matching ``int`` bounds, in the same
    order — so a caller splices the fragments into its WHERE (joined with AND) and
    extends its params. Empty on both sides when unbounded, so an unscoped read is
    byte-for-byte unchanged. ``col`` is a fixed identifier (``"id"``, ``"f.id"``,
    ``"frame_id"``), never user input, so interpolating it is safe. Centralizing it
    means a future change to the range semantics can't scope one of the seven-plus
    call sites differently from the rest.
    """
    fragments: list = []
    params: list = []
    if since_id is not None:
        fragments.append(f"{col} >= ?")
        params.append(int(since_id))
    if until_id is not None:
        fragments.append(f"{col} <= ?")
        params.append(int(until_id))
    return fragments, params


class Store:
    """SQLite index + media dir + size-based retention for collected frames."""

    def __init__(
        self, db_path: str, media_root: str, max_bytes: int, dataset_root: "str | None" = None
    ) -> None:
        self._db_path = db_path
        self._media_root = media_root
        self._max_bytes = max_bytes
        # Durable annotation crops live in a dir SIBLING to the rolling media/ (a
        # default derived from db_path's parent — ``<root>/dataset`` — so existing
        # three-arg callers and tests keep working). Kept separate from media/ so
        # crops persist when the frames they came from age out or ``clear``. The
        # store itself never writes crop files here (that is the dataset/crops
        # module's job); it only makes the root exist and hands out its path.
        self._dataset_root = dataset_root or os.path.join(os.path.dirname(db_path) or ".", "dataset")
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        os.makedirs(media_root, exist_ok=True)
        os.makedirs(self._dataset_root, exist_ok=True)
        # One shared connection across the collector and API threads; the lock
        # (not sqlite's own thread check) is what makes that safe, so disable the
        # check. A short busy_timeout is belt-and-braces against any stray lock.
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA busy_timeout = 5000")
        # WAL + synchronous=NORMAL make each commit cheap — fsync is deferred to
        # the next checkpoint instead of running on every commit. Because a single
        # connection under one lock serializes ALL access, the win is commit COST,
        # not reader/writer concurrency (there is none to gain). Accepted
        # consequence: a hard power loss may lose the last few un-checkpointed
        # commits, which can leave an orphan JPEG on the collector (file written,
        # its row lost) that _total_bytes never counts and so never evicts — a
        # small, non-self-healing leak, NOT corruption. WAL persists on the DB
        # file (adds -wal/-shm sidecars); `close` bounds the exposure.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._lock = threading.Lock()
        self._init_schema()
        # Recompute the running byte total once from the DB rather than tracking
        # it across restarts — it must survive a process restart and stay exact.
        row = self._conn.execute("SELECT COALESCE(SUM(bytes), 0) FROM frames").fetchone()
        self._total_bytes = int(row[0])

    def _init_schema(self) -> None:
        # Schema is fixed by the spec; the (motion, area) index is what makes the
        # triage views (top-area still / bottom-area motion) fast.
        #
        # The `analysis` table holds one offline-oracle verdict per (frame, analyzer)
        # — an observation *about* a frame, kept separate from the collector's hot
        # `frames` write table so a new oracle is new rows, not a migration (see
        # the motion-gate-oracles spec). `PRIMARY KEY (frame_id, analyzer)` makes a
        # re-run an INSERT OR REPLACE; there is no FK to `frames` — the cascade on
        # eviction/clear is handled in code, under the same lock, so a verdict
        # never outlives its frame. The (analyzer, verdict) index serves the
        # disagreement query's verdict filter and the present-count summary.
        #
        # The `groups` table is a thin name→bounds bookmark: a saved, contiguous
        # frame window [start_id, end_id] that scopes the tuning tools to a slice
        # (see the frame-range-groups spec). It stores no membership — a group is
        # id *bounds* evaluated live against `frames`, so its `count` is a
        # primary-key range scan, not a stored set. That is why it needs no
        # eviction cascade (a wholly-evicted group simply counts zero, its bounds
        # still valid as ids advance monotonically) and, unlike `analysis`, is
        # dropped only by a full `clear` (see the note there). `start_ts`/`end_ts`
        # denormalize the endpoints' recv_ts so the window's wall-clock span
        # survives those endpoint frames aging out.
        #
        # The `settings` table is a tiny key→value config store (motion-only
        # capture flag + the last operator collector-running intent) that reuses
        # this same connection + lock instead of a second settings.json file. It
        # is CONFIG, not frame data, so — unlike `analysis`/`groups`/`mode_changes`
        # — `clear` does NOT wipe it (see the note there).
        #
        # The `mode_changes` table is an append-only log of motion-only capture
        # toggles: one row per flip (and one for the initial state on first
        # collect), stamped with the store's latest frame id + recv_ts AT the flip.
        # That is a step function over the id axis, from which `motion_only_spans`
        # reconstructs the ON sub-ranges overlapping any window — so a window that
        # spans a motion-only stretch is flagged "misses unmeasurable" rather than
        # read as perfect recall. Like `groups` it is keyed to frame ids, so a full
        # `clear` (which reuses rowids from 1) drops it too, or stale boundaries
        # would misalign against new frames. `idx_frames_recv_ts` makes the clock→id
        # resolution and the density timeline's recv_ts binning indexed lookups.
        #
        # The `cats` + `dataset_items` tables back the cat-identity annotation tool
        # (see its spec). `cats` is the editable roster (residents + named
        # neighbours); `dataset_items` is one self-contained labelled crop per row
        # (`cat_id`/`label_kind`/`quality`/`bbox`/`crop_path` + the source frame
        # linkage). Unlike every frame-keyed table above, NEITHER is touched by
        # `_evict_locked` OR `clear` — they hold hand-made labels, the precious
        # output, and carry no FK to `frames` (only a `src_frame_id`/`src_recv_ts`
        # snapshot), so they outlive the rolling buffer exactly like `settings`.
        # The queue's "already decided" check keys on BOTH `src_frame_id` AND
        # `src_recv_ts` (idx_dataset_src), so a `clear` + rowid-reuse can't make an
        # old label mask a brand-new frame that reused its id: recv_ts won't match.
        # idx_dataset_src is UNIQUE (not just an index): the dedup key is enforced at
        # WRITE time, not only read time, so a double-submit / stale second tab can't
        # insert a second, possibly-conflicting row for the same crop (see add_dataset_items).
        # idx_dataset_cat serves the per-cat label rollups a later gallery build wants.
        #
        # The `feasibility_runs` table is one row per validation (feasibility-probe)
        # run from the Training page (see the training-page spec): the separability
        # metrics (kNN accuracy, same-vs-different-cat AUC, suggested threshold) over
        # the labelled crops of a chosen `quality` selection, plus the `report_dir`
        # basename of the run's rendered HTML report. Like `cats`/`dataset_items` it is
        # precious hand-derived output, so — unlike every frame-keyed table above —
        # NEITHER `_evict_locked` NOR `clear` touches it: run history is decoupled from
        # the rolling frame buffer and survives a wipe. Rows are kept indefinitely (a
        # row is tiny); only the on-disk report DIRS are bounded (prune_feasibility_reports),
        # so an aged-out run keeps its metrics row while its report endpoint 404s.
        #
        # The `model_versions` + `identifications` tables back the runtime
        # identification pass (see the identification-gallery-activity spec).
        # `model_versions` is one row per built gallery: `id` IS the human-facing
        # version number (`v<id>`, AUTOINCREMENT so it is never reused even across
        # deletes), `status` is draft|active|retired with EXACTLY ONE active enforced
        # by `promote_model`, and `gallery_dir` is the `models_root`-relative basename
        # holding the on-disk `gallery.npz` (vectors live on the filesystem, not in the
        # row — mirroring the architecture's file-based model store). Like
        # `cats`/`dataset_items`/`feasibility_runs` it is PRECIOUS hand-derived output:
        # NEITHER `_evict_locked` NOR `clear` touches it, so a promoted model is
        # decoupled from the rolling frame buffer and survives a wipe.
        # `identifications` is one row per (frame, model): the NEAREST gallery cat and
        # its cosine `distance`, ALWAYS stored — "unknown" is derived at READ time by
        # `events()` comparing that distance to the model's tunable `threshold`, never
        # baked into the row (so editing the threshold re-renders the feed with no
        # re-identify). `PRIMARY KEY (frame_id, model_version_id)` makes an identify
        # re-run an INSERT OR REPLACE; and — exactly like `analysis` — it is
        # FRAME-KEYED with no FK to `frames`, so BOTH `_evict_locked` and `clear`
        # cascade-delete it in code under the same lock (an identification about a gone
        # frame is meaningless and cheaply recomputed from the durable gallery).
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS frames (
              id       INTEGER PRIMARY KEY,
              recv_ts  INTEGER NOT NULL,
              edge_ts  INTEGER NOT NULL,
              frame_id INTEGER NOT NULL,
              motion   INTEGER NOT NULL,
              area     REAL    NOT NULL,
              bbox     TEXT,
              path     TEXT    NOT NULL,
              bytes    INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_frames_motion_area ON frames(motion, area);
            CREATE TABLE IF NOT EXISTS analysis (
              frame_id INTEGER NOT NULL,
              analyzer TEXT    NOT NULL,
              verdict  INTEGER NOT NULL,
              score    REAL,
              detail   TEXT,
              ran_at   INTEGER NOT NULL,
              PRIMARY KEY (frame_id, analyzer)
            );
            CREATE INDEX IF NOT EXISTS idx_analysis_analyzer_verdict ON analysis(analyzer, verdict);
            CREATE TABLE IF NOT EXISTS groups (
              id         INTEGER PRIMARY KEY,
              name       TEXT    NOT NULL,
              start_id   INTEGER NOT NULL,   -- frames.id lower bound (inclusive)
              end_id     INTEGER NOT NULL,   -- frames.id upper bound (inclusive)
              start_ts   INTEGER NOT NULL,   -- recv_ts of the start frame, captured at create
              end_ts     INTEGER NOT NULL,   -- recv_ts of the end frame, captured at create
              created_ts INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (
              key   TEXT PRIMARY KEY,
              value TEXT
            );
            CREATE TABLE IF NOT EXISTS mode_changes (
              at_id       INTEGER NOT NULL,   -- frames.id at the flip (latest id at the time)
              at_ts       INTEGER NOT NULL,   -- recv_ts at the flip
              motion_only INTEGER NOT NULL    -- 1 = motion-only capture on, 0 = full capture
            );
            CREATE INDEX IF NOT EXISTS idx_frames_recv_ts ON frames(recv_ts);
            CREATE TABLE IF NOT EXISTS cats (
              id          INTEGER PRIMARY KEY,
              name        TEXT    NOT NULL UNIQUE,
              is_resident INTEGER NOT NULL DEFAULT 0,   -- 1 = our cat, 0 = named foreign/neighbour
              active      INTEGER NOT NULL DEFAULT 1,   -- retire without deleting labels
              notes       TEXT,
              created_ts  INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS dataset_items (
              id           INTEGER PRIMARY KEY,
              cat_id       INTEGER,                     -- set iff label_kind = 'identified'
              label_kind   TEXT    NOT NULL,            -- 'identified' | 'unknown_cat' | 'not_cat'
              quality      TEXT,                        -- 'gallery' | 'ok' | 'poor' (NULL for not_cat)
              bbox         TEXT,                        -- "x1,y1,x2,y2" px in the source frame (NULL for not_cat)
              crop_path    TEXT,                        -- dataset-media-relative jpg (NULL for not_cat)
              src_frame_id INTEGER NOT NULL,            -- frames.id at label time (linkage only)
              src_recv_ts  INTEGER NOT NULL,            -- frames.recv_ts, the clear()-safe dedup guard
              source       TEXT    NOT NULL DEFAULT 'detector',
              labeled_ts   INTEGER NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_dataset_src ON dataset_items(src_frame_id, src_recv_ts);
            CREATE INDEX IF NOT EXISTS idx_dataset_cat ON dataset_items(cat_id);
            CREATE TABLE IF NOT EXISTS feasibility_runs (
              id           INTEGER PRIMARY KEY AUTOINCREMENT,
              ts           INTEGER NOT NULL,          -- ms epoch at the run's completion
              quality      TEXT    NOT NULL,          -- slug: 'all' | 'gallery' | 'gallery+ok' | ...
              n_crops      INTEGER NOT NULL,
              n_cats       INTEGER NOT NULL,
              knn_accuracy REAL,
              auc          REAL,
              threshold    REAL,
              report_dir   TEXT    NOT NULL,          -- training_root-relative basename of the report dir
              notes        TEXT
            );
            CREATE TABLE IF NOT EXISTS model_versions (
              id          INTEGER PRIMARY KEY AUTOINCREMENT,  -- IS the version number ("v3"); never reused (precious)
              status      TEXT    NOT NULL,        -- 'draft' | 'active' | 'retired'
              kind        TEXT    NOT NULL,        -- 'gallery' (only kind for now)
              backbone    TEXT    NOT NULL,        -- resolved embedder backbone, e.g. 'dinov2_vits14'
              imgsz       INTEGER NOT NULL,        -- resolved embedder input side
              n_cats      INTEGER NOT NULL,
              n_vectors   INTEGER NOT NULL,
              threshold   REAL,                    -- suggested same/different cutoff; NULL when uncomputable
              quality     TEXT    NOT NULL,        -- slug: 'all' | 'gallery' | 'gallery+ok' | ...
              metrics     TEXT,                    -- JSON: per-cat counts + build separability
              gallery_dir TEXT    NOT NULL,        -- models_root-relative basename holding gallery.npz
              created_ts  INTEGER NOT NULL,
              notes       TEXT
            );
            CREATE TABLE IF NOT EXISTS identifications (
              frame_id         INTEGER NOT NULL,   -- frames.id (the identified frame)
              model_version_id INTEGER NOT NULL,   -- which gallery produced this
              cat_id           INTEGER,            -- NEAREST gallery cat (match); NULL = "processed, un-embeddable" marker
              distance         REAL,               -- cosine distance to that nearest vector; NULL for a marker row
              bbox             TEXT,               -- "x1,y1,x2,y2" the crop was cut from (audit)
              ran_at           INTEGER NOT NULL,
              PRIMARY KEY (frame_id, model_version_id)
            );
            """
        )
        self._conn.commit()

    def add(self, frame, recv_ts_ms: int) -> int:
        """Persist one stream frame and return its new row id.

        Writes ``frame.jpeg`` verbatim (the edge already encoded q90 — never
        decode/re-encode here), inserts the row, adds its size to the running
        total, then evicts oldest rows+files while the total exceeds the cap.
        The ENTIRE operation — file write included — runs under the lock so it is
        atomic against a concurrent ``clear`` (which walks live rows): the row
        and its file always appear and disappear together.
        """
        meta = frame.meta
        jpeg = frame.jpeg
        # Per-hour buckets keep any one dir near ~36k files at 10 fps. recv_ts is
        # the compute receive clock (the reliable axis; the Pi has no RTC).
        dt = datetime.fromtimestamp(recv_ts_ms / 1000.0)
        rel_dir = os.path.join(dt.strftime("%Y-%m-%d"), dt.strftime("%H"))
        rel_path = os.path.join(rel_dir, f"{recv_ts_ms}_f{meta.frame_id}.jpg")
        abs_path = os.path.join(self._media_root, rel_path)
        # bbox is None when motion is inactive (no blob → no box); store the
        # normalized 4-tuple as "x,y,w,h" text, NULL otherwise.
        bbox_text = ",".join(str(v) for v in meta.bbox) if meta.bbox is not None else None

        with self._lock:
            try:
                os.makedirs(os.path.join(self._media_root, rel_dir), exist_ok=True)
                with open(abs_path, "wb") as fh:
                    fh.write(jpeg)
                n_bytes = len(jpeg)
                cur = self._conn.execute(
                    "INSERT INTO frames (recv_ts, edge_ts, frame_id, motion, area, bbox, path, bytes)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        int(recv_ts_ms),
                        int(meta.ts),
                        int(meta.frame_id),
                        1 if meta.motion else 0,
                        float(meta.area),
                        bbox_text,
                        rel_path,
                        n_bytes,
                    ),
                )
                new_id = cur.lastrowid
                self._total_bytes += n_bytes
                self._evict_locked()
                self._conn.commit()
                return int(new_id)
            except Exception:
                # A failure ANYWHERE in the write→insert→evict sequence (a
                # disk-full write, a DB error, an eviction error) must leave the
                # store consistent: don't leave a half-done transaction on this
                # shared connection — the NEXT add's commit would flush it — an
                # uncounted orphan file on disk, or the in-memory total diverged
                # from the DB. Roll the DB back, drop this frame's just-written
                # file if it reached disk (its row no longer exists), and resync
                # the total from the committed rows. Then re-raise for the caller.
                self._conn.rollback()
                self._unlink(rel_path)
                row = self._conn.execute("SELECT COALESCE(SUM(bytes), 0) FROM frames").fetchone()
                self._total_bytes = int(row[0])
                raise

    def _evict_locked(self) -> None:
        """Delete oldest rows+files while the running total exceeds the cap.

        Caller holds the lock. Evicts by ascending ``id`` (insertion order = age),
        keeping ``_total_bytes`` in lockstep as each file is removed. A missing
        file (already gone) is ignored — the row is still dropped and its recorded
        size still subtracted, so the total can't drift.
        """
        while self._total_bytes > self._max_bytes:
            rows = self._conn.execute(
                "SELECT id, path, bytes FROM frames ORDER BY id ASC LIMIT ?",
                (_EVICT_BATCH,),
            ).fetchall()
            if not rows:
                # Total says we're over cap but there are no rows to drop — the
                # total is authoritative-from-DB, so this can't happen; guard
                # anyway so a bad cap (< one frame) can't spin forever.
                self._total_bytes = 0
                return
            for row_id, rel_path, n_bytes in rows:
                self._unlink(rel_path)
                self._conn.execute("DELETE FROM frames WHERE id = ?", (row_id,))
                # Cascade: drop any oracle verdicts about this frame in the same
                # locked section, so retention can never leave an analysis row
                # pointing at a frame (and its file) that no longer exists.
                self._conn.execute("DELETE FROM analysis WHERE frame_id = ?", (row_id,))
                # Same frame-keyed cascade for identifications (identification-gallery
                # spec): an identification describes a frame, so it evicts with it —
                # cheap to recompute from the durable gallery, so never precious.
                self._conn.execute("DELETE FROM identifications WHERE frame_id = ?", (row_id,))
                self._total_bytes -= int(n_bytes)
                if self._total_bytes <= self._max_bytes:
                    break

    def _unlink(self, rel_path: str) -> None:
        """Remove one media file by its DB-relative path; best-effort.

        Swallows FileNotFoundError (already gone) AND any other OSError. Letting a
        stray delete error escape eviction would propagate into ``add``'s rollback
        and RESURRECT rows whose files were already deleted earlier in the same
        batch — a worse, inconsistent state (rows pointing at missing files, an
        inflated total). A file we genuinely can't delete is a rare, pathological
        leak; keeping the DB and byte total consistent matters more.
        """
        try:
            os.remove(os.path.join(self._media_root, rel_path))
        except OSError:
            pass

    def query(
        self,
        cursor: "str | None",
        limit: int,
        motion: str,
        order: str,
        since_id: "int | None" = None,
        until_id: "int | None" = None,
    ):
        """Return ``(rows, next_cursor)`` for one page of the browse feed.

        ``motion`` ∈ {all, motion, still}; ``order`` ∈ {time, area_desc, area_asc}.
        Every order is keyset-paginated (no OFFSET), so paging stays stable and
        deep even while the collector keeps inserting newer frames. ``cursor`` is
        an OPAQUE token — the caller passes back whatever the prior page returned
        as ``next_cursor`` (``None`` for the first page); ``next_cursor`` is
        ``None`` once a page is short (fewer than ``limit`` → no more rows).

        - ``time``: newest-first (``id DESC``); the window advances with
          ``id < cursor``. Token is the last id.
        - ``area_desc`` / ``area_asc``: ordered by ``area`` then ``id DESC``; the
          window advances over the compound ``(area, id)`` key. Token is
          ``"<area>:<id>"``.

        ``since_id`` / ``until_id`` are an optional inclusive id-range scope
        (``None`` = unbounded on that side), so a group can scope the feed to its
        window (see the frame-range-groups spec). They AND with the keyset cursor
        predicate, so paging is unaffected; absent, the feed is the whole store
        exactly as before.
        """
        if motion not in _ALLOWED_MOTION:
            raise ValueError(f"motion must be one of {_ALLOWED_MOTION}, got {motion!r}")
        if order not in _ALLOWED_ORDER:
            raise ValueError(f"order must be one of {_ALLOWED_ORDER}, got {order!r}")

        where = []
        params: list = []
        if motion == "motion":
            where.append("motion = 1")
        elif motion == "still":
            where.append("motion = 0")

        if order == "time":
            order_by = "id DESC"
            if cursor is not None:
                where.append("id < ?")
                params.append(_parse_id_cursor(cursor))
        else:
            # Compound (area, id) keyset. id DESC breaks ties within an equal
            # area; the seek predicate is the standard row-value comparison
            # unrolled into an OR so SQLite can use the (motion, area) index.
            area_dir = "DESC" if order == "area_desc" else "ASC"
            order_by = f"area {area_dir}, id DESC"
            if cursor is not None:
                c_area, c_id = _parse_area_cursor(cursor)
                cmp = "<" if order == "area_desc" else ">"
                where.append(f"(area {cmp} ? OR (area = ? AND id < ?))")
                params.extend([c_area, c_area, c_id])

        # Optional id-range scope; ANDs with the motion filter and keyset cursor.
        range_frags, range_params = _range_bounds("id", since_id, until_id)
        where.extend(range_frags)
        params.extend(range_params)

        clause = (" WHERE " + " AND ".join(where)) if where else ""
        params.append(int(limit))
        with self._lock:
            fetched = self._conn.execute(
                f"SELECT {_ROW_COLUMNS} FROM frames{clause} ORDER BY {order_by} LIMIT ?",
                params,
            ).fetchall()

        rows = [self._row_to_dict(r) for r in fetched]
        # A full page implies there may be more → hand back an opaque token the
        # caller passes straight back. str(float) round-trips exactly through
        # float(), so the area token seeks to precisely this row's key.
        next_cursor = None
        if len(rows) == limit:
            last = rows[-1]
            next_cursor = str(last["id"]) if order == "time" else f"{last['area']}:{last['id']}"
        return rows, next_cursor

    @staticmethod
    def _row_to_dict(row, score=_NO_SCORE) -> dict:
        row_id, recv_ts, edge_ts, frame_id, motion, area, bbox = row
        result = {
            "id": row_id,
            "recv_ts": recv_ts,
            "edge_ts": edge_ts,
            "frame_id": frame_id,
            "motion": bool(motion),
            "area": area,
            "bbox": [float(v) for v in bbox.split(",")] if bbox else None,
            "url": f"/media/{row_id}",
        }
        # Disagreement rows carry the oracle's score alongside the frame fields;
        # the plain browse feed omits it. The sentinel default keeps query()'s row
        # shape byte-for-byte unchanged while query_disagreements() adds "score"
        # (which may itself be None — a verdict with no numeric score).
        if score is not _NO_SCORE:
            result["score"] = score
        return result

    def path_for(self, frame_row_id: int) -> "str | None":
        """Absolute path to a row's JPEG, or ``None`` if the row is unknown.

        Returns the path even if the file is missing on disk — existence is the
        caller's (the media route's) check — so a stale row still resolves to
        where its file *would* be.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT path FROM frames WHERE id = ?", (int(frame_row_id),)
            ).fetchone()
        if row is None:
            return None
        return os.path.join(self._media_root, row[0])

    @property
    def dataset_root(self) -> str:
        """Root dir for durable annotation crops (sibling of ``media/``).

        The dataset/crops module materialises labelled crops under here and stores
        their ``crop_path`` relative to it; the store only guarantees the dir
        exists (``__init__``) and never writes into it itself.
        """
        return self._dataset_root

    def stats(self) -> dict:
        """Store summary: counts, size vs cap, and the recv_ts time span.

        ``oldest_ts`` / ``newest_ts`` are ``recv_ts`` (the reliable compute axis)
        and are ``None`` when the store is empty.
        """
        with self._lock:
            count, motion_count, oldest_ts, newest_ts = self._conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(motion), 0), MIN(recv_ts), MAX(recv_ts) FROM frames"
            ).fetchone()
            total_bytes = self._total_bytes
        return {
            "count": count,
            "bytes": total_bytes,
            "cap_bytes": self._max_bytes,
            "motion_count": motion_count,
            "oldest_ts": oldest_ts,
            "newest_ts": newest_ts,
        }

    def clear(self) -> int:
        """Delete all rows and their media files; reset the total. Returns count.

        Runs under the lock, so it is atomic against ``add`` (which also holds the
        lock for its whole write): ``clear`` only ever sees complete rows, and
        deletes each row's file by its recorded path before dropping the row.
        """
        with self._lock:
            rows = self._conn.execute("SELECT path FROM frames").fetchall()
            for (rel_path,) in rows:
                self._unlink(rel_path)
            self._conn.execute("DELETE FROM frames")
            # Cascade: wiping the frames wipes every oracle verdict too, so a
            # cleared store starts with no analysis rows dangling.
            self._conn.execute("DELETE FROM analysis")
            # Same frame-keyed cascade for identifications: they describe frames, so a
            # full wipe drops them alongside the analysis verdicts (identification-
            # gallery spec). `model_versions` is NOT dropped — see the note below.
            self._conn.execute("DELETE FROM identifications")
            # Also drop every saved group — unlike eviction, which leaves groups
            # alone. ``_evict_locked`` only removes the OLDEST rows while ids keep
            # advancing monotonically, so a surviving group's [start_id, end_id]
            # still bounds valid (if fewer) live frames. ``clear`` is a FULL wipe,
            # after which SQLite reuses rowids from 1 — a stale group's old id
            # range would then spuriously match brand-new, unrelated frames, so a
            # full clear must drop groups too.
            self._conn.execute("DELETE FROM groups")
            # Drop the motion-only change log for the SAME rowid-reuse reason as
            # groups: its `at_id` boundaries are frame ids, and a full clear resets
            # ids from 1, so stale spans would misalign against brand-new frames. A
            # clear during a live motion-only run leaves the mode active over an
            # EMPTY log, so re-seeding the current mode (latest id after the wipe)
            # is the caller's job (the /api/clear route) — NOT clear()'s, which
            # doesn't know whether collection is live. `settings` is deliberately
            # NOT dropped — it is config, so `motion_only` survives the wipe. For the
            # SAME reason, `cats`, `dataset_items`, `feasibility_runs`, and
            # `model_versions` are NOT dropped here (nor by `_evict_locked`): they are
            # precious hand-made output (the annotation tool's labels, the Training
            # page's validation history, and the built/promoted gallery versions),
            # self-contained (no FK to `frames`), and the annotation tables' queue-dedup
            # keys on (`src_frame_id`, `src_recv_ts`) so a post-clear rowid reuse can't
            # collide (recv_ts advances) — see the annotation-tool + training-page +
            # identification-gallery specs.
            self._conn.execute("DELETE FROM mode_changes")
            self._conn.commit()
            self._total_bytes = 0
            return len(rows)

    # --- Settings + collector-mode persistence ------------------------------
    #
    # A tiny KV (`settings`) plus the motion-only change log (`mode_changes`),
    # both on the store's single connection + lock. The store stays policy-free:
    # it records what it is told and reconstructs spans, but the WHEN of writing
    # intent (only on operator start/stop, never on the process-exit hook) is the
    # API route's decision, not the store's.

    def get_setting(self, key: str) -> "str | None":
        """Value stored under ``key`` in the settings KV, or ``None`` if unset."""
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        return row[0] if row is not None else None

    def set_setting(self, key: str, value: str) -> None:
        """Upsert ``key`` → ``value`` in the settings KV (INSERT OR REPLACE)."""
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value)
            )
            self._conn.commit()

    def record_mode_change(self, motion_only: bool) -> None:
        """Append one ``mode_changes`` row for a motion-only capture flip.

        Stamps the row with ``at_id = COALESCE(MAX(id), 0)`` and ``at_ts =
        COALESCE(MAX(recv_ts), now_ms)`` from ``frames`` — i.e. the store's latest
        frame at the moment of the flip, so the new mode applies to frames
        collected AFTER it. Called for the initial mode on first collect, on every
        operator toggle, and again after a mid-run ``clear`` re-seeds the current
        mode (see ``clear``). ``motion_only`` is coerced to a 0/1 int.
        """
        now_ms = int(time.time() * 1000)
        with self._lock:
            (at_id,) = self._conn.execute("SELECT COALESCE(MAX(id), 0) FROM frames").fetchone()
            (at_ts,) = self._conn.execute(
                "SELECT COALESCE(MAX(recv_ts), ?) FROM frames", (now_ms,)
            ).fetchone()
            self._conn.execute(
                "INSERT INTO mode_changes (at_id, at_ts, motion_only) VALUES (?, ?, ?)",
                (int(at_id), int(at_ts), 1 if motion_only else 0),
            )
            self._conn.commit()

    def motion_only_spans(
        self, since_id: "int | None" = None, until_id: "int | None" = None
    ) -> "list[dict]":
        """The motion-only ON sub-ranges overlapping ``[since_id, until_id]``.

        Reconstructs the motion-only step function from ``mode_changes`` (ordered
        by ``at_id`` ASC, ties by insertion ``rowid``), coalescing consecutive
        equal states, and returns the ON segments that overlap the window as
        ``{"start_id", "end_id"}`` dicts clipped to it. Each segment runs from its
        change's ``at_id`` to the NEXT change's ``at_id``; the final segment (no
        successor) runs to ``until_id`` — or, when ``until_id`` is ``None``, to
        ``COALESCE(MAX(frames.id), 0)`` (the store end). ``since_id``/``until_id``
        ``None`` mean the store start / end. Empty list when the window is wholly
        full-capture (the common, default-off case).

        A window overlapping any returned span is where a *miss* — a cat present
        while the motion flag is 0 — was never stored, so recall reads as
        unmeasurable rather than perfect (and BSUV / MOG2-rerun verdicts across the
        span are unreliable too, since those oracles assume contiguous frames).
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT at_id, motion_only FROM mode_changes ORDER BY at_id ASC, rowid ASC"
            ).fetchall()
            if not rows:
                return []
            if until_id is None:
                (store_end,) = self._conn.execute(
                    "SELECT COALESCE(MAX(id), 0) FROM frames"
                ).fetchone()
                store_end = int(store_end)
            else:
                store_end = int(until_id)

        # Coalesce consecutive equal states into change points; a redundant flip
        # (same state as the running one) leaves the step function unchanged.
        changes: "list[tuple[int, bool]]" = []
        for at_id, mo in rows:
            state = bool(mo)
            if changes and changes[-1][1] == state:
                continue
            changes.append((int(at_id), state))

        spans: "list[dict]" = []
        for i, (start, state) in enumerate(changes):
            if not state:
                continue  # only ON segments are motion-only spans
            end = changes[i + 1][0] if i + 1 < len(changes) else store_end
            lo = start if since_id is None else max(start, int(since_id))
            hi = end if until_id is None else min(end, int(until_id))
            if lo <= hi:  # overlaps the window after clipping
                spans.append({"start_id": lo, "end_id": hi})
        return spans

    # --- Frame-range groups -------------------------------------------------
    #
    # A named, contiguous frame window [start_id, end_id] the tuning tools scope
    # to (see the frame-range-groups spec). The store stays group-agnostic
    # everywhere else: a group is just a saved (name, bounds) bookmark, and the
    # scoped reads below take raw since_id/until_id bounds a caller expands a
    # group into. Same single connection + single lock as every other op.

    @staticmethod
    def _group_to_dict(row) -> dict:
        """Map a groups row + its live count to the API dict shape.

        ``row`` is ``(id, name, start_id, end_id, start_ts, end_ts, created_ts,
        count)`` — the fixed column order both ``create_group`` and
        ``list_groups`` build, so the returned key set can never drift between the
        two (the same discipline ``_row_to_dict`` gives the frame feed).
        """
        group_id, name, start_id, end_id, start_ts, end_ts, created_ts, count = row
        return {
            "id": group_id,
            "name": name,
            "start_id": start_id,
            "end_id": end_id,
            "start_ts": start_ts,
            "end_ts": end_ts,
            "created_ts": created_ts,
            "count": int(count),
        }

    def create_group(self, name: str, start_id: int, end_id: int) -> dict:
        """Save a contiguous frame window and return it (with a live ``count``).

        The two endpoint ids may arrive in either click order, so they are
        normalized to ``start_id = min`` / ``end_id = max``. Each endpoint's
        ``recv_ts`` is resolved from ``frames`` and denormalized into the row, so
        the window's wall-clock span survives the endpoint frames aging out. If
        EITHER id is not a current frame row the range can't be anchored — a
        client-input error — so this raises ``ValueError`` (the API maps it to a
        400), before inserting anything.
        """
        lo = min(int(start_id), int(end_id))
        hi = max(int(start_id), int(end_id))
        created_ts = int(time.time() * 1000)
        with self._lock:
            lo_row = self._conn.execute("SELECT recv_ts FROM frames WHERE id = ?", (lo,)).fetchone()
            hi_row = self._conn.execute("SELECT recv_ts FROM frames WHERE id = ?", (hi,)).fetchone()
            if lo_row is None or hi_row is None:
                raise ValueError(f"group endpoints must be current frame ids: {start_id!r}, {end_id!r}")
            start_ts, end_ts = int(lo_row[0]), int(hi_row[0])
            cur = self._conn.execute(
                "INSERT INTO groups (name, start_id, end_id, start_ts, end_ts, created_ts)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (name, lo, hi, start_ts, end_ts, created_ts),
            )
            group_id = int(cur.lastrowid)
            # A fast primary-key range scan; taken under the same lock as the
            # insert so the returned count is consistent with the just-saved row.
            (count,) = self._conn.execute(
                "SELECT COUNT(*) FROM frames WHERE id BETWEEN ? AND ?", (lo, hi)
            ).fetchone()
            self._conn.commit()
        return self._group_to_dict((group_id, name, lo, hi, start_ts, end_ts, created_ts, count))

    def list_groups(self) -> "list[dict]":
        """All saved groups, newest-first (``id DESC``), each with a live ``count``.

        ``count`` is a correlated ``COUNT(*) FROM frames WHERE id BETWEEN
        start_id AND end_id`` — a primary-key range scan per group — so it
        reflects the live frames still in the window, not a stored membership
        (which lets a wholly-evicted group report 0 rather than vanish).
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT g.id, g.name, g.start_id, g.end_id, g.start_ts, g.end_ts, g.created_ts,"
                " (SELECT COUNT(*) FROM frames WHERE id BETWEEN g.start_id AND g.end_id)"
                " FROM groups g ORDER BY g.id DESC"
            ).fetchall()
        return [self._group_to_dict(r) for r in rows]

    def delete_group(self, group_id: int) -> int:
        """Delete one saved group by id; return the rowcount (0 if unknown).

        Removes only the bookmark row — never touches ``frames`` (a group is id
        *bounds*, not a membership set), so deleting a group leaves every frame it
        spanned in place.
        """
        with self._lock:
            cur = self._conn.execute("DELETE FROM groups WHERE id = ?", (int(group_id),))
            self._conn.commit()
            return cur.rowcount

    def count_in_range(self, since_id: "int | None" = None, until_id: "int | None" = None) -> int:
        """Count of frames whose id is in the inclusive ``[since_id, until_id]`` range.

        Both bounds are optional (``None`` = unbounded on that side, so an
        all-``None`` call counts the whole store), matching the scope params the
        reads below take. Backs the windowed sweep's progress denominator (frames
        in the scoped window) and the range-count endpoint the UI shows as "N
        frames in range" while picking a pending range. A fast primary-key range
        scan — ``id`` is the primary key.
        """
        where, params = _range_bounds("id", since_id, until_id)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        with self._lock:
            (count,) = self._conn.execute(
                "SELECT COUNT(*) FROM frames" + clause, params
            ).fetchone()
        return int(count)

    def resolve_ts_range(
        self, start_ts: "int | None", end_ts: "int | None"
    ) -> "tuple[int | None, int | None]":
        """Resolve a wall-clock window to inclusive frame-id bounds.

        Maps the clock-picker range to the id axis every scoped read shares:
        ``since_id`` = ``MIN(id) WHERE recv_ts >= start_ts`` (the nearest frame
        at-or-after the start), ``until_id`` = ``MAX(id) WHERE recv_ts <= end_ts``
        (nearest at-or-before the end). A ``None`` bound stays ``None`` (unbounded
        on that side); a bound that matches no frame also resolves to ``None`` on
        that side. Served off ``idx_frames_recv_ts`` so it is an indexed lookup,
        not a full-table walk. Returns ``(since_id, until_id)``.
        """
        since_id: "int | None" = None
        until_id: "int | None" = None
        with self._lock:
            if start_ts is not None:
                (since_id,) = self._conn.execute(
                    "SELECT MIN(id) FROM frames WHERE recv_ts >= ?", (int(start_ts),)
                ).fetchone()
            if end_ts is not None:
                (until_id,) = self._conn.execute(
                    "SELECT MAX(id) FROM frames WHERE recv_ts <= ?", (int(end_ts),)
                ).fetchone()
        return (
            int(since_id) if since_id is not None else None,
            int(until_id) if until_id is not None else None,
        )

    def frame_recv_ts(self, frame_id: "int | None") -> "int | None":
        """The ``recv_ts`` of one frame id, or ``None`` if it isn't a live row.

        Lets the resolve endpoint report each id bound's wall-clock instant
        without a second round trip, so the Buckets viewer can label a
        whole-window ("Select all") selection with real frame times rather than
        the requested clock instants. ``None`` id passes through as ``None``.
        """
        if frame_id is None:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT recv_ts FROM frames WHERE id = ?", (int(frame_id),)
            ).fetchone()
        return int(row[0]) if row is not None else None

    def sample_frames(
        self, since_id: "int | None", until_id: "int | None", count: int
    ) -> "list[dict]":
        """~``count`` frames evenly spread across ``[since_id, until_id]`` by INDEX, id ASC.

        A count-based decimation: pick ~``count`` frames uniformly over the id
        range. ``count`` is clamped server-side into ``[1, _MAX_SAMPLE]`` so a wide
        window can't request tens of thousands of thumbnails. Selects every
        ``stride``-th row where ``stride = max(1, ceil(matched / count))`` via
        ``ROW_NUMBER() OVER (ORDER BY id)`` with ``(rn - 1) % stride = 0`` — the
        ``rn - 1`` offset keeps the FIRST frame (``rn = 1``) always included.

        NOTE: this spaces frames by index, not by time, so it only tracks a
        wall-clock rate when the capture is uniform. The density viewer's "per
        minute / per hour" uses ``sample_frames_by_interval`` instead, which is a
        true time rate. Each row is ``{"id", "recv_ts", "url"}`` with
        ``url = f"/media/{id}"``. Empty list when the window matches no frame.
        """
        count = max(1, min(int(count), _MAX_SAMPLE))
        frags, params = _range_bounds("id", since_id, until_id)
        clause = (" WHERE " + " AND ".join(frags)) if frags else ""
        with self._lock:
            (matched,) = self._conn.execute(
                "SELECT COUNT(*) FROM frames" + clause, params
            ).fetchone()
            if not matched:
                return []
            stride = max(1, math.ceil(matched / count))
            rows = self._conn.execute(
                "SELECT id, recv_ts FROM ("
                "  SELECT id, recv_ts, ROW_NUMBER() OVER (ORDER BY id) AS rn FROM frames"
                + clause +
                ") WHERE (rn - 1) % ? = 0 ORDER BY id ASC",
                params + [stride],
            ).fetchall()
        return [{"id": int(r[0]), "recv_ts": r[1], "url": f"/media/{int(r[0])}"} for r in rows]

    def sample_frames_by_interval(
        self, since_id: "int | None", until_id: "int | None", interval_ms: int
    ) -> "list[dict]":
        """One frame per ``interval_ms`` of wall-clock ``recv_ts`` across the window, id ASC.

        The density viewer's TRUE "X per minute / per hour" rate: unlike
        ``sample_frames`` (which decimates by frame index and so only tracks a time
        rate when capture is uniform), this buckets the window's frames into fixed
        ``interval_ms`` recv_ts intervals and returns the EARLIEST frame in each
        non-empty bucket — so the result is ~one frame per interval regardless of
        the capture fps, of how wide the (mostly-empty) clock window is, or of gaps
        where the collector was stopped. Empty intervals simply yield no thumbnail.

        ``interval_ms`` is the requested spacing (e.g. 60000 for 1/min, 30000 for
        2/min, 3600000 for 1/hour); it is raised if needed so the number of buckets
        can't exceed ``_MAX_SAMPLE`` (a very fine rate over a very wide window can't
        return tens of thousands of thumbs). Buckets are measured from the window's
        first ``recv_ts``. Each row is ``{"id", "recv_ts", "url"}``; empty list when
        the window matches no frame.
        """
        interval_ms = max(1, int(interval_ms))
        frags, params = _range_bounds("id", since_id, until_id)
        clause = (" WHERE " + " AND ".join(frags)) if frags else ""
        with self._lock:
            mn, _mx, matched = self._conn.execute(
                "SELECT MIN(recv_ts), MAX(recv_ts), COUNT(*) FROM frames" + clause, params
            ).fetchone()
            if not matched:
                return []
            mn, mx = int(mn), int(_mx)
            # Raise the interval if a finer one would bucket into more than
            # _MAX_SAMPLE thumbnails (span/interval buckets). +1 so a single-instant
            # window still asks for at least one bucket-width.
            floor_interval = max(1, math.ceil((mx - mn + 1) / _MAX_SAMPLE))
            interval_ms = max(interval_ms, floor_interval)
            rows = self._conn.execute(
                "SELECT id, recv_ts FROM ("
                "  SELECT id, recv_ts,"
                "    ROW_NUMBER() OVER (PARTITION BY (recv_ts - ?) / ? ORDER BY recv_ts ASC, id ASC) AS rn"
                "  FROM frames" + clause +
                ") WHERE rn = 1 ORDER BY id ASC",
                [mn, interval_ms] + params,
            ).fetchall()
        return [{"id": int(r[0]), "recv_ts": r[1], "url": f"/media/{int(r[0])}"} for r in rows]

    # --- Analysis layer -----------------------------------------------------
    #
    # Offline-oracle verdicts *about* stored frames (see the motion-gate-oracles
    # spec). The store deliberately knows nothing of the analysis package: these
    # methods take and return raw values (bool/float/dict), never an
    # AnalysisResult, so the dependency points one way (runner → store) and the
    # store stays importable without the heavy CV/ML deps. Every op still goes
    # through the single connection + single lock, exactly like the frames path.
    #
    # The iterators are the exception to "hold the lock for the whole op": a sweep
    # over the store is long, and inference between rows is slow and must run
    # UNLOCKED so the always-on collector keeps writing `frames`. So they fetch
    # one keyset batch under the lock, RELEASE it, then yield that batch's rows;
    # keyseting on ``id`` (not OFFSET) makes them correct across the concurrent
    # inserts and evictions that happen while a batch is being consumed.

    def write_analysis(
        self, frame_id: int, analyzer: str, verdict: bool, score: "float | None", detail: "dict | None"
    ) -> None:
        """Record one oracle verdict for a frame; idempotent per (frame, analyzer).

        INSERT OR REPLACE keyed on the primary key, so a re-run of the same
        analyzer over the same frame overwrites its prior verdict rather than
        erroring or duplicating — which is what lets a windowed sweep revisit
        every frame and a stateless sweep resume without special-casing done rows.
        ``detail`` is JSON-serialized (NULL when None); ``ran_at`` is the compute
        wall clock in epoch ms, the same axis ``recv_ts`` uses.
        """
        detail_text = json.dumps(detail) if detail is not None else None
        with self._lock:
            # Guard against a race with the collector's eviction: a sweep lists a
            # frame, releases the lock, decodes+infers slowly, then writes here —
            # meanwhile ``_evict_locked`` may have dropped that oldest frame. INSERT
            # only if the frames row still exists (INSERT ... SELECT ... WHERE
            # EXISTS), so a verdict can never outlive its frame — the orphan row the
            # schema comment forbids. If the frame is gone the verdict is simply
            # dropped; it describes a frame that no longer exists.
            self._conn.execute(
                "INSERT OR REPLACE INTO analysis (frame_id, analyzer, verdict, score, detail, ran_at)"
                " SELECT ?, ?, ?, ?, ?, ? WHERE EXISTS (SELECT 1 FROM frames WHERE id = ?)",
                (
                    int(frame_id),
                    analyzer,
                    1 if verdict else 0,
                    float(score) if score is not None else None,
                    detail_text,
                    int(time.time() * 1000),
                    int(frame_id),
                ),
            )
            self._conn.commit()

    def write_analysis_batch(self, rows: "list[tuple[int, str, bool, float | None, dict | None]]") -> None:
        """Record many oracle verdicts at once; one lock hold, one commit.

        The batched sibling of ``write_analysis`` for the stateless sweep's
        prefetch/batch path (see the yolo-sweep-throughput spec). ``rows`` is a
        list of ``(frame_id, analyzer, verdict, score, detail)`` tuples — the exact
        argument order of ``write_analysis``, and a CONTRACT the runner builds to.
        Each ``detail`` is JSON-serialized (NULL when None); ``ran_at`` is the
        compute wall clock in epoch ms (same axis as ``write_analysis`` and
        ``recv_ts``). Under a SINGLE lock hold it runs ONE ``executemany`` of the
        IDENTICAL INSERT OR REPLACE guard ``write_analysis`` uses, then ONE
        ``commit`` — so it preserves both idempotency (INSERT OR REPLACE on the
        (frame_id, analyzer) key) and the eviction guard (WHERE EXISTS on the
        frames row, so a verdict can never outlive its frame). Empty ``rows`` is a
        no-op — no commit, no error.
        """
        if not rows:
            return
        ran_at = int(time.time() * 1000)
        params = [
            (
                int(frame_id),
                analyzer,
                1 if verdict else 0,
                float(score) if score is not None else None,
                json.dumps(detail) if detail is not None else None,
                ran_at,
                int(frame_id),
            )
            for frame_id, analyzer, verdict, score, detail in rows
        ]
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO analysis (frame_id, analyzer, verdict, score, detail, ran_at)"
                " SELECT ?, ?, ?, ?, ?, ? WHERE EXISTS (SELECT 1 FROM frames WHERE id = ?)",
                params,
            )
            self._conn.commit()

    def close(self) -> None:
        """Checkpoint the WAL and close the connection; safe to call once at shutdown.

        ``PRAGMA wal_checkpoint(TRUNCATE)`` flushes the WAL back into the main DB
        file and truncates the ``-wal`` sidecar, bounding the orphan-file exposure
        the ``__init__`` PRAGMA note describes (only frames committed since the
        last checkpoint are at risk on a hard power loss). Idempotent: a second
        call after the connection is closed swallows the resulting error, so wiring
        it to the app's shutdown hook can never itself raise.
        """
        with self._lock:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self._conn.close()
            except sqlite3.Error:
                pass

    def iter_unanalyzed(
        self, analyzer: str, batch: int = 512, since_id: "int | None" = None, until_id: "int | None" = None
    ):
        """Yield ``(frame_id, abs_path)`` for frames lacking a verdict for ``analyzer``.

        Oldest-first (id ASC), the driver for a STATELESS sweep (e.g. YOLO): a
        LEFT JOIN with ``analysis.frame_id IS NULL`` skips already-done frames, so
        a re-run resumes cheaply and only pays for new work. Fetched in keyset
        batches (``f.id > last``, lock acquired per batch, rows yielded outside the
        lock) so inference between rows never blocks the collector. Because it
        advances by ascending id, frames the sweep verdicts mid-iteration simply
        fall behind the cursor — no infinite loop, no re-yield.

        ``until_id`` caps the sweep to frames present when it started (``f.id <=
        until_id``): paired with the same cap on ``count_unanalyzed``, it keeps the
        progress denominator honest — frames the collector inserts mid-sweep are
        out of scope for this pass (the next sweep picks them up) rather than
        pushing ``done`` past ``total``. ``since_id`` is the symmetric floor
        (``f.id >= since_id``) that scopes the sweep to a group's window; both
        ``None`` sweeps the whole store exactly as before.
        """
        last_id = 0
        range_frags, range_params = _range_bounds("f.id", since_id, until_id)
        range_sql = "".join(" AND " + frag for frag in range_frags)
        while True:
            params: list = [analyzer, last_id] + range_params + [int(batch)]
            with self._lock:
                rows = self._conn.execute(
                    "SELECT f.id, f.path FROM frames f"
                    " LEFT JOIN analysis a ON a.frame_id = f.id AND a.analyzer = ?"
                    " WHERE a.frame_id IS NULL AND f.id > ?" + range_sql +
                    " ORDER BY f.id ASC LIMIT ?",
                    params,
                ).fetchall()
            if not rows:
                return
            for row_id, rel_path in rows:
                yield int(row_id), os.path.join(self._media_root, rel_path)
            last_id = rows[-1][0]

    def iter_time_order(self, batch: int = 512, since_id: "int | None" = None, until_id: "int | None" = None):
        """Yield ``(frame_id, abs_path)`` for EVERY frame, oldest-first (id ASC).

        The driver for a WINDOWED sweep (e.g. BSUV), which must see frames in
        strict time order to keep its rolling recent-background window contiguous
        — hence it revisits every frame each run rather than skipping done work.
        Same keyset-per-batch, yield-outside-the-lock discipline as
        ``iter_unanalyzed`` so a long sweep never starves the collector.
        ``until_id`` caps the pass to frames present at start (see
        ``iter_unanalyzed``), keeping ``done`` bounded by the snapshot ``total``;
        ``since_id`` is the symmetric floor (``id >= since_id``) that scopes a
        windowed re-run to a group's window. Both ``None`` sweeps the whole store
        exactly as before.
        """
        last_id = 0
        range_frags, range_params = _range_bounds("id", since_id, until_id)
        range_sql = "".join(" AND " + frag for frag in range_frags)
        while True:
            params: list = [last_id] + range_params + [int(batch)]
            with self._lock:
                rows = self._conn.execute(
                    "SELECT id, path FROM frames WHERE id > ?" + range_sql + " ORDER BY id ASC LIMIT ?",
                    params,
                ).fetchall()
            if not rows:
                return
            for row_id, rel_path in rows:
                yield int(row_id), os.path.join(self._media_root, rel_path)
            last_id = rows[-1][0]

    def recent_before(self, frame_id: int, n: int) -> "list[str]":
        """Absolute paths of the ``n`` frames just before ``frame_id`` in time order.

        Returned in CHRONOLOGICAL (ascending id) order, so a windowed analyzer can
        replay them to prime its recent-background window on (re)start instead of
        cold-starting at the resume point. Queried ``id DESC LIMIT n`` (the newest
        preceding frames) then reversed into chronological order for replay.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT path FROM frames WHERE id < ? ORDER BY id DESC LIMIT ?",
                (int(frame_id), int(n)),
            ).fetchall()
        return [os.path.join(self._media_root, rel_path) for (rel_path,) in reversed(rows)]

    def recent_before_rows(self, frame_id: int, n: int) -> "list[dict]":
        """The ``n`` frames just before ``frame_id`` as ``{id, recv_ts, url}`` dicts.

        The row-shaped sibling of ``recent_before`` (which returns bare filesystem
        paths for a windowed analyzer's warm-start): the visit inbox's filmstrip
        needs frame ids → ``/media/{id}`` URLs to show the gate's warm-up context
        (a few frames preceding the visit). Returned in CHRONOLOGICAL (id ASC)
        order — queried ``id DESC LIMIT n`` (the newest preceding frames), then
        reversed for replay order.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, recv_ts FROM frames WHERE id < ? ORDER BY id DESC LIMIT ?",
                (int(frame_id), int(n)),
            ).fetchall()
        return [
            {"id": int(r[0]), "recv_ts": r[1], "url": f"/media/{int(r[0])}"}
            for r in reversed(rows)
        ]

    def count_unanalyzed(
        self, analyzer: str, since_id: "int | None" = None, until_id: "int | None" = None
    ) -> int:
        """Count of frames with no verdict for ``analyzer`` — a stateless sweep's TODO.

        ``until_id`` caps to frames present at sweep start (``f.id <= until_id``),
        matching ``iter_unanalyzed``'s cap so this count is the true denominator
        for exactly the frames that pass will visit; ``since_id`` (``f.id >=
        since_id``) is the symmetric floor for a scoped sweep. Both ``None`` counts
        the whole store's un-analyzed frames exactly as before.
        """
        range_frags, range_params = _range_bounds("f.id", since_id, until_id)
        range_sql = "".join(" AND " + frag for frag in range_frags)
        params: list = [analyzer] + range_params
        with self._lock:
            (count,) = self._conn.execute(
                "SELECT COUNT(*) FROM frames f"
                " LEFT JOIN analysis a ON a.frame_id = f.id AND a.analyzer = ?"
                " WHERE a.frame_id IS NULL" + range_sql,
                params,
            ).fetchone()
        return int(count)

    def latest_id(self) -> int:
        """Largest frame row id currently stored, or 0 when empty.

        A sweep snapshots this at start and caps its iteration + count to it, so
        concurrent collector inserts during a long pass can't overrun the progress
        denominator (see ``iter_unanalyzed``'s ``until_id``).
        """
        with self._lock:
            (mx,) = self._conn.execute("SELECT COALESCE(MAX(id), 0) FROM frames").fetchone()
        return int(mx)

    def analysis_summary(self, analyzer: str) -> dict:
        """``{analyzed, present}`` verdict counts for ``analyzer`` — coverage/progress.

        ``analyzed`` is the number of verdict rows; ``present`` is how many say the
        subject is present (SUM over the 0/1 verdict, COALESCE'd so an
        never-analyzed analyzer reports 0 rather than None).
        """
        with self._lock:
            analyzed, present = self._conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(verdict), 0) FROM analysis WHERE analyzer = ?",
                (analyzer,),
            ).fetchone()
        return {"analyzed": int(analyzed), "present": int(present)}

    def analysis_coverage(
        self, analyzer: str, since_id: "int | None" = None, until_id: "int | None" = None
    ) -> dict:
        """``{total, analyzed, present}`` for ``analyzer`` over an id WINDOW.

        Unlike ``analysis_summary`` (whole-store verdict counts), this scopes to
        ``[since_id, until_id]`` and reports the counts against the window's frame
        ``total`` — so the UI can show what a bucket-scoped sweep will actually
        cover ("0/356 analyzed in this bucket") instead of whole-store numbers.
        ``total`` = frames in the window; ``analyzed`` = those carrying a verdict
        for ``analyzer``; ``present`` = those whose verdict says the subject is
        present. One LEFT JOIN pass (a frame with no verdict contributes to
        ``total`` only). Both bounds ``None`` = the whole store.
        """
        frags, params = _range_bounds("f.id", since_id, until_id)
        where = (" WHERE " + " AND ".join(frags)) if frags else ""
        with self._lock:
            total, analyzed, present = self._conn.execute(
                "SELECT COUNT(*),"
                " COALESCE(SUM(CASE WHEN a.frame_id IS NOT NULL THEN 1 ELSE 0 END), 0),"
                " COALESCE(SUM(CASE WHEN a.verdict = 1 THEN 1 ELSE 0 END), 0)"
                " FROM frames f"
                " LEFT JOIN analysis a ON a.frame_id = f.id AND a.analyzer = ?"
                + where,
                [analyzer] + params,
            ).fetchone()
        return {"total": int(total), "analyzed": int(analyzed), "present": int(present)}

    def clear_analysis(
        self, analyzer: str, since_id: "int | None" = None, until_id: "int | None" = None
    ) -> int:
        """Delete verdicts for ``analyzer``; return the rowcount.

        The reanalyze path: dropping prior rows makes the next sweep re-verdict
        the store (e.g. after swapping the model/threshold). Only this analyzer's
        rows go; other oracles' verdicts are untouched.

        ``since_id`` / ``until_id`` optionally restrict the delete to an inclusive
        ``frame_id`` range (``None`` = unbounded). A *scoped* reanalyze clears only
        the window's verdicts, so re-running an oracle (or a MOG2 slot) over one
        group re-verdicts just that window instead of discarding every verdict
        OUTSIDE it — the whole-store clear a scoped run does NOT want. Unscoped
        (both ``None``) it clears the analyzer's whole slot exactly as before.
        """
        range_frags, range_params = _range_bounds("frame_id", since_id, until_id)
        range_sql = "".join(" AND " + frag for frag in range_frags)
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM analysis WHERE analyzer = ?" + range_sql, [analyzer] + range_params
            )
            self._conn.commit()
            return cur.rowcount

    def query_disagreements(
        self,
        analyzer: str,
        mode: str,
        cursor: "str | None",
        limit: int,
        since_id: "int | None" = None,
        until_id: "int | None" = None,
    ):
        """Return ``(rows, next_cursor)`` for frames where MOG2 and ``analyzer`` disagree.

        The analysis analogue of ``query``'s "Missed?"/"False triggers" triage
        presets, and it honors the SAME keyset contract: newest-first on
        ``frames.id`` (``id DESC``), an OPAQUE token (the last id) the caller
        passes back as ``cursor`` (``None`` for the first page), and a ``None``
        ``next_cursor`` once a page is short. ``mode``:

        - ``missed`` → ``frames.motion = 0 AND analysis.verdict = 1`` — the edge
          gate stayed still but the oracle sees the subject: a genuine miss.
        - ``false``  → ``frames.motion = 1 AND analysis.verdict = 0`` — the gate
          fired but the oracle sees nothing: a false trigger.

        An unknown ``mode`` raises ValueError, mirroring ``query``'s handling of a
        bad ``motion``/``order``. The INNER JOIN restricts the result to analyzed
        frames only. Each row is exactly what ``_row_to_dict`` produces plus a
        ``score`` key carrying the oracle's ``analysis.score``. ``since_id`` /
        ``until_id`` are the same optional inclusive id-range scope as ``query``
        (``None`` = unbounded), so the disagreement view scopes to a group's
        window without touching the keyset paging.
        """
        if mode == "missed":
            disagree_clause = "f.motion = 0 AND a.verdict = 1"
        elif mode == "false":
            disagree_clause = "f.motion = 1 AND a.verdict = 0"
        else:
            raise ValueError(f"mode must be one of {_ALLOWED_DISAGREE}, got {mode!r}")

        where = [disagree_clause]
        # The analyzer binds the JOIN's ON (evaluated before WHERE), so it is the
        # first param; the optional cursor id, range bounds, and the limit follow,
        # in SQL order.
        params: list = [analyzer]
        if cursor is not None:
            where.append("f.id < ?")
            params.append(_parse_id_cursor(cursor))
        range_frags, range_params = _range_bounds("f.id", since_id, until_id)
        where.extend(range_frags)
        params.extend(range_params)
        params.append(int(limit))

        with self._lock:
            fetched = self._conn.execute(
                f"SELECT {_ROW_COLUMNS_F}, a.score FROM frames f"
                " JOIN analysis a ON a.frame_id = f.id AND a.analyzer = ?"
                f" WHERE {' AND '.join(where)} ORDER BY f.id DESC LIMIT ?",
                params,
            ).fetchall()

        # The trailing column is a.score; the leading ones match _ROW_COLUMNS, so
        # reuse _row_to_dict for the frame fields and attach score as the extra key.
        rows = [self._row_to_dict(r[:-1], score=r[-1]) for r in fetched]
        next_cursor = str(rows[-1]["id"]) if len(rows) == limit else None
        return rows, next_cursor

    # --- Gate tuning scorecards --------------------------------------------
    #
    # The offline motion-gate compare (see the motion-gate-diagnostic spec):
    # score a motion *source* — the live gate (`frames.motion`/`frames.area`) or
    # a re-run slot (`mog2:candidate`, motion=`analysis.verdict`,
    # area=`analysis.score`) — against a ground-truth *oracle* (`yolo`/`bsuv`).
    # Everything runs under the single store lock: these are on-demand,
    # human-paced reads, so serializing them against the collector costs nothing,
    # and it keeps the connection discipline identical to the rest of the store.

    def gate_scorecard(
        self,
        source: str,
        oracle: str,
        *,
        warmup: int = 500,
        min_area: float,
        max_area: float,
        persistence: int,
        oracle_floor: float = 0.0,
        since_id: "int | None" = None,
        until_id: "int | None" = None,
    ) -> dict:
        """Recall / false-trigger / miss-breakdown scorecard for one (source, oracle).

        ``source`` is ``"live"`` (motion = ``frames.motion``, area =
        ``frames.area``) or an analysis slot name such as ``"mog2:candidate"``
        (motion = ``analysis.verdict``, area = ``analysis.score`` for that
        analyzer, JOINed in). ``oracle`` ∈ ``{yolo, bsuv}`` supplies ground truth
        (its ``verdict`` = subject present, ``score`` = detection confidence /
        foreground fraction).

        Scoring is restricted to the frames **past the warmup prefix**: the
        scored set (frames carrying an oracle verdict — and, for a slot source, a
        source verdict too) is ordered by ``id`` ASC and its oldest ``warmup``
        rows are dropped, since a cold-started MOG2 re-run hasn't stabilized its
        background over that prefix.

        ``min_area``/``max_area`` bucket the *missed* frames by their source area
        (which knob would recover them); ``persistence`` names the knob for the
        in-band bucket (misses with adequate area that the debounce dropped) and
        is not otherwise used in the computation — the source's motion flag
        already reflects the persistence it was produced with.

        ``oracle_floor`` re-slices "present" to ``oracle verdict = 1 AND score >=
        floor`` — the same stored verdicts, counted only where the oracle was at
        least this sure. It exists because the YOLO oracle runs at a recall-first
        ``conf 0.15`` and hallucinates cats on empty frames (an empty tile floor
        reads as a low-conf "cat"), so those phantoms inflate ``present``, the
        ``missed`` count, and — fragmenting into tiny clusters — the visit count,
        making recall look far worse than the gate is. A floor of ~0.3 drops them
        without re-running the oracle. ``<= 0`` disables the filter and the
        scorecard is byte-for-byte the unfloored one. ``score`` semantics differ
        per oracle (YOLO: detection confidence; BSUV: foreground fraction), so a
        floor means "how sure", scaled to whichever oracle is scored.

        Returns a dict of the shape::

            {source, oracle, warmup, analyzed, present,
             recall: {caught, missed, rate},
             false_triggers: {count},
             confidence: {high, medium, low},
             area_buckets: {below_min, near_zero, above_max, in_band},
             visits: {total, caught, wholly_missed}}

        except that a slot ``source`` with **zero** analysis rows short-circuits
        to ``{source, oracle, needs_rerun: True}`` — nothing to score until the
        re-run has populated the slot. ``near_zero`` (area < ``min_area``/10, MOG2
        saw ~nothing) is a subset of ``below_min``, so ``below_min + above_max +
        in_band`` equals the total missed count.

        ``since_id`` / ``until_id`` optionally scope every scored-set query below
        to an inclusive id range (``None`` = unbounded), so all four compare
        columns (live / baseline / candidate / oracle) score the same window and
        the numbers stay comparable (see the frame-range-groups spec). ``warmup``
        is still honored as passed — a scoped caller supplies ``warmup=0`` because
        a scoped re-run is warm-started from the frames just before the window, so
        its first frames are already warm and must not be dropped.
        """
        if oracle not in _SCORECARD_ORACLES:
            raise ValueError(f"oracle must be one of {_SCORECARD_ORACLES}, got {oracle!r}")
        warmup = max(0, int(warmup))
        is_live = source == "live"

        # Source column expressions + the optional source-slot JOIN. These are
        # fixed identifiers (never user input), so interpolating them is safe; the
        # analyzer/oracle names and thresholds all bind through ``?`` params.
        if is_live:
            src_motion, src_area = "f.motion", "f.area"
            src_join, src_params = "", []
        else:
            src_motion, src_area = "s.verdict", "s.score"
            src_join, src_params = " JOIN analysis s ON s.frame_id = f.id AND s.analyzer = ?", [source]

        # FROM + oracle JOIN (its analyzer param leads) + optional source JOIN.
        base_from = " FROM frames f JOIN analysis o ON o.frame_id = f.id AND o.analyzer = ?" + src_join
        join_params = [oracle] + src_params
        near_zero_area = min_area / 10.0
        # "Present" = the oracle called the subject here AND (when a floor is set) it
        # was at least this sure. The floor re-slices the SAME stored verdicts; a
        # low-conf oracle (YOLO at conf 0.15, recall-first) hallucinates cats on empty
        # frames, inflating present / missed / the visit count, and a floor of ~0.3
        # drops those phantoms without a re-sweep. ``oracle_floor <= 0`` restores the
        # exact unfloored predicate (byte-for-byte). Formatted as a validated float
        # literal (never ``?``) so it needn't thread a param through the nine CASE arms
        # that reference "present"; parenthesized so ``NOT {present_core}`` is safe.
        if oracle_floor and oracle_floor > 0:
            present_core = f"(o.verdict = 1 AND o.score >= {float(oracle_floor)!r})"
        else:
            present_core = "(o.verdict = 1)"
        missed = f"({src_motion} = 0 AND {present_core})"

        # Optional id-range scope, applied to EVERY scored-set query below so a
        # scoped compare scores exactly the window. ``scope_where`` is the
        # standalone WHERE for the threshold probe (which has no other predicate);
        # ``scope_and`` is the AND-continuation for the two queries that already
        # filter on the warmup threshold id. Both empty (and no params) when
        # unscoped, so an unscoped scorecard is byte-for-byte unchanged.
        scope_fragments, scope_params = _range_bounds("f.id", since_id, until_id)
        scope_where = (" WHERE " + " AND ".join(scope_fragments)) if scope_fragments else ""
        scope_and = "".join(" AND " + frag for frag in scope_fragments)

        with self._lock:
            if not is_live:
                # Scope the "has this slot been run?" check to the SAME window
                # (analysis.frame_id, not the JOINed f.id — this query hits only
                # `analysis`). Otherwise a scoped compare over a window with no
                # verdicts for this slot would see the slot's OTHER-window rows,
                # skip needs_rerun, and fall through to fabricate an all-zero
                # scorecard from an empty scored set instead of reporting
                # "Not yet run".
                slot_frags, slot_params = _range_bounds("frame_id", since_id, until_id)
                slot_and = "".join(" AND " + frag for frag in slot_frags)
                (slot_rows,) = self._conn.execute(
                    "SELECT COUNT(*) FROM analysis WHERE analyzer = ?" + slot_and,
                    [source] + slot_params,
                ).fetchone()
                if slot_rows == 0:
                    return {"source": source, "oracle": oracle, "needs_rerun": True}

            # Warmup threshold: the id at rank ``warmup`` (0-indexed) in the scored
            # set ordered id ASC — rows with id >= it are scored. None when the
            # scored set has <= warmup rows (nothing past the cold-start prefix).
            # The scope narrows the scored set here too, so the threshold ranks
            # within the window rather than the whole store.
            threshold_row = self._conn.execute(
                "SELECT f.id" + base_from + scope_where + " ORDER BY f.id ASC LIMIT 1 OFFSET ?",
                join_params + scope_params + [warmup],
            ).fetchone()

            if threshold_row is None:
                counts = [0] * 11
                interesting: list = []
            else:
                threshold_id = threshold_row[0]
                # One aggregate pass over the scored set (id >= threshold). Every
                # SUM(CASE ...) returns an int over the >= 1 scored rows, so the
                # int(x or 0) coercion is belt-and-braces, not load-bearing.
                counts = list(
                    self._conn.execute(
                        "SELECT COUNT(*),"
                        f" SUM(CASE WHEN {present_core} THEN 1 ELSE 0 END),"
                        f" SUM(CASE WHEN {src_motion} = 1 AND {present_core} THEN 1 ELSE 0 END),"
                        f" SUM(CASE WHEN {missed} THEN 1 ELSE 0 END),"
                        f" SUM(CASE WHEN {src_motion} = 1 AND NOT {present_core} THEN 1 ELSE 0 END),"
                        f" SUM(CASE WHEN {missed} AND o.score >= 0.5 THEN 1 ELSE 0 END),"
                        f" SUM(CASE WHEN {missed} AND o.score >= 0.3 AND o.score < 0.5 THEN 1 ELSE 0 END),"
                        f" SUM(CASE WHEN {missed} AND {src_area} < ? THEN 1 ELSE 0 END),"
                        f" SUM(CASE WHEN {missed} AND {src_area} < ? THEN 1 ELSE 0 END),"
                        f" SUM(CASE WHEN {missed} AND {src_area} > ? THEN 1 ELSE 0 END),"
                        f" SUM(CASE WHEN {missed} AND {src_area} >= ? AND {src_area} <= ? THEN 1 ELSE 0 END)"
                        + base_from + " WHERE f.id >= ?" + scope_and,
                        [min_area, near_zero_area, max_area, min_area, max_area]
                        + join_params + [threshold_id] + scope_params,
                    ).fetchone()
                )
                # Visit clustering needs only the "interesting" rows — present
                # (floored: seeds a visit) or source-motion (tests each visit's
                # window) — in time order, not the whole scored set. The third column
                # is the floored present flag, not the raw verdict, so a below-floor
                # detection that coincides with motion can't masquerade as present.
                interesting = self._conn.execute(
                    f"SELECT f.recv_ts, {src_motion}, CASE WHEN {present_core} THEN 1 ELSE 0 END" + base_from
                    + " WHERE f.id >= ?" + scope_and + f" AND ({present_core} OR {src_motion} = 1)"
                    " ORDER BY f.recv_ts ASC, f.id ASC",
                    join_params + [threshold_id] + scope_params,
                ).fetchall()

        (analyzed, present, caught, missed_n, false_n, conf_high, conf_med,
         below_min, near_zero, above_max, in_band) = (int(x or 0) for x in counts)
        # low = every miss not already high/medium, i.e. oracle score < 0.3 OR NULL.
        conf_low = missed_n - conf_high - conf_med

        total_visits, caught_visits = self._cluster_visits(interesting)

        return {
            "source": source,
            "oracle": oracle,
            "warmup": warmup,
            "analyzed": analyzed,
            "present": present,
            "recall": {
                "caught": caught,
                "missed": missed_n,
                "rate": (caught / present) if present else 0.0,
            },
            "false_triggers": {"count": false_n},
            "confidence": {"high": conf_high, "medium": conf_med, "low": conf_low},
            "area_buckets": {
                "below_min": below_min,
                "near_zero": near_zero,
                "above_max": above_max,
                "in_band": in_band,
            },
            "visits": {
                "total": total_visits,
                "caught": caught_visits,
                "wholly_missed": total_visits - caught_visits,
            },
        }

    @staticmethod
    def _gap_split(items: "list", gap_ms: int, ts_of) -> "list[list]":
        """Cluster a recv_ts-ascending sequence into runs split at recv_ts gaps.

        The single gap-clustering primitive BOTH the scorecard's visit counts
        (``_cluster_visits`` → ``_split_into_visits``) and the visit inbox's
        per-visit records (``visits``) share, so the two can never cluster the same
        frames differently. ``items`` must already be sorted ascending by the
        recv_ts that ``ts_of(item)`` extracts; a new run starts wherever
        consecutive timestamps differ by more than ``gap_ms``. Returns a list of
        runs (each a non-empty list of the original items, order preserved); an
        empty input yields an empty list.
        """
        runs: "list[list]" = []
        run: "list" = []
        prev_ts: "int | None" = None
        for item in items:
            ts = ts_of(item)
            if prev_ts is not None and ts - prev_ts > gap_ms:
                runs.append(run)
                run = []
            run.append(item)
            prev_ts = ts
        if run:
            runs.append(run)
        return runs

    @classmethod
    def _split_into_visits(cls, present_ts: "list[int]") -> "list[tuple[int, int]]":
        """Scorecard visit spans: sorted present timestamps → ``(lo_ts, hi_ts)``.

        Thin wrapper over ``_gap_split`` for ``_cluster_visits``: ``present_ts`` is
        ascending, and each run of frames closer than ``_VISIT_GAP_MS`` collapses
        to its ``(first, last)`` recv_ts span — the span shape the caught test
        consumes. Kept separate so ``gate_scorecard``'s counts stay byte-for-byte
        while ``visits`` builds full records over the same primitive.
        """
        return [
            (run[0], run[-1])
            for run in cls._gap_split(present_ts, _VISIT_GAP_MS, lambda ts: ts)
        ]

    @staticmethod
    def _visit_caught(lo_ts: int, hi_ts: int, motion_ts: "list[int]") -> bool:
        """Whether any source-motion frame lands in ``[lo_ts, hi_ts]`` ±window.

        A visit counts as caught when a source-motion recv_ts falls inside its span
        expanded by ``_VISIT_WINDOW_MS`` on each side (the gate may fire just
        before/after the oracle sees the cat — on approach or in the tail — so an
        exact-frame match is too strict). ``motion_ts`` must be sorted ascending;
        found by binary search.
        """
        lo, hi = lo_ts - _VISIT_WINDOW_MS, hi_ts + _VISIT_WINDOW_MS
        i = bisect.bisect_left(motion_ts, lo)
        return i < len(motion_ts) and motion_ts[i] <= hi

    @staticmethod
    def _conflict_present_score(row) -> float:
        """Presence confidence of a YOLO-vs-BSUV conflict row.

        ``row`` is ``(id, recv_ts, yolo_verdict, yolo_score, bsuv_verdict,
        bsuv_score)`` where the two verdicts differ, so exactly one oracle claims
        the subject present — its score is the presence confidence used to pick the
        representative frame. Returns ``-inf`` when that oracle stored no numeric
        score, so it never wins the representative pick over a scored peer.
        """
        score = row[3] if row[2] == 1 else row[5]
        return score if score is not None else float("-inf")

    @classmethod
    def _cluster_visits(cls, interesting: "list") -> "tuple[int, int]":
        """Cluster oracle-present frames into visits and count how many were caught.

        ``interesting`` is ``(recv_ts, source_motion, present_flag)`` rows in
        recv_ts order (only present-or-motion rows), where ``present_flag`` is the
        oracle verdict after ``gate_scorecard``'s ``oracle_floor`` (1 = present).
        Present frames split into a new visit wherever the recv_ts gap exceeds
        ``_VISIT_GAP_MS``; a visit is
        caught when any source-motion frame lands in its span ±``_VISIT_WINDOW_MS``.
        Returns ``(total_visits, caught_visits)`` — the counts ``gate_scorecard``
        reports, now computed over the shared ``_split_into_visits`` /
        ``_visit_caught`` primitives so it can never drift from ``visits``.
        """
        present_ts = sorted(ts for ts, _motion, verdict in interesting if verdict == 1)
        if not present_ts:
            return 0, 0
        motion_ts = sorted(ts for ts, motion, _verdict in interesting if motion == 1)
        spans = cls._split_into_visits(present_ts)
        caught = sum(1 for lo_ts, hi_ts in spans if cls._visit_caught(lo_ts, hi_ts, motion_ts))
        return len(spans), caught

    def gate_fidelity(self, slot: str, since_id: "int | None" = None, until_id: "int | None" = None) -> dict:
        """How faithfully a re-run slot reproduces the live gate it was seeded from.

        Over the frames that carry a verdict for ``slot`` (INNER JOIN, so only
        frames still present), returns ``{compared, agree, rate}`` where ``agree``
        counts the frames whose ``analysis.verdict`` equals the stored
        ``frames.motion`` and ``rate`` is ``agree / compared`` (0.0 when nothing is
        compared). High agreement empirically validates the offline method; low
        agreement quantifies the transfer gap. No warmup exclusion — this is the
        raw reproduction check across the whole slot. ``since_id`` / ``until_id``
        optionally restrict the check to an inclusive id range (``None`` =
        unbounded) so a scoped compare reports fidelity over the same window.
        """
        range_frags, range_params = _range_bounds("f.id", since_id, until_id)
        range_sql = "".join(" AND " + frag for frag in range_frags)
        params: list = [slot] + range_params
        with self._lock:
            compared, agree = self._conn.execute(
                "SELECT COUNT(*),"
                " COALESCE(SUM(CASE WHEN a.verdict = f.motion THEN 1 ELSE 0 END), 0)"
                " FROM analysis a JOIN frames f ON f.id = a.frame_id"
                " WHERE a.analyzer = ?" + range_sql,
                params,
            ).fetchone()
        compared, agree = int(compared), int(agree)
        return {"compared": compared, "agree": agree, "rate": (agree / compared) if compared else 0.0}

    def latest_analysis_detail(self, analyzer: str) -> "dict | None":
        """The parsed ``detail`` of ``analyzer``'s most recent verdict, or ``None``.

        The newest (highest ``frame_id``) row carrying a non-NULL ``detail``,
        JSON-decoded to a dict — or ``None`` when the analyzer has no such row or the
        stored detail isn't a JSON object. Lets a caller recover, e.g., the
        ``MotionParams`` a re-run slot recorded (``detail['params']``) without reaching
        into the connection. Read-only, under the store lock like every other accessor.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT detail FROM analysis WHERE analyzer = ? AND detail IS NOT NULL"
                " ORDER BY frame_id DESC LIMIT 1",
                (analyzer,),
            ).fetchone()
        if row is None or row[0] is None:
            return None
        try:
            detail = json.loads(row[0])
        except (ValueError, TypeError):
            return None
        return detail if isinstance(detail, dict) else None

    # --- Density timeline + visit inbox ------------------------------------
    #
    # The two read endpoints the motion-detection workflow's drill-down leans on
    # (see the motion-detection-workflow spec). Both judge disagreements against
    # the LIVE edge gate — the stored ``frames.motion`` flag, NOT a ``mog2:*``
    # re-run slot — because that flag came from a MOG2 model already warm at
    # capture time, so there is no un-primed warm-up prefix to drop (unlike
    # ``gate_scorecard``, whose offline re-run must). Their totals therefore
    # legitimately differ from a scorecard's over the same window. Both scope to
    # an id window and run under the single store lock like every other read.

    def timeline_bins(
        self, since_id: "int | None", until_id: "int | None", oracle: str, bins: int
    ) -> "list[dict]":
        """Per-bin disagreement counts across the window — the density overview.

        Bins the window's frames by ``recv_ts`` into ``bins`` equal-width bins
        spanning ``[min(recv_ts), max(recv_ts)]`` of the window (so quiet hours
        read as sparse bins — bins with no frames are simply absent — which
        equal-id-count bins would hide). Returns one dict per NON-empty bin, time
        ordered::

            {t0, t1, total, motion, present, missed, false}

        ``motion = SUM(frames.motion)`` (the live gate). ``present``/``missed``/
        ``false`` come from a LEFT JOIN on ``analysis`` for ``oracle`` and are 0
        in bins where it has no verdicts: ``present`` = oracle verdict 1,
        ``missed`` = ``frames.motion = 0 AND verdict = 1`` (a gate miss),
        ``false`` = ``frames.motion = 1 AND verdict = 0`` (a false trigger).
        ``t0``/``t1`` are the bin's nominal recv_ts boundaries. Empty list when the
        window has no frames. One grouped query does the binning.
        """
        bins = max(1, int(bins))
        # Range fragments for the aliased-``f`` grouped query and the bare-``id``
        # bounds probe; the recv_ts index backs the min/max lookup.
        f_frags, f_params = _range_bounds("f.id", since_id, until_id)
        id_frags, id_params = _range_bounds("id", since_id, until_id)
        id_clause = (" WHERE " + " AND ".join(id_frags)) if id_frags else ""
        with self._lock:
            mn, mx, total_all = self._conn.execute(
                "SELECT MIN(recv_ts), MAX(recv_ts), COUNT(*) FROM frames" + id_clause,
                id_params,
            ).fetchone()
            if not total_all or mn is None:
                return []
            mn, mx = int(mn), int(mx)
            span = mx - mn
            # span == 0 (a single distinct recv_ts) would divide by zero; a
            # divisor of 1 with recv_ts - mn == 0 for every row lands them all in
            # bin 0, which is exactly the one-bin degenerate we want.
            divisor = span if span > 0 else 1
            f_where = (" WHERE " + " AND ".join(f_frags)) if f_frags else ""
            grouped = self._conn.execute(
                "SELECT bin_idx, COUNT(*),"
                " COALESCE(SUM(motion), 0),"
                " COALESCE(SUM(present), 0),"
                " COALESCE(SUM(missed), 0),"
                " COALESCE(SUM(false_ct), 0)"
                " FROM ("
                "   SELECT MIN(?, (f.recv_ts - ?) * ? / ?) AS bin_idx,"
                "          f.motion AS motion,"
                "          CASE WHEN o.verdict = 1 THEN 1 ELSE 0 END AS present,"
                "          CASE WHEN f.motion = 0 AND o.verdict = 1 THEN 1 ELSE 0 END AS missed,"
                "          CASE WHEN f.motion = 1 AND o.verdict = 0 THEN 1 ELSE 0 END AS false_ct"
                "   FROM frames f"
                "   LEFT JOIN analysis o ON o.frame_id = f.id AND o.analyzer = ?"
                + f_where +
                " ) GROUP BY bin_idx ORDER BY bin_idx",
                [bins - 1, mn, bins, divisor, oracle] + f_params,
            ).fetchall()

        result: "list[dict]" = []
        for bin_idx, total, motion, present, missed, false_ct in grouped:
            i = int(bin_idx)
            result.append(
                {
                    "t0": mn + (span * i) // bins,
                    "t1": mn + (span * (i + 1)) // bins,
                    "total": int(total),
                    "motion": int(motion),
                    "present": int(present),
                    "missed": int(missed),
                    "false": int(false_ct),
                }
            )
        return result

    def visits(
        self, since_id: "int | None", until_id: "int | None", oracle: str, mode: str
    ) -> "list[dict]":
        """The ranked visit inbox for one error mode over the window (worst-first).

        ``mode`` ∈ ``{"missed", "false", "conflict"}`` (ValueError otherwise).
        Clusters the mode's candidate frames into visits by the shared
        ``_gap_split`` recv_ts-gap primitive and returns per-visit records::

            {start_id, end_id, start_ts, end_ts, rep_frame_id, n_frames,
             present_count, caught}

        ``start_id``/``end_id`` are the id bounds of the visit's clustered frames
        and are load-bearing: the inbox fetches the visit's own frames via
        ``/api/frames?since_id=&until_id=``, so a record must hand back the id
        window it clustered. ``caught`` is whether any live-gate motion frame lands
        in the visit's span ±``_VISIT_WINDOW_MS`` (the same rule for every mode).

        - ``missed``: cluster oracle-present frames (verdict 1). ``rep_frame_id`` =
          highest oracle-score frame; ``present_count`` = oracle-present count in
          the visit. Sorted wholly-missed (``caught`` False) before caught, then
          ``n_frames`` desc, then peak oracle score desc — the wholly-missed, long
          visits that cost a real GPU trigger surface first.
        - ``false``: cluster ``frames.motion = 1 AND`` oracle verdict 0.
          ``rep_frame_id`` = highest-area frame. Sorted ``n_frames`` desc, then
          peak area desc.
        - ``conflict``: cluster frames where YOLO and BSUV verdicts differ (needs
          both oracles run for the frame); ignores ``oracle``. ``rep_frame_id`` =
          the frame whose present oracle is most confident. Sorted ``n_frames``
          desc, then peak score-gap desc.

        ``present_count`` is the oracle-present count for ``missed`` and the cluster
        size otherwise. Judged against the LIVE gate, so no warm-up prefix is
        dropped — totals may differ from a scorecard's over the same window.
        """
        if mode not in _VISIT_MODES:
            raise ValueError(f"mode must be one of {_VISIT_MODES}, got {mode!r}")

        f_frags, f_params = _range_bounds("f.id", since_id, until_id)
        f_and = "".join(" AND " + frag for frag in f_frags)
        mo_frags, mo_params = _range_bounds("id", since_id, until_id)
        mo_where = " AND ".join(["motion = 1"] + mo_frags)

        with self._lock:
            # Live-gate motion timestamps for the shared caught test.
            motion_ts = sorted(
                int(r[0])
                for r in self._conn.execute(
                    "SELECT recv_ts FROM frames WHERE " + mo_where, mo_params
                ).fetchall()
            )
            if mode == "missed":
                rows = self._conn.execute(
                    "SELECT f.id, f.recv_ts, o.score FROM frames f"
                    " JOIN analysis o ON o.frame_id = f.id AND o.analyzer = ?"
                    " WHERE o.verdict = 1" + f_and +
                    " ORDER BY f.recv_ts ASC, f.id ASC",
                    [oracle] + f_params,
                ).fetchall()
            elif mode == "false":
                rows = self._conn.execute(
                    "SELECT f.id, f.recv_ts, f.area FROM frames f"
                    " JOIN analysis o ON o.frame_id = f.id AND o.analyzer = ?"
                    " WHERE f.motion = 1 AND o.verdict = 0" + f_and +
                    " ORDER BY f.recv_ts ASC, f.id ASC",
                    [oracle] + f_params,
                ).fetchall()
            else:  # conflict — compare the two oracles, ignore `oracle`
                rows = self._conn.execute(
                    "SELECT f.id, f.recv_ts, y.verdict, y.score, b.verdict, b.score FROM frames f"
                    " JOIN analysis y ON y.frame_id = f.id AND y.analyzer = ?"
                    " JOIN analysis b ON b.frame_id = f.id AND b.analyzer = ?"
                    " WHERE y.verdict <> b.verdict" + f_and +
                    " ORDER BY f.recv_ts ASC, f.id ASC",
                    ["yolo", "bsuv"] + f_params,
                ).fetchall()

        # Cluster + build records outside the lock (pure Python over the fetched
        # rows, already recv_ts-ordered by the query's ORDER BY).
        scored: "list[tuple]" = []
        for cluster in self._gap_split(rows, _VISIT_GAP_MS, lambda r: r[1]):
            ids = [int(r[0]) for r in cluster]
            start_ts, end_ts = int(cluster[0][1]), int(cluster[-1][1])
            n_frames = len(cluster)
            caught = self._visit_caught(start_ts, end_ts, motion_ts)
            record = {
                "start_id": min(ids),
                "end_id": max(ids),
                "start_ts": start_ts,
                "end_ts": end_ts,
                "n_frames": n_frames,
                "present_count": n_frames,
                "caught": caught,
            }
            if mode == "missed":
                rep = max(
                    cluster,
                    key=lambda r: (r[2] if r[2] is not None else float("-inf"), r[0]),
                )
                peak = max((r[2] for r in cluster if r[2] is not None), default=float("-inf"))
                record["rep_frame_id"] = int(rep[0])
                sort_key: "tuple" = (caught, -n_frames, -peak)
            elif mode == "false":
                rep = max(cluster, key=lambda r: (r[2], r[0]))  # area is NOT NULL
                peak_area = max(r[2] for r in cluster)
                record["rep_frame_id"] = int(rep[0])
                sort_key = (-n_frames, -peak_area)
            else:  # conflict
                rep = max(cluster, key=lambda r: (self._conflict_present_score(r), r[0]))
                peak_gap = max(abs((r[3] or 0.0) - (r[5] or 0.0)) for r in cluster)
                record["rep_frame_id"] = int(rep[0])
                sort_key = (-n_frames, -peak_gap)
            scored.append((sort_key, record))

        scored.sort(key=lambda x: x[0])
        return [rec for _, rec in scored]

    def events(
        self,
        since_id: "int | None",
        until_id: "int | None",
        *,
        min_frames: int = 1,
        limit: int = _MAX_EVENTS,
    ) -> dict:
        """The user-facing activity feed: motion frames clustered into events, newest-first.

        Returns ``{"events": [...], "truncated": bool}``. Each event is a run of
        MOTION frames close in time — "something happened at the door" — built for
        the activity page (see the activity-page spec), the oracle-free, human-facing
        cousin of ``visits``: it needs no oracle sweep, so the view is populated the
        moment any frames are collected.

        Clusters ONLY ``frames.motion = 1``. This is the load-bearing choice: the
        collector saves EVERY frame continuously (~5–10 fps), so gap-splitting *all*
        frames would find no gaps and yield one blob per collection session — time
        gaps only exist among the sparse motion frames. Clustering reuses the shared
        ``_gap_split`` primitive with ``_VISIT_GAP_MS``, exactly as ``visits`` does,
        so the two clusterings can never drift.

        ``since_id`` / ``until_id`` are the same optional inclusive id scope every
        windowed read shares (``None`` = unbounded on that side; both ``None`` =
        whole store), which the client resolves a date filter into via
        ``resolve_ts_range``. Clusters with fewer than ``min_frames`` motion frames
        are dropped (a per-event noise floor; default 1 keeps every cluster).

        Per surviving cluster the record is::

            {start_id, end_id, start_ts, end_ts, n_frames, rep_frame_id}

        ``start_id``/``end_id`` = min/max frame id in the cluster (the id span the
        player fetches its frames over); ``start_ts``/``end_ts`` = first/last
        ``recv_ts``; ``n_frames`` = motion-frame count; ``rep_frame_id`` = the
        highest-``area`` frame (tie-break by id), matching how ``visits`` picks a
        representative — the peak-area frame is likelier to show the cat prominently
        than a middle-by-time one.

        Events are sorted NEWEST-FIRST (``start_ts`` desc, tie-break ``start_id``
        desc) and capped at ``limit`` (clamped into ``[1, _MAX_EVENTS]``);
        ``truncated`` is True iff the cap dropped events. The cap bounds the
        response and the client's DOM, not compute — clustering over the sparse
        motion frames is cheap regardless.
        """
        limit = max(1, min(int(limit), _MAX_EVENTS))
        min_frames = max(1, int(min_frames))

        frags, params = _range_bounds("id", since_id, until_id)
        where = " AND ".join(["motion = 1"] + frags)
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, recv_ts, area FROM frames WHERE " + where +
                " ORDER BY recv_ts ASC, id ASC",
                params,
            ).fetchall()

        # Cluster + build records outside the lock (pure Python over the fetched
        # rows, already recv_ts-ordered by the query's ORDER BY).
        events: "list[dict]" = []
        for cluster in self._gap_split(rows, _VISIT_GAP_MS, lambda r: r[1]):
            n_frames = len(cluster)
            if n_frames < min_frames:
                continue
            ids = [int(r[0]) for r in cluster]
            # area is NOT NULL; tie-break by id so the pick is deterministic,
            # matching ``visits``'s ``(area, id)`` representative rule.
            rep = max(cluster, key=lambda r: (r[2], r[0]))
            events.append(
                {
                    "start_id": min(ids),
                    "end_id": max(ids),
                    "start_ts": int(cluster[0][1]),
                    "end_ts": int(cluster[-1][1]),
                    "n_frames": n_frames,
                    "rep_frame_id": int(rep[0]),
                }
            )

        # Newest-first: most recent event on top, ties broken by id so ordering is
        # stable across calls.
        events.sort(key=lambda e: (e["start_ts"], e["start_id"]), reverse=True)
        truncated = len(events) > limit
        events = events[:limit]

        # --- Active-model identity join (identification-gallery-activity spec).
        # Annotate each RETURNED event with the active gallery's aggregated identity
        # (or None). Runs AFTER the cap so the join is bounded by `limit` events, and
        # with the store lock free (clustering released it above) — `active_model()`
        # and the reads below each re-acquire it per the single-lock discipline. With
        # no active model, every event's `identity` is None, so the feed renders
        # exactly like the oracle-free base feed did.
        model = self.active_model()
        if model is None or not events:
            for event in events:
                event["identity"] = None
            return {"events": events, "truncated": truncated}

        # One indexed read of the active model's identifications across the returned
        # events' overall id span, plus one cats lookup — then aggregate per event in
        # pure Python (no numpy). Idents in the gaps between events match no event's
        # [start_id, end_id] and are simply ignored.
        lo = min(e["start_id"] for e in events)
        hi = max(e["end_id"] for e in events)
        with self._lock:
            # frame_id-ORDERED so each event's span is a contiguous slice found by
            # bisect (below) — O(events·log N) instead of a full re-scan per event.
            # `cat_id IS NOT NULL` drops the "processed but un-embeddable" marker rows
            # (see write_identifications_batch): they mark a frame done so the identify
            # pass doesn't re-attempt it, but they carry no identity to aggregate.
            ident_rows = self._conn.execute(
                "SELECT frame_id, cat_id, distance FROM identifications"
                " WHERE model_version_id = ? AND frame_id BETWEEN ? AND ? AND cat_id IS NOT NULL"
                " ORDER BY frame_id ASC",
                (int(model["id"]), int(lo), int(hi)),
            ).fetchall()
            cat_names = dict(self._conn.execute("SELECT id, name FROM cats").fetchall())

        threshold = model["threshold"]
        ident_fids = [r[0] for r in ident_rows]
        for event in events:
            e_lo, e_hi = event["start_id"], event["end_id"]
            # Contiguous slice of idents whose frame_id ∈ [e_lo, e_hi]; events are
            # disjoint motion clusters, so slices never overlap.
            lo_i = bisect.bisect_left(ident_fids, e_lo)
            hi_i = bisect.bisect_right(ident_fids, e_hi)
            span = [(int(cid), float(dist)) for _fid, cid, dist in ident_rows[lo_i:hi_i]]
            event["identity"] = self._aggregate_identity(span, threshold, cat_names)
        return {"events": events, "truncated": truncated}

    @staticmethod
    def _aggregate_identity(
        span_idents: "list[tuple[int, float]]", threshold: "float | None", cat_names: dict
    ) -> "dict | None":
        """Aggregate one event span's ``(cat_id, distance)`` identifications into an identity.

        The pure-Python voter behind ``events()``'s identity join (no numpy, no DB).
        ``span_idents`` is every identification whose frame fell inside the event's
        id span; ``threshold`` is the active model's cutoff. A ``None`` threshold
        means the model is UNCALIBRATED, and the fail-safe rule applies: nothing is
        "below", so every event degrades to "unknown cat" rather than confidently
        naming a resident (see the ``threshold is None`` branch). ``cat_names`` maps
        ``cat_id`` → name. Returns::

            {cat_id, cat_name, distance, n_identified, n_frames_voted} | None

        Outcomes (per the spec):

        - **no identifications in the span** → ``None`` (renders as today).
        - **some frame below threshold** → the cat with the most below-threshold
          frames wins (ties broken by that cat's MINIMUM distance); ``distance`` is
          the winner's min distance, ``n_frames_voted`` its below-threshold count,
          ``n_identified`` all identified frames in the span.
        - **identified but none below threshold** → ``{cat_id: None, cat_name:
          None, ...}`` (an *unknown cat* was seen — nearest match too far);
          ``distance`` is the nearest (min) distance any frame reached,
          ``n_frames_voted`` 0.
        """
        if not span_idents:
            return None
        n_identified = len(span_idents)
        if threshold is None:
            # An uncomputable threshold means the gallery could not be calibrated
            # (e.g. one crop per cat → no same-cat pairs). Per CONCEPT/CLAUDE.md the
            # safe default is to DEGRADE TO UNKNOWN rather than confidently name — an
            # uncalibrated model must never label a foreign cat as a resident. So
            # nothing counts as "below": every event resolves to "unknown cat" until a
            # calibrated model is promoted (the UI flags such a model).
            below: "list[tuple[int, float]]" = []
        else:
            below = [(cid, dist) for cid, dist in span_idents if dist <= threshold]
        if not below:
            # Identified, but nothing near enough → an unknown cat; report the
            # nearest distance any frame reached.
            return {
                "cat_id": None,
                "cat_name": None,
                "distance": min(dist for _cid, dist in span_idents),
                "n_identified": n_identified,
                "n_frames_voted": 0,
            }
        # Vote among below-threshold frames: per-cat (count, min-distance).
        per_cat: "dict[int, list]" = {}
        for cid, dist in below:
            entry = per_cat.get(cid)
            if entry is None:
                per_cat[cid] = [1, dist]
            else:
                entry[0] += 1
                if dist < entry[1]:
                    entry[1] = dist
        # Winner: most below-threshold frames, tie-broken by that cat's min distance.
        winner_id = min(per_cat, key=lambda cid: (-per_cat[cid][0], per_cat[cid][1]))
        count, min_dist = per_cat[winner_id]
        return {
            "cat_id": winner_id,
            "cat_name": cat_names.get(winner_id),
            "distance": min_dist,
            "n_identified": n_identified,
            "n_frames_voted": count,
        }

    # --- Cat-identity annotation tool: roster + dataset items --------------
    #
    # The label surface (see the cat-identity annotation-tool spec). It reuses the
    # store's building blocks — the ``_gap_split``/``_VISIT_GAP_MS`` visit
    # primitive, the ``analysis`` verdicts, the id-range scope — over two NEW,
    # eviction- and ``clear``-surviving tables (`cats`, `dataset_items`). The store
    # stays cv2-free and stdlib-only: it reads the stored ``yolo-serial`` boxes out
    # of ``analysis.detail`` and records label rows, but NEVER crops or writes an
    # image (the dataset/crops module owns crop file I/O). The virtual annotation
    # queue is derived, not materialised: a ``dataset_items`` row exists only once
    # the owner has decided on a frame, and its absence is what keeps the frame in
    # the queue. All ops go through the single connection + single lock like the
    # rest of the store.

    @staticmethod
    def _best_box(detail_text: "str | None") -> "tuple[list[float], float] | None":
        """The highest-confidence box in an ``analysis.detail`` JSON, or ``None``.

        ``detail`` is a YOLO analyzer's ``{"boxes": [[x1,y1,x2,y2,conf], ...], ...}``
        in the STORED JPEG's own pixel space (so a crop of that JPEG is
        coordinate-consistent). Returns ``([x1,y1,x2,y2], conf)`` for the box with
        the max ``conf``, or ``None`` when the detail is missing, malformed, or has
        no usable box — so a present verdict whose detail can't yield a box is
        skipped rather than crashing the queue (in practice ``yolo-serial`` always
        writes a box when its verdict is present, so this is a defensive guard).
        """
        if not detail_text:
            return None
        try:
            detail = json.loads(detail_text)
        except (ValueError, TypeError):
            return None
        boxes = detail.get("boxes") if isinstance(detail, dict) else None
        if not boxes:
            return None
        usable = [b for b in boxes if isinstance(b, (list, tuple)) and len(b) >= 5]
        if not usable:
            return None
        best = max(usable, key=lambda b: b[4])
        return [float(best[0]), float(best[1]), float(best[2]), float(best[3])], float(best[4])

    @staticmethod
    def _bbox_area(bbox: "list[float]") -> float:
        """Pixel area of an ``[x1,y1,x2,y2]`` box (clamped at 0 for a degenerate box)."""
        return max(0.0, (bbox[2] - bbox[0])) * max(0.0, (bbox[3] - bbox[1]))

    @staticmethod
    def _bbox_text(bbox) -> "str | None":
        """Serialize a bbox to the ``"x1,y1,x2,y2"`` text the column stores.

        Accepts an ``[x1,y1,x2,y2]`` list/tuple (joined, matching how ``frames``
        stores its motion bbox), an already-formatted string (passed through), or
        ``None`` (a not_cat row has no box) → ``None``.
        """
        if bbox is None:
            return None
        if isinstance(bbox, str):
            return bbox
        return ",".join(str(v) for v in bbox)

    def _present_frames(
        self, oracle: str, since_id: "int | None", until_id: "int | None"
    ) -> "list[dict]":
        """Live ``oracle``-present frames in the window, chronological, box-parsed.

        The shared universe BOTH ``annotation_visits`` (undecided subset →
        queue) and ``label_progress`` (whole set → progress) cluster, so the two
        can never disagree on which frames exist or how they group. One JOINed read
        (frames × ``analysis`` verdict-1 rows for ``oracle`` **at or above
        ``_ANNOTATE_MIN_CONF``**, scoped to the id window) plus an ``EXISTS`` probe
        of ``dataset_items`` keyed on BOTH (``src_frame_id``, ``src_recv_ts``) — the
        ``clear``-safe "already decided" predicate. Each surviving frame is
        ``{id, recv_ts, bbox, score, decided}``; a present frame whose ``detail``
        yields no box (see ``_best_box``) is dropped, so it never enters a visit nor
        the progress count. Ordered by ``recv_ts`` ASC (id ASC tie-break) so
        ``_gap_split`` can consume it directly.

        The confidence floor drops the low-conf phantom detections the recall-first
        oracle scatters on empty frames — otherwise the queue fills with empty-scene
        noise. ``analysis.score`` is the max-box confidence ``_best_box`` also
        surfaces (both from the analyzer's one reduce), so the SQL filter and the
        per-frame ``score`` agree. This floors the queue and the progress readout
        together; the labelled/undo mirror (``labeled_visits``) is deliberately NOT
        floored, so a decision made before the floor stays reviewable.
        """
        frags, params = _range_bounds("f.id", since_id, until_id)
        and_sql = "".join(" AND " + frag for frag in frags)
        with self._lock:
            rows = self._conn.execute(
                "SELECT f.id, f.recv_ts, o.detail,"
                " EXISTS (SELECT 1 FROM dataset_items d"
                "   WHERE d.src_frame_id = f.id AND d.src_recv_ts = f.recv_ts)"
                " FROM frames f"
                " JOIN analysis o ON o.frame_id = f.id AND o.analyzer = ?"
                " WHERE o.verdict = 1 AND o.score >= ?" + and_sql +
                " ORDER BY f.recv_ts ASC, f.id ASC",
                [oracle, _ANNOTATE_MIN_CONF] + params,
            ).fetchall()
        # Box-parse outside the lock (pure Python over the fetched rows).
        frames: "list[dict]" = []
        for row_id, recv_ts, detail, decided in rows:
            box = self._best_box(detail)
            if box is None:
                continue
            bbox, score = box
            frames.append(
                {
                    "id": int(row_id),
                    "recv_ts": recv_ts,
                    "bbox": bbox,
                    "score": score,
                    "decided": bool(decided),
                }
            )
        return frames

    def annotation_visits(
        self,
        oracle: str,
        since_id: "int | None" = None,
        until_id: "int | None" = None,
        *,
        _frames: "list[dict] | None" = None,
    ) -> "list[dict]":
        """The virtual annotation queue: undecided ``oracle``-present visits, chronological.

        Takes the live ``oracle`` (``yolo-serial``)-present frames in the window
        whose (``id``, ``recv_ts``) has NO ``dataset_items`` row, clusters them by
        ``recv_ts`` with the shared ``_gap_split``/``_VISIT_GAP_MS`` primitive (the
        same one ``visits``/``events`` use, so the grouping can't drift), and
        returns one record per visit in ``recv_ts`` order::

            {frames: [{id, recv_ts, bbox: [x1,y1,x2,y2], score, url}, ...],
             rep_frame_id, peak_area, peak_score, span: [lo_ts, hi_ts]}

        ``rep_frame_id`` is the peak box-AREA frame (the fullest dorsal view, best
        for the roster), tie-broken by id, independent of queue order; ``peak_area``
        is that area and ``peak_score`` the best detection confidence in the visit;
        ``span`` is the visit's first/last ``recv_ts``. ``since_id``/``until_id`` are
        the same inclusive id-range scope every windowed read shares (``None`` =
        unbounded), so a bucket scopes the queue exactly like every other tool.

        ``_frames`` is a private hook: pass an already-fetched ``_present_frames``
        result (see ``label_queue``) to skip a second identical JOIN+EXISTS read
        when the caller also needs ``label_progress`` over the same window; omit it
        (the default) to fetch fresh, as every direct/test caller does.

        NOTE: like the disagreement inbox this returns EVERY visit's EVERY frame
        inline, unpaginated — bounded by today's ``yolo-serial`` coverage but a
        known scaling limit as that coverage grows (see the spec's Open questions).
        """
        frames = self._present_frames(oracle, since_id, until_id) if _frames is None else _frames
        queue = [fr for fr in frames if not fr["decided"]]
        visits: "list[dict]" = []
        for cluster in self._gap_split(queue, _VISIT_GAP_MS, lambda fr: fr["recv_ts"]):
            out_frames = [
                {
                    "id": fr["id"],
                    "recv_ts": fr["recv_ts"],
                    "bbox": fr["bbox"],
                    "score": fr["score"],
                    "url": f"/media/{fr['id']}",
                }
                for fr in cluster
            ]
            rep = max(cluster, key=lambda fr: (self._bbox_area(fr["bbox"]), fr["id"]))
            visits.append(
                {
                    "frames": out_frames,
                    "rep_frame_id": rep["id"],
                    "peak_area": self._bbox_area(rep["bbox"]),
                    "peak_score": max(fr["score"] for fr in cluster),
                    "span": [cluster[0]["recv_ts"], cluster[-1]["recv_ts"]],
                }
            )
        return visits

    def label_progress(
        self,
        oracle: str,
        since_id: "int | None" = None,
        until_id: "int | None" = None,
        *,
        _frames: "list[dict] | None" = None,
    ) -> dict:
        """``{total_visits, decided_visits, crops_labeled}`` for the window.

        Clusters ALL ``oracle``-present frames in the window (the same universe and
        gap primitive as ``annotation_visits``, but INCLUDING already-decided ones)
        so the readout stays consistent with the queue. ``total_visits`` = clusters;
        ``decided_visits`` = clusters whose every frame already has a
        ``dataset_items`` row; ``crops_labeled`` = present frames in the window that
        carry a label row. Because the queue only ever labels present-with-box
        frames, ``crops_labeled`` counts exactly this window's durable label rows
        (label crops whose source frame later evicted fall out of scope, matching
        the queue's live-frame universe).

        ``_frames`` is the same private reuse hook ``annotation_visits`` takes (see
        ``label_queue``); omit it to fetch fresh, as every direct/test caller does.
        """
        frames = self._present_frames(oracle, since_id, until_id) if _frames is None else _frames
        total_visits = 0
        decided_visits = 0
        crops_labeled = 0
        for cluster in self._gap_split(frames, _VISIT_GAP_MS, lambda fr: fr["recv_ts"]):
            total_visits += 1
            decided_flags = [fr["decided"] for fr in cluster]
            crops_labeled += sum(1 for d in decided_flags if d)
            if all(decided_flags):
                decided_visits += 1
        return {
            "total_visits": total_visits,
            "decided_visits": decided_visits,
            "crops_labeled": crops_labeled,
        }

    def label_queue(
        self, oracle: str, since_id: "int | None" = None, until_id: "int | None" = None
    ) -> dict:
        """``{visits, total_visits, decided_visits, crops_labeled}`` in ONE scan.

        ``GET /api/label/visits`` needs both ``annotation_visits`` (the queue) and
        ``label_progress`` (the readout) for the SAME window every call; called
        independently they each run their own ``_present_frames`` — the full
        frames×analysis JOIN plus a per-row ``dataset_items`` EXISTS probe — so the
        route paid that cost twice. This runs ``_present_frames`` once and feeds the
        same list to both via their private ``_frames`` hook; the two return the
        exact values they always did (they still each derive their own subset/gap-
        split from it), just from a shared read.
        """
        frames = self._present_frames(oracle, since_id, until_id)
        return {
            "visits": self.annotation_visits(oracle, since_id, until_id, _frames=frames),
            **self.label_progress(oracle, since_id, until_id, _frames=frames),
        }

    def labeled_visits(
        self, oracle: str, since_id: "int | None" = None, until_id: "int | None" = None
    ) -> "list[dict]":
        """Already-decided ``oracle``-present visits, newest-labelled first — the undo/re-label feed.

        The mirror of ``annotation_visits`` over the DECIDED subset: live ``oracle``
        (``yolo-serial``)-present frames in the window that HAVE a ``dataset_items``
        row, INNER-JOINed to that row for the current identity (``cat_id`` /
        ``label_kind`` / ``quality`` / ``crop_path``) and the cat name, clustered by
        the shared ``_gap_split`` / ``_VISIT_GAP_MS`` primitive so the grouping
        matches the queue's exactly. Each cluster is one past labelling gesture and
        its rows share a decision, so the visit identity is taken from them;
        ``mixed`` flags the rare case where two adjacent-in-time gestures merged into
        one recv_ts cluster. The display ``bbox`` per frame comes from
        ``analysis.detail`` (the same box the queue showed), so a crop renders via
        the same ``/api/label/crop`` path even for a ``not_cat`` row (whose stored
        bbox is NULL). Returned newest-labelled first (max ``labeled_ts`` per
        cluster, DESC) so the most recent mistake is on top. Per visit::

            {frames: [{id, recv_ts, bbox, quality, crop_path, url}, ...],
             rep_frame_id, span: [lo_ts, hi_ts], label_kind, cat_id, cat_name,
             mixed, labeled_ts}

        ``since_id`` / ``until_id`` are the same inclusive id-range scope every
        windowed read shares. Like ``annotation_visits`` it is unpaginated (a known
        scaling limit); the labelled set is bounded by how much has been labelled.
        """
        frags, params = _range_bounds("f.id", since_id, until_id)
        and_sql = "".join(" AND " + frag for frag in frags)
        with self._lock:
            rows = self._conn.execute(
                "SELECT f.id, f.recv_ts, o.detail, d.cat_id, d.label_kind, d.quality,"
                " d.crop_path, d.labeled_ts, c.name"
                " FROM frames f"
                " JOIN analysis o ON o.frame_id = f.id AND o.analyzer = ?"
                " JOIN dataset_items d ON d.src_frame_id = f.id AND d.src_recv_ts = f.recv_ts"
                " LEFT JOIN cats c ON c.id = d.cat_id"
                " WHERE o.verdict = 1" + and_sql +
                " ORDER BY f.recv_ts ASC, f.id ASC",
                [oracle] + params,
            ).fetchall()
        # Box-parse + assemble outside the lock (pure Python over fetched rows).
        decided: "list[dict]" = []
        for row_id, recv_ts, detail, cat_id, label_kind, quality, crop_path, labeled_ts, cat_name in rows:
            box = self._best_box(detail)
            if box is None:
                continue  # defensive: a decided frame should still parse a box
            bbox, _score = box
            decided.append(
                {
                    "id": int(row_id),
                    "recv_ts": recv_ts,
                    "bbox": bbox,
                    "quality": quality,
                    "crop_path": crop_path,
                    "cat_id": cat_id,
                    "label_kind": label_kind,
                    "cat_name": cat_name,
                    "labeled_ts": labeled_ts,
                }
            )
        visits: "list[dict]" = []
        for cluster in self._gap_split(decided, _VISIT_GAP_MS, lambda fr: fr["recv_ts"]):
            out_frames = [
                {
                    "id": fr["id"],
                    "recv_ts": fr["recv_ts"],
                    "bbox": fr["bbox"],
                    "quality": fr["quality"],
                    "crop_path": fr["crop_path"],
                    "url": f"/media/{fr['id']}",
                }
                for fr in cluster
            ]
            rep = max(cluster, key=lambda fr: (self._bbox_area(fr["bbox"]), fr["id"]))
            mixed = len({(fr["label_kind"], fr["cat_id"]) for fr in cluster}) > 1
            visits.append(
                {
                    "frames": out_frames,
                    "rep_frame_id": rep["id"],
                    "span": [cluster[0]["recv_ts"], cluster[-1]["recv_ts"]],
                    "label_kind": rep["label_kind"],
                    "cat_id": rep["cat_id"],
                    "cat_name": rep["cat_name"],
                    "mixed": mixed,
                    "labeled_ts": max(fr["labeled_ts"] for fr in cluster),
                }
            )
        visits.sort(key=lambda v: v["labeled_ts"], reverse=True)
        return visits

    def add_dataset_items(self, rows: "list[dict]") -> int:
        """Bulk-insert one ``dataset_items`` row per labelled crop; return the count.

        ``rows`` is a list of dicts — the wire contract the label route builds — one
        per visit frame, each with keys::

            frame_id     (int, required)  the source frames.id
            label_kind   (str, required)  'identified' | 'unknown_cat' | 'not_cat'
            cat_id       (int|None)       set iff label_kind == 'identified'
            quality      (str|None)       'gallery'|'ok'|'poor' (None for not_cat)
            bbox         (list|str|None)  [x1,y1,x2,y2] or "x1,y1,x2,y2" (None for not_cat)
            crop_path    (str|None)       dataset-root-relative jpg (None for not_cat)
            source       (str, optional)  defaults to 'detector'

        ``src_recv_ts`` is resolved from ``frames`` by ``frame_id`` AT INSERT, so it
        snapshots the frame the label was made against; a ``frame_id`` no longer
        live (evicted / cleared) is SKIPPED, not inserted (its frame is gone, so a
        (frame_id, recv_ts) dedup key would be meaningless). A (``src_frame_id``,
        ``src_recv_ts``) pair that ALREADY has a row is also SKIPPED — ``idx_dataset_src``
        is a UNIQUE index and the insert is ``INSERT OR IGNORE``, so a double-submit,
        a stale duplicate request, or a second labeller can never create a second,
        possibly-conflicting ``dataset_items`` row for the same crop; the first write
        wins and the return count reflects only rows genuinely inserted (a caller
        that needs to know about a rejected duplicate would see a lower count than
        ``len(rows)``). ``label_kind`` and ``quality`` are validated for the WHOLE
        batch BEFORE any write, so a bad row raises ``ValueError`` without touching
        the DB. Runs under one lock hold, one commit; any exception during the
        write loop rolls back the transaction (mirroring ``add``'s discipline) so a
        mid-batch failure (disk-full, a lock beyond ``busy_timeout``) can't leave
        partial rows sitting uncommitted on the shared connection to be silently
        flushed by the next unrelated commit elsewhere.

        CONTRACT for the caller: materialise each crop FILE first, then call this to
        record its row, so a crash orphans a (harmless) crop file rather than a
        ``dataset_items`` row pointing at a missing crop.
        """
        if not rows:
            return 0
        # Validate + coerce the whole batch up front (no DB touched yet), so a bad
        # enum can't abort mid-loop and strand uncommitted inserts on the connection.
        prepared: "list[tuple]" = []
        for row in rows:
            label_kind = row["label_kind"]
            if label_kind not in _LABEL_KINDS:
                raise ValueError(f"label_kind must be one of {_LABEL_KINDS}, got {label_kind!r}")
            quality = row.get("quality")
            if quality is not None and quality not in _QUALITIES:
                raise ValueError(f"quality must be one of {_QUALITIES} or None, got {quality!r}")
            cat_id = row.get("cat_id")
            prepared.append(
                (
                    int(row["frame_id"]),
                    int(cat_id) if cat_id is not None else None,
                    label_kind,
                    quality,
                    self._bbox_text(row.get("bbox")),
                    row.get("crop_path"),
                    row.get("source") or "detector",
                )
            )
        labeled_ts = int(time.time() * 1000)
        inserted = 0
        with self._lock:
            try:
                for frame_id, cat_id, label_kind, quality, bbox_text, crop_path, source in prepared:
                    r = self._conn.execute(
                        "SELECT recv_ts FROM frames WHERE id = ?", (frame_id,)
                    ).fetchone()
                    if r is None:
                        continue  # frame no longer live — nothing to anchor the label to
                    cur = self._conn.execute(
                        "INSERT OR IGNORE INTO dataset_items (cat_id, label_kind, quality, bbox, crop_path,"
                        " src_frame_id, src_recv_ts, source, labeled_ts)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (cat_id, label_kind, quality, bbox_text, crop_path,
                         frame_id, int(r[0]), source, labeled_ts),
                    )
                    inserted += cur.rowcount  # 0 when (frame_id, recv_ts) already has a row
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return inserted

    def delete_dataset_items(self, frame_ids: "list[int]") -> "list[dict]":
        """Delete label rows for these source frames; return their crop paths.

        The undo / re-label primitive: drops the ``dataset_items`` rows for
        ``frame_ids`` (matched on ``src_frame_id``) so those frames fall back into
        the annotation queue (the queue's "undecided" predicate is the ABSENCE of a
        row), and returns ``[{frame_id, crop_path}, ...]`` for the rows removed so
        the caller — the API, which owns crop file I/O — can delete the now-orphaned
        crop files. Matching on ``src_frame_id`` alone also sweeps any stale
        pre-``clear`` orphan row for a reused id, which is harmless cleanup since
        such a row's frame is long gone. Empty input is a no-op. One lock hold, one
        commit, rollback on error (``add``'s discipline)."""
        if not frame_ids:
            return []
        ids = [int(f) for f in frame_ids]
        placeholders = ",".join("?" for _ in ids)
        with self._lock:
            try:
                rows = self._conn.execute(
                    f"SELECT src_frame_id, crop_path FROM dataset_items"
                    f" WHERE src_frame_id IN ({placeholders})",
                    ids,
                ).fetchall()
                self._conn.execute(
                    f"DELETE FROM dataset_items WHERE src_frame_id IN ({placeholders})", ids
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return [{"frame_id": int(r[0]), "crop_path": r[1]} for r in rows]

    def labeled_crops(
        self,
        label_kinds: "tuple[str, ...]" = ("identified",),
        qualities: "tuple[str, ...] | None" = None,
    ) -> "list[dict]":
        """Durable labelled crops for the feasibility / gallery experiments.

        Returns one dict per ``dataset_items`` row whose ``label_kind`` is in
        ``label_kinds`` AND which has a materialised crop file (``crop_path`` not
        NULL), joined to ``cats`` for the display name::

            {cat_id, cat_name, label_kind, quality, crop_path (ABSOLUTE), src_frame_id}

        ``crop_path`` is joined to ``dataset_root`` so an OFFLINE reader (the
        embedding/feasibility tool, which the lean collector never imports) opens
        it directly without knowing the store layout. Ordered by ``cat_id`` then
        ``id`` so same-cat crops are contiguous. Defaults to the ``identified``
        crops (the ones with a known individual — what a separability probe or a
        gallery is built from); pass more kinds (e.g. ``"unknown_cat"``) to include
        the ambiguous set for threshold tuning.

        ``qualities`` optionally restricts to crops whose ``quality`` is in the
        given set — e.g. ``("gallery",)`` for a gallery-only separability run, or
        ``("gallery", "ok")`` to drop only the poor crops. ``None`` (default)
        applies NO quality filter, matching the prior behaviour (every crop of the
        requested kinds). A quality filter excludes NULL-``quality`` rows — a crop
        with no grade can't satisfy ``IN (...)``, which is correct: an un-graded
        crop is not gallery-grade. An explicitly empty tuple selects nothing
        (``[]``), symmetric with an empty ``label_kinds``. The store stays cv2-free:
        it hands back paths, never pixels."""
        kinds = tuple(label_kinds)
        if not kinds:
            return []
        quals = tuple(qualities) if qualities is not None else None
        if quals is not None:
            for q in quals:
                if q not in _QUALITIES:
                    raise ValueError(f"quality must be one of {_QUALITIES}, got {q!r}")
            if not quals:
                return []
        where = "d.label_kind IN (%s) AND d.crop_path IS NOT NULL" % ",".join("?" for _ in kinds)
        params: "list" = list(kinds)
        if quals is not None:
            where += " AND d.quality IN (%s)" % ",".join("?" for _ in quals)
            params += list(quals)
        with self._lock:
            rows = self._conn.execute(
                "SELECT d.cat_id, c.name, d.label_kind, d.quality, d.crop_path, d.src_frame_id"
                " FROM dataset_items d LEFT JOIN cats c ON c.id = d.cat_id"
                f" WHERE {where}"
                " ORDER BY d.cat_id, d.id",
                params,
            ).fetchall()
        return [
            {
                "cat_id": r[0],
                "cat_name": r[1],
                "label_kind": r[2],
                "quality": r[3],
                "crop_path": os.path.join(self._dataset_root, r[4]),
                "src_frame_id": r[5],
            }
            for r in rows
        ]

    def count_identified_crops(
        self, qualities: "tuple[str, ...] | None"
    ) -> "tuple[int, int]":
        """The (crop count, distinct-cat count) of ``identified`` crops for a quality filter.

        The cheap pre-check the Training page runs BEFORE enqueuing a feasibility
        run: it answers "is there enough labelled data yet?" (at least 2 crops
        across at least 2 distinct cats) with two aggregate COUNTs, loading no rows
        or paths — unlike ``labeled_crops``, which materialises every row for the
        embedder. Counts ``dataset_items`` rows whose ``label_kind = 'identified'``
        AND which have a materialised crop file (``crop_path`` not NULL), applying
        the SAME ``qualities`` semantics as ``labeled_crops``: ``None`` applies NO
        quality filter; a tuple restricts to ``quality IN (...)`` (excluding
        NULL-``quality`` rows, which can't be gallery-grade); an explicitly empty
        tuple selects nothing (``(0, 0)``). A grade outside ``_QUALITIES`` raises
        ``ValueError``, exactly as ``labeled_crops`` does, so a bad request is a 400,
        not a silent empty result. Returns ``(n_crops, n_cats)``.
        """
        quals = tuple(qualities) if qualities is not None else None
        if quals is not None:
            for q in quals:
                if q not in _QUALITIES:
                    raise ValueError(f"quality must be one of {_QUALITIES}, got {q!r}")
            if not quals:
                return (0, 0)
        where = "label_kind = 'identified' AND crop_path IS NOT NULL"
        params: "list" = []
        if quals is not None:
            where += " AND quality IN (%s)" % ",".join("?" for _ in quals)
            params += list(quals)
        with self._lock:
            n_crops, n_cats = self._conn.execute(
                f"SELECT COUNT(*), COUNT(DISTINCT cat_id) FROM dataset_items WHERE {where}",
                params,
            ).fetchone()
        return (int(n_crops), int(n_cats))

    # --- Feasibility (validation) run history ------------------------------
    #
    # One row per Training-page validation run (see the training-page spec). The
    # store persists the metrics + the report-dir basename; it never runs the probe
    # (that is the identification orchestrator's job, kept heavy-dep-free of the
    # store) and never touches the report FILES except to prune old dirs. Same
    # single connection + single lock as every other op.

    @property
    def training_root(self) -> str:
        """Root dir for Training-page artifacts (feasibility reports), under the collection root.

        ``os.path.join(os.path.dirname(self._db_path), 'training')`` — a SIBLING of
        ``media/`` and the dataset root, so validation reports persist alongside the
        labels they measure and outlive the rolling frame buffer. Unlike ``media/``
        and the dataset root, the store does NOT create this eagerly in ``__init__``:
        reports are rare, so the dir is made lazily where a report is written (the
        orchestrator ``os.makedirs`` its per-run subdir). This only hands out the
        path.
        """
        return os.path.join(os.path.dirname(self._db_path) or ".", "training")

    def add_feasibility_run(
        self,
        quality: str,
        n_crops: int,
        n_cats: int,
        knn_accuracy: "float | None",
        auc: "float | None",
        threshold: "float | None",
        report_dir: str,
        notes: "str | None" = None,
    ) -> int:
        """Record one completed validation run; return its new row id.

        ``report_dir`` is the ``training_root``-relative BASENAME of the run's
        report directory (e.g. ``'1721136000000-gallery'``), NOT an absolute path —
        so a store whose collection root moves keeps resolving its reports. ``ts`` is
        stamped here as the compute wall clock in epoch ms (``int(time.time() *
        1000)``), the same axis ``recv_ts`` / ``ran_at`` use. Any of ``knn_accuracy``
        / ``auc`` / ``threshold`` may be ``None`` (a metric the probe couldn't
        compute — e.g. a single-cat degenerate case the caller still chose to
        record). One lock hold, one commit.
        """
        ts = int(time.time() * 1000)
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO feasibility_runs (ts, quality, n_crops, n_cats, knn_accuracy, auc,"
                " threshold, report_dir, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ts,
                    quality,
                    int(n_crops),
                    int(n_cats),
                    float(knn_accuracy) if knn_accuracy is not None else None,
                    float(auc) if auc is not None else None,
                    float(threshold) if threshold is not None else None,
                    report_dir,
                    notes,
                ),
            )
            run_id = int(cur.lastrowid)
            self._conn.commit()
        return run_id

    def feasibility_runs(self, limit: "int | None" = None) -> "list[dict]":
        """Validation runs, most-recent-first (``id DESC``), each with a report-availability flag.

        One dict per ``feasibility_runs`` row::

            {run_id, ts, quality, n_crops, n_cats, knn_accuracy, auc, threshold,
             report_available, notes}

        ``report_available`` is computed at read time — whether
        ``<training_root>/<report_dir>/feasibility.html`` still exists on disk — so a
        run whose report dir has been pruned (see ``prune_feasibility_reports``) still
        lists its metrics but is flagged ``False`` (the UI shows a "report pruned"
        placeholder instead of loading a 404 into the iframe). ``limit`` optionally
        caps the number of rows returned (``None`` = all).
        """
        sql = "SELECT id, ts, quality, n_crops, n_cats, knn_accuracy, auc, threshold, report_dir, notes" \
              " FROM feasibility_runs ORDER BY id DESC"
        params: "list" = []
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        root = self.training_root
        return [
            {
                "run_id": r[0],
                "ts": r[1],
                "quality": r[2],
                "n_crops": r[3],
                "n_cats": r[4],
                "knn_accuracy": r[5],
                "auc": r[6],
                "threshold": r[7],
                "report_available": os.path.isfile(os.path.join(root, r[8], "feasibility.html")),
                "notes": r[9],
            }
            for r in rows
        ]

    def feasibility_run_report_path(self, run_id: int) -> "str | None":
        """Absolute path to a run's ``feasibility.html``, or ``None`` if it isn't on disk.

        Resolves the run's ``report_dir`` basename against ``training_root`` and
        returns the report path only when the file actually exists — so a pruned (or
        never-written) report resolves to ``None``, which the report endpoint maps to
        a 404 while the run's metrics row keeps listing. An unknown ``run_id`` also
        returns ``None``.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT report_dir FROM feasibility_runs WHERE id = ?", (int(run_id),)
            ).fetchone()
        if row is None:
            return None
        path = os.path.join(self.training_root, row[0], "feasibility.html")
        return path if os.path.isfile(path) else None

    def prune_feasibility_reports(self, keep: int) -> int:
        """Delete all but the newest ``keep`` runs' report DIRS from disk; return dirs removed.

        Bounds the on-disk report footprint while leaving ALL ``feasibility_runs``
        rows intact — an aged-out run keeps its (tiny) metrics row and simply reports
        ``report_available = False``. Keeps the report dirs of the newest ``keep``
        runs by ``id DESC`` and ``shutil.rmtree``s each older run's dir under
        ``training_root``. Each per-dir delete swallows ``OSError`` (mirroring
        ``_unlink``): on the Windows compute PC, removing a report file currently open
        in a served ``FileResponse`` raises a sharing violation that must not crash the
        run that triggered the prune. Only dirs that actually existed and were removed
        are counted. ``keep <= 0`` prunes every run's dir; a store with ``keep`` or
        fewer runs removes nothing.
        """
        keep = max(0, int(keep))
        with self._lock:
            rows = self._conn.execute(
                "SELECT report_dir FROM feasibility_runs ORDER BY id DESC"
            ).fetchall()
        root = self.training_root
        removed = 0
        for (report_dir,) in rows[keep:]:
            path = os.path.join(root, report_dir)
            if not os.path.isdir(path):
                continue
            try:
                shutil.rmtree(path)
                removed += 1
            except OSError:
                pass
        return removed

    # --- Identification: model versions + identifications ------------------
    #
    # The runtime side of the learning loop's Train → Run (see the
    # identification-gallery-activity spec). `model_versions` is a precious,
    # eviction-/clear-surviving registry of built galleries (vectors themselves
    # live on disk as `<models_root>/<gallery_dir>/gallery.npz`); `identifications`
    # is a frame-keyed, cascade-evicting record of the nearest gallery cat per
    # frame per model. The store stays numpy-/torch-free: `add_model_version`
    # records what the gallery builder computed, and the identify pass hands
    # `write_identifications_batch` raw (frame, model, cat, distance, bbox) tuples —
    # the store never embeds or matches. The iterator mirrors `iter_unanalyzed`'s
    # keyset-batch-yield-outside-the-lock discipline so a long identify pass never
    # starves the collector. Every op goes through the single connection + lock.

    @property
    def models_root(self) -> str:
        """Root dir for versioned gallery artifacts (``gallery.npz`` per version).

        ``os.path.join(os.path.dirname(self._db_path), 'models')`` — a SIBLING of
        ``media/``, the dataset root, and ``training/``, so promoted galleries persist
        alongside the labels they were built from and outlive the rolling frame
        buffer. Like ``training_root`` the store does NOT create this eagerly in
        ``__init__``; the gallery builder ``os.makedirs`` its per-version subdir where
        it writes. This only hands out the path.
        """
        return os.path.join(os.path.dirname(self._db_path) or ".", "models")

    @staticmethod
    def _model_row_to_dict(row) -> dict:
        """Map a ``model_versions`` row to the API dict, ``metrics`` JSON-parsed.

        ``row`` is the fixed column order every model-versions SELECT below builds —
        ``(id, status, kind, backbone, imgsz, n_cats, n_vectors, threshold, quality,
        metrics, gallery_dir, created_ts, notes)`` — so the returned key set can't
        drift across ``list_model_versions`` / ``active_model`` / ``promote_model``
        (the discipline ``_group_to_dict`` / ``_cat_to_dict`` give). ``metrics`` is
        decoded back from its stored JSON (``None`` when NULL or unparsable, mirroring
        ``latest_analysis_detail``'s defensive decode). Callers add their own extra
        keys (``gallery_available`` / ``gallery_path``).
        """
        (mid, status, kind, backbone, imgsz, n_cats, n_vectors, threshold,
         quality, metrics, gallery_dir, created_ts, notes) = row
        parsed_metrics = None
        if metrics is not None:
            try:
                parsed_metrics = json.loads(metrics)
            except (ValueError, TypeError):
                parsed_metrics = None
        return {
            "id": mid,
            "status": status,
            "kind": kind,
            "backbone": backbone,
            "imgsz": imgsz,
            "n_cats": n_cats,
            "n_vectors": n_vectors,
            "threshold": threshold,
            "quality": quality,
            "metrics": parsed_metrics,
            "gallery_dir": gallery_dir,
            "created_ts": created_ts,
            "notes": notes,
        }

    # The fixed column order _model_row_to_dict unpacks; kept next to it so the
    # SELECT list and the unpack can never drift (as _ROW_COLUMNS does for frames).
    _MODEL_COLUMNS = (
        "id, status, kind, backbone, imgsz, n_cats, n_vectors, threshold,"
        " quality, metrics, gallery_dir, created_ts, notes"
    )

    def add_model_version(
        self,
        status: str,
        kind: str,
        backbone: str,
        imgsz: int,
        n_cats: int,
        n_vectors: int,
        threshold: "float | None",
        quality: str,
        metrics: "dict | None",
        gallery_dir: str,
        notes: "str | None" = None,
    ) -> int:
        """Insert one ``model_versions`` row and return its new id (the version number).

        ``metrics`` is JSON-serialized (NULL when ``None``); ``threshold`` may be
        ``None`` (uncomputable — e.g. one crop per cat, so no same-cat pair);
        ``created_ts`` is stamped here as the compute wall clock in epoch ms (the same
        axis ``recv_ts`` / ``ran_at`` use). ``gallery_dir`` is the ``models_root``-
        relative basename holding the version's ``gallery.npz`` (written by the caller
        BEFORE this insert, so a failed insert orphans a harmless artifact dir rather
        than a row without its file). One lock hold, one commit.
        """
        metrics_text = json.dumps(metrics) if metrics is not None else None
        created_ts = int(time.time() * 1000)
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO model_versions (status, kind, backbone, imgsz, n_cats, n_vectors,"
                " threshold, quality, metrics, gallery_dir, created_ts, notes)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    status,
                    kind,
                    backbone,
                    int(imgsz),
                    int(n_cats),
                    int(n_vectors),
                    float(threshold) if threshold is not None else None,
                    quality,
                    metrics_text,
                    gallery_dir,
                    created_ts,
                    notes,
                ),
            )
            version_id = int(cur.lastrowid)
            self._conn.commit()
        return version_id

    def list_model_versions(self) -> "list[dict]":
        """All model versions, newest-first (``id DESC``), each with a ``gallery_available`` flag.

        One dict per ``model_versions`` row (see ``_model_row_to_dict``) plus
        ``gallery_available`` — whether ``<models_root>/<gallery_dir>/gallery.npz``
        still exists on disk, computed at read time (like ``feasibility_runs``'s
        ``report_available``) so a version whose artifact was lost still lists but
        flags its missing file. Backs the Promote panel and the Activity "active
        model" readout.
        """
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {self._MODEL_COLUMNS} FROM model_versions ORDER BY id DESC"
            ).fetchall()
        root = self.models_root
        result: "list[dict]" = []
        for row in rows:
            model = self._model_row_to_dict(row)
            model["gallery_available"] = os.path.isfile(
                os.path.join(root, model["gallery_dir"], "gallery.npz")
            )
            result.append(model)
        return result

    def active_model(self) -> "dict | None":
        """The single ``status='active'`` version + its ``gallery_path``, or ``None``.

        Returns the active row (see ``_model_row_to_dict``) with an added
        ``gallery_path`` = the ABSOLUTE path to its ``gallery.npz``. Returns ``None``
        when there is no active version OR its ``gallery.npz`` is missing on disk — so
        the identify pass and ``events()`` treat a lost artifact exactly like "no
        model promoted yet" rather than failing later on a missing file.
        """
        with self._lock:
            row = self._conn.execute(
                f"SELECT {self._MODEL_COLUMNS} FROM model_versions WHERE status = 'active' LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        model = self._model_row_to_dict(row)
        gallery_path = os.path.join(self.models_root, model["gallery_dir"], "gallery.npz")
        if not os.path.isfile(gallery_path):
            return None
        model["gallery_path"] = gallery_path
        return model

    def promote_model(self, version_id: int) -> dict:
        """Make ``version_id`` the sole active version; return its row dict.

        One locked transaction flips any current ``active`` → ``retired`` and the
        target → ``active``, so EXACTLY ONE active version exists. Accepts any
        existing version — promoting a ``retired`` one back is how a bad model is
        rolled back (``ARCHITECTURE.md``). Promoting the already-active version is a
        no-op success. Raises ``ValueError("no such model version: ...")`` for an
        unknown id and ``ValueError("gallery artifact missing: ...")`` when the
        target's ``gallery.npz`` is gone (checked in-txn, so a lost artifact can never
        be promoted live). Returns the promoted row dict (``status='active'``).
        """
        version_id = int(version_id)
        with self._lock:
            row = self._conn.execute(
                f"SELECT {self._MODEL_COLUMNS} FROM model_versions WHERE id = ?", (version_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"no such model version: {version_id}")
            model = self._model_row_to_dict(row)
            gallery_path = os.path.join(self.models_root, model["gallery_dir"], "gallery.npz")
            if not os.path.isfile(gallery_path):
                raise ValueError(f"gallery artifact missing: {gallery_path}")
            if model["status"] != "active":
                # Retire the incumbent, then activate the target — one txn so there is
                # never a moment with zero or two active versions.
                self._conn.execute(
                    "UPDATE model_versions SET status = 'retired' WHERE status = 'active'"
                )
                self._conn.execute(
                    "UPDATE model_versions SET status = 'active' WHERE id = ?", (version_id,)
                )
                self._conn.commit()
                model["status"] = "active"
        return model

    def iter_unidentified(
        self,
        model_version_id: int,
        since_id: "int | None" = None,
        until_id: "int | None" = None,
        batch: int = 512,
    ):
        """Yield ``(frame_id, abs_path, bbox)`` for detected frames not yet identified for a model.

        The identify pass's driver, mirroring ``iter_unanalyzed``: a frame qualifies
        when it has a ``yolo-serial`` present verdict (``verdict = 1``, its
        ``detail`` carrying the detection box) AND has NO ``identifications`` row for
        ``model_version_id`` (the LEFT JOIN ... IS NULL resume shape) — so a re-run
        resumes cheaply and promoting a new model makes its rows a fresh un-identified
        set. ``bbox`` is the highest-confidence box (``[x1,y1,x2,y2]``) parsed from
        ``analysis.detail`` via ``_best_box``, in the STORED JPEG's pixel space, OR
        ``None`` when the detail yields no usable box (defensive — yolo-serial always
        writes one when present). A no-box frame is still YIELDED (with ``bbox =
        None``) rather than skipped, so it matches ``count_unidentified`` exactly (the
        SQL count can't parse boxes) — the caller writes it a "processed" marker row
        so the pass reaches 100% and never re-attempts it, instead of it lingering
        un-identified forever. Oldest-first (id ASC), fetched in keyset batches
        (``f.id > last`` under the lock, rows box-parsed + yielded OUTSIDE it) so slow
        embedding between rows never blocks the collector. ``until_id`` caps the pass
        to frames present at start and ``since_id`` is the symmetric floor scoping it
        to a window; both ``None`` sweeps the whole store.
        """
        last_id = 0
        range_frags, range_params = _range_bounds("f.id", since_id, until_id)
        range_sql = "".join(" AND " + frag for frag in range_frags)
        while True:
            params: list = ["yolo-serial", int(model_version_id), last_id] + range_params + [int(batch)]
            with self._lock:
                rows = self._conn.execute(
                    "SELECT f.id, f.path, a.detail FROM frames f"
                    " JOIN analysis a ON a.frame_id = f.id AND a.analyzer = ? AND a.verdict = 1"
                    " LEFT JOIN identifications i ON i.frame_id = f.id AND i.model_version_id = ?"
                    " WHERE i.frame_id IS NULL AND f.id > ?" + range_sql +
                    " ORDER BY f.id ASC LIMIT ?",
                    params,
                ).fetchall()
            if not rows:
                return
            for row_id, rel_path, detail in rows:
                box = self._best_box(detail)
                bbox = box[0] if box is not None else None  # None → caller markers it done
                yield int(row_id), os.path.join(self._media_root, rel_path), bbox
            last_id = rows[-1][0]

    def count_unidentified(
        self,
        model_version_id: int,
        since_id: "int | None" = None,
        until_id: "int | None" = None,
    ) -> int:
        """Count of ``yolo-serial``-detected frames not yet identified for ``model_version_id``.

        The identify pass's progress denominator, with the SAME predicate as
        ``iter_unidentified`` (a present ``yolo-serial`` verdict, no
        ``identifications`` row for this model), ``until_id``-capped to frames present
        at start and ``since_id``-floored for a scoped window. It does not parse
        boxes, and neither does the iterator's yield-set: ``iter_unidentified`` now
        yields no-box frames too (with ``bbox = None``), so this count matches the
        iterator's yield exactly — the progress bar reaches 100%. Both bounds ``None``
        counts the whole store.
        """
        range_frags, range_params = _range_bounds("f.id", since_id, until_id)
        range_sql = "".join(" AND " + frag for frag in range_frags)
        params: list = ["yolo-serial", int(model_version_id)] + range_params
        with self._lock:
            (count,) = self._conn.execute(
                "SELECT COUNT(*) FROM frames f"
                " JOIN analysis a ON a.frame_id = f.id AND a.analyzer = ? AND a.verdict = 1"
                " LEFT JOIN identifications i ON i.frame_id = f.id AND i.model_version_id = ?"
                " WHERE i.frame_id IS NULL" + range_sql,
                params,
            ).fetchone()
        return int(count)

    def write_identifications_batch(
        self, rows: "list[tuple[int, int, int | None, float | None, object]]"
    ) -> int:
        """Record many identifications at once; one lock hold, one commit. Returns rows inserted.

        The identify pass's batched writer, the ``write_analysis_batch`` sibling.
        ``rows`` is a list of ``(frame_id, model_version_id, cat_id, distance, bbox)``
        tuples — a CONTRACT the identify orchestrator builds to. A MATCH row carries a
        ``cat_id`` + ``distance``; a MARKER row (a present frame whose crop could not
        be embedded — no box, or an undecodable/degenerate crop) carries ``cat_id =
        None`` and ``distance = None`` so the pass records it as processed and never
        re-attempts it, without inventing an identity. ``bbox`` is serialized via
        ``_bbox_text``; ``ran_at`` is the compute wall clock in epoch ms. Under a
        SINGLE lock hold it runs ONE ``executemany`` of an ``INSERT OR REPLACE``
        guarded by ``WHERE EXISTS (frames row)`` then ONE commit — so it preserves
        both idempotency (INSERT OR REPLACE on the ``(frame_id, model_version_id)``
        key) and the eviction guard (a slow pass can't resurrect an evicted frame).

        RETURNS the number of rows actually inserted — the ``total_changes`` delta
        across the ``executemany`` — so a frame evicted between the iterator listing it
        and this write (dropped by ``WHERE EXISTS``) is NOT counted, letting the caller
        report a truthful identified-count instead of over-counting by the evicted
        rows. Empty ``rows`` is a no-op returning 0.
        """
        if not rows:
            return 0
        ran_at = int(time.time() * 1000)
        params = [
            (
                int(frame_id),
                int(model_version_id),
                int(cat_id) if cat_id is not None else None,
                float(distance) if distance is not None else None,
                self._bbox_text(bbox),
                ran_at,
                int(frame_id),
            )
            for frame_id, model_version_id, cat_id, distance, bbox in rows
        ]
        with self._lock:
            before = self._conn.total_changes
            self._conn.executemany(
                "INSERT OR REPLACE INTO identifications"
                " (frame_id, model_version_id, cat_id, distance, bbox, ran_at)"
                " SELECT ?, ?, ?, ?, ?, ? WHERE EXISTS (SELECT 1 FROM frames WHERE id = ?)",
                params,
            )
            self._conn.commit()
            return self._conn.total_changes - before

    # --- Roster (cats) CRUD ------------------------------------------------

    @staticmethod
    def _cat_to_dict(row) -> dict:
        """Map a ``cats`` row ``(id, name, is_resident, active, notes, created_ts)`` to the API shape.

        ``is_resident``/``active`` surface as bools. Both ``create_cat`` and
        ``list_cats``/``update_cat`` build the same column order, so the returned
        key set can't drift (the discipline ``_group_to_dict`` gives groups).
        """
        cat_id, name, is_resident, active, notes, created_ts = row
        return {
            "id": cat_id,
            "name": name,
            "is_resident": bool(is_resident),
            "active": bool(active),
            "notes": notes,
            "created_ts": created_ts,
        }

    def list_cats(self) -> "list[dict]":
        """The whole roster, ordered by ``id`` ASC (creation order).

        The order is stable and append-only, so the digit-key binding a caller
        derives from it (``1``–``9`` → the first nine cats) never shifts under a
        mid-session add — a rename changes a name in place, an add lands at the end.
        Includes retired (``active = 0``) cats so the roster panel can show and
        un-retire them; a caller that only wants pickable cats filters on ``active``.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, name, is_resident, active, notes, created_ts FROM cats ORDER BY id ASC"
            ).fetchall()
        return [self._cat_to_dict(r) for r in rows]

    def create_cat(self, name: str, is_resident: bool = False) -> dict:
        """Add a roster cat and return it. Duplicate name → ``ValueError``.

        ``name`` is stripped and must be non-empty; ``is_resident`` is coerced to
        0/1 (``active`` starts 1, ``notes`` NULL). The UNIQUE constraint on ``name``
        surfaces as ``ValueError`` (the API maps it to a 400), rolling back the
        failed insert so the shared connection is left clean.
        """
        name = (name or "").strip()
        if not name:
            raise ValueError("cat name must be non-empty")
        resident = 1 if is_resident else 0
        created_ts = int(time.time() * 1000)
        with self._lock:
            try:
                cur = self._conn.execute(
                    "INSERT INTO cats (name, is_resident, active, notes, created_ts)"
                    " VALUES (?, ?, 1, NULL, ?)",
                    (name, resident, created_ts),
                )
            except sqlite3.IntegrityError:
                self._conn.rollback()
                raise ValueError(f"cat name already exists: {name!r}")
            cat_id = int(cur.lastrowid)
            self._conn.commit()
        return self._cat_to_dict((cat_id, name, resident, 1, None, created_ts))

    def update_cat(self, cat_id: int, fields: dict) -> dict:
        """Update a roster cat's ``name`` / ``is_resident`` / ``active``; return the row.

        ``fields`` is a partial dict — only the keys present are changed (retire by
        setting ``active`` False without deleting the cat's labels; ``cat_id`` on
        ``dataset_items`` keeps pointing at it). An empty/unknown-only ``fields``, an
        empty ``name``, or an unknown ``cat_id`` → ``ValueError``; a duplicate
        ``name`` → ``ValueError`` (UNIQUE), each rolling back so the connection stays
        clean. ``is_resident``/``active`` are coerced to 0/1.
        """
        cat_id = int(cat_id)
        sets: "list[str]" = []
        params: list = []
        if "name" in fields:
            name = (fields["name"] or "").strip()
            if not name:
                raise ValueError("cat name must be non-empty")
            sets.append("name = ?")
            params.append(name)
        if "is_resident" in fields:
            sets.append("is_resident = ?")
            params.append(1 if fields["is_resident"] else 0)
        if "active" in fields:
            sets.append("active = ?")
            params.append(1 if fields["active"] else 0)
        if not sets:
            raise ValueError("no updatable fields provided (name / is_resident / active)")
        with self._lock:
            try:
                cur = self._conn.execute(
                    "UPDATE cats SET " + ", ".join(sets) + " WHERE id = ?", params + [cat_id]
                )
            except sqlite3.IntegrityError:
                self._conn.rollback()
                raise ValueError(f"cat name already exists: {fields.get('name')!r}")
            if cur.rowcount == 0:
                self._conn.rollback()
                raise ValueError(f"no such cat: {cat_id}")
            row = self._conn.execute(
                "SELECT id, name, is_resident, active, notes, created_ts FROM cats WHERE id = ?",
                (cat_id,),
            ).fetchone()
            self._conn.commit()
        return self._cat_to_dict(row)
