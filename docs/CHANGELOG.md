# Changelog

Each entry is numbered with a monotonically increasing integer. Append new entries to the end. Never reuse or reorder numbers. Numbers are globally unique across this file and any future `CHANGELOG-archive.md` — never reused. Write each entry as durable project memory: what is now true that wasn't before, plus the why in a clause when not obvious — not a recap of the diff (filenames and mechanical edits live there). Keep it to 1–5 lines, ~20 words per line at most; never one packed run-on line.

1. Established the anchoring docs and CLAUDE.md scaffolding for the cat-door vision prototype:
   CONCEPT (why/what), ARCHITECTURE (how — thin Pi edge streaming MJPEG to an NVIDIA compute PC),
   and root + edge/compute/shared CLAUDE.md.
   Framed as an early prototype on a trusted LAN: no auth between components, and door actuation
   plus its access-decision policy are deferred to a later phase.

2. Edge tier first slice: Flask `/frame` (JPEG q90), config UI with Capture button, API (GET /api/cameras, GET|POST /api/config).
   Pluggable CaptureSource (edge/capture/) with OpenCV backend; device id opaque (int or /dev/video* path) avoids lossy conversion.
   FakeCaptureSource for tests. Persistent capture self-heals on failure; device switches new-before-close under lock.

3. `POST /api/config` now returns 422 (not 503) when the candidate device fails to open.
   503 stays reserved for `/frame`'s already-working camera failing at read time; a rejected
   device selection is a client-input problem, not a service outage — per the MVP spec.

4. Edge hardened after code review. A hand-edited or corrupt `settings.json` can no longer
   crash boot: invalid or non-object values fall back to defaults, and `POST /api/config`
   rejects a non-object body with 400 (was 500). Device switch now persists *before* the
   in-memory swap and outside the slot lock, so a failed write can't leak the old capture
   handle or diverge live-vs-saved state. Config UI always shows the active device even when
   enumeration omits it (default index 0 vs Linux `/dev/video*`).

5. `./edge.sh` is the entrypoint to run the edge server: it bootstraps `.venv` from
   `edge/requirements.txt` on first run, then launches `edge.server.app` (honors
   `CAT_EDGE_PORT`). One command to start the edge on a fresh checkout.

6. CSI camera support: a Picamera2 backend drives the Pi Camera Module, which
   OpenCV's V4L2 path cannot capture from on current Pi OS (libcamera). Backend is
   chosen from the opaque device id — `csi[:N]` → Picamera2, else OpenCV — and
   `/api/cameras` lists detected CSI cameras. Picamera2 is apt-only, so the Pi venv
   needs `--system-site-packages` (`EDGE_VENV_SYSTEM_SITE_PACKAGES=1 ./edge.sh`).

7. `edge.sh` now enables the venv's `--system-site-packages` automatically on Linux
   (so the Pi can import apt's picamera2), off elsewhere, and rebuilds the venv when
   that setting changes — no more manual `rm -rf .venv` or env var on the Pi. The
   `EDGE_VENV_SYSTEM_SITE_PACKAGES` var remains as an override.

8. Edge applies per-frame rotate+crop transform: rotation (0/90/180/270) and normalized clip persist in settings.json.
   `/frame` returns rotated+cropped door region; `/frame?raw=1` returns rotated+uncropped for ROI editing.
   `POST /api/config` accepts any field subset (device now optional) and persists full config
   before swapping source, so bad values fail safe to defaults. Foundation for motion gate and `/stream`.

9. Edge serves `/stream` as continuous MJPEG (multipart/x-mixed-replace) from a
   background grabber thread reading at persisted fps (default 5); `/stream` and
   `/frame` both serve the shared latest-frame slot with X-Frame-Id/X-Timestamp.
   Config UI added Live toggle and fps control. CaptureSource.close() poisons
   read-after-close to seal the device-swap race; motion gating is the next increment.

10. Edge motion detection (MOG2 on downscaled clipped ROI in the grabber loop): motion gates
    the compute's GPU cost, not frame delivery—/stream stays continuous. Motion pulled via GET /status
    (camera_ok, bbox, area) and X-Motion headers on /stream parts. Locality/area gating + slow
    learning + persistence reject global illumination. Config UI: overlay + tuning + Relearn.
    Fixed exposure deferred; ARCHITECTURE.md updated to match the pull-signal design.

11. Edge reports its version. `edge.sh` resolves `git describe` once at launch and bakes it into
    `CAT_EDGE_VERSION`; the server reads that env var (never shells out to git) and returns it on
    `GET /status` as `version`, falling back to "unknown" when the bake step didn't run. Versioning
    is git-tag-based — a new release is a new annotated tag, no code bump; first tag `v0.1.0`.

12. Edge reports host CPU% and memory on `GET /status` under a `system` object
    (`cpu_percent`, `mem_percent`, `mem_used_mb`, `mem_total_mb`), shown as two badges
    in a slim top bar in the config UI. Measured with `psutil` — one portable path for Pi OS
    and macOS. CPU% is host-wide (not per-process) and resampled at most once per ~2s.
    Fails soft: psutil missing or a read error → `system: null` and /status still 200.

13. Edge↔compute wire contract now lives in `shared/wire.py` — single source of truth.
    Edge serializes frames through it, compute ingest (`EdgeClient`) parses through it; format can't drift.
    Round-trip test locks it. One wire change: `/stream` emits `X-Area` on every part (matching `/status`),
    not just when motion. `/status` is the camera health and liveness oracle; stream is the data plane.

14. Edge now controls Module-3 lens focus, fixing blurry close-ups (the lens sat near-infinity
    by default). New `focus` config: `null` = continuous autofocus, a number = manual dioptres
    LOCKED there — a fixed door scene beats hunting AF. Capability-gated (`focus_capabilities()`),
    so the UI focus slider shows only on a focus-capable camera. New endpoints
    `GET /api/capabilities` + `POST /api/focus/autofocus` (locks & persists the AF result).

15. Added `edge/tools/focus_test.py`, a standalone Picamera2 focus diagnostic (run with the
    edge server stopped). It isolates whether the Module-3 lens physically moves from whether
    the edge's best-effort, error-swallowing focus path silently failed — the two look
    identical from the UI, so a hardware fault couldn't be told from a code fault otherwise.

16. Compute-side always-on frame collector: saves every edge frame (motion + non-motion) with motion flag + area
    to a bounded (default 5 GB) SQLite-indexed store, indexed by recv_ts. FastAPI browse UI (port 8001) shows
    frames in time order with motion frames visually marked; triage presets (Missed? = non-motion by area;
    False triggers = motion by area) make motion-gate tuning findable. Reuses EdgeClient, writes raw JPEG bytes
    (no re-encode) — purpose is *seeing* where the edge motion gate is wrong (missed cats + false triggers).

