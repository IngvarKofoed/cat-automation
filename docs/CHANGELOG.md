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

36. Motion-view oracle coverage is scoped to the selected bucket, not whole-store. New GET
    /api/analysis/coverage returns per-oracle {analyzed, present} against the window's frame total,
    so "X/N analyzed · P present (in this bucket)" and the enqueue confirmation ("enqueued over
    bucket …") make it clear what a scoped sweep will actually cover — the enqueue was already
    bucket-scoped; only the display lagged.

37. Clock-picker End dropdown shows slot ENDS (03:00 … 24:00), not slot starts. Previously the end
    bound was inclusive-through-the-3 h-slot but the dropdown still displayed the slot start, so
    picking "21:00" misleadingly meant "through 24:00". The End value now IS the end instant (no
    hidden +step at load); Start still shows slot starts (00:00 … 21:00).

38. Buckets "Select all" button: one click makes the pending bucket the whole loaded window
    (its resolved [since_id, until_id]), instead of hunting for the exact first/last tile in a
    decimated grid. Uses the resolved id bounds, not on-screen tiles, so density/paging can't
    truncate it. GET /api/frames/resolve now also returns since_ts/until_ts so the readout labels
    the selection with real frame times.

39. Motion view: enqueuing an oracle now shows an in-flight state ("Enqueuing YOLO…", buttons
    disabled) and the running job shows a live ETA ("~Xm Ys left"). The first YOLO/BSUV enqueue
    blocks several seconds on the synchronous ensure_available() dep import (torch/ultralytics),
    during which the job isn't queued yet — so the click looked dead. ETA is client-side: rate
    extrapolated from progress across polls, re-anchored per job (total is in the anchor key, so
    it re-anchors when the denominator resolves), dropped when idle. No server change.

40. Visit-inbox filmstrip now red-borders the frames the gate missed (a visit frame with
    motion=false, not the rep, not warm-up context) — matching the timeline's "Missed" swatch.
    A visit is a cluster of oracle-present frames, so a still gate inside one is a true miss;
    the strip now reads caught (green) vs missed (red) at a glance. The rep keeps its purple even
    when it is itself a miss. Client-only; keys on the stored motion flag, not a per-frame oracle join.

41. Offline stateless (YOLO) sweep now batches + prefetches — a decode-ahead thread feeds the GPU
    one predict() per batch, de-starving it (was ~35% util, batch=1 FP32) for ~2–4× throughput.
    Windowed BSUV/MOG2 path unchanged: batching would break its rolling background.
    Batching is verdict-preserving — shape-boundary chunking prevents letterbox drift, FP16/FP32 single-sourced.
    New knobs: CAT_YOLO_BATCH (default 8), CAT_YOLO_HALF (FP16, off by default, cuda-only — the only lever that can move a verdict).

42. Store opened WAL + synchronous=NORMAL store-wide — commits get cheap (fsync deferred to checkpoint).
    Accepted consequence: a hard power loss may orphan a JPEG (row lost, file kept) that is never
    counted/evicted — a small non-self-healing disk leak, never corruption.
    New batched write_analysis_batch and Store.close() (checkpoints on exit). Shutdown now stops AND JOINS both the
    collector and the analysis worker before store.close() — both write the one shared connection, so closing under a live writer races a closed DB.

43. Reskinned the compute motion-workbench UI (compute/api/web/index.html); presentation only, behavior unchanged.
    Full design record in docs/specs/2026-07-12-motion-workbench-ui-reskin.md.
    CSS now flows from a two-tier design-token layer, so a theme is a ~20-var swap: dark "review console" is the
    default (:root), light lives under [data-theme="light"] — no visible toggle, deliberately not OS-following.
    Organizing rule "color = verdict": neutral canvas, saturation only for the 4 verdict colors + one cool accent;
    the bucket-picker start/end selection now uses the accent with S/E corner tags (was green/red, which clashed
    with caught/missed).
    Topbar is now a true full-bleed app bar (a .app-main column + scrollbar-gutter); it previously floated inside
    the centered 1100px column. Pills reserved for state chips, nav is an underline, static readouts are quiet
    labels, and .warn still renders amber on any element the JS toggles it on.
    Layout-jump fixed at its frequent sources: a fixed-height visit stage (collapses when empty), tabular-nums +
    min-width on live badges, and a 1px-transparent button border so disabling never resizes the box. Rare
    state-driven banners (#error, warn notes) just collapse when hidden — reserving permanent slots for them
    only left empty gaps.
    Sole JS change: renderTimeline's three rgba() literals → rgb(var(--v-*-rgb)/α) token form; every id and
    JS-toggled class preserved. Text on saturated fills uses --color-on-accent (dark on the light accent) so
    buttons meet WCAG contrast — white on the accent was ~2.3:1.

44. Added `#activity` — a user-facing SPA view showing motion-based events (time-gap frame clusters).
    New Store.events() + /api/events reuse _gap_split/_VISIT_GAP_MS to prevent clustering drift.
    No oracle required — populated instantly. Event thumbnail is the peak-area frame; click opens
    an in-view player (play/pause/scrub, ~8 fps) via /api/frames + filmstrip. From/to date filter; cat-id filtering deferred.

45. `compute.ps1` now defaults `CAT_COLLECT_AUTOSTART=1` (a caller-set value still wins; =0 launches
    stopped). The Windows box is the dedicated collection PC, so a stop/start to `git pull` resumes
    collecting with nothing to remember — the real footgun, since the one-click Resume prompt (31)
    was being forgotten. Per-machine and explicit, not inferred: `compute.sh` (dev) stays off, so
    changelog 28's "a bare launch never silently writes" holds where it matters.

46. Tuning scorecards (Live gate / Baseline / Candidate) now headline **visit recall** as a big
    footer % instead of a one-line "Visits" row. Visit recall (caught/total visits) — not frame
    recall — is the metric the gate is tuned toward: one caught frame per visit is enough to wake
    the GPU, so a wholly-missed visit is the only miss that costs a real trigger. Presentation only.

47. Activity playback now opens in an almost-full-page modal, replacing the in-view panel that
    read as cramped. **Double-click** an event to open (single click is a no-op, so scanning the
    grid never launches it); Esc, the × button, or a backdrop click closes it. Opening locks
    body scroll and moves focus into the dialog; leaving the route fully closes it. Same player
    logic (same element ids) — only the container changed.

48. Activity playback now auto-plays: opening an event (and each Prev/Next hop, both routed
    through openEvent) starts from frame 0 instead of waiting for a Play click. Playback rate
    dropped 8→5 fps to match the ~5 fps capture, so the clip plays back at real speed.

49. Viewing a saved bucket now shows its wall-clock start → end times (plus duration), not just
    the duration — you couldn't tell *when* a bucket sat, only how long it was. New formatRange
    helper; presentation only.

50. All dashboard timestamps now render on a 24-hour clock (`formatTime` forces `hour12:false`)
    instead of following the locale's AM/PM default — every readout flows through formatTime, the
    lone chokepoint. Explicit date+h:m:s components keep the prior layout (4-digit year); only the
    clock changed. Presentation only.

51. Dashboard date format fixed to `dd/mm-yyyy` (e.g. `12/07-2026`), superseding the locale's
    m/d/y order. formatTime now builds the string by hand (local time) since no locale yields the
    mixed "/" then "-" separators. Presentation only.

52. Analysis-queue running line now shows throughput as `N.N fps` alongside the ETA. The rate
    (frames/sec, client-side average since the job's anchor poll) was already computed to derive
    the ETA — it's now surfaced instead of only its reciprocal. Presentation only.

53. CSI capture fixed the green-stripe / purple corrupted frames on the Module 3 (IMX708):
    the backend used a full-res `create_still_configuration` (a SINGLE buffer, meant for one-shot
    stills) driven as a continuous ~5 fps loop, so libcamera handed back half-filled buffers.
    Now a `create_video_configuration` at 2304x1296 (2x2-binned, lower-noise at night) with
    buffer_count=4. Also likely quiets the benign `PDAF data in unsupported format` log spam,
    which rides the full-res sensor mode; that error was never the corruption and is harmless.

54. Added the `yolo-serial` oracle: the SAME YOLO backend in its pre-batching, bare-per-frame
    call shape (`YoloAnalyzer(serial=True)`; distinct name, batch_size 1), registered beside
    `yolo`/`bsuv`. It A/Bs the batched sweep — run both over one bucket, compare each vs MOG2 in
    the scorecard — isolating the batching *code*, not the unpinned ultralytics version (both run
    under whatever is installed). Also unified the scorecard's oracle allow-list onto the registry:
    `store._SCORECARD_ORACLES` was a hardcoded second copy of the names that 500'd
    `/api/tuning/compare` for any newly registered oracle; it now derives from `ANALYZER_NAMES`.

55. Motion workbench split into two hash-routed pages: #sweeps (oracle sweeps + job queue) and
    #tuning (MOG2 tuning + timeline + inbox); #motion redirects to #sweeps.
    On Tuning, a coverage-driven multi-select drives a visit-recall matrix comparing the gate
    against multiple trusted oracles side-by-side (client-side fan-out, no backend change).
    Bucket scope mirrors across both pages. Separates producing oracle verdicts from evaluating
    the gate, enabling the YOLO vs YOLO-serial vs BSUV comparison (entry 54).

56. Added `compute/tools/diff_yolo_batch_serial.py`, a read-only diagnostic root-causing a `yolo`
    vs `yolo-serial` oracle disagreement: coverage parity, verdict diff, visit reconstruction, and
    `--rerun` re-running both YOLO paths on disagreeing frames. Key insight it encodes: `gate_scorecard`
    scores each oracle over only ITS OWN analyzed frames, so unequal coverage or cross-session
    `CAT_YOLO_*` drift (invisible — `detail` omits imgsz/conf) can move a matrix column ~15pt, no bug.

57. Cat-identity annotation tool (compute `#annotate`) — first slice of the learning loop: per-visit keyboard
    labelling of live `yolo-serial` detections (clustered via `_gap_split`) into per-frame `dataset_items` rows +
    durable crops under `<CAT_COLLECT_DIR>/dataset/`, each tagged quality `gallery`/`ok`/`poor` for a future gallery.
    New `cats` + `dataset_items` tables SURVIVE eviction AND `clear()` — labels are the precious output, decoupled
    from the rolling frame buffer; dedup on `(src_frame_id, src_recv_ts)` defeats a `clear()`+rowid-reuse mislabel.
    Deferred: training/gallery-build, in-tool undo/re-label, and `annotation_visits` pagination.
    Spec: docs/specs/2026-07-15-annotation-tool.md.

58. Annotation tool gains in-tool undo / re-label — a "Labelled" mode on `#annotate` (newest-labelled-first
    review via new `Store.labeled_visits` + `GET /api/label/labeled`). Per visit: re-label with 1–9/u/x
    (`POST /api/label/relabel`: delete rows+crop files, then re-commit) or send back to the queue with `d`
    (`POST /api/label/delete` → `Store.delete_dataset_items`). A mislabel is now fixable without SQL, and both
    paths delete the orphaned crop files so the durable set never drifts from the DB.
    Also hardened: `POST /api/label` validates per-frame quality BEFORE any crop is written (a bad value
    previously left orphan crop files); `*.pt` gitignored (ultralytics drops weights in the repo root).

59. Feasibility probe (`compute/identification/` + `compute/tools/feasibility.py`) answers Phase-1's "can we
    tell our cats apart?" over labelled crops — offline, read-only, NO training. Embeds `identified` crops
    (new `Store.labeled_crops`) with a pretrained DINOv2 backbone (`torch.hub`, lazy/torch-gated like the YOLO
    oracle — first run downloads it), then scores separability: leave-one-out kNN accuracy + confusion,
    same-vs-different-cat cosine-distance AUC + a suggested confidence threshold, and a PCA-2D scatter — emitted
    as a self-contained HTML report + JSON. Separability maths is pure-numpy (unit-tested with synthetic
    vectors); DINOv2 + matplotlib are opt-in analysis extras. Runs on the compute PC (labels + net + GPU there).

60. Feasibility probe gains a `--quality gallery[,ok[,poor]]` filter (new `qualities` arg on
    `Store.labeled_crops`): A/B gallery-only vs all-crops to test whether crop quality — not the
    cats — is the separability bottleneck, answering the report's "weak ≠ hopeless" hedge with a
    measurement. Filtered runs write to `feasibility-<slug>` (grade stamped in report + console) and
    exclude NULL-grade crops; default (no flag) unchanged. Grades still have no other consumer.

61. Training page (`#train`) — the learning loop's Train stage in the compute UI; only "Validate" is built.
    Validate runs the DINOv2 feasibility probe as a cancelable background job on a new dedicated
    `TrainingManager` (own queue, separate from the sweep `AnalysisManager`), with a gallery/ok/poor A/B and the
    report in-page; runs persist to a durable `feasibility_runs` table. The probe pipeline moved to
    `compute/identification/probe.py` (CLI now a thin wrapper). Build/promote deferred. Spec: docs/specs/2026-07-16-training-page.md.

62. Annotation tool (`#annotate`) rep stage now shows the **full frame with the detection box
    overlaid** beside the tight crop, in both Queue and Labelled modes — top-down scene context and
    cat scale (a resident-vs-foreign cue) alongside the dorsal-coat detail. Overlay is client-only:
    the box is a percentage of the frame's natural dimensions (bbox is in stored-JPEG pixel space),
    and the wrapper is pinned to the image's aspect ratio so it can't letterbox and mis-place the box.

63. Annotation tool's Labelled mode gains a **"Show label" filter** — review annotated events by one
    resident cat, unknown cat, or not-a-cat (or all). Client-only: options are built from the labels
    actually present in the fetched set, with per-label counts. Undo now removes the visit from the
    unfiltered backing set too, so a sent-back visit can't reappear when the filter changes.

64. Training-page Run button no longer gets stuck. It's driven by a `trainSubmitting` flag from click
    until the enqueue POST resolves, so the periodic status poll — which fires during the seconds the
    server spends importing torch BEFORE the job exists — can no longer see `running=false` and re-enable
    a mid-submit button. Button + progress now read Starting… → Running… → Run/Idle from one source.

65. Motion-workbench UI coherence pass (compute/api/web, presentation only).
    Buttons now share one geometry with a calm hierarchy: neutral surface default, saturated accent
    fill reserved for a single `.btn-primary` per group; the green `.btn-preset` is retired and danger
    is a red *tint* — so no button competes with the red "missed" verdict (upholds #43's "color = verdict").
    Badges are squared, not pills; checkboxes and radios are custom-styled (native controls never took the
    dark theme). Job/queue lists are now real record rows — uppercase headings, mono right-aligned meta, a
    live green-dot status strip, dot-led terminal-state log; Annotate's Queue/Labelled toggle is a segmented control.

66. Replaced the single catch-all `.badge` with ONE readout: the `.metrics`/`.metric` cluster
    (small-caps caption over mono value, in a bordered hairline-divided strip). EVERY data reading
    is a cluster — multi-cell where related (store-stats header; bucket start/end/in-range; playback
    time+frame; annotate decided/labelled/identity), single-cell otherwise (store range, visit
    position). State is a status chip (dot + word, Collecting); scope/params/window/page/status
    context stay quiet divider-labels. `.badge` backs only those; Activity has no badges.

67. Gallery-build + promote: new `model_versions` table (survives eviction/clear like `cats`/`dataset_items`)
    versioning k-NN galleries built from labelled crops. `gallery-build` is a TrainingManager job embedding
    selected-quality `identified` crops and writing their vectors+cat_ids to `<CAT_COLLECT_DIR>/models/<ts>/gallery.npz`.
    `promote` is synchronous (flips target→active, current-active→retired, one active at a time — rollback by promoting retired).
    Spec: docs/specs/2026-07-17-identification-gallery-activity.md.

68. Identification pass: new frame-keyed `identifications` table (evicts with frames, like `analysis`)
    storing per-frame nearest-neighbour match to active gallery. `identify` TrainingManager job embeds
    yolo-serial-detected crops from live frames, matches to gallery, stores cat_id+distance (no threshold baked).
    Threshold lives on model_versions row, applied only at read—always tunable without re-identify.
    Resumable, idempotent per model.

69. Activity feed now shows resident/neighbour name or "unknown cat" on event cards, derived from aggregated
    identifications within each event's frame span. Vote among below-threshold frames; unknown when nearest cat's distance > threshold,
    or null when no active model/identifications. Additive: base feed (motion clusters, no names) unchanged without a promoted model.

70. Uncalibrated identification fails safe: a model whose threshold is NULL (uncomputable — e.g. one crop
    per cat, no same-cat pair) resolves EVERY event to "unknown cat" rather than naming the nearest
    resident. An uncalibrated model must never admit a foreign cat as a resident.

71. Identify pass now converges and counts truthfully: a detected frame it can't embed (no yolo-serial
    box, or an undecodable/degenerate crop) gets a marker row (`cat_id` NULL, ignored at read) so it's
    recorded processed and never re-attempted, and iter/count agree so progress reaches 100%.
    `n_identified` counts only rows that actually persisted (frames evicted mid-pass excluded).

72. Gate scorecard gains a tunable oracle-confidence floor (`gate_scorecard(oracle_floor=)` store-default 0;
    `/api/tuning/compare?oracle_floor=` default 0.30; "oracle conf ≥" field on #tuning). "Present" is now
    verdict=1 AND score ≥ floor — re-slicing the SAME stored verdicts, no re-sweep; floor 0 = old scorecard.
    Why: YOLO runs recall-first at conf 0.15 and hallucinates cats on empty frames, so phantoms inflated
    present/missed and fragmented into thousands of bogus visits. Caveat: metrics below ~0.3 are phantom-dominated.

73. Annotation queue (`#annotate`) now floors detections at `_ANNOTATE_MIN_CONF` (0.3): `_present_frames`
    admits only yolo-serial verdicts with score ≥ floor, so the recall-first oracle's empty-frame phantoms
    (conf 0.15) no longer bloat the queue + progress with junk "not a cat" visits (an empty scene isn't a
    useful negative). Floors queue and progress together (shared universe); `labeled_visits` (undo/review)
    stays unfloored so a decision made before the floor stays reviewable. Fixed, not per-request.

74. Activity page now names new visits automatically: `LiveIdentifyManager` (mirroring `CollectorManager`)
    ticks every 5s over closed motion clusters (settled ≥ `_VISIT_GAP_MS`), running `yolo-serial` detect +
    `run_identify` against the active gallery per cluster. Reads `active_model()` each tick (promotion live),
    yields GPU to manual jobs, holds resident detector+embedder. `run_identify` accepts optional `embedder`
    to avoid per-tick reload. Historical re-identification needs manual pass; worker runs only on compute PC.

75. Hardened the live-identify worker (review). First enable seeds the watermark to the frame
    horizon: it names only NEW visits, never back-identifies the whole store (history = manual pass).
    Each tick re-checks stop/`is_busy` and caps spans (`_MAX_SPANS_PER_TICK`), so a manual job or a
    backlog can't be starved or monopolize the GPU; a stop mid-detect/identify no longer advances the
    watermark (idempotent resume finishes the span). Resident gallery + idempotent
    `YoloAnalyzer.prepare()` end the last per-visit model/gallery reloads.

76. Known limit of the live worker: it writes `yolo-serial` verdicts only within visit spans, so
    gate-scorecard / annotation coverage over a live-populated window is non-uniform — tune the motion
    gate from a full manual sweep, not a window the live worker has already touched.

77. Activity feed distinguishes resident from foreign matches: the event identity now carries
    `is_resident`, and a named NON-resident (neighbour) cat renders RED, not green — a green chip
    always means one of our cats. Resident = green, non-resident = red, unknown cat = amber (unchanged).
    Chose red for a known stranger over reusing amber so a confident foreign match reads as an alert,
    not a "second look".

78. Activity gained a "Non-residents & unidentified only" checkbox filter (client-only, no refetch):
    hides events confidently identified as a resident, leaving foreign/unknown/unidentified visits —
    the events worth a look. The player + Prev/Next now step the filtered subset, so navigation can't
    land on a hidden resident.

79. #tuning MOG2 fields now carry a per-knob description (`.param-hint`): what each param does and
    which way to turn it to detect more (↓ var_threshold/learning_rate/min_area/persistence,
    ↑ max_area_fraction/motion_downscale). persistence notes it's frames-not-seconds, so a higher
    capture fps shortens the same value's time window. Presentation only.

80. Compute UI split into two independently-styled front doors: the workbench SPA moved to `/admin`,
    a near-blank user page now serves `/`. Separate HTML files (own inline `<style>`) share NO CSS,
    so the coming user dashboard styles free of the admin look; only `/api/*` + `/media/*` stay shared.
    Admin moved verbatim (absolute API calls + hash routing → `/admin#activity`); user page has a
    stopgap `/admin` link, legacy bookmarks not redirected, no auth. Spec: docs/specs/2026-07-22-admin-user-area-split.md.

81. Built the real user dashboard at `/` (warm "Threshold" SPA, own CSS, no admin sharing): an Activity
    feed and a Cats roster. Activity is a day-grouped time-rail of door events reusing `/api/events`
    (no backend change) with identity chips (resident/neighbour/unknown) and click-to-play.
    Cats shows residents-first cards; each cat's "last seen" is DERIVED from the same `events()` feed
    (new `Store.cats_overview`), so Cats and Activity can never name the same moment differently, and it
    inherits the uncalibrated fail-safe (an uncalibrated gallery names no resident).
    Per-cat avatars are uploadable — a file convention `<dataset_root>/avatars/cat_<id>.jpg` (no schema
    column; survives eviction/clear), served with an auto labelled-crop fallback; upload is a raw-body
    POST re-encoded via base-dep cv2 (no new dependency). "Who's home" is a deferred placeholder — needs
    direction detection. Spec: docs/specs/2026-07-22-user-activity-cats.md.

82. Activity feed dropped its from/to date picker — it now just shows the recent visits, newest first
    (`/api/events` unbounded, capped server-side). The user view is a glance at recent door activity,
    not a searchable log (date-scoped browsing lives in `/admin`). The non-residents-only filter stays.

83. Added a frontend dev proxy (`./frontend-dev.sh` → `compute/tools/frontend_dev_proxy.py`): serves the
    LOCAL `web/{user,admin}/index.html` (no-store, so edit→refresh) and reverse-proxies every other
    request to the real compute PC (`CAT_COMPUTE_URL`, default :8001). Iterate dashboard visuals on the
    dev box against live data — no backend change, no CORS (the frontend uses same-origin absolute paths),
    no data copy. Dev convenience only; reuses `.venv-compute` (fastapi+uvicorn+requests, already deps).

84. Activity feed made denser: dropped the subtitle and the "showing N" note, and folded the filter
    (relabelled "Hide our cats") onto the heading line — more visits fit on screen.
    Event thumbnails are now round and show the identified cat's AVATAR (falling back to the door frame
    if that cat has no photo); an unknown/unidentified visit still shows the frame, where seeing the cat
    is the point. The feed fetches `/api/cats/overview` alongside events so a photoless cat shows its
    frame rather than 404-ing on an avatar. Frontend-only.

85. Avatar URLs are version-stamped for caching without staleness: `/api/cats/overview` returns each
    cat's `avatar_version` (the served avatar file's mtime, ms) and the UI stamps it on the URL
    (`…/avatar?v=<mtime>`) on both the Cats and Activity views. An unchanged avatar keeps one cacheable
    URL (big images stay cached); a re-uploaded one gets a fresh URL that auto-busts, so the new photo
    shows everywhere at once — no `Cache-Control` change needed. `has_avatar` now derives from real file
    existence (a crop row whose file is gone reads false); the per-session `avatarBust` hack is gone.

86. User dashboard now refreshes its data on foreground, fixing the stale feed on a pinned/home-screen
    iOS web app — iOS resumes the frozen WebView from memory rather than reloading, so the feed never
    updated. `visibilitychange`→visible and `pageshow(persisted)` re-run the active view's loader (data
    only, no shell reload — keeps scroll/route), plus a 60s visible-only poll of the Activity feed.
    Guarded: never while playback is open (won't yank frames) or scrolled down (a rebuild jumps to top).

87. Live push for the Activity feed over SSE (`GET /api/events/stream`): the server nudges connected
    clients when the feed actually changes, so a foregrounded dashboard updates in near-real-time instead
    of waiting for the poll. Signal is `Store.activity_signal()` — MAX motion-frame id (new door event),
    MAX identifications rowid (a late naming), and the active model id (a promotion). Motion-SCOPED on
    purpose: continuous frame capture would fire a whole-store "newest frame" signal every tick.
    Client opens the stream while visible, tears it down when hidden; the 60s poll (86) stays as a
    fallback for when SSE can't connect (unsupported client, buffering proxy).

88. User dashboard is now an installable home-screen app: `apple-mobile-web-app-capable` (+ modern
    `mobile-web-app-capable`), a real `/apple-touch-icon.png` (served at the root paths iOS probes; a
    door-mark PNG generated once with cv2), an app title, and per-theme `theme-color`. Status bar is
    `default`, not black-translucent — the latter forces white text, unreadable over the light interior.
    Also: the SPA shells (`/`, `/admin`) now send `Cache-Control: no-cache`, so a redeployed single-file
    shell is picked up on the next launch (revalidates against FileResponse's ETag; unchanged → 304).

89. Activity events now carry a `subject` ("what is it": cat / person / bird / unrecognized / motion_only)
    beside `identity` ("which cat"), so a false-motion trigger, a human, and an unnameable subject each get
    a distinct chip instead of all collapsing to one blank "no chip" card. `yolo-serial` broadened to detect
    person(0)+bird(14)+cat(15); verdict/score stay CAT-ONLY so the motion-gate scorecard's "verdict=1 ⇒ cat"
    contract is unchanged; detail boxes gain a 6th class element (legacy 5-elem rows read as cat).
    The floor splitting `unrecognized` (cat-scale motion YOLO couldn't name — worth a look) from `motion_only`
    (below it — likely noise) is LEARNED from labelled cat visits' motion, stamped on `model_versions.metrics`
    at gallery-build; a conservative default applies pre-calibration.
    A confident NAMED gallery match promotes the subject to `cat` even below the 0.3 detection floor, so a
    low-confidence resident is never hidden behind a motion chip (an unknown/far match is not promoted —
    phantom-safe). Read-time + additive: event clustering, the identify path, and the batched `yolo` oracle
    are untouched. Spec: docs/specs/2026-07-22-event-subject-classification.md.

90. Admin Activity page gains an **Analyze** button (left of Identify) that enqueues a `yolo-serial`
    re-detection (`reanalyze=true`) over the shown date window, so historical events backfill their
    person/bird/cat subjects — the DETECT step, vs Identify's MATCH step. `reanalyze` is required
    because a plain sweep skips already-analyzed frames; the old cat-only rows must be cleared and
    re-detected by the broadened detector. Reuses `/api/analysis/run` + the window resolver; progress
    lives on Sweeps (the button reflects a running yolo-serial sweep). Forward path stays the live
    worker; this is the manual backfill for frames scored before the detector was broadened.

91. Activity **Analyze** button is now tight + fast (buckets/Sweeps stay the breadth tool). It scopes to
    the LOADED events' bounding id-span (min start_id .. max end_id), not the whole date window, and sends
    a new opt-in `motion_only` so the sweep skips the non-motion majority (~95% at continuous capture).
    `motion_only` threads through `/api/analysis/run` → `enqueue_named` → `run_analysis` →
    `iter_unanalyzed`/`count_unanalyzed` (add `frames.motion=1`); it's in the job dedup key so a tight vs
    full sweep of the same window don't collide. The `reanalyze` clear is motion-scoped under `motion_only`,
    so the tight button re-detects the visits' motion frames WITHOUT wiping non-motion verdicts a breadth
    sweep produced. Default off everywhere → every existing sweep path is byte-identical.

92. User Activity feed gained a "Show all" toggle beside "Hide our cats" (default off): off hides the two
    low-signal subject kinds — `unrecognized` (cat-scale motion the detector couldn't name) and
    `motion_only` (below-floor noise); on shows them. Any real subject (a cat named or not, person, bird)
    always shows, so the default feed stays useful before a gallery is promoted (every cat is unidentified).
    Client-only; the two toggles compose. Empty-state hint names whichever toggle would reveal something.
