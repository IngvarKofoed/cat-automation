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

17. Compute-tier offline oracles validate the edge MOG2 gate: YOLO (cat detector) and
    BSUV-Net (background subtraction) run over stored frames, verdicts persisted to SQLite.
    Background sweep job—YOLO iterates un-analyzed frames (resumable), BSUV the full time-ordered
    set. Browse-UI shows disagreements (missed cats / false triggers). Heavy ML deps opt-in
    (compute/requirements-analysis.txt, lazily imported); BSUV is CUDA-bound.

18. Added `compute.ps1`, a Windows PowerShell port of `compute.sh` — the compute tier's
    real home is the NVIDIA PC, which here runs Windows. Same behavior: bootstraps
    `.venv-compute`, resolves the edge URL (arg > CAT_PI_URL > localhost:8000, scheme
    auto-prepended), launches the collector UI via uvicorn. Probes for a Python >= 3.10
    interpreter (the compute code uses `str | None` unions) rather than trusting the
    `py` launcher's default, which can be an older 3.8.

19. `compute.ps1` now sets and exports `CAT_COLLECT_MAX_BYTES`, defaulting the frame-store
    retention cap to 1 TiB (the Windows PC has ample disk) vs. the app's 5 GiB default.
    Prior scripts only echoed the cap without exporting it, so the app silently used 5 GiB;
    a caller-set env var still wins.

20. Documented the GPU-install footgun in `compute/requirements-analysis.txt`: `ultralytics`
    pulls `torchvision`, which pins PyPI's CPU torch and silently clobbers a CUDA build, so
    torch+torchvision must be installed together from the CUDA index. Blackwell GPUs (RTX
    5060 Ti, sm_120) need cu128+ wheels — older CUDA wheels lack Blackwell kernels. `torchvision`
    is now listed explicitly alongside torch.

21. Added `compute/tools/diagnose_misses.py`, a read-only tuning diagnostic: given the YOLO
    oracle's verdicts, it classifies MOG2 *misses* (motion=0 but cat present) so a raw miss
    count becomes an actionable one. It reports gate recall on cat-present frames, splits
    misses by YOLO confidence (recall-first YOLO over-calls at conf 0.15 — borderline misses
    may be oracle noise, not gate faults), buckets each miss by stored blob `area` vs the
    thresholds to name the knob (min_area / learning_rate / max_area / persistence), and —
    the load-bearing part — clusters misses into visits to separate harmless per-frame drops
    from wholly-missed visits (the only misses that cost a real GPU trigger). Thresholds are
    flags, not read from the Pi, so they must be confirmed against the edge's live settings.

22. Single source of truth — edge and compute instantiate shared `MotionGate` (post-transform MOG2 core:
    downscale → gray → threshold → morph → largest blob → area gate → debounce). Edge's refactor is
    behavior-preserving; kills the "second MOG2 drifts" risk.

23. Compute's `MogAnalyzer` re-runs the gate offline with adjustable params over stored frames.
    Baseline from Pi's live settings (new `GET /api/edge/config`), candidate from edited knobs.
    Windowed/stateful (MOG2 background builds frame-by-frame); results persist to analysis table.
    Tunes all six params offline — including var_threshold/learning_rate (stored area alone can't recover).

24. Gate scorecard generalized across motion sources (live or offline re-run) and oracles.
    Computes recall, missed frames (source-still ∧ oracle-present), false triggers (source-motion ∧
    oracle-absent), misses split by oracle confidence, area-vs-knob buckets (diagnoses which param),
    visit clustering (wholly-missed visits cost GPU). Fidelity check (baseline vs frames.motion) validates
    method transfer. Subsumes diagnose_misses.py into Store.gate_scorecard.

25. Tuning panel (vanilla JS): six param fields prefilled from edge, baseline/candidate buttons.
    `/api/tuning/compare?oracle=yolo` returns scorecards for live + baseline + candidate with
    per-metric deltas highlighted (green = fewer misses, red = more false triggers). Fidelity
    agreement shown. Winning params for copy-paste to edge config UI.

26. Frame-range groups: named, contiguous [start_id, end_id] windows scope oracle sweeps, MOG2 reruns, and
    scorecards to time slices. Bounds (since_id/until_id) thread through Store reads and API endpoints.
    Scoped reruns warm-start from the frames just before the window and clear only that window's verdicts;
    scoped scorecards drop only the still-unprimed prefix (0 when fully primed, full warm-up at the store's start).
    Persist via /api/groups CRUD (new groups table); groups survive eviction but drop on full clear() (rowid reuse).

27. CLAUDE.md guidance refreshed to current scaffold conventions. Root code-review mandate now ends a
    significant change by suggesting a deliberate user-run `/code-review medium` pass — the single
    auto-`--fix` pass is never re-reviewed, so a big diff still gets a human second look.
    Edge/compute UI-verification now name the installed Playwright MCP (`mcp__playwright__*`),
    replacing the uninstalled `claude-in-chrome` that couldn't actually be loaded here.

28. Compute collector no longer auto-starts at launch — a fresh `compute.sh` / `compute.ps1` run
    wires the collector but stays stopped, so the operator clicks Start in the browse UI before
    any frame is written to the store (avoids silently filling the store on every launch).
    `create_app`'s `start_collector` now only *wires* the live client + shutdown hook; a separate
    `autostart` flag — default off, resolved from `CAT_COLLECT_AUTOSTART` — gates begin-immediately.

29. Three-view motion-detection workflow: start collection, define buckets, review & tune — replaced single-page layout.
    Hash-routed (#start / #buckets / #motion) UI in one file; redistributes existing panels without rewriting.
    Starts addressing 24-h collection scalability: walk-away oracle jobs on several buckets, findable errors, bucket definition by eye.

30. Analysis job queue (FIFO, in-memory): enqueue replaces refuse-second-job 409; jobs drain serially with history.
    Cancel current / Clear pending / Stop all controls + per-job terminal state (done/failed/canceled).
    Addresses walk-away workflow — several buckets × oracles queued unattended, outcomes visible on return.

31. Collector intent persisted across restart (settings KV table in index.db): on-launch restore, one-click Resume.
    Intent written on user-initiated start/stop only — never by shutdown hook (preserves changelog 28's safety property).
    Collection survives mid-run restart; CAT_COLLECT_AUTOSTART=1 still forces immediate-start for unattended runs.

32. Optional motion-only capture mode (compute-side filter, default off): drops non-motion frames to save disk.
    Toggleable live via motion_only setting. Mode transitions recorded in mode_changes table with frame id + ts.
    Caveat: misses unmeasurable when motion-only is on; buckets/timelines flag "misses unmeasurable here" if overlapping a motion-only span.

33. Density timeline + visit inbox (keyboard-first review): clock→id bucket boundary definition via recv_ts index.
    Timeline bins a bucket by recv_ts; inbox clusters visits worst-first, surfaces rep frame + warm-up context.
    Addresses scale review — 864k frames becomes findable at a glance (density control + visit ranks).

34. Buckets viewer refinements. The clock end bound is now inclusive through the selected 3 h block
    (+step), so the newest frames — which fell after the last 21:00 option — are reachable, not silently
    excluded. Added a Clear-window button (reset the grid to re-see the saved-buckets list), a total
    frame count on the "All frames" badge, and a "Per hour" decimation density alongside "Per minute".
    "Per minute/hour" now decimates by TIME (one frame per recv_ts interval, via /api/frames/sample?per_ms)
    instead of by frame index — so the rate is a true wall-clock rate regardless of capture fps, clock-window
    width, or collector gaps. The prior index-stride computed its count from the (often huge, mostly-empty)
    clock window, yielding near-every-frame at "1/min".

35. The density-rate field refreshes the preview live. It now reloads on `input` (debounced), not `change`,
    so a typed "frames / min|hour" updates the grid and count immediately instead of only on blur/Enter.

