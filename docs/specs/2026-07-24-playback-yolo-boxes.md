# Confidence-colored filmstrip + YOLO boxes on playback

Show YOLO's per-frame detection during event playback, in both the user (`/`) and
admin (`/admin#activity`) Activity modals: **color each filmstrip tile by that
frame's detection confidence** (red/amber/green), and **draw the detection box** on
the played-back stage frame. Both are fed by a small read-time addition to the
frames-sample call the players already make — the store attaches each sampled
frame's stored `yolo-serial` box + score. No schema change, no new endpoint; the
per-frame boxes are already durable in `analysis.detail`.

## Key decisions

- **Per-frame data rides `GET /api/frames/sample`** (extends). Add an optional
  `detections=yolo-serial` query param (validated against `ANALYZER_NAMES`, 400
  otherwise). When set, each returned frame gains `{analyzed, score, box, cls}`
  (below); when absent, the response is byte-identical, so the density/buckets
  viewers that also call this endpoint are untouched. Chosen over a new
  `/api/events/detections` endpoint because both players already fetch
  `/api/frames/sample` for the exact frames shown — one call, no extra round-trip
  (see Alternatives).
- **Store attaches via a shared box-parse helper** (reuses). A new
  `Store` helper joins `analysis` for the requested analyzer over the *already-
  sampled* frame ids and, per frame, reduces `detail.boxes` to the single
  highest-confidence detection box using the existing `_detail_boxes` /
  `_box_class` parse (`store.py:2871`, `:2888`). Keeps the store cv2-free and the
  legacy-5-element box rule in one place. Applied inside `sample_frames` (the
  index-based strategy the player uses) behind the param.
- **Box = highest-conf box over {cat, person, bird}** (extends). Not the cat-only
  `_best_box` (`store.py:2907`, which is cat-only): the box shows what YOLO
  actually detected in this frame, over the same {cat, person, bird} class set the
  detection aggregates use (the module-level `_COCO_*` constants; `store.py:2616`
  builds that set locally). Returned as `box:[x1,y1,x2,y2]` (stored-JPEG pixel
  space) + `cls` (COCO id) + `score` (that box's conf). It is per-frame truth and
  is **not** tied to the event's per-visit `subject` chip: on a frame whose top
  detection is a different class than the visit summary (which applies a 0.3 floor,
  area gating, and resident-identity promotion), the box shows the frame's actual
  detection — so a `person 0.9` box can legitimately appear during a mostly-`cat`
  visit. The box is honest about this frame; the chip summarizes the visit.
- **Reuse the health-band threshold *values*, per page** (extends). Tile color =
  a `bandOf(score, 0.40, 0.65)` → red / amber / green, using the same thresholds
  the user page's visit-health `conf_max` band already uses. But `bandOf` /
  `visitHealth` / the traffic-light colors exist **only in `user/index.html`**;
  the admin page has no health dot (its detection display is text-only —
  `detectionStats`, admin `:3255`). The two `index.html` files deliberately share
  no JS (changelog 80), so admin gains its own `bandOf` copy + the threshold
  literals. This is *value-level* consistency (both pages agree on what a
  confidence looks like), not a shared source. The per-frame tile color is a
  **standalone signal**, deliberately independent of the health dot: the dot
  aggregates over motion frames only (`store.py:2633`) while the filmstrip samples
  the whole span, so a non-motion tile can be colored without touching the dot.
  Provisional/uncalibrated — raw top-down scores.
- **Confidence is a bottom color-bar, not the tile border** (new). The tile
  border/inset is already claimed by `film-current` (accent) and `film-rep`
  (purple). A thin bar along the tile's bottom edge carries the confidence band so
  the three cues layer without fighting. This is a new visual element in both
  filmstrips.
- **Box overlay reuses the annotation %-of-natural-dimensions pattern** (reuses).
  The drawn box is positioned as a percentage of the stage image's *natural*
  dimensions — exactly `annotateRepStage`'s technique (`admin …:5132`) — inside an
  aspect-locked wrapper so `object-fit: contain` letterboxing can't mis-place it
  (see Design). Repositioned per frame in `showPlayerFrame`.
- **Read-time, frontend-first** (reuses). No new table, no persisted field; the
  only backend code is the sample-time join. All rendering is client-side in the
  two `index.html` files. Fully reversible.

## Goals

- During playback, at a glance see which frames YOLO detected the subject in and
  how confidently — as a color per filmstrip tile.
- See *where* YOLO detected it — a box drawn over the played frame, tracking the
  image at any rendered size.
- Do it in both the user and admin players, from the same backend addition and the
  same color semantics.

## Non-goals

- No change to detection itself, to the `yolo-serial` sweep, or to how/when frames
  are swept. This only *displays* already-stored verdicts.
- No re-detection trigger from the player (admin already has "Re-analyze frames").
- No calibration of the confidence thresholds — they inherit the health dot's
  provisional bands and are re-tuned separately.
- No coloring of the event *cards* / thumbnails (this is the filmstrip + stage
  only); the cards already carry the visit-health dot and detection line.

## Design

### Backend — `/api/frames/sample` + store

`api_frames_sample` (`app.py:860`) gains `detections: str | None`. When provided it
must be in `ANALYZER_NAMES` (else 400, matching `/api/frames`'s analyzer gate);
it's threaded into `store.sample_frames(...)`. `sample_frames` (`store.py:1094`)
selects its `~count` frame ids as today, then — when `detections` is set — runs one
extra indexed read over just those ids:

```
SELECT frame_id, detail FROM analysis
 WHERE analyzer = ? AND frame_id IN (<sampled ids>)
```

and, in pure Python, reduces each frame's `detail.boxes` to the best detection box
over {cat, person, bird}. The `IN` list is bounded — `count` is clamped to
`_MAX_SAMPLE` and both players pass the player's small `MAX_PLAY_FRAMES` — so the
bound-parameter count stays modest. Each frame dict becomes:

```
{id, recv_ts, url,
 analyzed: bool,          # a yolo-serial row exists for this frame (i.e. swept)
 score:    float | null,  # best-box conf; null when no detection box
 box:      [x1,y1,x2,y2] | null,   # stored-JPEG px; null when no detection
 cls:      int | null}    # COCO class of the drawn box
```

`analyzed=false` (no row) is "not measured"; `analyzed=true, box=null` is "swept,
nothing detected." v1 renders both as neutral (no bar), but the payload keeps them
distinct so a later revision can mark them differently without a backend change —
preserving the not-measured-vs-measured-miss distinction the detection aggregates
already honor.

Only `sample_frames` (the count strategy the players use) needs the param;
`sample_frames_by_interval` is left as-is (the density viewer doesn't ask for
detections). Both players call `/api/frames/sample?...&detections=yolo-serial`.

### Frontend — filmstrip tile coloring

In `buildPlayerFilmstrip` (admin `:3349`) / `buildFilmstrip` (user `:1256`), each
tile gets a child `<span class="film-conf">` whose color is set from
`bandOf(f.score, 0.40, 0.65)` — red / amber / green — rendered as a thin bar on the
tile's bottom edge. A frame with `score == null` (no detection or not measured) gets
no bar. `film-rep` and `film-current` keep the border/inset unchanged.

### Frontend — box overlay on the stage

The single `#playerImg` stays; it's wrapped in a positioned `.player-frame`
container. The container is **aspect-locked** to the image's natural
`nw/nh` (set once the first frame's `naturalWidth/Height` are known) *and*
constrained `max-width: 100%; max-height: 100%` **inside the existing fixed
stage** (admin `#playerStage` 420px `:1208`; user `.stage` 4/3 `:582`). So it
occupies exactly the contained-image rectangle without ever *resizing* the stage —
preserving the fixed stage's deliberate no-reflow property (admin comment `:1206`:
the fixed size exists so scrubbing/flipping events "never resizes the box and
reflows the strip below"). Because one edge camera feeds every event, `nw/nh` is
constant across events too, so Prev/Next doesn't re-jump. This is
`annotateRepStage`'s %-overlay technique (`admin …:5132`), adapted to sit within a
fixed stage rather than grow one. `showPlayerFrame(idx)` positions a single
`.player-bbox` div from `playerFrames[idx].box`:

```
left = x1/nw*100%   top = y1/nh*100%
width = (x2-x1)/nw*100%   height = (y2-y1)/nh*100%
```

The box is hidden when the current frame's `box` is null. Its border color matches
the tile band (`bandOf(score)`), and a small class+confidence caption (`cat 82%`)
rides its corner — naming what the box is and how sure YOLO was. Because
coordinates are relative to natural dimensions, the box tracks the image at any
modal/stage size with no resize math.

### Interplay note

This adds a *new* coloring dimension (detection confidence) distinct from the motion
caught/miss verdicts the admin player deliberately turns off
(`filmTile(..., {verdict:false})`, admin `:3352`, comment at `:4666`). We are not
re-enabling motion coloring — confidence is about YOLO detection strength, which is
meaningful on playback in a way the gate's per-frame "miss" is not. The two never
appear together in the player.

## Alternatives considered

- **New `/api/events/detections` endpoint + client-side join.** Returns
  `{frame_id: {score, box, cls}}` for a span; each player fetches it after sampling
  and joins by id. Rejected: the players already fetch the exact frames from
  `/api/frames/sample`, so attaching to that response is one call instead of two
  and needs no client join — the endpoints would share the same store box-parse
  anyway, so the extra surface buys nothing here.
