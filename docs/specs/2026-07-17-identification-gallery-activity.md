# Cat-identification gallery + activity-feed names

Build the runtime side of the learning loop's **Train → Run**: a **gallery-build**
job that turns labelled crops into a versioned gallery model, a **promote** step
that makes one version active, and an **identify** pass that matches detected cats
against the active gallery and writes per-frame identifications — which the
existing **activity feed** then aggregates into a cat name (or "unknown cat") on
each event card. This is the deferred `gallery-build`/`promote` work the
training-page spec reserved, plus the first consumer of the crop-quality grades
(which so far have none). It is **offline identification over collected frames — a
Phase-1 payoff, not the live door loop**; run-mode, the decision engine, and
actuation stay deferred.

## Key decisions

- **Identity reaches the feed via persisted identifications** (new). An
  `identify` background pass embeds detected crops, matches the active gallery, and
  writes one `identifications` row per frame; `Store.events()` aggregates the rows
  inside each event's id-span into an event identity. Reuses the resumable-sweep +
  persisted-verdict shape the oracle layer already uses, rather than embedding on
  page load. (Chosen over on-demand-per-event and identify-at-visit-level; see
  *Alternatives*.)
- **New `identifications` table, frame-keyed, evicts with its frame** (extends).
  Cascaded in `_evict_locked` and `clear` exactly like `analysis` — an
  identification about a gone frame is meaningless and is cheap to recompute from
  the durable gallery, so it is **not** precious.
- **`model_versions` table built now** (new). The training-page spec designed it
  and deferred it; this builds it. Like `cats`/`dataset_items`/`feasibility_runs`
  it survives `_evict_locked` **and** `clear()` — a promoted model is precious,
  decoupled from the rolling frame buffer.
- **Gallery = all enrolled vectors, matched by k-NN, in an on-disk `.npz`**
  (new). `gallery-build` embeds the selected-quality `identified` crops and writes
  their vectors + parallel `cat_id` array to
  `<CAT_COLLECT_DIR>/models/<ts>-<slug>/gallery.npz`, referenced by the row.
  Matching is k=1 nearest-neighbour cosine distance, mirroring the probe that
  scored 99% kNN accuracy. Matches the architecture's file-based *model store*;
  keeps the DB lean.
- **`gallery-build` and `identify` are new `TrainingManager` kinds; `promote` is
  synchronous** (extends / diverges). The manager already dispatches on `kind` and
  reserved these two heavy (embedding, cancelable) jobs. `promote` is a trivial
  status flip, so it is a synchronous endpoint, not a job — a deliberate divergence
  from the training-page spec's "`'promote'` kind" suggestion.
- **`Embedder` gains a crop-then-embed path** (extends). `identify` embeds *live
  frames* cropped to their yolo-serial box; `gallery-build` reuses `embed_paths`
  over the already-materialised crop files. Both go through the same
  resize+ImageNet-normalise, so gallery and query distances are comparable.
- **Detection source is `yolo-serial`, not the motion bbox** (reuses). Crops come
  from the trustworthy `yolo-serial` boxes in `analysis.detail` (as the annotation
  tool does), never the crude motion blob. So names appear only where a cat was
  *detected* — identity depends on yolo-serial coverage of the window, surfaced in
  the UI.
- **Identity is additive to the oracle-free feed** (reuses). `events()`
  LEFT-joins identifications for the *active* model; an event with none renders
  exactly as today. The instant, sweep-free base activity feed is unchanged.
- **Threshold lives on the model version and is applied at read, not baked**
  (new). `gallery-build` computes a suggested cutoff (reusing
  `feasibility._best_threshold`) and stores it on the row; `events()` applies it
  when aggregating. Identify itself never uses the threshold — it stores the
  nearest cat + distance — so the threshold stays a stored, *tunable* number
  (acknowledged uncalibrated per CONCEPT) rather than a value frozen into
  idempotent rows.

## Goals

- Build a versioned gallery from labelled crops and promote one active — from the
  Training page, no CLI.
- Show each resident/neighbour cat's name (or "unknown cat") on activity events,
  the fun end-to-end payoff of Phase 1.
- Reuse the existing `Embedder`, metrics, and `TrainingManager` machinery; add the
  smallest new data model (`model_versions` + `identifications`).
- Give the crop-quality grades their first real consumer (gallery quality A/B).

## Non-goals

- Real-time run-mode, the decision engine, and any actuation — stay deferred.
- Fine-tuning the backbone or changing the re-ID architecture.
- Direction / enter-leave / occupancy — identifications are the primitive a later
  occupancy view reads, but that view is not built here.
- Auto-enqueuing uncertain identifications back into the annotation queue (the
  active-learning arm of Run) — designed-adjacent but deferred.
- Native / dark re-render of any report; fancy identity visualisation.

## Design

### Data model

Two new tables on the store's single connection + lock, plus a `models/` root.

```
model_versions(
  id           INTEGER PRIMARY KEY AUTOINCREMENT,   -- IS the version number ("v3"); never reused (precious)
  status       TEXT    NOT NULL,        -- 'draft' | 'active' | 'retired'
  kind         TEXT    NOT NULL,        -- 'gallery' (only kind for now)
  backbone     TEXT    NOT NULL,        -- resolved at build, e.g. 'dinov2_vits14' — query MUST rebuild from this
  imgsz        INTEGER NOT NULL,        -- resolved embedder input side — query MUST rebuild from this
  n_cats       INTEGER NOT NULL,
  n_vectors    INTEGER NOT NULL,
  threshold    REAL,                    -- suggested same/different cutoff; NULL when uncomputable (see below)
  quality      TEXT    NOT NULL,        -- slug: 'all' | 'gallery' | 'gallery+ok' | ...
  metrics      TEXT,                    -- JSON: per-cat counts, build separability
  gallery_dir  TEXT    NOT NULL,        -- models_root-relative basename holding gallery.npz
  created_ts   INTEGER NOT NULL,
  notes        TEXT
)

identifications(
  frame_id         INTEGER NOT NULL,    -- frames.id (the identified frame)
  model_version_id INTEGER NOT NULL,    -- which gallery produced this
  cat_id           INTEGER,             -- NEAREST gallery cat (match); NULL = "processed, un-embeddable" marker
  distance         REAL,                -- cosine distance to that nearest vector; NULL for a marker row
  bbox             TEXT,                -- "x1,y1,x2,y2" the crop was cut from (audit)
  ran_at           INTEGER NOT NULL,
  PRIMARY KEY (frame_id, model_version_id)
)
```

`id` doubles as the human-facing version number (`v<id>`); there is no separate
`version` column (the two would increment in lockstep since versions are never
deleted — dropped per the review). This diverges from the training-page spec's
designed `version` column, deliberately.

**The threshold is applied only at read, never baked into a row.** A *match* row
stores the nearest cat + its distance; "unknown" is derived by `events()` comparing
that distance to the model's threshold. This is what makes the threshold a genuinely
*tunable* number (edit `model_versions.threshold`, re-render the feed — no
re-identify), which a write-time NULL cutoff would silently defeat, since identify is
idempotent and never revisits an existing row. A frame the pass cannot embed (no
`yolo-serial` box, or a crop that won't decode / clamps to zero area) instead gets a
*marker* row (`cat_id`/`distance` NULL): it records the frame as processed so the
pass converges and never re-attempts it, and `events()` ignores marker rows (they
carry no identity). This is why `cat_id`/`distance` are nullable.

`model_versions` is precious (survives `clear()`/eviction). `identifications` is
frame-keyed: `_evict_locked` and `clear` both `DELETE ... WHERE frame_id = ?` / all
rows, exactly like the `analysis` cascade, so an identification can never outlive
its frame. `Store.models_root` = `os.path.join(os.path.dirname(db_path), 'models')`
— a sibling of `training/`/`dataset/`/`media/`, created lazily where a gallery is
written (like `training_root`); `clear` never touches it.

The `PRIMARY KEY (frame_id, model_version_id)` makes identify idempotent per model
(a re-run `INSERT OR REPLACE`s), and the write reuses `write_analysis`'s
eviction-race guard (`INSERT ... SELECT ... WHERE EXISTS (frame)`), so a slow
identify pass can't resurrect an evicted frame.

### Gallery-build job (`kind='gallery-build'`)

Enqueued on `TrainingManager` with a `qualities` selection (same control as the
Validate panel). The worker:

1. `store.count_identified_crops(qualities)` pre-check (endpoint-side, mirroring
   feasibility): fewer than 2 cats → friendly empty-state, no version built. The
   quality selection **defaults to gallery-grade only** — honoring the "protect the
   gallery" principle (`compute/CLAUDE.md`, CONCEPT): enroll clean crops, keep hard
   ones for threshold-tuning. If the gallery-grade set has fewer than 2 cats the
   empty-state says "grade representative crops as `gallery`, or widen the
   selection" rather than silently building nothing. The checkboxes still let a
   build widen to `gallery+ok` / all per run.
2. `store.labeled_crops(("identified",), qualities)` → `Embedder.embed_paths` over
   the materialised crop files (with the job's progress/cancel callback).
3. Read the **resolved** `backbone` + `imgsz` off the `Embedder` used (not the
   env, which can drift), and compute the suggested threshold from the enrolled
   vectors' same-vs-different distances (reuse `feasibility._best_threshold` — which
   returns `None` when a cat has only one crop, so no same-cat pair exists; stored
   as-is, see below) plus per-cat counts for `metrics`.
4. Write `<models_root>/<ts>-<slug>/gallery.npz` (`vectors` float32 `(N,D)`,
   `cat_ids` int `(N,)`, plus the resolved `backbone`/`imgsz`), then insert a
   `model_versions` row with `status='draft'` and `gallery_dir` = that basename.
   The dir is named by **timestamp** (like `_run_feasibility`'s report dir), NOT by
   the row id/version — so it is known *before* the insert and the file-first
   ordering holds: a crash orphans a harmless artifact dir, never a row without its
   file; a failed row-insert `rmtree`s the just-written dir.

Each build produces a new immutable `draft` version. Note the threshold computed
here is over the *enrolled* crops; if those are the clean gallery-grade set it will
read **tight** relative to messy at-the-door query crops (per CONCEPT, hard crops
belong to threshold-tuning, not the gallery). That is acceptable because the
threshold is tunable at read (edit the row) — but it means the build value is a
starting estimate to refine against a Validate run over the harder crops, not a
final cutoff.

### Promote (synchronous)

`POST /api/training/models/{id}/promote`: in one locked transaction flip the target
version → `active` and any current `active` → `retired`, so **exactly one `active`**
exists. It accepts **any existing** version, not only drafts — promoting a
`retired` version back to `active` is how `ARCHITECTURE.md`'s "a bad one can be
rolled back" works. An unknown id → 404; promoting the already-`active` version is a
no-op success. `Store.active_model()` reads the active row + its resolved
`gallery.npz` path (`None` when nothing is promoted yet, or when the artifact file
is missing — a promote-time check rejects a version whose `.npz` was lost). Promote
is instant, so no job/queue.

### Identify pass (`kind='identify'`)

Enqueued for the **active** model over an id-window (`since_id`/`until_id`, the
shared scope). No active model → the endpoint 409s. The worker:

1. `store.iter_unidentified(active_version_id, since_id, until_id, batch)` — a new
   iterator mirroring `iter_unanalyzed`: yields `(frame_id, abs_path, bbox)` for
   `yolo-serial`-present frames (box parsed via `_best_box`) that lack an
   `identifications` row for this model. Keyset-batched, yielded outside the lock,
   `until_id`-capped so a live collector can't make it loop.
2. `Embedder.embed_crops([(path, bbox), ...])` (new — see below) → query vectors.
   The `Embedder` is constructed from the active model's **stored** `backbone` +
   `imgsz`, NOT the env defaults — so a build-time/identify-time env drift can't
   embed queries in a different feature space than the gallery (a silent
   garbage-match). If that backbone won't load, the identify job fails with a clear
   error rather than falling back to a mismatched default.
3. Load the active `gallery.npz` once (cached on the job); k=1 nearest cosine
   distance → `(cat_id, distance)`, both stored verbatim. Identify applies **no**
   threshold (unknown is derived at read — see the data model and `events()`).
4. `store.write_identifications_batch(rows)` — batched `INSERT OR REPLACE` with the
   `WHERE EXISTS (frame)` guard, one lock hold + commit, like
   `write_analysis_batch`.

Resumable and idempotent: a re-run only pays for frames not yet identified for this
model; promoting a new model makes its rows a fresh un-identified set.

### `events()` aggregation

`Store.events()` gains an optional active-model join. For each event cluster
(unchanged motion-frame clustering), gather `identifications` for the active model
whose `frame_id` is in `[start_id, end_id]`, and aggregate → an `identity` field:

```
identity: {cat_id, cat_name, distance, n_identified, n_frames_voted} | null
```

Aggregation (default): **vote among below-threshold frames**, where "below
threshold" means `distance ≤ model.threshold`. The cat with the most
below-threshold frames wins; ties broken by that cat's *minimum* distance. The
payload's `distance` is the winning cat's minimum distance, `n_frames_voted` its
below-threshold count, and `n_identified` the total identified frames in the span.
Outcomes:

- some frame below threshold → `{cat_id, cat_name, ...}` (named).
- frames identified but none below threshold → `{cat_id: null}` (an *unknown cat*
  was seen — the nearest match was too far).
- no frame in the span identified at all, or no active model → `identity: null`
  (renders exactly as today).
- **model threshold is NULL** (uncomputable — e.g. one crop per cat, no same-cat
  pair): the model is *uncalibrated*, so it **fails safe** — nothing is below the
  (absent) cutoff and every event degrades to "unknown cat" rather than confidently
  naming a resident (CONCEPT/CLAUDE.md: an unknown must never be admitted as a
  resident). The UI flags such a model as having no calibrated threshold, so the fix
  (label more crops per cat, rebuild) is clear. *(Corrected from an earlier draft
  that named the nearest cat with no cutoff — that was fail-unsafe.)*

Runs as a second indexed read joined by id-range, filtered to the **active** model;
identifications from a prior (retired) version are left in place and evict with
their frames — no clean-up pass on promote.

### `Embedder` crop path

Add `Embedder.embed_crops(items, progress=...)` where `items` is `[(path, box)]`:
decode the stored JPEG, crop to `box` (reuse `dataset.crops._clamp_box` semantics),
then the existing resize→ImageNet-normalise→batch path. `embed_paths` stays for the
materialised-crop gallery build. A frame whose box is degenerate/undecodable is
skipped (returned `kept` indices align rows to inputs, as `embed_paths` does).

### Endpoints (`/api/training/*`, `/api/identify/*`)

- `POST /api/training/gallery/build` — body `{qualities}`. Count pre-check +
  `ensure_available` (503) like feasibility, then enqueue.
- `GET /api/training/models` — `model_versions` rows (newest-first) for the
  Promote panel and the Activity "active model" readout.
- `POST /api/training/models/{id}/promote` — synchronous flip; accepts any
  existing version (rollback included), 404 on unknown id, rejects a version whose
  `.npz` is missing; returns the promoted row.
- `POST /api/identify/run` — body `{since_id, until_id}`. 409 if no active model.
  The Activity/Training UI first reads the window's `yolo-serial` detection count
  (existing analysis-coverage endpoint) and refuses to enqueue a zero-detection
  window with a "run a sweep first" message; otherwise enqueue. Reuses the training
  status poll for progress/ETA.
- Cancel/queue controls are the existing `/api/training/*` ones (shared manager).

### UI

- **Training page (`#train`)** — the two stubbed cards go live:
  - *1. Build gallery*: quality checkboxes (**default gallery-grade only**), a
    **Build** button, the shared progress/ETA line, and on success the new draft
    appears in…
  - *3. Promote*: a `model_versions` list (`v<id>` · status · n_cats · n_vectors ·
    threshold · quality · time) with a **Promote** button on every non-active
    version (a `retired` one too — that's rollback); the current `active` is badged,
    and a version with a NULL threshold is flagged "no calibrated threshold". Uses
    the metrics/status chips from changelog 66. Promoting shows a note that names in
    the feed **won't update until Identify is re-run for the new model** (the prior
    version's identifications are filtered out; see below).
  - An **Identify** control (also on the Activity page, below) so the whole loop —
    build → promote → identify — is drivable from one page; window-scoped to the
    same date/bucket selection the Training page already carries.
- **Activity page (`#activity`)**:
  - An **Identify** button scoped to the current date window → `POST
    /api/identify/run`, progress via the training poll. Disabled with a note when no
    model is active ("build & promote a gallery first"). The same trigger lives on
    the Training page; both post to the one endpoint and read the one training
    status, so they can't drift.
  - **Before running, it shows how many frames in the window carry a `yolo-serial`
    detection** (reusing the existing analysis-coverage read) — so a window with no
    sweep says "0 detections here — run a yolo-serial sweep first" instead of the
    identify job completing green with zero names written (the silent-success trap).
  - The event card caption gains a name chip: the resident/neighbour **name**, or
    "unknown cat", or nothing when `identity` is null. Presentation reuses the
    existing card/chip styling.

## Alternatives considered

- **On-demand, cached per event (Approach B).** Identify each event's frames lazily
  when a window is opened, cache per (rep frame, model). Less infrastructure, names
  fill in while browsing — but couples GPU embedding to page interaction and throws
  the identities away for any other consumer (a future timeline/occupancy).
- **Identify at the annotation-visit level (Approach C).** Identify `yolo-serial`
  visits, map activity events onto overlapping visits. Most faithful to
  one-visit-one-identity and reuses the visit primitive, but the activity feed
  clusters *motion* frames while visits cluster *yolo* frames, forcing an
  event↔visit reconciliation layer that A avoids by keying identity to the frame.
- **Per-cat centroid gallery.** One mean vector per cat: tiny and in-DB, but
  untested here and discards multi-pose appearance — it could diverge from the
  probe's measured k-NN accuracy, and the point of Phase 1 is measured trust.
- **In-DB gallery blob.** Vectors as a `model_versions` BLOB instead of an `.npz`.
  Survives `clear()` for free, but bloats the row and diverges from the
  architecture's file-based model store.
