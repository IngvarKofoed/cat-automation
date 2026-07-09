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

import os
import sqlite3
import threading
from datetime import datetime

# Oldest-first eviction batch size: eviction selects and deletes rows in chunks
# rather than one round-trip per row, so freeing space after a burst stays cheap.
_EVICT_BATCH = 64

# The columns every query selects, in this order, so _row_to_dict can unpack a
# fetched tuple positionally without re-stating the layout at each call site.
_ROW_COLUMNS = "id, recv_ts, edge_ts, frame_id, motion, area, bbox"

_ALLOWED_MOTION = ("all", "motion", "still")
_ALLOWED_ORDER = ("time", "area_desc", "area_asc")


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
    def _row_to_dict(row) -> dict:
        row_id, recv_ts, edge_ts, frame_id, motion, area, bbox = row
        return {
            "id": row_id,
            "recv_ts": recv_ts,
            "edge_ts": edge_ts,
            "frame_id": frame_id,
            "motion": bool(motion),
            "area": area,
            "bbox": [float(v) for v in bbox.split(",")] if bbox else None,
            "url": f"/media/{row_id}",
        }

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
            self._conn.commit()
            self._total_bytes = 0
            return len(rows)
