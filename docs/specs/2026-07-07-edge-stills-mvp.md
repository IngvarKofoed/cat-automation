# Edge stills MVP

The first slice of the edge tier: a Flask HTTP server on the door node that serves
a single JPEG still at `GET /frame`, backed by one **pluggable capture source**
(an OpenCV `VideoCapture` backend that runs on both a Mac for development and a
Raspberry Pi for deployment), plus a one-page config UI whose **Capture** button
fetches and displays the current still. Camera config is minimal: pick a camera,
persisted to a JSON file. No clipping, no motion gate, no stream, no
actuators — those are later increments behind the same interface.

## Key decisions

- **Capture-source interface, realized now** (new). Introduce the
  `CaptureSource` abstraction the architecture mandates (`edge/capture/`), with
  `read() -> frame` and `close()`. Everything downstream consumes frames through
  it and knows nothing about the specific camera. Building it now — not a bare
  `cv2` call — is what makes the edge CLAUDE.md "test against fakes" rule
  possible and gives every future backend a slot.
- **One cross-platform OpenCV backend** (new). A single `OpenCVCaptureSource`
  wrapping `cv2.VideoCapture(device)` covers Mac (AVFoundation → built-in/USB cam)
  *and* Pi (V4L2 → USB webcam). One backend serves both dev and deploy;
  Picamera2/CSI is deferred behind the same interface (non-goal).
- **`device` is an opaque camera identifier** (new). It is an **int index or a
  device-path string** (`"/dev/video0"`) — `cv2.VideoCapture` accepts both. The
  config field, the backend constructor, and the enumeration entries all use this
  one type. Reason: Linux enumeration yields `/dev/video*` paths, and mapping
  those to int indices isn't 1:1 (a USB cam exposes multiple video nodes), so an
  opaque id avoids a lossy conversion.
- **`FakeCaptureSource` for tests** (new). A backend returning a synthetic frame,
  so the server and encoding path are unit-testable with no camera present —
  satisfying the edge subtree's hardware-behind-interface testing rule.
- **Flask web server** (new, tech-stack). The architecture lists "Flask/FastAPI";
  pick Flask for the edge — synchronous, tiny, a natural fit for blocking camera
  reads. FastAPI stays the choice for the `compute/` dashboard.
- **`opencv-python-headless` dependency** (new, tech-stack). Headless build — the
  edge needs frame capture and JPEG encoding, never OpenCV's GUI/highgui, and
  headless avoids pulling GUI libs onto the Pi. Brings NumPy transitively.
- **`GET /frame` → `image/jpeg`** (reuses). Adopts the architecture's defined
  still endpoint as-is: one request, one JPEG, connection closes. JPEG quality
  90 (architecture's q≈90–95 default).
- **Persistent, lock-guarded capture handle** (new). Open the `VideoCapture`
  lazily once and keep it, guarded by a `threading.Lock` (OpenCV capture is not
  thread-safe). Avoids the ~1–2 s AVFoundation warmup on every click and
  serializes concurrent `/frame` requests.
- **Config as a gitignored JSON file** (new). Persist the selected device index
  to a JSON file on disk (realizes the architecture's "settings persist on the
  Pi"). Runtime state, so it is not committed.
- **OS-specific camera enumeration** (new). `/api/cameras` branches on platform:
  on macOS it returns just the built-in webcam (index 0) with no probing — the
  Mac is only a dev box, and probing each index would cost AVFoundation warmup and
  flash the camera light; on Linux/Pi it enumerates `/dev/video*` device paths.

## Goals

- Get a real still off the camera through an HTTP endpoint, on Mac and on Pi,
  from the same code.
- A minimal browser page with a Capture button that shows the latest still.
- Let the user pick which camera and remember it.
- Establish the capture-source interface + fake so the rest of the edge grows on
  a testable foundation.

## Non-goals

- The MJPEG `/stream` endpoint, clipping (ROI), and the motion gate — later edge
  increments.
- Picamera2 / CSI camera backend, and RTSP/IP backends.
- Focus, resolution, and fps controls; capability advertising by the source.
- Actuators (lock/sound/light) and the control API.
- Any authentication (trusted-LAN prototype, per CONCEPT/ARCHITECTURE).
- Lossless (PNG/WebP) stills.

## Design

### Layout

Slots into the edge tree from ARCHITECTURE.md:

```
edge/
├── capture/            # CaptureSource interface + backends
│   ├── base.py         #   CaptureSource ABC + CaptureError
│   ├── opencv_source.py#   OpenCVCaptureSource (Mac + Pi)
│   └── fake_source.py  #   FakeCaptureSource (tests)
├── config/             # settings load/save
│   └── settings.py     #   read/write the JSON config file
├── server/             # the Flask app
│   ├── app.py          #   routes + app factory + __main__
│   └── ui/
│       └── index.html  #   the config page (Capture button)
├── tests/              # pytest, against the fake source
└── requirements.txt    # flask, opencv-python-headless
```

### Capture-source interface

```python
# edge/capture/base.py
class CaptureError(Exception): ...

class CaptureSource(ABC):
    @abstractmethod
    def read(self) -> "np.ndarray":   # one BGR frame; raises CaptureError on failure
        ...
    @abstractmethod
    def close(self) -> None:
        ...
```

The source returns a **decoded frame** (BGR ndarray), not encoded bytes —
JPEG encoding lives at the server boundary so future consumers (clip, motion,
stream) get raw frames. Frame id / timestamp metadata is deferred to the stream
increment where it earns its keep.

### OpenCV backend

`OpenCVCaptureSource(device: int | str)` holds a `cv2.VideoCapture(device)` and a
`threading.Lock`. `read()` locks, opens the handle lazily if needed, calls
`cap.read()`, and raises `CaptureError` if the capture won't open or returns no
frame. `close()` releases the handle. The handle stays open between calls (see
the persistent-handle decision).

**Failure recovery.** On any failed `read()` the source *releases and invalidates*
the handle so the next call reopens from scratch. Without this, a persistent
handle that starts failing (camera unplugged/replugged, grabbed by another app,
or macOS permission denied on first open then granted) would return 503 forever
until a restart. Recovery is retry-on-next-request — no timed backoff in the MVP;
each `/frame` simply attempts a fresh open.

### Fake backend

`FakeCaptureSource` returns a small synthetic BGR frame (e.g. a solid or
gradient array). It lets tests exercise the `/frame` encode path and the config
routes with no hardware.

### Server and routes

Flask app (`edge/server/app.py`), run with `python -m edge.server.app`, binding
`0.0.0.0:8000` on both platforms (so it is LAN-reachable on the Pi and on
`localhost` on the Mac). Port via env (`CAT_EDGE_PORT`, default 8000). Dev server
runs `threaded=True`.

**App factory and the source seam.** `create_app(source=None)` builds the Flask
app. In production `source` is omitted and the app builds an `OpenCVCaptureSource`
from the persisted device; tests pass a `FakeCaptureSource` in. This injection
point is what makes the `/frame` route testable with no camera — the whole reason
the interface exists. The app holds the *current* source in one slot guarded by a
lock; there is no import-time camera open.

**Switching devices.** A `/api/config` POST builds the new source, then under the
lock swaps it into the slot and `close()`s the old one — new-before-close so a
failed open leaves the working source in place, and lock-guarded so an in-flight
`/frame` never reads a handle being closed.

| Route | Method | Returns |
|---|---|---|
| `/` | GET | the config UI HTML page |
| `/frame` | GET | `image/jpeg` — one still, `cv2.imencode('.jpg', frame, [IMWRITE_JPEG_QUALITY, 90])` |
| `/api/config` | GET | `{"device": <int|string>}` current settings |
| `/api/config` | POST | body `{"device": <int|string>}`; validate, persist, switch source |
| `/api/cameras` | GET | list of selectable cameras (OS-specific enumeration, below) |

`/frame` returns HTTP 503 with a JSON body `{"error": "<message>"}` when the
camera can't produce a frame (unplugged, index wrong, macOS camera permission
denied), so the UI can show a clear error rather than a broken image.

`POST /api/config` rejects a missing or malformed `device` with 400. It attempts
to open the new source before committing; if the camera won't open it returns a
4xx and keeps the previous working source (and unchanged persisted config), so a
bad selection can't wedge the node.

`/api/cameras` enumerates per OS. On macOS it returns a single fixed entry — the
built-in webcam at index 0 — with no probing (probing each index would incur
AVFoundation warmup and flash the camera light for nothing on a dev box). On
Linux/Pi it lists `/dev/video*` device paths. Each entry carries the opaque
`device` id plus a display label for the `<select>`. The camera's native
resolution is used throughout — no resolution control in the MVP.

### Config UI

`edge/server/ui/index.html` — one page, plain HTML + a little vanilla JS, no
build step:

- A **Capture** button that reloads the still: `img.src = '/frame?t=' + Date.now()`
  (cache-buster), and surfaces the 503 case as an inline error message.
- An `<img>` showing the last captured still.
- A camera `<select>` populated from `GET /api/cameras`, whose change `POST`s to
  `/api/config`. On load it reads `/api/config` to show the current selection.

### Config persistence

`edge/config/settings.py` reads/writes a JSON file (`{"device": 0}`, where
`device` is the opaque int-or-string id). Path from `CAT_EDGE_CONFIG` env, default
`edge/config/settings.json`; the file is gitignored (runtime state). A missing
*or unparseable* file → defaults (`device: 0`), so a truncated/partial write or a
manual edit can't crash startup.

### Running

Dependencies live in `edge/requirements.txt` (`flask`, `opencv-python-headless`)
and are installed into a virtualenv — never the system Python.

- **Mac (dev):** `python3 -m venv .venv && source .venv/bin/activate &&
  pip install -r edge/requirements.txt`, then `python -m edge.server.app`, open
  `http://localhost:8000`. First `/frame` may prompt for camera permission; a
  denied permission surfaces as the 503 path.
- **Pi (deploy):** same — a venv with the same `requirements.txt` — plus a USB
  webcam on `/dev/video0` (index 0). systemd packaging is a later increment.

## Alternatives considered

- **Bare `cv2` in the route, no interface (Approach B).** Fastest to a demo but
  violates the edge "test against fakes" rule, can't be unit-tested without a
  camera, and forces rework when the interface arrives. Rejected.
- **Add a Picamera2/CSI backend now (Approach C).** Picamera2 can't run or be
  tested on the Mac, and a USB webcam on the Pi already works through the OpenCV
  backend, so CSI is scope the MVP doesn't need. Deferred behind the interface.
- **FastAPI instead of Flask.** Mirrors the compute tier, but async buys nothing
  for blocking single-camera reads and is heavier than this thin node needs.
