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
import sqlite3
import threading
import time
from datetime import datetime

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
# gate ("live") or an analysis slot name (e.g. "mog2:candidate").
_SCORECARD_ORACLES = ("yolo", "bsuv")

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

# The visit-inbox error modes ``visits`` clusters and ranks (see the
# motion-detection-workflow spec). "missed"/"false" judge the LIVE edge gate
# (``frames.motion``) against one oracle; "conflict" compares the two oracles
# (YOLO vs BSUV) and ignores the ``oracle`` argument.
_VISIT_MODES = ("missed", "false", "conflict")


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

    def __init__(self, db_path: str, media_root: str, max_bytes: int) -> None:
        self._media_root = media_root
        self._max_bytes = max_bytes
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        os.makedirs(media_root, exist_ok=True)
        # One shared connection across the collector and API threads; the lock
        # (not sqlite's own thread check) is what makes that safe, so disable the
        # check. A short busy_timeout is belt-and-braces against any stray lock.
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA busy_timeout = 5000")
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
            # NOT dropped — it is config, so `motion_only` survives the wipe.
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
        missed = f"({src_motion} = 0 AND o.verdict = 1)"

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
                        " COALESCE(SUM(o.verdict), 0),"
                        f" SUM(CASE WHEN {src_motion} = 1 AND o.verdict = 1 THEN 1 ELSE 0 END),"
                        f" SUM(CASE WHEN {missed} THEN 1 ELSE 0 END),"
                        f" SUM(CASE WHEN {src_motion} = 1 AND o.verdict = 0 THEN 1 ELSE 0 END),"
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
                # Visit clustering needs only the "interesting" rows — oracle-present
                # (to cluster into visits) or source-motion (to test each visit's
                # window) — in time order, not the whole scored set.
                interesting = self._conn.execute(
                    f"SELECT f.recv_ts, {src_motion}, o.verdict" + base_from
                    + " WHERE f.id >= ?" + scope_and + f" AND (o.verdict = 1 OR {src_motion} = 1)"
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

        ``interesting`` is ``(recv_ts, source_motion, oracle_verdict)`` rows in
        recv_ts order (only present-or-motion rows). Present frames split into a
        new visit wherever the recv_ts gap exceeds ``_VISIT_GAP_MS``; a visit is
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
