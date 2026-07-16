# Cat-identity annotation tool

A keyboard-first page in the compute dashboard for labelling *who* each detected
cat is, at volume. It reuses the trustworthy `yolo-serial` detections already in
the store, clusters them into **visits**, and lets the owner tap one identity per
visit — a named individual (resident or foreign), "unknown cat", or "not-a-cat".
Labels land in a new, durable `dataset_items` table that becomes the training set
for a later identification model. **Training itself is out of scope here** — this
spec produces labelled crops, nothing more.

Grounding: the camera is top-down, so identity comes from dorsal coat/pattern,
tail, and size — no faces. Many detections are partial (a tail or head at the
frame edge near the door), so a crop-quality signal keeps junk out of the future
gallery.

## Key decisions

- **Durable `dataset_items`, decoupled from the frame buffer** (new / diverges).
  One new table, its own autoincrement id, each row a *self-contained* labelled
  crop: `cat_id`, `label_kind`, `quality`, `bbox`, `crop_path`, `src_frame_id`,
  `src_recv_ts`, `labeled_ts`. It is **not** dropped by eviction or `clear()` —
  diverging from the store convention that frame-keyed tables (`analysis`,
  `groups`, `mode_changes`) get wiped — because hand-made labels are the precious
  output, while frames are a rolling buffer. The queue's "already decided" check
  keys on **(`src_frame_id`, `src_recv_ts`)** so a `clear()` + rowid-reuse can
  never mislabel a brand-new frame that happens to reuse an old id (the recv_ts
  won't match). See *Durability*.
- **Populated lazily on label, not pre-extracted** (new). No bulk candidate rows;
  a `dataset_items` row exists only once the owner decides on it. The annotation
  queue is *virtual*: live `yolo-serial`-present frames, clustered into visits,
  minus already-decided ones.
- **Per-frame rows, written per-visit gesture** (new). Labelling a visit
  bulk-inserts one row per visit frame with the same `cat_id`. A "visit" is only
  the UI grouping — it has no stable id, so the durable unit is the frame.
- **Reuse the visit clustering + inbox UI** (reuses / extends). A new
  `Store.annotation_visits(...)` clusters *all* `yolo-serial`-present frames via
  the existing `_gap_split`/`_VISIT_GAP_MS` primitive (the current
  `Store.visits()` only does disagreement modes). The page reuses the visit
  inbox's representative-frame + filmstrip + `j`/`k` navigation.
- **Crops from stored serial-YOLO boxes; server-side crop endpoint** (reuses /
  new). Boxes are read from `analysis.detail` (`{"boxes": [[x1,y1,x2,y2,conf]]}`).
  A new `GET /api/label/crop/{frame_id}?box=...` crops the stored JPEG on the fly
  with cv2 (already a dep); the same function materialises durable crops into a
  new `dataset/` media dir. No re-detection.
- **Editable roster** (new). A `cats` table + CRUD; number keys bind to it; cats
  are added/renamed mid-annotation.
- **Manual per-frame crop quality, auto-seeded** (new). Each visit frame gets a
  `quality` (`gallery`/`ok`/`poor`) seeded from box area + detection confidence,
  then adjustable by eye in the filmstrip (click a crop to cycle) before the visit
  is committed. A future gallery build consumes only `gallery`.
- **New `#annotate` SPA route** (extends). Add to the router at `index.html:1787`
  (nav-link + `view-annotate` container + `ROUTES` + `onRouteEnter`), with the
  bucket scope mirrored like `#sweeps`/`#tuning`.

## Goals

- Label the identity of detected cats fast and keyboard-first, over the ~1,628
  (and growing) `yolo-serial` cat-present detections already stored.
- Capture a per-crop quality signal so only clean crops can later feed a gallery.
- Keep an editable roster of named individuals (residents + named neighbours).
- Persist labels durably in the existing SQLite store, surviving frame eviction
  and `clear()`.

## Non-goals

- **Training / gallery build / embeddings** — the next spec, deliberately separate.
- **Manual box-drawing for detector *misses*.** The queue is detector-found cats
  only; frames where YOLO missed the cat aren't annotatable here (revisit once
  recall is measured on real data).
- **Two cats in one frame simultaneously.** One cat per frame is assumed;
  tailgating within a visit is handled coarsely (see Open questions).
- **Correcting run-mode identifications** — there is no run mode yet.
- Night/IR image-quality work; the decision engine / access control.

## Design

### Schema (added to `index.db`, on the Store's single connection + lock)

```sql
CREATE TABLE cats (
  id          INTEGER PRIMARY KEY,
  name        TEXT    NOT NULL UNIQUE,
  is_resident INTEGER NOT NULL DEFAULT 0,   -- 1 = our cat, 0 = named foreign/neighbour
  active      INTEGER NOT NULL DEFAULT 1,   -- retire without deleting labels
  notes       TEXT,
  created_ts  INTEGER NOT NULL
);

CREATE TABLE dataset_items (
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
CREATE INDEX idx_dataset_src ON dataset_items(src_frame_id, src_recv_ts);
CREATE INDEX idx_dataset_cat ON dataset_items(cat_id);
```

`cats` and `dataset_items` are self-contained (no frame FK); neither is touched by
`_evict_locked` or `clear()`. Media lives in a new `<CAT_COLLECT_DIR>/dataset/`
tree, separate from the rolling `media/`, so crops persist when frames age out.

### The virtual queue — `Store.annotation_visits(oracle, since_id, until_id)`

Selects live frames with a present verdict for `oracle` (`yolo-serial`) whose
`(id, recv_ts)` has **no** matching `dataset_items` row, joins each frame's box
from `analysis.detail`, and clusters them by `recv_ts` with `_gap_split` /
`_VISIT_GAP_MS`. Returns visit records: `{frames:[{id,recv_ts,bbox,score,url}],
rep_frame_id, peak_area, peak_score, span}`. `rep_frame_id` is the peak-area
frame (fullest view of the cat), independent of queue order. Visits are returned
**chronologically** (`recv_ts` ascending). Bucket scope rides the same
`since_id`/`until_id` as every other windowed read.

Caveat, stated in the UI: `yolo-serial` coverage is partial (~145k/1.09M frames,
concentrated early), so visits only form where coverage is contiguous. Growing
the candidate pool = running more `yolo-serial` sweeps on the existing Sweeps
page — not this tool's job.

### Labelling flow (per visit)

The page shows the rep crop large + a filmstrip of the visit's crops (reusing the
inbox layout). Keys:

- **`1`–`9`** — assign that roster cat to the whole visit.
- **`u`** — unknown cat (a cat, not identifiable / one-off).
- **`x`** — not-a-cat (detector false positive).
- **`s`** — skip (leave undecided, re-appears next load).
- **`j` / `k`** — next / prev visit.

Each filmstrip crop shows a `quality` badge, seeded from area + confidence and
clickable to cycle `gallery`→`ok`→`poor`. Assigning an identity (or `u`)
bulk-inserts one `dataset_items` row per visit frame with its current quality,
materialises a durable crop for each, and advances. `x` inserts `not_cat` rows
(no crop) for the visit's frames. A small progress readout shows visits decided /
total-in-window and crops labelled.

A visit with more than one cat is out of the MVP: mark it `unknown cat` (`u`) and
move on. A per-frame split mode is a fast-follow.

### Crop serving + materialisation

`GET /api/label/crop/{frame_id}?box=x1,y1,x2,y2` reads the frame JPEG via
`store.path_for`, crops to the (clamped) box with cv2, returns `image/jpeg`. Used
for both the rep and the filmstrip. Materialisation calls the *same* crop helper
and writes the result to `dataset/<cat-or-kind>/<frame_id>_<ts>.jpg`, recording
`crop_path`. Re-encode is acceptable here (unlike the collector's verbatim
store) — these are a small, deliberate training set, not the hot ingest path.

### Durability rules

- Eviction / `clear()` never delete `cats` or `dataset_items`, and never touch
  `dataset/` media.
- The queue's "already decided" predicate is
  `NOT EXISTS (SELECT 1 FROM dataset_items d WHERE d.src_frame_id = f.id AND
  d.src_recv_ts = f.recv_ts)`. After a `clear()` reuses id 5 for a new frame, an
  old label for the previous frame 5 has a different `src_recv_ts`, so the new
  frame is correctly still-pending.
- A labelled crop whose source frame later evicts remains valid — the crop file,
  `bbox`, and `cat_id` are self-contained; only the (unused-after-label) live
  link goes stale.

### Roster CRUD + route

- `GET /api/cats`, `POST /api/cats` (name, is_resident), `PATCH /api/cats/{id}`
  (rename, toggle resident/active). A duplicate name → 400 (UNIQUE).
- `GET /api/label/visits` wraps `annotation_visits`; `POST /api/label` takes
  `{decision, cat_id?, frame_ids[], bboxes[]}` and writes the rows + crops.
- New `#annotate` route + `view-annotate` container; a roster panel (add/rename)
  plus the visit stage. Build follows the compute UI conventions (the
  `frontend-design` skill) — presentation only, not a design decision here.

## Alternatives considered

- **Per-detection labelling (Approach A).** Simpler, but the data is dense with
  intra-visit near-duplicates — one 8-second visit is ~40 near-identical frames to
  tap through. Rejected for a volume workflow; per-visit yields the same labelled
  crops for far fewer clicks.
- **Pre-extracted first-class dataset (Approach C).** A bulk step materialising a
  candidate row per detection up front. Cleanest long-term artifact but the most
  infra, and it creates thousands of noise rows (tails you never label). We get
  the durable artifact anyway by writing `dataset_items` lazily on label.
- **Crop-on-read only (no materialisation).** Rejected: labels would die with the
  frame buffer, and the training set is the whole point of the exercise.
