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

import json
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

    def query(self, cursor: "str | None", limit: int, motion: str, order: str):
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
            self._conn.commit()
            self._total_bytes = 0
            return len(rows)

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

    def iter_unanalyzed(self, analyzer: str, batch: int = 512, until_id: "int | None" = None):
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
        pushing ``done`` past ``total``.
        """
        last_id = 0
        cap = "" if until_id is None else " AND f.id <= ?"
        while True:
            params: list = [analyzer, last_id]
            if until_id is not None:
                params.append(int(until_id))
            params.append(int(batch))
            with self._lock:
                rows = self._conn.execute(
                    "SELECT f.id, f.path FROM frames f"
                    " LEFT JOIN analysis a ON a.frame_id = f.id AND a.analyzer = ?"
                    " WHERE a.frame_id IS NULL AND f.id > ?" + cap +
                    " ORDER BY f.id ASC LIMIT ?",
                    params,
                ).fetchall()
            if not rows:
                return
            for row_id, rel_path in rows:
                yield int(row_id), os.path.join(self._media_root, rel_path)
            last_id = rows[-1][0]

    def iter_time_order(self, batch: int = 512, until_id: "int | None" = None):
        """Yield ``(frame_id, abs_path)`` for EVERY frame, oldest-first (id ASC).

        The driver for a WINDOWED sweep (e.g. BSUV), which must see frames in
        strict time order to keep its rolling recent-background window contiguous
        — hence it revisits every frame each run rather than skipping done work.
        Same keyset-per-batch, yield-outside-the-lock discipline as
        ``iter_unanalyzed`` so a long sweep never starves the collector.
        ``until_id`` caps the pass to frames present at start (see
        ``iter_unanalyzed``), keeping ``done`` bounded by the snapshot ``total``.
        """
        last_id = 0
        cap = "" if until_id is None else " AND id <= ?"
        while True:
            params: list = [last_id]
            if until_id is not None:
                params.append(int(until_id))
            params.append(int(batch))
            with self._lock:
                rows = self._conn.execute(
                    "SELECT id, path FROM frames WHERE id > ?" + cap + " ORDER BY id ASC LIMIT ?",
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

    def count_unanalyzed(self, analyzer: str, until_id: "int | None" = None) -> int:
        """Count of frames with no verdict for ``analyzer`` — a stateless sweep's TODO.

        ``until_id`` caps to frames present at sweep start (``f.id <= until_id``),
        matching ``iter_unanalyzed``'s cap so this count is the true denominator
        for exactly the frames that pass will visit.
        """
        cap = "" if until_id is None else " AND f.id <= ?"
        params: list = [analyzer]
        if until_id is not None:
            params.append(int(until_id))
        with self._lock:
            (count,) = self._conn.execute(
                "SELECT COUNT(*) FROM frames f"
                " LEFT JOIN analysis a ON a.frame_id = f.id AND a.analyzer = ?"
                " WHERE a.frame_id IS NULL" + cap,
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

    def clear_analysis(self, analyzer: str) -> int:
        """Delete every verdict for ``analyzer``; return the rowcount.

        The reanalyze path: dropping prior rows makes the next sweep re-verdict
        the whole store (e.g. after swapping the model/threshold). Only this
        analyzer's rows go; other oracles' verdicts are untouched.
        """
        with self._lock:
            cur = self._conn.execute("DELETE FROM analysis WHERE analyzer = ?", (analyzer,))
            self._conn.commit()
            return cur.rowcount

    def query_disagreements(self, analyzer: str, mode: str, cursor: "str | None", limit: int):
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
        ``score`` key carrying the oracle's ``analysis.score``.
        """
        if mode == "missed":
            disagree_clause = "f.motion = 0 AND a.verdict = 1"
        elif mode == "false":
            disagree_clause = "f.motion = 1 AND a.verdict = 0"
        else:
            raise ValueError(f"mode must be one of {_ALLOWED_DISAGREE}, got {mode!r}")

        where = [disagree_clause]
        # The analyzer binds the JOIN's ON (evaluated before WHERE), so it is the
        # first param; the optional cursor id and the limit follow, in SQL order.
        params: list = [analyzer]
        if cursor is not None:
            where.append("f.id < ?")
            params.append(_parse_id_cursor(cursor))
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
