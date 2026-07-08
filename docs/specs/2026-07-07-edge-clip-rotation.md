# Edge clip + rotation

A per-frame transform stage on the Pi that **rotates then crops** every captured
frame to the door region before it is served. Rotation (`0/90/180/270`) and a
normalized clip rectangle live in `settings.json` alongside `device`; `GET /frame`
returns the oriented, cropped region, and `GET /frame?raw=1` returns the oriented
but *uncropped* frame so the config UI can drag out the ROI on a correctly-oriented
image. Backend-agnostic (works for the Picamera2/CSI and OpenCV backends alike),
and the same transform will feed the future `/stream`.

## Key decisions

- **Pure transform stage in `edge/clip/`** (new). New `edge/clip/transform.py`
  with `rotate(frame, degrees)` and `crop(frame, clip)` — plain functions over a
  BGR ndarray — matching the architecture's `edge/clip/` component. Applied in the
  frame path, *not* as a `CaptureSource` decorator, so backends stay dumb frame
  producers and "raw vs cropped" is just "call `crop` or don't."
- **Applied post-capture, outside the slot lock** (extends). In `app.py`'s
  `/frame`, the rotate+crop+encode runs after `source.read()` returns, outside the
  lock — same placement as the existing `cv2.imencode` (per the earlier review fix
  that moved encoding out of the critical section).
- **Backend-agnostic rotation** (new). `cv2.rotate` (90° multiples, clockwise) on
  the ndarray — one code path for CSI and USB — rather than Picamera2/OpenCV
  native rotation, which would be two paths and need a camera reconfigure per
  change.
- **Rotate → crop, ROI in the oriented frame** (new). Rotation is applied first;
  the clip rect is defined against the rotated frame. Clip is stored **normalized**
  `{x, y, w, h}` in 0–1 so it's resolution-independent.
- **Config schema grows** (extends). `settings.json` gains `rotation` and `clip`
  next to `device`; `DEFAULTS` becomes `{"device": 0, "rotation": 0, "clip": null}`
  (`null` = full frame). `GET /api/config` returns all three.
- **`POST /api/config` accepts any subset; `device` becomes optional** (extends).
  A `device` change still builds+validates+swaps the source; `rotation`/`clip` just
  validate and update the transform params — no camera rebuild. All present fields
  are validated *before* any swap, and the full merged config is **persisted before
  the swap** (preserving the existing invariant), so a partial or failed update
  can't diverge live state from disk or wipe a saved ROI.
- **Raw preview via `GET /frame?raw=1`** (extends). Returns the rotated,
  *uncropped* frame; the UI shows it while setting the ROI so the box is drawn on a
  correctly-oriented image, and derives normalized coords from the box over the
  displayed image.
- **Fail-safe transforms** (reuses). `crop` with a missing/malformed/empty clip
  returns the full frame; an unknown rotation is treated as 0°. Same
  can't-crash-on-bad-config spirit as the settings/boot fixes, so a hand-edited
  `settings.json` degrades to full-frame rather than erroring at render time.

## Goals

- `GET /frame` returns the door region, rotated the right way up.
- Set rotation and the ROI from the config UI, against a live oriented preview,
  and have both persist across restarts.
- Resolution-independent config (normalized clip) that works on any capture size.
- One transform path for both camera backends, reusable by the future `/stream`.

## Non-goals

- The motion gate and the `/stream` endpoint themselves (later increments) — this
  only provides the transform they'll reuse.
- Focus, fps, and resolution controls.
- Arbitrary-angle rotation (only 90° multiples) and any perspective/warp
  correction.
- The compute-tier **door zone** (in/out crossing geometry) — that's a distinct
  concept computed on the frames the Pi already cropped, not this clip rect.

## Design

### Layout

```
edge/clip/
├── __init__.py
└── transform.py     # rotate(frame, degrees) + crop(frame, clip)
```

### Transform functions (`edge/clip/transform.py`)

- `rotate(frame, degrees)` — `degrees` in `{0, 90, 180, 270}` (clockwise). Maps to
  `cv2.ROTATE_90_CLOCKWISE` / `ROTATE_180` / `ROTATE_90_COUNTERCLOCKWISE`; `0` (or
  any unrecognized value) returns the frame unchanged. Note 90/270 swap width and
  height.
- `crop(frame, clip)` — `clip` is `{"x","y","w","h"}` normalized to 0–1 against the
  frame passed in (i.e. the already-rotated frame). Converts to pixel bounds via
  `frame.shape`, clamps to the frame, and slices. Returns the full frame unchanged
  when `clip` is `None`, not a valid rect, or resolves to an empty region.

Both are pure and defensive — they never raise on bad input, they fall back to the
identity/full-frame result.

### Frame path (`app.py` `/frame`)

The app `state` holds `rotation` and `clip` alongside `source`/`device`, seeded at
`create_app` from `load_settings()` the same way `device` is (no validation needed
at load — the transform functions are fail-safe, so a bad stored value degrades to
full-frame/0° rather than erroring). `/frame`:

```python
raw = request.args.get("raw") not in (None, "", "0", "false")  # present & truthy → skip crop
with lock:
    source = state["source"]
    rotation, clip = state["rotation"], state["clip"]
    try:
        img = source.read()
    except CaptureError as e:
        return jsonify(error=str(e)), 503
# transform + encode outside the lock (operates on the captured frame)
img = rotate(img, rotation)
if not raw:
    img = crop(img, clip)
ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])
...
```

### Config API

- `GET /api/config` → the full config `{"device", "rotation", "clip"}` (the shape
  grows from today's `{"device"}`).
- `POST /api/config` accepts **any non-empty subset** of `{device, rotation, clip}`
  — `device` is now optional (the UI sends rotation-only and clip-only POSTs). The
  handler runs in this order:
  1. **Validate every present field first**, before any camera work, returning
     `400` on the first bad one: `device`, if present, must be a valid device id
     (non-null int or non-empty string, via `_coerce_device`); `rotation` must be
     one of `0/90/180/270`; `clip` must be `null` (clear) or a valid normalized
     rect (`0 ≤ x,y`; `w,h > 0`; `x+w ≤ 1`; `y+h ≤ 1`). A body with **none** of the
     three keys → `400`.
  2. If `device` is present **and changed**, build the candidate and validate it
     with a `read()` (→ `422`, keeping the old source), as today.
  3. Assemble the full next config from in-memory `state` overlaid with the present
     fields, and **persist that complete dict before swapping** — preserving the
     persist-before-swap invariant (a failed write → `500`; old source and saved
     config still agree; close the candidate). `save_settings` overwrites the whole
     file, so it must be handed the *complete* `{device, rotation, clip}`, never a
     single key — otherwise changing the camera would wipe a saved ROI/rotation.
  4. Under the lock, apply: new-before-close swap if `device` changed, and set
     `rotation`/`clip` in `state`; close the old source outside the lock. Return
     the full config.

`device` absent means "leave the camera as-is"; `clip: null` is the explicit "no
crop." Validation rejects clearly-bad input at the boundary (`400`); the transform
layer stays defensively fail-safe for anything already on disk.

### Config UI (`edge/server/ui/index.html`)

- A **rotation** control (0/90/180/270) that `POST`s `{rotation}` and re-previews.
- A **Set ROI** mode: the preview switches to `GET /frame?raw=1` (oriented, full
  frame); the user drags a rectangle over the displayed image, then **adjusts it
  with edge/corner resize handles** (and can drag the whole box) before saving.
  A **Save ROI** action computes `{x,y,w,h}` normalized to the image element and
  `POST`s `{clip}`; **Clear ROI** `POST`s `{clip: null}`. Because the box is
  editable before saving, no `POST` fires mid-drag.
- Normal **Capture** shows `GET /frame` (rotated + cropped). On load, `GET
  /api/config` seeds the current rotation and, if a clip is set, pre-draws the
  handled box over the raw preview so it can be tweaked rather than redrawn.
- Changing rotation leaves the saved clip untouched (not auto-cleared). Note a
  90°/270° change swaps the frame's aspect ratio, so an existing clip stays
  *numerically* valid but crops the **wrong** region until re-adjusted — `/frame`
  serves that until the installer nudges the still-editable box. Chosen over
  auto-clearing so a carefully-set region isn't silently discarded.

## Alternatives considered

- **`CaptureSource` decorator that returns transformed frames.** Elegant for
  auto-applying to `/stream`, but the raw preview then needs a bypass hatch to
  reach the pre-crop frame, adding indirection for no real gain over calling the
  transform functions where frames are served.
- **Native backend rotation** (Picamera2 `Transform`, OpenCV capture props).
  Efficient but backend-specific — two code paths and a camera reconfigure on every
  rotation change — versus one uniform post-capture rotate that's trivially cheap
  at ~5 fps.
