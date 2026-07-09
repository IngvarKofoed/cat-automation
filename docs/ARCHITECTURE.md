# Cat Automation — Architecture

This is the *how*. It builds on `docs/CONCEPT.md` (the *why/what*) and describes
the system's shape: its tiers, components, the vision pipeline, data flow, data
model, and the technology choices behind them. Where a choice is not yet
settled it is called out as a **decision** or listed under **Open questions**.

## Scope

This document covers the runtime system that turns a camera feed at the door
into recognized cats, recorded enter/leave history, notifications, and —
**optionally** — physical responses (locking the door, playing a deterrent
sound, switching on a light). Enrollment and model training are covered at the
level needed to explain the runtime; their detailed workflow is deferred.

**Prototype context.** This is an early prototype for a single home on a trusted
LAN (behind the household firewall and protected Wi-Fi), built in phases (see
*Status & phasing* in `CONCEPT.md`). Phase 1 is getting images out, tuning
background/reference handling, and basic collection + training to see whether
individual identification is even feasible; actuation and its access-decision
policy come later. The design below is written in the present tense as the
*target* shape — not everything exists yet. And because it runs on a trusted LAN,
the prototype deliberately uses **no authentication or user management** between
components.

## Guiding principles

These constraints from the concept shape every decision below:

1. **Thin edge, smart core.** The Raspberry Pi does only cheap, local work —
   capture, clip, simple motion detection, and actuation. *All* intelligence
   (detecting cats, identifying which cat, direction, decisions) lives on the
   compute PC. The Pi holds no models and makes no recognition decisions. It is
   a **pure server**: it only ever listens, and the compute PC (and the config
   browser) are the clients that connect to it — the Pi never dials out.
2. **Fail safe for residents.** Every path defaults to *not* trapping or
   shutting out one of our own cats. When the Pi loses its connection to the
   brain, it reverts to a safe default (door unlocked) rather than guessing.
3. **Actuators are optional and pluggable.** The core system (identify → track →
   record → notify) is fully useful with *no* lock, *no* speaker, and *no* light
   installed. Physical responses sit behind an intent-based interface and can be
   added, removed, or swapped without touching the brain.
4. **Trusted LAN, stay local.** Everything runs on a trusted home network
   (firewall + protected Wi-Fi), so the prototype uses **no auth between
   components** and no user management; camera imagery and history never leave the
   LAN. The only outbound dependency is the push-notification service. (Auth is a
   revisit-if-it-ever-leaves-home decision, not an oversight.)
5. **Uncertainty is first-class.** Every identification carries a confidence;
   thresholds turn low confidence into "unknown," and unknown fails safe.

## System topology

Two tiers on one home LAN, split by where the compute has to live. **The Pi is a
pure server — it only ever listens; the compute PC (and the config browser) are
the clients that connect to it.** The Pi never dials out.

```
        ┌───────────────────── Door (edge tier) ──────────────────────┐
        │  Raspberry Pi + camera  —  thin smart-camera node (SERVER)   │
        │                                                              │
        │   Camera → Capture → Clip (ROI) → Motion gate ──┐            │
        │                                                 ▼            │
        │   ┌──────────────────── Pi web server ────────────────────┐  │
        │   │ Video stream  (GET /stream, MJPEG, continuous)        │  │
        │   │ Status/motion (GET /status; X-Motion on /stream)      │  │
        │   │ Control API   (POST lock/unlock, sound, light)        │  │
        │   │ Config UI     (clip, focus, fps, background)          │  │
        │   └───────────────────────────────────────────────────────┘  │
        │        Actuator drivers (optional):                          │
        │        • door lock • speaker • light                         │
        └───────▲───────────────────▲──────────────────────▲──────────┘
                │ GET /stream        │ POST control         │ browser
                │ (PC connects,      │ (PC connects)        │ (config)
                │  frames flow Pi→PC)│                      │
        ┌───────┴───────────────────┴──────── Network (compute tier) ───┐
        │  PC with NVIDIA GPU  —  the brain (CLIENT)                     │
        │                                                                │
        │  Stream client → Detect cat → Track → Embed → Identify → Dir.  │
        │                                            │                   │
        │                                     Decision engine ───────────┘
        │        │                                   │      (intents →
        │   Model store                        Event store / state       PC calls
        │                                        │        │              control API)
        │                                    Notifier   API + Dashboard    │
        │                                                Enrollment/Train  │
        └────────────────────────────────────────────────────────────────┘
```

- **Edge tier — Raspberry Pi + camera at the door.** Always-on **server**.
  Captures video, clips it to a configured region, runs *simple* motion
  detection, and serves the clipped frames as a stream to whoever connects. It
  also hosts a control API and a config UI, and drives whatever actuators are
  installed. It does **not** detect or recognize cats, and it never initiates a
  connection.
- **Compute tier — PC with NVIDIA GPU on the LAN.** The brain, and the **client**
  that connects to the Pi. Opens the video stream, does everything intelligent
  (detecting that a cat is present, identifying *which* cat, resolving direction,
  deciding what to do), calls the Pi's control API to actuate, stores history,
  sends notifications, and serves the dashboard/API and enrollment/training.

## Components

### Edge tier (Raspberry Pi)

| Component | Responsibility |
|---|---|
| **Capture service** | Pull frames from the camera at the configured **fps** and **focus**, through a pluggable **capture-source interface** (CSI / USB / IP) so different cameras drop in without touching the rest of the edge. See *Camera source*. |
| **Clipper** | Crop each frame to the configured **clipping rectangle** (region of interest) before anything else — the door area only. |
| **Motion gate** | *Simple* motion detection (e.g. frame differencing / background subtraction) against a **dynamic reference (background)** that adapts to changing light. Motion does **not** gate frame delivery: `/stream` and `/frame` serve every frame continuously; the motion result is published as a *separate pulled signal* (`GET /status`, plus `X-Motion` and `X-Area` headers on every `/stream` part, plus `X-Bbox` when motion is detected) that the compute pulls to decide when to spend GPU. Motion decisions are logged, so missed or spurious triggers are auditable while tuning the background (see *Observability*). |
| **Pi web server** | The Pi's single HTTP server. Hosts the **Video stream** (and single-frame **snapshots**), the **motion/health status signal** (`GET /status`), the **Control API**, and the **Config UI** (below). Purely inbound — the PC and browser connect to it. |
| **Actuator drivers** *(optional)* | Hardware drivers behind the control API: door-lock (relay/servo over GPIO), speaker, and light. Present only if the hardware is installed. |
| **Local clip buffer** *(optional)* | Retains recent clips when the compute tier is unreachable, for later review. |

The Pi holds **no ML models** and makes **no recognition decisions**.

### Compute tier (NVIDIA PC)

| Component | Responsibility |
|---|---|
| **Stream client / ingest** | Connect to the Pi's `GET /stream` and read the clipped frames off the open response as they arrive (delivered continuously; motion is a separate signal, read from `X-Motion` part headers or `GET /status`). Always-on frame collection stores every frame (motion + non-motion) with motion flag + area to a bounded local store for motion-gate tuning via a browse UI. |
| **Detection + inference** | Detect that a cat is present, track it, crop, embed, and match against the resident gallery → identity + confidence. GPU-accelerated. |
| **Tracker / direction resolver** | Associate detections across frames into tracks; resolve a track's path across the door zone into *enter* vs *leave*. |
| **Decision engine** | The policy brain. Maps (identity, confidence, direction, context) → **intents** (allow / deny / deter / notify). Knows nothing about specific hardware. |
| **Event store & state** | Durable record of cats, sightings, identifications, transitions, actions; derives current occupancy ("who's home"). |
| **Notifier** | Turns notify intents into push notifications to the owner. |
| **Dashboard + API (main UI)** | The user-facing web app on the compute PC: occupancy (who's home/out + when), the enter/leave timeline, and the foreign-cat log; plus the management surfaces — review/correct identifications, work the annotation queue, switch operating mode, start training, and configure policy. Distinct from the Pi's camera-setup page. |
| **Dataset / annotation / training** | Store collected crops and their labels, serve the annotation queue, and run training jobs that (re)build the gallery/model and promote a new version. Enrollment of a new cat is a focused case of this. |
| **Model store** | Versioned detection, embedding, and gallery artifacts served to the inference service. |

## The Pi as a thin smart-camera node

The Pi runs **one small HTTP server** that exposes three surfaces — the
machine-facing **video stream** (with its paired `GET /status` motion/health
signal), the **control API**, and the human-facing **config UI** — fed by a
pluggable camera source and backed by optional actuators. Everything HTTP is
**inbound**: the compute PC and the config browser connect *to* the Pi; the Pi
never connects out.

### Camera source (pluggable)

The camera sits behind a single **capture-source interface**, the same way
actuators sit behind the control API. Everything downstream — clip, motion gate,
stream, snapshots — consumes frames from that interface and knows nothing about
the specific camera, so swapping cameras is a backend choice, not an
architectural change. Planned backends:

- **CSI — Pi Camera Module** via `Picamera2`/`libcamera` (autofocus on Module 3).
- **USB / UVC webcam** via OpenCV/V4L2.
- **IP camera (RTSP/HTTP)** via OpenCV or GStreamer — for a camera that already
  lives at the door.

A source advertises its **capabilities** — resolution and fps ranges, whether
focus is controllable, IR/low-light — and the Config UI adapts to them (e.g. the
focus control appears only when the active source supports it). This keeps "which
camera" a config/backend decision rather than a fork in the design, and lets the
edge run on whatever hardware is at the door.

**Selection lives in the Config UI.** The frontend is where you pick the active
source — from the detected CSI/USB devices, or by entering an IP/RTSP camera's
URL and credentials — and switching sources re-initializes only the capture
stage, not the whole Pi. The UI can only offer backends that have been
implemented; supporting a genuinely new *type* of camera means adding a backend
behind the interface (a small Pi-side change), after which it too becomes
UI-selectable.

### Video stream (machine-facing)

A long-lived `GET /stream` endpoint. When the PC connects, the Pi responds with
`Content-Type: multipart/x-mixed-replace; boundary=…` and keeps the response
open, writing the clipped frames into it one after another (**MJPEG over HTTP**)
— **continuously**, every frame, *not* gated by motion. (Early-prototype
decision: at ~5 fps on the small ROI over the LAN, bandwidth is trivial, so
decoupling lets motion gate the compute's *GPU cost* — when to run detection —
rather than frame delivery, keeping frames available for preview, enrollment, and
audit regardless of motion.) Each part carries the frame's motion result in its
headers (`X-Motion` and `X-Area`, plus `X-Bbox` when motion is active), and the same signal is
pullable via `GET /status`, so the compute reads motion inline while every frame
keeps flowing. One request, one endless response, many frames — this is how
streaming works over plain HTTP, and it puts the PC in the connection-initiating
role. See *Communication and data flow* for the mechanism and the more-efficient
alternatives.

**Single stills.** The multipart wrapper is *only* for the continuous stream.
For one image, the Pi serves an ordinary `GET /frame` → `Content-Type:
image/jpeg`, one request, one image, connection closes — no multipart. Both
endpoints draw on the same captured/clipped frames; they differ only in
delivery. This works because MJPEG is **intra-frame**: every frame is a
self-contained image with no dependency on its neighbours, so "a still" is just
"one frame of the stream." Stills serve the config preview, enrollment captures,
high-quality grabs for identification, and debugging — and a client that only
wants the occasional frame can simply poll `/frame` (with HTTP keep-alive) rather
than hold a stream open.

**Lossy vs. lossless.** MJPEG frames are JPEG, which is *lossy*, and that is the
right default here: high-quality JPEG (q≈90–95) is indistinguishable to the
vision models — which are robust to JPEG artifacts — while keeping the stream
small. When a *lossless* copy is genuinely wanted (a pristine enrollment
reference, a debugging capture), `/frame` can return **PNG or lossless WebP**
instead; both are intra-frame and keep the grab-any-single-frame property. Making
the whole *stream* lossless means leaving MJPEG for a lossless video codec (FFV1,
or lossless H.264) — far heavier, and inter-frame codecs also give up MJPEG's
independent-still property. Recommendation: **lossy JPEG for the stream, lossless
stills only where they're needed.**

### Config UI (human-facing HTML)

A simple web page for setting up the camera at install time and tuning it later:

- **Camera source** — pick the active camera from the detected CSI/USB devices,
  or add an IP/RTSP camera by URL (+ credentials). Switching re-initializes the
  capture stage. The remaining controls adapt to the selected source.
- **Clipping rectangle** — the region of interest to crop to (the door area).
- **Focus** — lens focus; shown only when the active camera source reports focus
  control (a `GET /api/capabilities` probe; e.g. Pi Camera Module 3). Persisted
  `focus` is `null` for continuous autofocus or a number for a manual lens
  position (dioptres, 0 = far) *locked* there — a fixed door scene is sharpest at
  a stable lens rather than continuously hunting. An "autofocus once"
  (`POST /api/focus/autofocus`) runs a single AF cycle and stores the found
  position as the manual lock.
- **FPS** — capture frame rate. The target is low — around **5 fps**, not the
  25–30 fps of video. A cat approaching and pausing at the flap is well captured
  at 5 fps, and it keeps bandwidth, GPU load, and per-frame cost tiny. The value
  is configurable in case tracking/direction needs a few more frames.
- **Dynamic reference / background handling** — motion-detection tuning:
  sensitivity, how fast the background model adapts to light changes, and a way
  to reset/relearn the reference.

The UI shows a live preview so the rectangle, focus, and motion sensitivity can
be dialed in visually. Settings persist on the Pi.

### Control API (machine-facing)

A small HTTP API the compute tier calls to actuate hardware:

- `lock` / `unlock` the door
- `sound` on (play deterrent)
- `light` on/off

The API is the *only* way to move the hardware, and the Pi enforces safe
behavior locally (e.g. reverting the lock to unlocked if it loses contact with
the brain — see Offline behavior). If an actuator isn't installed, its endpoints
are simply absent/no-ops.

## The vision pipeline

The path from raw frames to a decision, and where each stage runs. Note how
little is on the Pi:

```
[Pi]  1. Capture (fps, focus)
[Pi]  2. Clip to ROI rectangle
[Pi]  3. Motion detection vs. dynamic background ─ publish on /status + X-Motion headers
[GPU] 4. Cat detection ─ is there a cat? bounding box
[GPU] 5. Tracking ─ associate boxes across frames into a track
[GPU] 6. Crop + quality filter ─ pick the best views of the cat
[GPU] 7. Embedding ─ per-crop feature vector (re-ID model)
[GPU] 8. Identification ─ match vector to resident gallery → name + confidence,
                          or "unknown" below threshold
[GPU] 9. Direction ─ track path across the door zone → enter / leave
[GPU] 10. Decision ─ policy engine emits intents
[*]  11. Actuate (optional, via Pi control API) + record + notify
```

The Pi's job ends at step 3: clip and detect motion — but that motion result is
now a *pulled signal* (`/status` + per-part stream headers) the compute uses to
decide when to run the GPU, not a gate on frame delivery (frames stream
continuously). **Detecting that the moving thing is a cat happens on the GPU**,
not the Pi.

### Identification approach (decision)

**Recommendation: open-set recognition via an embedding model + a per-cat
gallery, not a fixed N-way classifier.**

- An **embedding (re-ID) model** maps a cat crop to a feature vector. A resident
  is represented by a small **gallery** of enrolled vectors. Identification is
  nearest-neighbor against the gallery, with a distance threshold.
- Why this over a fixed classifier: it handles **enrollment of a new cat without
  full retraining** (just add its vectors to the gallery), and it handles the
  **open-set "stranger"** case naturally — a crop far from every resident is
  *unknown*, which is exactly the safe default. A fixed N-class softmax
  classifier forces every input into a known class and must be retrained
  whenever the household changes.
- The distance/similarity margin provides the confidence signal the concept
  relies on — though raw distance isn't calibrated out of the box, so the
  threshold(s) get tuned against real collected data (a Phase-1 goal).

This is the single highest-risk part of the system (distinguishing 4+ possibly
similar cats), so it is designed to degrade to "unknown" rather than guess.

### Direction and occupancy

- The **door zone** is a configured region (an inside side and an outside side,
  or a crossing line) within the clipped frame. *(Distinct from the Pi's clip
  rectangle: the Pi crops to the door area; the compute tier defines the
  in/out geometry within it.)*
- A **track** (stage 5) gives the cat's path over time. Its crossing of the zone
  yields *enter* or *leave* with a direction confidence.
- Tracking also prevents double-counting: one visit = one track = one
  transition, no matter how many frames the cat appears in.
- **Low frame rate matters here.** At ~5 fps a crossing may be only a handful of
  frames, so the tracker and direction logic must be robust to sparse, larger
  per-frame jumps rather than assuming smooth 30-fps motion. This is the main
  place the low fps constrains the design; if crossings prove too sparse, the
  configurable fps is the lever to raise.
- **Occupancy** is derived state: fold the ordered stream of per-cat transitions
  into a current in/out flag and "last crossed" timestamp per resident.

## Decision engine and the actuator split

The decision engine consumes an identification + direction + context and emits
**intents**, never hardware commands:

| Intent | Meaning |
|---|---|
| `ALLOW_ENTRY` | A recognized resident may come in. |
| `DENY_ENTRY` | A foreign/unknown cat should be kept out. |
| `DETER` | A lingering foreign cat should be scared off. |
| `NOTIFY` | The owner should be told about this event. |
| `RECORD` | Persist the event to history (always). |

The compute tier maps intents to calls on the **Pi control API**, according to
which actuators are installed:

- **Door lock:** `DENY_ENTRY` → `lock`, `ALLOW_ENTRY` → `unlock`.
- **Speaker:** `DETER` → `sound` (rate-limited, tuned to not distress residents
  or neighbors).
- **Light:** `DETER` → `light` (as a visual deterrent) and/or as **illumination**
  to help the night camera. Its exact role is a decision (see Open questions).
- **None installed:** `DENY_ENTRY`/`DETER` degrade to `RECORD` + `NOTIFY` only —
  the system still identifies, tracks, and tells you a stranger showed up; it
  just can't physically stop it.

This decoupling is what makes locking, sound, and light optional: adding or
removing that hardware changes only the mapping and the Pi's control API,
never the brain.

The mapping above applies in **run mode**. In **collection mode** (see *Operating
modes and the learning loop*) identity isn't trusted, so the engine holds
actuators at their safe default and only records/collects.

In the current prototype there is **no actuation at all** — the engine only
records and collects. The full access-decision policy (how "fail safe" resolves
an uncertain identity vs. never shutting out a resident, and what to do with
multiple cats in frame) is deferred to the phase where the lock hardware is
added; it is not settled here.

### Escalation

Consistent with the concept, the door lock is the first line and sound/light are
escalation: `DENY_ENTRY` fires immediately on a foreign cat; `DETER` fires only
when a foreign cat *lingers* or repeatedly works the flap, and is rate-limited.

## Latency and offline behavior

There is now a single decision path — the GPU — because the Pi can't recognize
anything on its own:

- **Normal operation.** Frames stream continuously, and the motion signal fires
  while a cat is still *approaching* the door, so the compute can begin GPU
  detection seconds before the cat reaches the flap. A GPU identification (tens of
  ms) plus a LAN round trip is small against a cat's approach time, so the decision
  usually arrives in time to drive the lock via the control API.
- **Compute tier or network down.** Because the PC is the client, the Pi detects
  this directly: the stream/control connection drops, or no PC has connected for
  N seconds. The Pi cannot identify or decide on its own, so it **fails safe** —
  reverts the door to *unlocked* (residents are never trapped by an outage),
  takes no deterrent action, and optionally buffers clips locally for later
  review. When the PC reconnects, buffered clips can be ingested to backfill the
  record.
- **Pi down.** The PC's stream/control calls fail immediately, so the brain knows
  the door node is offline and can notify the owner.
- **Idempotency.** Backfilled/replayed clips must not double-count transitions.
- **Budget is phase-2.** The concrete latency budget — including the *mechanical*
  lock actuation time — can only be measured once the door hardware exists, so it
  is validated then. If a crossing turns out too fast for ~5 fps, the fps can
  burst on motion.

## Communication and data flow

Every connection is **initiated by the compute PC (and the config browser); the
Pi only ever listens.** The Pi is a pure HTTP server and never dials out. This
keeps it a self-contained, replaceable device and puts the brain in charge of
when to connect. Four inbound surfaces:

- **Video stream (data plane).** The PC's stream client opens a single
  long-lived `GET /stream` to the Pi and holds it open. The Pi answers with
  `Content-Type: multipart/x-mixed-replace; boundary=…` and — using HTTP chunked
  transfer — writes one JPEG after another into that *same* response, each framed
  by the boundary and its own part headers. The connection never closes; the PC
  reads frames off it as they arrive. This is **MJPEG over HTTP**, the classic
  IP-camera mechanism, and it is exactly how "streaming over HTTP" works: one
  request, one endless response, many frames. OpenCV can consume such a URL
  directly.
  - **Continuous, not motion-gated:** the Pi writes *every* frame into the open
    stream — motion does not gate delivery (an early-prototype call: at ~5 fps on
    the small ROI, LAN bandwidth is trivial, so decoupling motion from frame
    delivery — letting motion gate the compute's *GPU cost*, not frame
    availability — is worth more than the saved bytes). Each part carries the
    frame's motion result in its headers (`X-Motion` and `X-Area`, plus `X-Bbox` when
    motion is active) so a stream consumer reads motion inline.
  - **Single stills:** the Pi also serves `GET /frame` — one plain image per
    request (JPEG by default, PNG/WebP when a lossless copy is needed) — for
    previews, enrollment grabs, and debugging, or for a client that prefers to
    poll rather than hold the stream open. See *The Pi as a thin smart-camera
    node* for the lossy-vs-lossless rationale.
  - **Frame identity:** each streamed frame carries a monotonic timestamp / frame
    id (e.g. in its part headers) so the PC can order frames, account for drops,
    and keep event handling idempotent — rather than trusting arrival order alone.
- **Status / motion (data plane).** The PC polls the Pi's `GET /status` for a
  JSON snapshot of the latest frame's motion and camera health (`{frame_id, ts,
  motion, bbox, area, camera_ok, last_error, system}`) — the signal it uses to decide when
  to spend GPU on detection, and (via `camera_ok`) how it learns the camera has
  died. The `system` field is `{cpu_percent, mem_percent, mem_used_mb, mem_total_mb}` or `null` (the edge
  host's CPU% and memory, measured via `psutil`). Inbound, one request per poll; it correlates to stream frames by
  `frame_id`.
- **Control (control plane).** The decision engine's actuation hits the Pi's
  **control API** (`POST lock`/`unlock`/`sound`/`light`) — again the PC
  connecting inbound to the Pi.
- **Config.** The installer/owner's browser connects to the Pi's **Config UI**.

Because every connection is inbound to the Pi, liveness is easy to reason about:
if the Pi is down the PC's calls fail at once. For the reverse, the Pi doesn't
trust TCP state alone (a silently dropped connection can look open) — it treats
the PC as present only while the stream is actively read and an application-level
keepalive keeps ticking, and fails safe once that keepalive times out.

**Why MJPEG-over-HTTP — and the upgrade path.** At ~5 fps, over a LAN, carrying
only the small clipped ROI, MJPEG is not merely tolerable but a genuinely
good fit. Its one real weakness — no inter-frame compression — barely bites here:
bandwidth is trivial at this rate (a ~640×480 q90 JPEG × 5 fps ≈ 2–4 Mbit/s,
streamed continuously rather than motion-gated — a cost this prototype gladly pays
to keep motion decoupled from frame delivery), and a video codec's temporal
advantage actually *shrinks* at low fps, because widely-spaced frames share little
— a poor case for inter-frame prediction. Meanwhile
MJPEG's strengths are exactly what a CV pipeline wants: every frame is an
independent still (easy to grab, no decoder/keyframe state, a dropped frame is
harmless), it is **low-latency** (no B-frame reordering or encoder lookahead —
which matters for a timely lock decision), and it is trivial to serve and
consume. If bandwidth ever *does* matter — a much larger/high-res ROI or a higher
fps — the same "PC connects to the Pi" model still holds with a more efficient
transport (**H.264 via RTSP or WebRTC**, or **gRPC/WebSocket server-streaming**),
at the cost of more setup; and clips can always be archived as H.264 while the
live stream stays MJPEG. **Decision:** start with MJPEG-over-HTTP.

Inside the compute tier, components communicate in-process or via a lightweight
internal mechanism; an MQTT pub/sub bus remains **optional** and is not required
for the two-node core.

## Data model

Core entities (storage-agnostic):

- **Cat** — `id`, `name`, `active`, enrollment metadata. The known residents.
- **Sighting** — a detection at a time: `timestamp`, `camera`, `bbox`,
  `track_id`, reference to the stored crop/clip. (Detection is done on the GPU.)
- **Identification** — resolves a sighting/track to a cat: `cat_id` **or**
  `unknown`, `confidence`, `model_version`.
- **Transition** — a directional crossing: `cat_id` (or unknown), `direction`
  (enter/leave), `timestamp`, `direction_confidence`, source track.
- **Occupancy** — derived per-resident current state: `in`/`out`,
  `last_transition`.
- **Action** — something the system did: `type` (lock/unlock/sound/light/notify),
  `timestamp`, triggering intent/event.
- **Notification** — an alert sent to the owner and its delivery state.
- **Dataset item** — a stored crop for learning: image ref, `source`
  (collection | run-uncertain | correction), and a **label** once annotated
  (`cat_id` / stranger / not-cat), or `unlabelled` while queued.
- **Model version** — a built gallery/model: `version`, `status`
  (draft / active / retired), training metrics; the active one is what Run uses.
- **Mode** — the current operating mode (`collection` | `run`), as system state.

Sightings and transitions are an append-only time series; occupancy is a
projection over transitions and can always be rebuilt from them.

## Storage

**Decision:** start with **SQLite** on the compute tier for the event store
(single-host, simple, more than fast enough for one door), with a clear path to
Postgres if needs grow. Media (crops/clips) is stored on the compute tier's
filesystem, referenced by path/URL from the DB — not inlined as blobs. Retention
is bounded (keep recent media for debugging/retraining, age out the rest).

## Operating modes and the learning loop

Recognizing 4+ similar cats is the central risk, so the system is taught through
an explicit, human-in-the-loop loop driven from the dashboard. It has two runtime
**modes**, switchable at any time:

- **Collection.** Gather images: the generic cat detector finds cats at the door
  and stores crops into the dataset. Access decisions stay passive/safe (identity
  isn't trusted yet), which also makes this the **cold-start** mode — the very
  first setup runs here, door in its safe default, until a first model exists.
- **Run.** Normal autonomous operation (detect → identify → decide → actuate →
  record → notify). Identifications below the confidence threshold have their
  crops **auto-added to the annotation queue** — active learning on exactly the
  cases the model finds hard. Only uncertain samples are enqueued, and at most a
  few crops per track (not one per frame), so a cat loitering at the door doesn't
  flood the queue.

The loop is **Collect → Annotate → Train → Run**, with Run continuously feeding
the queue back into Annotate:

- **Annotation.** In the dashboard the owner labels queued crops — assign a
  resident identity, or mark stranger / not-a-cat. Three feeders reach the queue:
  collection-mode captures, run-mode uncertain samples, and corrections of wrong
  identifications.
- **Training.** The owner triggers a training job over the labelled data. With
  the embedding + gallery approach this is usually just **rebuilding the gallery**
  from the annotated crops — cheap and fast; a full fine-tune of the embedding
  model is the heavier, rarer path. Training produces a new **model version**.
  Keep the gallery built from *clean, representative* crops of each cat; blurry or
  extreme-angle hard cases are useful for tuning the threshold and for validation,
  but folding them into the gallery would blur the embedding space (a stranger's
  blurry crop could then match a resident).
- **Promotion.** A new version is validated, then promoted to the active model
  that Run uses. Versions are retained in the model store so a bad one can be
  rolled back.

Registering a new resident is just collection + annotation focused on that one
cat, followed by a gallery rebuild — no separate mechanism.

Crucially, this teaching loop is **separate from the real-time door loop**: the
door keeps deciding autonomously on whatever model is currently active;
annotating and training never block or sit in the live decision path.

## Notifications

The Notifier turns `NOTIFY` intents into push notifications (**decision:** which
push service — e.g. ntfy, Pushover, Telegram, or a home-automation hub). Policy
controls which events notify (stranger detected, deterrent fired, resident
enter/leave) so alerts stay signal, not noise. This is the system's only
outbound-internet dependency.

## Deployment and runtime

- **Raspberry Pi (edge).**
  - **Camera (pluggable backend).** The specific camera is a backend behind the
    capture-source interface (see *Camera source*), not an architectural fork —
    CSI Pi Camera Module (via `libcamera`/`Picamera2`), USB/UVC, or IP/RTSP. The
    open choices are which backend to build first and the hardware itself;
    IR/low-light capability matters for night operation.
  - Runs a small set of services under `systemd`: capture + clip + motion gate,
    and the web server (Video stream + Control API + Config UI). **No ML
    runtime.**
  - GPIO drives the optional lock relay/servo, speaker, and light.
- **NVIDIA PC (compute).**
  - Python vision stack (**decision:** PyTorch + an Ultralytics/YOLO detector +
    a re-ID/embedding model), GPU via CUDA.
  - Services containerized (Docker/Compose): stream client + inference, decision
    engine + event store, notifier, dashboard/API.
- **Networking.** Both hosts on the LAN; the PC needs a stable address for the
  Pi (static IP or mDNS) to open the stream and call the control API. The Pi
  needs no address for the PC — it never initiates.

## Recommended tech stack (summary)

| Concern | Recommendation | Why |
|---|---|---|
| Language | Python across both tiers | Ecosystem for CV/ML; one language to maintain. |
| Pi capture | Capture-source interface: `Picamera2` (CSI), OpenCV/V4L2 (USB), OpenCV/GStreamer (RTSP) | Pluggable per camera; frame grab, fps, focus, capability reporting. |
| Pi motion + clip | OpenCV background subtraction (MOG2/KNN) | Simple, adaptive dynamic reference; cheap on the Pi. |
| Pi web server | Lightweight framework (Flask/FastAPI) | Serves the video stream + Control API + Config UI; tiny footprint. |
| GPU detection | Ultralytics/YOLO | Mature, fast, GPU-accelerated cat detection. |
| Identification | Embedding/re-ID model + gallery (open-set) | New cats without retraining; strangers → "unknown". |
| Tracking | ByteTrack/SORT-style tracker | Standard; gives track IDs for direction + de-dup. |
| Frame transport | MJPEG over HTTP (`multipart/x-mixed-replace`) + `GET /frame` stills | PC connects to the Pi; one long-lived response streams the clipped ROI continuously (motion is a separate pulled signal — `GET /status` + per-part `X-Motion` headers); stills as JPEG, or PNG/WebP when lossless is needed. |
| Storage | SQLite → Postgres if needed | Simple single-host start; clear growth path. |
| Notifications | ntfy / Pushover / Telegram | Simple push to phone; only outbound dependency. |
| Dashboard + annotation UI | FastAPI backend + a lightweight SPA (Svelte/React); SSE or polling for live status | Main compute-side web app: occupancy, timeline, foreign-cat log, annotation queue, mode + training controls. HTMX + server-rendered Jinja is the simpler Python-only alternative. |
| Runtime | systemd (Pi), Docker Compose (PC) | Matches each host; easy restarts and updates. |

These are recommendations to anchor the first build, not locked-in mandates.

## Repository layout

A single **monorepo**. Both tiers share the data model and the event / intent /
control-API contracts and are developed together by one person, so keeping them
in one repo is what keeps the edge↔compute wire formats from drifting. The tree
mirrors the two tiers and the component tables above:

```
cat-automation/
├── docs/                # CONCEPT, ARCHITECTURE, CHANGELOG
├── edge/                # Raspberry Pi — thin smart-camera node (no ML)
│   ├── capture/         #   capture-source interface + backends (CSI/USB/IP)
│   ├── clip/            #   ROI cropping
│   ├── motion/          #   motion gate + dynamic background
│   ├── server/          #   HTTP server: /stream, /frame, control API, config UI
│   │   └── ui/          #     Config UI static assets
│   ├── actuators/       #   lock / sound / light drivers (GPIO) — optional
│   └── config/          #   persisted camera + motion settings
├── compute/             # NVIDIA PC — the brain
│   ├── ingest/          #   stream client (connects to the Pi's /stream)
│   ├── detection/       #   cat detection (YOLO)
│   ├── tracking/        #   tracker + direction resolver
│   ├── identification/  #   embedding model + gallery (open-set)
│   ├── decision/        #   decision engine → intents
│   ├── store/           #   event store + occupancy state
│   ├── notify/          #   push notifier
│   ├── api/             #   dashboard backend: query/config/annotation API (FastAPI)
│   │   └── web/         #     dashboard frontend (SPA, or server-rendered)
│   ├── learning/        #   collection, annotation queue, training + promotion
│   ├── dataset/         #   collected crops + labels (the training set)
│   └── models/          #   model store (versioned artifacts, not committed)
├── shared/              # contracts both tiers agree on: data model, event &
│                        #   intent schemas, the Pi control-API shape, constants
├── deploy/              # systemd units (Pi), docker-compose (PC), config
└── tests/               # (or tests alongside each package)
```

- `edge/`, `compute/`, and `shared/` are the natural top-level areas, each a home
  for its own focused ownership (and a subtree `CLAUDE.md`) as the code grows.
- `shared/` is deliberately small — only the contracts both tiers must agree on —
  so the edge and compute sides can evolve independently without drifting on the
  wire format.
- **Not committed to git:** model artifacts (`compute/models/`, kept via a model
  store or LFS) and captured media/clips (they live on the compute filesystem per
  *Storage*, referenced by path).

## Cross-cutting concerns

- **Reliability.** Idempotent ingest (a replayed clip must not double-count);
  the Pi reverts to safe defaults when it loses the brain. Liveness is one-way by
  construction: the PC learns the Pi's state from its own connections; the Pi
  infers the PC's from an application-level keepalive on the stream (with a
  timeout), since it never dials out.
- **Latency.** Budget the motion→ingest→decision→control path so the door acts
  before or as the cat reaches the flap; early motion triggering on approach is
  the main lever.
- **Security & privacy.** Prototype trust model: a single trusted home LAN behind
  the firewall and protected Wi-Fi, not exposed to the internet — so **no auth and
  no user management** between components in this phase. Imagery and history stay
  on the LAN; no cloud storage of video. Revisit auth only if the system is ever
  exposed beyond the home network.
- **Observability.** Structured logs and saved clips for every uncertain or
  wrong decision — these double as debugging aid and as retraining data.
- **Config.** The Pi owns camera/motion config (clip, focus, fps, background);
  the compute tier owns door-zone geometry, thresholds, which actuators exist,
  and notification policy. No hardcoded assumptions about installed hardware.

## Key decisions to confirm

1. Stream encoding: **MJPEG-over-HTTP (recommended start) vs. H.264 (RTSP/WebRTC)
   vs. gRPC/WebSocket** — the direction is settled (Pi is the server, PC connects
   and pulls); only the encoding is open.
2. Camera: which capture-source backend to build first (**CSI / USB / IP-RTSP**)
   and the hardware — the interface makes cameras swappable, but night/IR + focus
   support still guide the hardware pick.
3. Identification model family and the specific re-ID/embedding architecture.
4. Storage: confirm **SQLite** start (vs. Postgres from day one).
5. Push-notification provider.
6. The **light**'s role: visual deterrent, camera illumination, or both.
7. Dashboard frontend: **lightweight SPA (Svelte/React) vs. HTMX + server-rendered
   Jinja** — the annotation UI (keyboard-driven image labelling) leans SPA;
   simplicity leans HTMX.

## Open questions (deferred to design/build)

- Controllable door hardware: retrofit an existing flap vs. a purpose-built
  lock; actuation speed and reliability. *(Only relevant if the lock is added.)*
- Robust directionality: camera geometry, or an auxiliary non-vision signal
  (motion sensor / flap or beam sensor). Identification stays vision-based;
  direction need not be.
- Deterrent tuning: loudness/brightness, cadence, habituation, resident/neighbor
  impact. *(Only relevant if sound/light is added.)*
- Access-decision policy (deferred to the actuation phase): how "fail safe"
  resolves an uncertain identity vs. never shutting out a resident, and multiple
  cats in frame (e.g. a stranger tailgating a resident).
- Active-learning thresholds: what confidence routes a sample to the annotation
  queue, and what validation gates a model version before it is promoted to Run.
- Retraining cadence and how much labelled data a reliable model needs.
- Multiple cameras/doors (out of scope for now; keep the model from precluding
  it).
