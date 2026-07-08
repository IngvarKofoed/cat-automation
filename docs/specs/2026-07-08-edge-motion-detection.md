# Edge motion detection (pullable), continuous stream unchanged

Run MOG2 background-subtraction motion detection on the Pi, in the existing
grabber loop, over the downscaled clipped ROI — tuned for the static, top-down,
under-cover scene where the only real disturbance is global illumination
(clouds). Motion is exposed as a **separate signal a client pulls** (`GET
/status`), *not* as a gate on frame delivery: `/stream` and `/frame` keep serving
every frame exactly as today. The config UI gains a live detection overlay and
tuning controls so the gate can be dialed in.

## Key decisions

- **Frames stay continuous; motion does NOT gate delivery** (diverges from
  ARCHITECTURE.md). The architecture says the Pi emits frames "only when the
  clipped region changes"; per the owner's decision we keep `/stream` continuous
  and publish motion as a separate pullable signal instead. Reason: at ~5 fps on
  a small ROI over the LAN bandwidth is trivial (the doc itself says so), and
  decoupling lets motion gate the *compute's GPU cost* (when to run detection)
  rather than *frame availability* — frames stay usable for preview, enrollment,
  and audit regardless of motion. **ARCHITECTURE.md is updated when this ships.**
- **Motion runs inside the grabber loop** (extends). `Grabber._grab_once_internal`
  computes motion on each grabbed frame and stores the result in the slot
  alongside `frame`/`frame_id`/`ts`. One producer, no extra thread; the result
  rides the existing `FrameSnapshot` (new fields `motion`, `bbox`, `area`).
- **MOG2 on a downscaled grayscale ROI** (new; OpenCV
  `cv2.createBackgroundSubtractorMOG2`, already a dep). Cheap on a Pi 3. A **slow
  learning rate** so a cat that pauses under the camera isn't absorbed into the
  background — affordable precisely because locality (below), not fast
  adaptation, is what rejects illumination.
- **Locality/area gating is the decision rule** (new). Foreground mask →
  morphological open → connected components → `motion = True` iff the largest
  connected blob's area is within `[min_area, max_area_fraction × ROI]`. A
  near-whole-ROI change is rejected as illumination (a cloud lights the whole
  floor; a top-down cat is a compact blob). This is the cloud-robustness core.
- **Temporal persistence** (new). Require motion in `N` consecutive frames before
  reporting `motion=True` (debounce against single-frame flicker/noise).
- **Motion published on two pull channels** (new). (1) `GET /status` — a JSON
  snapshot of the latest slot `{frame_id, ts, motion, bbox, area, camera_ok,
  last_error}`, the home of **dead-camera** health (`camera_ok`, replacing the
  code-review's stream-keepalive idea) and the config UI's readout. (2) Each
  `/stream` multipart part *also* carries `X-Motion` (and `bbox`/`area` when
  active) in its headers, so a client already reading the stream gets motion
  inline without a second request. Both are inbound-only (the Pi stays a pure
  server); either way motion correlates to a frame by `frame_id`.
- **Detection shown in the config UI via a server-rendered overlay** (extends).
  A browser `<img src="/stream">` can't expose multipart headers to JS, so the UI
  reads numbers by polling `/status` and *sees* the box via an `overlay=1` param
  on `/stream`/`/frame` where the Pi draws the bbox onto the frame before
  encoding (reusing `_render`). Plus tuning inputs and a "Relearn background"
  button.
- **Motion config persisted like the rest** (extends `settings.py`). New keys —
  `var_threshold`, `learning_rate`, `min_area`, `max_area_fraction`,
  `persistence`, `motion_downscale` — ride the existing `GET|POST /api/config`
  and `save_settings`, validated like `fps`, applied live by the grabber each
  iteration (no restart), defaults merged so old settings files load fine.
- **Frame delivery unchanged; `/stream` parts gain motion headers** (extends).
  `/frame` and `/stream` keep serving every frame as today — no gating, same
  cadence and format; the only addition is the `X-Motion`/`bbox`/`area` part
  headers on `/stream` (above). `/frame` stills carry no motion headers — use
  `/status`.

## Goals

- Reliable motion detection on the Pi for the static top-down covered scene,
  robust to global illumination (clouds), cheap enough for a Pi 3.
- Motion available as a **separate signal a client pulls**, without affecting
  frame delivery.
- Frames keep flowing regardless of motion.
- Enough tuning + visual feedback in the config UI to make it reliable.

## Non-goals

- **Motion-gating the stream / bandwidth optimization** — explicitly dropped (see
  the first key decision).
- Cat detection, identification, direction/zones — all compute-tier.
- Fixed-exposure camera control — deferred to a follow-up increment (locality
  gating already handles clouds on this covered scene).
- Full MOG2 debug views (the raw foreground mask, `getBackgroundImage()`) — a
  possible fast-follow if the bbox overlay proves insufficient for tuning.
- Any compute-tier *consumption* of `/status` — the compute tier isn't built;
  this increment only defines and serves the signal.

## Design

### Motion in the grabber

The `Grabber` owns a MOG2 instance (created lazily, one per active source). Each
iteration, after `source.read()` succeeds, before publishing the slot:

1. **Derive the ROI:** the grabber stores the *raw* frame, so the motion step
   applies the current rotate+crop (the same transform the serving routes use) to
   get the door ROI, then **downscales** it to ~`motion_downscale` px wide and
   converts to grayscale (motion needs no resolution or color; this also cuts
   Pi-3 cost to single-digit % CPU and denoises).
2. `mask = mog2.apply(small, learningRate=learning_rate)` — slow rate.
3. Threshold shadows out (MOG2 marks them gray 127), morphological **open** to
   drop speckle.
4. `connectedComponentsWithStats` → take the largest blob. `motion = True` iff its
   area ≥ `min_area` **and** ≤ `max_area_fraction × ROI area` (the whole-frame /
   illumination reject), sustained for `persistence` consecutive frames.
5. Store `motion` (bool), `bbox` (normalized to the ROI, 0..1, so it's
   resolution-independent and matches the clip-rect convention), and `area`
   (fraction of ROI) into the slot with the frame.

`FrameSnapshot` gains `motion: bool`, `bbox: tuple|None`, `area: float`. Motion is
always computed (it's the pullable signal), never used to gate publishing.

**Live tuning.** The grabber reads the motion params each iteration (snapshotted
under the config lock, same mechanism as `fps`). `learning_rate` is passed to
`apply()`; `var_threshold` is set on the MOG2 object; the area/persistence params
are used in post-processing — all changeable without a restart.

**Reset / relearn.** The MOG2 model is tied to the exact ROI pixels and
dimensions, so anything that changes its input imagery must recreate the instance
(relearn from scratch): a **device swap**, a **clip (ROI) or rotation change**,
and the UI's **"Relearn background"** action. The config-change cases hook
`POST /api/config` (alongside the existing post-swap `grab_once()`); the manual
case is a small `POST /api/motion/reset`. Without this, re-drawing the ROI would
compare new pixels against a stale model and burst false motion until it
re-adapts.

### `GET /status`

Returns the latest slot as JSON:

```json
{ "frame_id": 1234, "ts": 1783483341554, "motion": true,
  "bbox": [0.31, 0.20, 0.22, 0.35], "area": 0.08,
  "camera_ok": true, "last_error": null }
```

`camera_ok` is `last_error is None` and the frame is fresh (reusing `/frame`'s
monotonic staleness check). A client polls this (the compute to decide when to
run its GPU detector; the UI for its readout) and correlates to stream frames by
`frame_id`. No waiting/long-poll this increment — a plain snapshot.

In addition, each `/stream` part header carries `X-Motion: 0|1` (plus `X-Bbox`
and `X-Area` when active) alongside the existing `X-Frame-Id`/`X-Timestamp` in
`_build_part`, so a stream-consuming client gets motion inline. `/status` remains
the channel for camera health and for the browser UI (which can't read multipart
headers from an `<img>`).

### Config UI

- Poll `/status` a few times a second → show a **motion indicator**, the area %,
  and camera health.
- **Overlay preview:** `<img src="/stream?overlay=1">` (and `/frame?overlay=1`)
  where the Pi draws the detected bbox (and area text) onto the frame before
  JPEG-encoding, so the box is visible live with no JS header access. `overlay`
  is handled in `_render`.
- **Tuning controls** for the persisted motion params, and a **Relearn
  background** button (`POST /api/motion/reset`).

### Pi 3 performance

MOG2 + morphology + connected-components on a ~160-px-wide grayscale ROI at 5 fps
is single-digit % CPU on the Pi 3's quad A53 — comfortably within budget. The
downscale is the main lever; full-res motion is unnecessary.

### Paused-cat absorption (known limit)

Because MOG2 keeps learning every frame, a cat that holds still long enough is
gradually absorbed into the background — `motion` goes false while it sits, and a
brief "ghost" (false motion) appears where it was when it leaves. Absorption time
scales inversely with the learning rate (≈ `1/learning_rate` frames, i.e.
≈ `1/(learning_rate × fps)` seconds), so the **slow** `learning_rate` we use
pushes it to several minutes. This is benign for a **door-transit** camera: a
crossing pauses for seconds not minutes; a motionless cat means nothing to
report; and any movement (including leaving) re-fires motion at once. Since motion
is a pull signal not a frame gate, the leave-ghost costs nothing. If it ever
matters, the mitigation is **selective update** — drop the learning rate toward 0
while a blob is present so a cat that's there is never learned — deferred until
tuning shows a need.

## Alternatives considered

- **Motion-gates the stream (the original architecture design).** Emit frames
  only during motion, idle with keepalives. Rejected by the owner: it couples
  motion to frame availability for a bandwidth saving that's negligible on this
  LAN, and it makes preview/enrollment/audit depend on motion. The pull-signal
  design keeps them independent.
- **Motion on the full-resolution ROI.** More faithful but needless cost on a Pi
  3; a downscaled grayscale ROI detects a cat-sized blob just as well.
- **Frame-differencing instead of MOG2.** Simpler but no adaptive background, so
  it can't absorb slow light drift and has no shadow handling — MOG2 is the
  architecture's pick and barely more cost.
