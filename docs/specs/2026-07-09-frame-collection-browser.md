# Frame collection & motion-tuning browser (compute)

A compute-side, always-on collector that saves **every** frame off the edge
stream — motion *and* non-motion — to a bounded local store tagged with its
motion flag and area, plus a simple web UI to browse the frames in time order
with motion frames visually marked. Its purpose is **tuning the edge motion
gate**: to see where it's wrong you need the frames it *didn't* flag (missed
cats) as well as the ones it did (false triggers), so keeping non-motion frames
is the whole point. This is the first compute-side web surface and a seed for the
later dashboard; it is *not* yet cat detection, cropping, or annotation.

## Key decisions

- **Reuse `EdgeClient.iter_stream_reconnecting()`** (reuses). The collector
  consumes the existing auto-reconnecting feed in `compute/ingest/client.py`;
  each `StreamFrame` already carries raw `.jpeg` bytes and `.meta`
  (`motion`/`ts`/`frame_id`/`area`/`bbox`). No new ingest code, no reconnection
  logic to re-invent.
- **Store raw JPEG bytes verbatim** (reuses). Write `frame.jpeg` as-is — the edge
  already encoded q90. No decode, no re-encode: collection is cheap I/O and
  `cv2`/`numpy` aren't even on the collection path (`StreamFrame.image` is never
  touched). ~67 KB/frame (ROI) measured, ≈55–66 GB/day at 10 fps.
- **SQLite index + media on the filesystem** (new; aligns with ARCHITECTURE.md's
  storage decision). Per-frame row in `sqlite3` (stdlib — **no new dependency**),
  JPEGs in date/hour-bucketed dirs referenced by path. SQLite gives fast
  time-ordered paging and motion/area filtering at 10 fps volume, which a flat
  folder or JSONL manifest can't.
- **`area` recorded per frame is the tuning triage signal** (extends the motion
  contract's use). The UI's two core presets sort by it: *missed cats* =
  non-motion ordered by highest area; *false triggers* = motion ordered by lowest
  area. This is what turns "eyeball 800k frames" into "look at the borderline
  dozen."
- **Compute receive-time is the time axis** (new). The Pi has no RTC so its
  `edge_ts` can be wrong; the collector stamps `recv_ts` from the compute clock
  and browses by the DB row `id` (insertion order). `edge_ts`/`frame_id` are
  stored for reference only.
- **Rolling retention by total media size** (new). Default cap **5 GB**
  (~2 hours at 10 fps — a testing window); when exceeded, delete oldest rows +
  files until under. Plus a manual **"Clear all"**. Tuning needs a recent window,
  not an archive.
- **FastAPI, one process + a background collector thread** (new; FastAPI is
  ARCHITECTURE.md's compute-dashboard choice). Mirrors the edge's Flask-app +
  background-grabber pattern. New deps: `fastapi`, `uvicorn`.
- **Config via environment variables** (reuses the edge's style). Pi address from
  the existing `CAT_PI_URL`; store dir, size cap, and port from new
  `CAT_COLLECT_*` vars. Compute has no config store yet.

## Goals

- Continuously save every edge frame (motion and non-motion) with its motion flag
  and area, always-on with auto-reconnect, bounded on disk.
- Browse frames in time order in a web UI, with motion frames visually
  distinguished (a border).
- Make the two motion-failure modes *findable*: missed cats (non-motion, high
  area) and false triggers (motion, low area).
- A manual "Clear all" to reset the store between test runs.

## Non-goals

- **Offline re-running of MOG2 with different parameters** over the saved frames —
  that reimplements the edge's stateful motion pipeline on compute; out of scope.
  The loop is: collect → see where it's wrong here → adjust params in the *edge*
  config UI → collect again.
- Cat detection, tracking, cropping, identification, annotation, or labels — the
  later dataset/learning phase reuses this plumbing but isn't built here.
- Live preview in the browser (the edge config UI already does live; this is a
  *historical* browser of stored frames).
- Editing motion parameters from this UI (they live in the edge config UI).
- Auth (trusted LAN, per project).

## Design

### Layout (new files)

```
compute/
  collection/
    store.py        # SQLite index + media dir + retention + clear
    collector.py    # background loop: iter_stream_reconnecting() -> store.add()
  api/
    app.py          # FastAPI: browse page, JSON endpoints, media, collector thread
    web/index.html  # the browse UI (vanilla JS, in the edge config-UI style)
compute.sh          # entrypoint (mirrors edge.sh): bootstrap venv + launch uvicorn
```

Media + DB live under `CAT_COLLECT_DIR` (default `./data/collection/`, gitignored
per ARCHITECTURE.md — captured media is never committed).

### Store (`compute/collection/store.py`)

Owns one SQLite DB (`index.db`) and a media root. Schema:

```sql
CREATE TABLE frames (
  id       INTEGER PRIMARY KEY,   -- compute insertion order = stable browse/time cursor
  recv_ts  INTEGER NOT NULL,      -- compute receive wall-clock (epoch ms) — the reliable axis
  edge_ts  INTEGER NOT NULL,      -- edge frame ts (may jump; Pi has no RTC) — reference only
  frame_id INTEGER NOT NULL,      -- edge frame_id (resets on edge restart) — reference only
  motion   INTEGER NOT NULL,      -- 0/1
  area     REAL    NOT NULL,      -- largest-blob area fraction of the ROI (0..1)
  bbox     TEXT,                  -- "x,y,w,h" normalized, or NULL when no motion
  path     TEXT    NOT NULL,      -- media path relative to the media root
  bytes    INTEGER NOT NULL       -- jpeg size, for the size-based retention sum
);
CREATE INDEX idx_frames_motion_area ON frames(motion, area);
```

- `add(frame)` — write `frame.jpeg` to `YYYY-MM-DD/HH/<recv_ts>_f<frame_id>.jpg`
  (per-hour buckets keep any one dir near ~36k files at 10 fps), insert the row,
  add to an in-memory running byte total, and **evict** oldest-`id` rows + files
  while the total exceeds the cap.
- `query(cursor, limit, motion, order)` — one keyset-paginated page for the
  browse feed; `order` ∈ {time, area_desc, area_asc}; `motion` ∈ {all, motion,
  still}. `cursor` is an **opaque token** the caller passes back from a prior
  page's `next_cursor` (`None` for the first page). `time` keysets on `id`
  (`id DESC`); the area orders keyset on the compound `(area, id)` key so they
  page beyond a single top-N slice. `next_cursor` is `None` once a page is short.
- `path_for(id)` / `stats()` / `clear()` (delete all rows + files, reset total).
- **Concurrency:** the collector thread is the sole writer; API handlers read (and
  `clear` writes). One connection opened `check_same_thread=False`, guarded by a
  single `threading.Lock` around every DB op — writes are tiny and browse reads
  are human-paced, so contention is negligible. Running total is recomputed from
  `SUM(bytes)` on startup.

### Collector (`compute/collection/collector.py`)

A daemon thread: `for frame in client.iter_stream_reconnecting(): store.add(frame)`,
checking a stop event between frames. Reconnection/backoff is already handled by
the client. Logs periodically (frames saved, current store size). No dedup needed
— the stream delivers each frame once and `id` is unique even across edge
restarts (where `frame_id` repeats).

### Web app (`compute/api/app.py`)

FastAPI app; on startup builds the `Store` + `EdgeClient` and starts the collector
thread. Routes:

- `GET /` → `web/index.html`.
- `GET /api/frames?cursor=&limit=200&motion=all|motion|still&order=time|area_desc|area_asc`
  → `{frames: [rows], next_cursor}`; each row is (`id`, `recv_ts`, `edge_ts`,
  `motion`, `area`, `bbox`, media URL). `cursor`/`next_cursor` are opaque tokens.
- `GET /api/stats` → `{count, bytes, cap_bytes, motion_count, oldest_ts, newest_ts}`.
- `GET /media/{id}` → the JPEG (looked up by `id`), `image/jpeg`.
- `POST /api/clear` → wipe the store.

### Browse UI (`compute/api/web/index.html`)

Vanilla JS in the edge config-UI style (no build step). A time-ordered grid of
frames; each **motion** frame gets a coloured border, non-motion none. A stats
header (count, size / cap, time span, motion count) and a **"Clear all"** button
(with a confirm). Controls: a motion filter (All / Motion / Still), a sort (Time /
Area high→low / Area low→high), and — the point of the tool — two one-click
**triage presets**:

- **"Missed?"** → still + area high→low (candidate false negatives).
- **"False triggers"** → motion + area low→high (candidate false positives).

Paging is **Prev / Next + "Page N"** (fixed page size, the grid is replaced per
page, not appended), driven by a client-side stack of the opaque cursors — so
Prev is stable even as the collector inserts newer frames, and it works for the
area/triage views too. Full JPEGs are served and the browser scales them in the
grid.

### Entry point (`compute.sh`)

Mirrors `edge.sh`: bootstrap a venv from `compute/requirements.txt`, then launch
uvicorn on the api app. Uses a compute-specific venv dir so it doesn't clobber the
edge's `.venv` if both are checked out on one dev box (in production they're on
different hosts). Env: `CAT_PI_URL`, `CAT_COLLECT_DIR`, `CAT_COLLECT_MAX_BYTES`
(default `5368709120` = 5 GB), `CAT_COLLECT_PORT` (default 8001; edge is 8000).

## Alternatives considered

- **Flat files + JSONL manifest (no DB).** Simplest, zero deps, but ~860k
  lines/day makes time-jump and motion/area filtering slow and clumsy — the exact
  operations the tuning workflow leans on.
- **Thin out non-motion frames to save space.** Rejected: the non-motion frames
  are precisely where missed cats (false negatives) hide, so discarding them
  defeats the tuning purpose.
- **Full dataset/annotation store now** (source/label columns, thumbnails,
  dashboard scaffolding). Over-builds a browse-frames MVP; annotation and labels
  belong to the later learning phase.
</content>
