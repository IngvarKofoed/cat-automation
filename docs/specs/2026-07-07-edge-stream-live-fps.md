# Edge MJPEG stream, live preview, and configurable fps

The next edge increment: a long-lived `GET /stream` that serves the door frames
as **MJPEG over HTTP** (`multipart/x-mixed-replace`), a **Live** toggle in the
config UI that renders that stream, and a persisted **fps** capture setting
(default 5). Frames are produced by a single **background grabber thread** that
reads the capture source continuously at the configured fps into a latest-frame
slot; both `/stream` and `/frame` serve from that slot. The stream is
**continuous** this increment — motion gating (which gates the same loop) is the
increment after. No actuators, no auth (trusted-LAN prototype).

## Key decisions

- **Shared grabber thread + latest-frame slot** (new). One background thread
  reads the current source at the configured fps and publishes each decoded frame
  into a slot (`frame`, monotonic `frame_id`, `ts`, `last_error`) guarded by a
  `threading.Condition`. `/stream` and `/frame` both serve from the slot. Reason:
  decouples camera read-rate from client count (two viewers don't double the read
  rate or split frames), drains OpenCV's internal buffer so readers never get
  stale frames, and puts fps pacing in one place. Chosen over a per-connection
  read loop, which contends on the single source and would be thrown away next
  increment — the motion gate needs exactly this always-on loop.
- **Grabber stores the RAW decoded frame** (reuses). The slot holds the
  untransformed BGR frame; each consumer applies `rotate`/`crop` from
  `edge/clip/transform.py` at the serving boundary — `/stream` and `/frame` do
  rotate+crop, `/frame?raw=1` does rotate only. Keeps the grabber dumb and lets
  rotation/ROI changes appear live without reconnecting or restarting the thread.
- **Source swap needs no thread restart** (extends). The grabber snapshots
  `state["source"]` under the config lock each iteration, so a `POST /api/config`
  device swap is picked up on the next grab. The grabber does **not** hold the
  config lock during `read()` — only long enough to snapshot the source ref and
  fps — so grabs never block config reads or device swaps. (Tearing down the *old*
  source during a swap, when a lock-free grab may still be mid-`read()` on it,
  needs care — see *Old-source teardown on swap*.)
- **`close()` poisons the source** (breaking, small). A closed
  `OpenCVCaptureSource` (and `FakeCaptureSource`) raises `CaptureError` from a
  later `read()` instead of reopening lazily. This closes the swap race: with the
  grabber reading lock-free, a grab that lands on the just-closed old source
  during a device swap would otherwise **reopen the old camera** and leak the
  handle. Strengthens the MVP's `close()` contract ("safe to call twice") with
  "read-after-close raises." Chosen over making the grabber own source teardown,
  or reference-counting in-flight grabs — both heavier for a single-grabber node.
  See *Old-source teardown on swap*.
- **`GET /stream` → `multipart/x-mixed-replace; boundary=frame`** (new). A Flask
  streaming `Response`; each part is `Content-Type: image/jpeg` +
  `Content-Length` + `X-Frame-Id` + `X-Timestamp` headers, then JPEG bytes
  (quality 90, same as `/frame`). Adopts the architecture's defined stream
  endpoint and its **frame-identity** requirement (monotonic id/timestamp per
  frame) — deferred by the stills MVP to "the stream increment," which is this.
- **Stream pacing comes from the grabber, not the generator** (new). Each
  `/stream` generator blocks on the condition until `frame_id` advances, then
  emits — so cadence is the grabber's fps and idle clients don't busy-loop. A
  connection that already has a frame emits it immediately on connect.
- **fps is persisted capture config** (extends). `settings.py` `DEFAULTS` gains
  `"fps": 5`; it rides the existing device/rotation/clip config through
  `GET|POST /api/config` and `save_settings`. It paces the grabber's read loop —
  **not** `cv2`'s `CAP_PROP_FPS`, which is unreliable across cameras. A settings
  file without `fps` loads as 5 (defaults merge), so it's backward compatible.
- **fps validation: numeric, 1–30** (new). `POST /api/config` rejects a
  non-numeric, boolean, or out-of-range fps with 400, mirroring the existing
  rotation/clip validation. The UI offers presets (5 / 10 / 15).
- **`/frame` serves the slot, not a fresh read** (diverges). `/frame` returns the
  latest grabbed frame instead of doing its own locked `read()` — at 5 fps it's
  ≤200 ms old, fine for previews/enrollment, and it removes `/frame`↔grabber lock
  contention. It returns 503 when the most recent grab failed (`last_error` set)
  or no frame exists yet, preserving the MVP's 503-on-camera-failure contract.
- **Live toggle = native `<img src="/stream">`** (new). The browser renders
  `multipart/x-mixed-replace` in an `<img>` with no JS timer. Turning Live off
  clears `src` to close the connection. Changing fps or rotation while live needs
  no reconnect — the grabber/serving boundary pick it up.
- **Grabber is start-controllable for tests** (extends). `create_app` starts the
  grabber by default and exposes it as `app.grabber`; tests pass
  `start_grabber=False` and call `app.grabber.grab_once()` to populate the slot
  deterministically — the same hardware-behind-a-seam approach that motivates the
  capture interface. No free-spinning thread in tests.

## Goals

- Serve the clipped door frames as a continuous MJPEG stream a browser (and,
  later, the compute PC) can consume by connecting to one long-lived endpoint.
- A Live toggle in the config UI to watch the camera in motion while tuning.
- A persisted, adjustable capture fps (default 5) that governs the stream rate.
- Land the always-on grab loop the motion gate will build on next, and the
  per-frame id/timestamp the compute tier needs for ordering/idempotency.

## Non-goals

- **Motion gating** — emitting only on motion, the dynamic background model, and
  idle keepalives. The stream is continuous while healthy this increment; the
  grabber loop is where motion slots in next.
- Lossless (PNG/WebP) stills; focus and resolution controls; per-source
  capability advertising.
- Encoded-frame caching / hardware JPEG, and any many-client throughput tuning
  beyond correctness.
- Actuators, the control API, and authentication.

## Design

### Grabber thread and the frame slot

A `Grabber` owns a loop that, each iteration:

1. Under the config `lock`, snapshots `source = state["source"]` and `fps`.
2. Calls `source.read()` **without** holding the config lock (the OpenCV source
   is internally locked already).
3. On success: under the frame `Condition`, stores the raw frame, `frame_id += 1`,
   a fresh `ts`, clears `last_error`, and `notify_all()`.
   On **any** exception (`CaptureError` or otherwise): stores `last_error`, leaves
   `frame_id` unchanged (so streams don't re-emit a stale frame), and
   `notify_all()`. The whole loop body is wrapped so an unexpected error can never
   kill the sole producer thread; the OpenCV source self-heals by invalidating its
   handle, so the next iteration retries a fresh open.
4. Sleeps `max(0, 1/fps - elapsed)` so the loop targets the configured fps.

A `read()` that *hangs* rather than raising (a wedged camera) would stall the sole
producer — a known gap left to the motion increment (which adds a watchdog);
called out here so it isn't assumed handled. `/frame`'s staleness check (below) is
the interim signal that the producer has stopped advancing.

The frame slot is `{frame, frame_id, ts, last_error}` guarded by a
`threading.Condition`, kept **separate** from the config `lock` so waiting for a
frame never holds the config lock. The thread is a daemon started in
`create_app`; it dies with the process. It runs continuously whenever the server
is up — even with zero clients connected (the accepted trade of Approach B over
the idle-when-unwatched Approach A), which is the always-on cadence the motion
gate needs next.

Reading `fps` each iteration means a `POST /api/config` fps change takes effect on
the next loop with no restart. Same for the source ref, so a device swap needs no
restart either.

### Old-source teardown on swap

`POST /api/config` keeps its new-before-close swap: build+open the candidate,
swap it into `state["source"]` under the lock, then `old.close()`. Because the
grabber reads lock-free, a grab can still be mid-`read()` on the old source when
`close()` lands — or start one just after. The **poisoned `close()`** decision
handles both: a closed source raises `CaptureError` from `read()` rather than
silently reopening, so the worst case is one spurious `last_error` on that grab,
which self-heals on the next iteration against the new source. No reopened stale
camera, no leaked handle, and no extra coordination between the grabber and the
config handler.

### `GET /stream`

Returns `Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame")`.
`gen()` tracks the last id it sent; it waits on the condition for `frame_id` to
advance (emitting the current frame immediately on connect if one exists), then
rotate+crops the raw frame, encodes JPEG q90, and yields:

```
--frame
Content-Type: image/jpeg
Content-Length: <n>
X-Frame-Id: <id>
X-Timestamp: <ms>

<jpeg bytes>
```

If the camera is dead, `frame_id` never advances and the generator simply blocks —
the connection stays open but emits nothing (acceptable; errors surface via
`/frame`'s 503 and logs). A disconnected client raises on write and ends the
generator, closing the loop.

**Frame identity.** `X-Timestamp` is Unix epoch milliseconds (wall clock);
`frame_id` is a per-process monotonic counter starting at 1 — **not** stable
across reboots or device swaps. The compute tier must therefore key
ordering/idempotency on `(frame_id, X-Timestamp)` together, not `frame_id` alone.
These two header names and units are an edge↔compute wire contract: they live
edge-side now and lift into `shared/` when the compute stream client is built.

### `GET /frame` and `/frame?raw=1`

Serve the slot's latest frame instead of reading the camera directly:

- `last_error` set (most recent grab failed) → **503** `{error}`.
- latest frame older than a staleness threshold (a small multiple of the grab
  interval) → **503**. This catches a producer that stopped advancing without a
  clean error, so `/frame` never returns a silently-frozen image on a wedged
  camera.
- no frame yet (just booted) → block on the condition until the first frame
  arrives or the first grab reports an error — out-waiting camera warmup (~1–2 s
  on AVFoundation) the way the MVP's blocking read did, rather than a fixed short
  timeout that could 503 a healthy-but-still-warming camera.
- otherwise → rotate (+ crop unless `raw`), encode q90, return `image/jpeg`.

### fps in config

`settings.py` `DEFAULTS` becomes `{"device": 0, "rotation": 0, "clip": None,
"fps": 5}`. `GET /api/config` returns `fps`; `POST /api/config` validates it with
a `_valid_fps` helper (numeric, not bool, `1 <= fps <= 30`), folds it into the
`next_config` dict persisted before the swap, and updates `state["fps"]` under the
lock. The "at least one of device/rotation/clip" guard extends to include `fps`.

### Config UI

`edge/server/ui/index.html` gains, near the Capture button:

- A **Live** toggle. On → `stillImg.src = '/stream'`; off → `stillImg.src = ''`
  (closes the multipart connection) and show a final still via a `/frame` capture.
- An **fps** control (`<select>` 5 / 10 / 15) that `POST`s `{fps}` and reflects
  `/api/config` on load. If the persisted fps isn't one of the presets (set via
  API or a hand-edited `settings.json`), insert it as an extra option so the
  control always shows the live value — mirroring the camera dropdown's existing
  "Active: …" fix in `index.html`. Changing it while live needs no reconnect.
- **Live and ROI mode are mutually exclusive.** Entering ROI mode turns Live off
  and loads the raw still (`/frame?raw=1`), because the ROI box math needs a
  stable image. Leaving ROI mode lands on a still capture (matching today's
  `exitRoiMode`); it does **not** auto-resume Live — the user re-toggles it.

### Concurrency

Flask dev server already runs `threaded=True`. Each `/stream` holds one worker
thread blocked on the condition (waking at fps to write a part); the grabber is
one more thread. Fine for the prototype's handful of clients (config browser +,
later, the compute PC); it is not a many-client server, and the werkzeug dev
server's thread-per-connection model is the ceiling.

### Testing

Against `FakeCaptureSource` with `start_grabber=False`:

- `grab_once()` populates the slot; `/frame` then returns 200 JPEG, and 503 when
  a grab error is injected or before any grab.
- `POST /api/config` fps validation: accepts 5/10/30, rejects 0, 31, `"x"`,
  `true`; persists and round-trips via `GET /api/config`.
- `GET /stream` returns the multipart content-type; reading the first part yields
  a valid JPEG carrying `X-Frame-Id`/`X-Timestamp`, then the test closes it.
- The `GET /api/config` response gains an `fps` field (backward-additive). The
  existing test that asserts the exact config dict must add `fps: 5`, and any
  exact-match consumer of that shape must update.
- A `read()` after `close()` raises `CaptureError` (poisoned-close contract), for
  both `FakeCaptureSource` and `OpenCVCaptureSource`.

## Alternatives considered

- **Per-connection read loop (Approach A).** Each `/stream` runs its own
  `read()`+pace loop on the shared source. Smaller, and the camera idles when
  unwatched, but two clients contend on the one source, slow readers get stale
  buffered frames, and the motion gate would replace it one increment later.
  Rejected for that rework and those correctness gaps.
- **Client-side `/frame` polling for "Live".** A JS timer re-fetching `/frame`.
  Needs no `/stream`, but it's throwaway UI, gives no real stream for the compute
  tier, and reinvents cadence in the browser. Rejected — `/stream` is the
  architecture's data plane and the live view falls out of it for free.
- **fps via `cv2` `CAP_PROP_FPS`.** Let the camera pace itself. Unreliable across
  backends/cameras, so pacing the grab loop is predictable and backend-agnostic.
