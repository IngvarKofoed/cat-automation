# User-facing dashboard: Activity + Cats

Build the real user-facing app at `/`, replacing the "Coming soon" placeholder
that changelog 80 carved out (`compute/api/web/user/index.html`). Two of the
three household-facing features ship now — **Activity** (latest door events with
time, identity, and image) and **Cats** (every named cat, residents first, with
last-seen and a photo) — in a warm, domestic "Threshold" style that shares no CSS
with the admin workbench. The third, **Who's home** (occupancy), is deferred: it
needs cat direction detection, which doesn't exist yet, so it ships as a quiet
"coming soon" panel and gets its own spec later.

Activity is fully served by the existing backend. Cats needs a small new backend:
a per-cat overview (roster + live last-seen) and an avatar the household can also
upload themselves.

## Key decisions

- **Activity reuses `/api/events` unchanged** (reuses). The oracle-free motion-cluster
  feed already returns newest-first events with `{start_id, end_id, start_ts, end_ts,
  n_frames, rep_frame_id, identity}` and the resident/foreign/unknown `identity` join.
  The user Activity view is a fresh presentation over the same data + `/api/frames/resolve`,
  `/api/frames/sample`, `/media/{id}`, `/api/stats`.
- **One self-contained hash-routed SPA** (reuses). Stays a single
  `compute/api/web/user/index.html` with its own inline `<style>`/`<script>` — matching the
  split spec (2026-07-22-admin-user-area-split). Routes `#activity` (default), `#cats`, `#home`.
  No `StaticFiles` mount, no build step.
- **"Threshold" warm design, own tokens** (new). Warm interior-vs-night palette, `ui-rounded`
  display / `system-ui` body / `ui-monospace` times, theme-aware (light + dark). No shared CSS
  with admin — deliberately upholding the split spec's independence.
- **Cats last-seen is derived from the Activity feed itself** (reuses). Per cat, the newest
  `store.events()` event whose aggregated `identity` resolves to that cat — the *same* per-event
  voting the Activity view shows, so the two views can never name a moment differently. The
  uncalibrated fail-safe carries over for free (an uncalibrated model resolves every event to
  "unknown", so no cat is "seen" — never name a resident from an uncalibrated model).
- **Avatar precedence: uploaded → labelled crop → placeholder** (new). A user-uploaded file wins;
  else the representative durable labelled crop; else a client-drawn initial-letter placeholder.
- **Uploaded avatars are a file convention, not a schema column** (new). Stored at
  `<dataset_root>/avatars/cat_<id>.jpg`; the file's presence *is* the "manual avatar set" flag. No
  `cats`-table migration. Lives in the dataset dir, so it survives eviction and `clear()` like the
  other labelled output.
- **Avatar upload is a raw request body, re-encoded with `cv2`** (new). `POST` sends the image as the
  raw body (`await request.body()`), avoiding a `python-multipart` dependency; the bytes are
  validated + downscaled + re-encoded to JPEG via the `cv2` already in the base compute deps (same
  lazy-import path as `compute/dataset/crops.py`). No new dependency.
- **Roster is display-only here** (decision). Add / rename / mark-resident / retire stays in the
  admin Annotate page. The one interactive exception on the user Cats page is setting a cat's photo.

## Goals

- A glanceable, phone-friendly view of what happened at the door — newest first, each event with its
  time, the identified cat (resident / neighbour / unknown), and an image, openable into playback.
- A "our cats" view: residents first, each with a photo, when it was last seen, and how long ago.
- Let the household set a nicer photo per cat by uploading one, without needing the admin tools.
- A visual identity for the user area that's clearly not the admin console.

## Non-goals

- **Who's home / occupancy** — deferred (no direction detection yet); ships as a placeholder panel.
- **Roster management** on the user page (lives in admin) — except the avatar photo.
- **Live video / MJPEG** — the user app shows stored snapshots + event playback, per CONCEPT.
- **Auth** — still a trusted-LAN prototype.
- Any change to `/api/events`, the collector, the identify pipeline, or admin behavior.

## Design

### Backend

**`compute/collection/store.py`**

- `cats_overview() -> list[dict]`: the roster (via existing `_cat_to_dict`) plus, per cat,
  `last_seen_ts` + `last_seen_frame_id` and `has_crop`.
  - Last-seen reuses `events()` directly: take its newest-first feed (each event already carries its
    aggregated `identity`) and, per cat, record the newest event whose `identity.cat_id` is that cat —
    `last_seen_ts` = that event's `start_ts`, `last_seen_frame_id` = its `rep_frame_id`. Same per-event
    voting the Activity view renders, so a cat's last-seen can never point at a moment the feed labels
    differently. Fail-safe carries over (uncalibrated → every event "unknown" → no cat seen); a cat
    absent from the feed → `None`. Neighbours (named non-residents) get a last-seen the same way.
  - **Bounded, not absolute history:** `events()` clusters only retained motion frames, and the identity
    join sees only `identifications` an identify pass / the live worker actually wrote (evicting with
    frames; named forward only — changelog 74–76), capped at the newest `_MAX_EVENTS`. So last-seen
    means "last positively matched in the recent feed," not the cat's whole history. The UI hedges accordingly.
  - `has_crop`: from one grouped `SELECT DISTINCT cat_id FROM dataset_items WHERE
    label_kind='identified' AND crop_path IS NOT NULL` — not a per-cat query.
- `cat_avatar_crop_path(cat_id) -> str | None`: absolute path of the representative durable labelled
  crop — preferring `quality='gallery'` → `'ok'` → any, and within a grade the most-recent by
  (`labeled_ts` DESC, `id` DESC). Reuses the `dataset_root` join from `labeled_crops()`.
- `avatar_path(cat_id) -> str`: the convention path `<dataset_root>/avatars/cat_<id>.jpg` (existence
  not guaranteed) — one place that owns the layout.

**`compute/dataset/crops.py`**

- `normalize_avatar_bytes(data, max_dim=512) -> bytes | None`: lazy-`cv2` decode → downscale to
  `max_dim` (preserve aspect, only if larger) → re-encode JPEG q95. `None` when `data` isn't a
  decodable image. Mirrors the module's existing lazy-cv2 `crop_bytes`/`materialize`.

**`compute/api/app.py`** (new routes, error-mapping matching the rest of the app)

- `GET /api/cats/overview` → `{cats: [...cats_overview fields + has_avatar], has_model, uncalibrated}`.
  `has_avatar` = uploaded file exists OR `has_crop`. `uncalibrated` = active model present but
  `threshold is None`, so the UI can note identification isn't calibrated yet.
- `GET /api/cats/{id}/avatar` → `FileResponse`: uploaded file → else `cat_avatar_crop_path` → else
  404 (client falls back to the initial-letter placeholder). Each candidate is `os.path.isfile`-checked
  before serving — matching `/media`'s guard (`app.py:706`) — so a labelled-crop row whose file was
  removed (relabel/undo) falls through to the next candidate rather than 500ing.
- `POST /api/cats/{id}/avatar` → read raw body; reject a body over ~10 MB (413) before decoding; 404 if
  the id isn't a current cat; `normalize_avatar_bytes` (512px longest side) → 400 if not a decodable
  image; write `avatar_path(id)` (mkdir the `avatars/` dir once).
- `DELETE /api/cats/{id}/avatar` → remove the uploaded file (revert to the auto crop); idempotent.

### Frontend (`compute/api/web/user/index.html`, full rewrite)

Shell: a warm app bar (product name + nav Activity / Cats / Who's home), hash routing, a discreet
`/admin` link in the footer. Own inline CSS/JS; reuse admin *idioms* (own copies): `formatTime`
(dd/mm-yyyy, 24h), a relative-time helper ("2h ago"), and the playback state machine.

- **Activity (`#activity`)** — the signature view. A date range (defaults to the **last 7 days**
  through the store's newest day, from `/api/stats`) resolves via `/api/frames/resolve` →
  `/api/events`. Rendered as a
  **chronological threshold time-rail**: day group headers (Today / Yesterday / dd/mm-yyyy), a
  hairline spine with a dot per event, event cards to the right — each a "porthole"-framed rep image
  (`/media/{rep_frame_id}`), the time + relative time, and an identity chip: resident = green,
  named neighbour = coral, unknown cat = amber, unidentified = no chip (reusing the `identity` shape).
  A "non-residents & unidentified only" toggle. Opening an event — a single tap/click, or Enter when
  the card is focused — shows a **playback modal** (re-implement `openEvent`: `/api/frames/sample` over
  `[start_id, end_id]`, bounded preload, interval play at ~5 fps, scrub, Prev/Next). Empty and truncated
  states written in-voice.
- **Cats (`#cats`)** — `/api/cats/overview`. "Our cats" (residents) first, then "Neighbours" (named
  non-residents); inactive (`active=0`) cats hidden. Each card: avatar (`<img src=/api/cats/{id}/avatar>`
  with initial-letter fallback on load error), name, resident/neighbour tag, and last-seen as absolute +
  relative, or "Not seen yet." A quiet "Set photo" control uploads a chosen file (POST raw body) then
  refetches — the client normalizes EXIF orientation before upload (draw through a canvas /
  `createImageBitmap({imageOrientation:'from-image'})`), since the cv2 re-encode drops the EXIF tag and
  phone photos would otherwise show sideways. Two distinct one-line notes cover the empty states so
  "Not seen yet" never reads as "the camera saw nothing": when `has_model` is false, identification
  isn't set up yet (build & promote a gallery in Admin); when `uncalibrated`, the model needs more
  labelled data to calibrate.
- **Who's home (`#home`)** — a short placeholder explaining occupancy arrives once the door can tell
  entering from leaving.

Quality floor: responsive to phone width, visible keyboard focus, `prefers-reduced-motion` respected,
graceful empty/error states.

## Alternatives considered

- **`cats.avatar_path` schema column** instead of the file convention. More explicit, but forces an
  `ALTER TABLE` migration on the live store for no functional gain; the file's presence already answers
  "is there a manual avatar." Rejected.
- **`python-multipart` file upload.** The idiomatic FastAPI path, but adds a dependency purely to parse
  one image field. A raw-body POST needs none. Rejected.
- **Live-pipeline-only Cats data** (photo + last-seen both from identifications). Simpler, but leaves
  every card blank until a gallery is promoted and an identify pass runs — the durable labelled crop
  gives each cat a photo from the moment it's annotated. Rejected in favor of avatar + live-seen.
- **Match the admin dark console.** Rejected by the split spec's intent and the user's pick — the user
  area is meant to diverge.
