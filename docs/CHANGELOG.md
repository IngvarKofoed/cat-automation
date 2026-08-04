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

93. Root CLAUDE.md review mandate reframed to match reality: the per-edit pass is the agent's OWN self-run
    review (single read for small edits; Agent-tool subagent fan-out with verified findings for big ones),
    NOT an invocation of the `/code-review` skill — that skill is user-invoke-only (`disable-model-invocation`),
    so the old "invoke code-review --fix" mandate silently failed every edit. Grouped-severity reporting and
    the user-run `/code-review medium` nudge are unchanged. Also: the ultracode section now notes a returning
    workflow's aggregate diff triggers that same self-review (no single subagent saw the whole change).

94. Edge now survives a camera-grab stall instead of leaking itself to death. A wedged CSI read froze
    `frame_id`, and each compute `/stream` reconnect then leaked a Werkzeug handler+FD until the process died.
    Now `/stream` sheds its handler after `CAT_EDGE_STREAM_STALL_S` (15s) idle, and a new watchdog `os._exit`s
    a frozen grabber (70=wedge, 71=never-got-a-frame) for systemd to respawn (new `deploy/cat-edge.service`,
    `Restart=always`); fires only while `Grabber.is_running()`. Spec: docs/specs/2026-07-23-edge-grab-stall-recovery.md.

95. Edge grab failures + liveness are now visible in journald (were only in the invisible `last_error`): the
    grabber logs the first failure + throttled repeats + recovery, the watchdog logs a 30s heartbeat, and
    routine `GET /stream` 200 access lines are filtered so a reconnect storm can't bury them. New `README.md`
    documents running the edge (bare + systemd), the reliability env knobs, and reading the crash cause from
    the journal / exit code.

96. Shared `MotionGate` skips CSI corruption frames (thin coloured lines + magenta cast) via a per-frame
    BGR-chroma test before MOG2 — no-motion, background unpoisoned, debounce streak untouched. `process()`
    now returns `MotionResult(motion, bbox, area, corrupt)`; both tiers get the guard, edge logs suppression.
    Thresholds are conservative fixed `_CORRUPT_*` constants (never suppress a real cat), tuned later on real
    frames. Expected: an offline baseline re-run over pre-guard frames diverges from stored `frames.motion` on corrupt frames. Spec: docs/specs/2026-07-23-corrupt-frame-motion-guard.md.

97. Corruption review page (`#corruption`): a NON-registered persisted `CorruptionAnalyzer` wraps the shared
    guard and is swept via the shared job queue (own `/api/corruption/run`; absent from `ANALYZER_NAMES` so it
    never leaks into scorecard/disagreement/oracle paths). `/api/corruption` reads the stored `corruption` flag
    to filter a range for corrupt, and the fail-non-safe danger set (corrupt AND cat), with staleness (verdicts
    predating a `_CORRUPT_*` change) and cat-coverage warnings so an empty danger set never reads as "safe".
    Caveat: the guard forces corrupt→motion=0, so a motion-only sweep can't find corruption in post-guard
    frames. Spec: docs/specs/2026-07-23-corruption-review-page.md.

98. Corruption page picker + paging reworked: a `datetime-local` START (to the minute) + a preset TIMESPAN
    (15 min … 24 h) set the window; the grid then pages by MINUTES (a 1/2/5 min-per-page selector between
    Prev/Next), each page a resolved time sub-window so the header counts stay range-wide while you scan
    chronologically. Feed is OLDEST-first (`corruption_feed` ORDER BY `id ASC` + forward keyset). Buckets
    picker unchanged.

99. Removed the edge motion-gate corruption guard (entry 96): a corrupt frame can still contain a real cat, so
    filtering corrupt frames out of motion is FAIL-NON-SAFE. `MotionGate.process` reverts to `(motion, bbox,
    area)` — no `MotionResult`/skip, `_is_corrupt` gone. Corruption is now a review-ONLY flag: the
    `classify_corruption`/`corruption_thresholds` detector stays in `shared.motion`, used solely by the
    `CorruptionAnalyzer` sweep + the `#corruption` page. Filtering belongs later at the GALLERY/ID, not the gate.

100. Compute auto-starts live naming at launch when a model has been promoted (an active gallery exists),
     not only when the operator left the intent on — a promoted model means new visits should be named
     without a manual toggle. First-ever enable still seeds the watermark to the frame horizon (names only
     NEW visits; back-identifying history stays the manual Identify pass). No active model → worker idles (no-op).

101. `events()` derives a `corrupted` subject + attaches per-visit detection aggregates — read-time, no
     schema/sweep. `corrupted` (between bird/unrecognized) fires only when YOLO saw NOTHING and any frame in
     the visit's id span is `corruption`-flagged, so a cat in a corrupt frame stays `cat`. `event["detection"]`
     = {ratio, conf_max, conf_mean} over the visit's motion frames, RAW; ratio is null when unswept ("not
     measured") vs 0.0 for a swept miss. Spec: docs/specs/2026-07-23-visit-detection-aggregates.md.

102. Activity feed (`events()`) no longer slows as the store grows: it read EVERY motion frame in history
     and temp-b-tree-sorted them on every call (twice per page load — the feed AND `cats_overview`), so the
     scan cost climbed with the store and inflated further under lock contention with the collector/live
     worker (the 13 s `/api/events`, 49 s `/api/cats/overview`). Now it scans only the newest
     `_EVENT_SCAN_FRAMES` (200k) motion frames — the feed's newest events live in the recv_ts tail — via a
     DESC+LIMIT read served in-order by new index `idx_frames_motion_recv (motion, recv_ts)`, no sort,
     bounded regardless of store size. A scan-capped window drops its oldest (possibly-partial) cluster and
     sets `truncated`. Existing stores build the index on next open.

103. `resolve_ts_range` (clock→id, run before every windowed read incl. the admin Activity page) no longer
     scans the whole store. `MIN(id) WHERE recv_ts >= ?` could NOT use the recv_ts index — SQLite walked the
     rowid from id=1 to the first match, so a recent "from" bound scanned almost every frame and grew with
     the store (~285 ms at 1.5M frames). Rewritten as an indexed `ORDER BY recv_ts … LIMIT 1` seek
     (idx_frames_recv_ts), O(log n) (~0.06 ms). Exactly equivalent because recv_ts is non-decreasing with id
     (frames added in receive order), so the first frame by recv_ts is the min id in range.

104. `stats()` no longer scans the whole store. It read `COUNT(*), SUM(motion) FROM frames` — an O(store)
     full scan — and the dashboard polls it every ~4 s, so that recurring scan held the shared lock and
     starved the collector and tuning sweeps as the store grew. Frame/motion counts are now kept in memory
     (`_count`/`_motion_count`), seeded once at open and maintained in lockstep by add/evict/clear exactly
     like `_total_bytes`; the recv_ts span is two O(1) index-endpoint seeks. stats() is now O(1), no scan.

105. Windowed analysis sweeps (MOG2/BSUV tuning) batch their verdict writes instead of committing per frame.
     Per-frame `commit` made the sweep grab the store's shared write lock once per frame, each contending
     with the collector's continuous inserts — the dominant cost behind a tuning sweep crawling at ~0.4 fps
     (decode+MOG2 alone run ~100 fps; the gap was lock contention, not compute). Now verdicts accumulate and
     flush every `_WRITE_BATCH` (256) in one lock hold + commit, plus a final/cancel flush. Safe because a
     windowed sweep revisits every frame each run, so verdicts lost to a cancel before their flush are
     recomputed next run. Inference stays strictly in-order (MOG2/BSUV are stateful).

106. Admin Activity cards now surface the entry-101 per-visit stats: a quiet mono line under each
     caption shows YOLO's detection RATE over the visit's motion frames (a recall proxy) plus PEAK
     and MEAN detection confidence, from `event["detection"]`. `—` marks not-measured (span unswept)
     vs `0%` for a swept miss — the same honest distinction the backend records.
     Also renders the `corrupted` subject chip (emitted by the backend since entry 101 but never
     shown — a corrupted event drew no chip); it takes the corruption purple, so a glitch-explained
     visit never reads as a caught/missed verdict. Presentation only; user dashboard unchanged.

107. Admin playback modal gained a per-event "Re-analyze frames" button: re-detects just the OPEN
     event's motion frames with yolo-serial (reanalyze + motion_only, scoped to its [start_id, end_id]),
     so one visit whose subject looks wrong is re-detected without re-sweeping the whole loaded block.
     Shares the yolo-serial queue + running state with the whole-events Analyze button (both disable
     while a serial sweep runs); feedback lands in the modal, progress on Sweeps.

108. User dashboard playback modal shows the per-visit YOLO detection aggregates below the filmstrip
     — rate / peak / mean, one per line, values right-aligned, all as % (same `event["detection"]` and
     `—`=not-measured distinction as the admin cards, entry 106), refreshed on open + Prev/Next hop.
     Also fixed the scrub slider overflowing on a narrow phone (`flex:1` lacked `min-width:0`, so it stayed
     wide and shoved the frame count off the dialog edge). Presentation only.

109. Frontend dev proxy no longer wedges or ignores Ctrl-C: a BLOCKING `requests` call in an async handler,
     buffering the dashboard's endless SSE stream (`/api/events/stream`), parked the event loop (stuck in a
     C-level read, so the interrupt never fired); no pooling also churned a TCP handshake per `/media` image.
     Now a pooled `httpx.AsyncClient` that STREAMS responses, plus uvicorn `timeout_graceful_shutdown=5` so
     one Ctrl-C stops it mid-stream. `httpx` added to compute/requirements.txt (was only transitive).

110. User Activity cards gained a single "visit-health" traffic-light dot beside the pill: the WEAKEST band
     of the visit's three detection aggregates (one strong peak frame shouldn't mask a poor visit). Thresholds
     (%): rate red<30/green≥60, peak red<40/green≥65, mean red<30/green≥55. No dot when unmeasured (`ratio`
     null). Provisional — raw uncalibrated scores, depressed by the top-down view; re-tune vs known-good visits.

111. `GET /api/frames/sample` gains an optional `detections=<analyzer>` param (400 if not in ANALYZER_NAMES,
     count branch only) that attaches each sampled frame's stored per-frame detection for playback overlays:
     `{analyzed, score, box, cls}` — the highest-confidence {cat, person, bird} box (new cv2-free
     `Store._best_detection_box`, distinct from cat-only `_best_box`), or None keys when the swept frame holds
     no such box; `analyzed=false` distinguishes an UN-swept frame from a swept miss. Without the param the
     response is byte-identical `{id, recv_ts, url}` (no extra read), so the density/buckets viewers are
     untouched. Per-frame truth (not the visit `subject` chip) so a `person` box can show during a `cat` visit.
     The API/data layer; the user+admin rendering that consumes it is entry 112. Spec: docs/specs/2026-07-24-playback-yolo-boxes.md.

112. Activity playback (user + admin) now renders per-frame YOLO detections: each filmstrip tile carries
     a bottom confidence bar (red<40/amber/green≥65, reusing visit-health thresholds), and the played stage
     frame draws the detected box (highest-conf cat/person/bird) with caption. Fed by `detections=yolo-serial`
     param on `/api/frames/sample` (read-time; zero overhead without it). Box is per-frame truth, not
     per-visit subject chip — a person box can appear mid-cat visit. Admin/user dashboards each own a
     `bandOf` copy so they drift independently if re-tuned. Presentation only; all payload in entry 111.

113. Playback filmstrip now separates YOLO "ran but found nothing" from "never swept": a grey bar
     (`band-none`, `analyzed=true` + no box) marks a real detection MISS, while an unswept frame
     (`analyzed=false`) still shows NO bar. A bar now means "YOLO ran here" — colour = confidence,
     grey = ran-but-empty. Previously both cases were blank, hiding a genuine miss among the (majority)
     not-measured non-motion frames. Frontend-only (entry 111's payload already carried `analyzed`);
     user + admin. Bars gained a confidence / "no detection" title tooltip.

114. First edge actuator: manual GPIO output switches in the config UI (`GET /api/gpio`,
     `POST /api/gpio/<name>` with `{high}`). New `edge/actuators/gpio.py` — a light on BCM 27, a spare
     relay on BCM 17 — drives raw pin LEVEL (HIGH/LOW), NOT "on/off": relay boards differ on active-
     high vs -low, so the operator maps level→behavior at the wiring. State is NOT persisted (pins
     init LOW on boot — the safe/neutral default).
     Backend pluggable like `CaptureSource`: real `GpioZeroBackend` lazily imports `gpiozero`
     (apt/system-site-packages, absent off a Pi), so `available:false` there and `set()` refuses
     (503) rather than faking actuation; the UI disables the switches with a note. Distinct from the
     deferred intent-based Control API (lock/unlock/sound/light) — this is a hardware bring-up tool.

115. GPIO outputs changed to match the actual door wiring: three relay channels on BCM 26/20/21
     — `channel1` (26), `channel2` (20), `channel3` (21) — replacing the earlier `light` (27) /
     `aux` (17). Backend/API/UI unchanged (all data-driven off `GPIO_OUTPUTS`); a Pi picks up the
     new pins on the next edge restart, releasing 17/27. Still raw HIGH/LOW level control.

116. GPIO channels now boot HIGH, not LOW (new `_INITIAL_HIGH` — the one source both the tracked
     state and the backend's `OutputDevice(initial_value=)` read, so they can't drift). The door's
     relay board is active-low, so HIGH = released/off: idling HIGH keeps relays de-energized at
     startup instead of switching them on. `/api/gpio` and the UI report HIGH from first load.

117. Swapped the pins behind `channel2`/`channel3` to match the wiring: now `channel2`→GPIO 21,
     `channel3`→GPIO 20 (`channel1`→26 unchanged).

118. Night-light scheduler (edge-side): drives GPIO channel LOW from sunset−30min to sunrise+30min to power
     a lamp; sun times computed offline via `astral` (no network). Survives compute-PC outages and
     preserves manual switch flips (write-on-change). Configured in the config UI (channel, offsets,
     location); no-ops off a Pi. Edge-side because fixed camera illumination must survive an outage,
     diverging from the deferred intent-based Control API (lock/sound/deterrent-light).

119. Config UI gained the Night light panel promised by entry 118: enable checkbox, channel dropdown
     (sourced from `/api/gpio`'s output names), on/off minute offsets, latitude/longitude, and a Save
     button that round-trips through `GET`/`POST /api/night-light`. Status line (on/off, next change,
     sunrise/sunset) renders server UTC instants in the browser's local time. Disables + notes when
     `available:false` (no GPIO backend), mirroring the existing GPIO-switches note.

120. Night-light offsets now accept NEGATIVE minutes (range widened to −240..240): a negative value
     shifts the transition the other way — lamp on AFTER sunset / off BEFORE sunrise, a narrower
     on-window — vs the prior 0..240 which only ever widened coverage (on before dark / off after light).
     Validation + UI min-bound only; the schedule math already handled negative timedeltas.

121. admin-next scaffold (Wave 0 / P0 of the admin redesign): new `GET /admin-next` route + a
     self-contained dark shell (own CSS/JS, entry-80 convention) hash-routing six stub pages
     (Start · Motion tuning · Frame review · Annotation · Model building · Activity). Built in
     PARALLEL to `/admin`, which stays untouched until a final flip swaps the two FileResponse paths —
     so the old workbench keeps working through the whole rebuild. Pages are filled in per phase.
     Plan: docs/NEW_ADMIN_PLAN.md; spec: docs/specs/2026-07-25-admin-next-redesign.md.

122. Compute-side location (lat/lon) setting gating the day/night split (admin-next P1): `Store.get_location`
     /`set_location` persist to the settings KV; `GET`/`POST /api/location`. Seeds ONCE from the edge's
     night-light config when unset — any failure / unreachable / out-of-range leaves it unset, never a
     silent (0,0). POST validates range and rejects booleans (pydantic's bool-is-int coercion) and
     NaN/Infinity; a new app-wide 422 handler sanitizes non-finite floats that would otherwise 500 the
     response encoder. Non-finite stored values also degrade to "unset".

123. Day/night tuning-scorecard split (admin-next P2): new `compute/analysis/suntimes.py` computes
     sunrise/sunset offline via `astral` (new compute dep, lazy-imported). `Store.gate_scorecard` gains an
     optional per-VISIT day/night split (bucketed by each visit's FIRST present frame; warm-up applied
     once); `GET /api/tuning/compare?split=1` splits when a location is set, else reports it unavailable
     (never guesses a boundary). The classifier keys day/night off the nearest sun EVENT, robust at any
     longitude — a single UTC date's (sunrise,sunset) pair mis-classifies far-from-Copenhagen longitudes.

124. Identity read API (admin-next P3): `GET /api/frames/sample?identify=1` attaches each frame's
     nearest-gallery match {cat_id, name, is_resident, distance, resolved} for Frame-review overlays,
     reusing the active model's read-time threshold and uncalibrated fail-safe (an uncalibrated model
     resolves every frame to "unknown"). Additive — byte-identical without the flag.

125. Annotation backend (admin-next P4): the queue is now bounded/paginated (no whole-store scan); an
     `ignored` `dataset_items` label (no crop) drops an event from the queue reversibly via the existing
     relabel/delete (no new table); per-cat day/night crop coverage; and, with an active model, the queue
     sorts below-threshold matches worst-first by distance (never-identified events after).

126. Cleanup purges (admin-next P7): `CleanupManager` runs two batched, cancelable background jobs that
     release the store lock between batches — drop non-motion frames (through the eviction accounting path,
     never a raw DELETE; a mid-batch error rolls back and resyncs counters so `_count`/`_motion_count`/
     `_total_bytes` can't drift) and sweep orphaned JPEGs (frames media dir only). Never touches
     `dataset_items`/crops/`model_versions`. A non-motion purge records a `purge_spans` marker so later
     scorecards/coverage over that window warn "misses unmeasurable". `/api/cleanup/*`; the reclaim estimate
     counts exactly and approximates bytes (avoids an O(store) SUM scan under the lock).

127. admin-next Start page (Wave 2 P1 frontend) — the first real page in the shell: capture-mode toggle
     (Tuning=keep-all vs Collecting=motion-only), collection controls (start/stop/resume, live-naming,
     clear-all), active-model+threshold readout, store stats, the lat/lon location setting, and the two
     cleanup purges (non-motion + orphan) with estimate/run/progress/cancel — wiring the Wave-1 backend.
     Shell pages are now a mount(view)→teardown model, so a live page polls (3s) and stops its timers on nav.

128. admin-next Motion tuning page (Wave 2 P2 frontend): day scope (resolved to an id window) → YOLO-serial
     coverage + sweep with a live job readout → six MOG2 params seeded from the edge baseline → baseline/
     candidate re-runs → visit-recall scorecards (Live / Baseline / Candidate) with the Day/Night split and a
     copy-to-edge param line. Wires the P2 backend (`/api/tuning/compare?split`, `/api/tuning/rerun`,
     `/api/analysis/*`, `/api/frames/resolve`, `/api/edge/config`).

129. Frontend dev proxy now serves the LOCAL admin-next rebuild at `/admin-next` (beside `/` and `/admin`),
     so its pages iterate on the dev box against the compute PC's live backend. `/api/*` still proxies to
     `CAT_COMPUTE_URL`, so the compute PC must already have the admin-next BACKEND endpoints (pull + restart)
     for the pages to work.

130. admin-next Frame review page (Wave 2 P3 frontend): a time-window frame grid with per-frame YOLO box +
     conf + class and identity overlays drawn from `/api/frames/sample?detections=yolo-serial&identify=1`
     (the P3 read); All / Detected / Identified filters and click-to-enlarge with the box + full caption.
     The model-evaluation surface — a box/identity chip renders only where a frame was swept/identified.

131. admin-next Annotation page (Wave 2 P4 frontend): keyboard-first labelling of the worst-first queue
     (`/api/label/queue`) — 1–9 label a roster cat, u/x unknown/not-a-cat, g ignore, n/p skip, z undo last.
     Crop quality is AUTO-SEEDED from detection score + area ratio (gallery/ok/poor, the old tool's formula),
     so labelling stays fast; rep crop + full-frame-with-box shown per visit, plus roster add-cat and per-cat
     day/night coverage. Deferred: full Labelled-review relabel + manual per-frame quality editing.

132. admin-next Model building page (Wave 2 P5 frontend): build gallery → validate (DINOv2 feasibility probe)
     → promote. Quality checkboxes (gallery-only default) feed `/api/training/gallery/build` +
     `/api/training/feasibility/run`; a shared training-job line polls status; the validation-run list shows
     kNN/AUC/threshold with a report link; the version list promotes/rolls-back, with a night-coverage warning
     (a resident with day crops but zero night crops) confirmed before promote. Not-enough-data + missing-torch handled.

133. admin-next Activity page (Wave 2 P6 frontend) — completes Wave 2: recent visit cards (rep thumbnail +
     identity/subject chip + detection rate) from `/api/events`, double-click to open a filmstrip player
     (play/scrub with per-frame YOLO box overlay), plus manual backfill — Analyze (`/api/analysis/run`
     yolo-serial reanalyze) and Identify (`/api/identify/run`) over the loaded events' span, both handling the
     no-model / no-torch states. All six admin-next pages are now built and browser-verified; only the flip remains.

134. admin-next Start + Motion-tuning polish. Start: Store timestamp tiles span two columns and never wrap
     (dd/mm-yyyy hh:mm on one line); the Location Save button + status note now share a bottom-aligned group
     so the note is centred on the button. Motion tuning: the Day card's id-range readout is replaced by a
     stats panel (Total frames, Events, and YOLO/Baseline/Candidate frame coverage x/y), the day `<select>`
     is themed, "YOLO oracle coverage" → "YOLO coverage" (shows frames-not-yet-swept, a "Run YOLO" button,
     and a new "Rerun all" checkbox → `reanalyze`), and the "present" wording is dropped for "Events".
     Backend: `/api/analysis/coverage` gains an additive `slots` field (mog2:baseline/candidate coverage),
     kept OUT of `oracles`/ANALYZER_NAMES so the re-run slots never leak into oracle-selection paths.

135. Motion-tuning "Day" is now a compact 4-week, Monday-first calendar (oldest week top, current week
     bottom) that doubles as the day picker — each cell shows the day's events + Y/B/C sweep-coverage %;
     the selected day drives the tiles (all/uncapped events, % swept on YOLO/baseline/candidate) and sweeps.
     New `GET /api/tuning/calendar` + `Store.tuning_calendar` return per-LOCAL-day frame/event/coverage
     aggregates, bucketed by a browser-supplied tz offset. It runs on its OWN short-lived WAL read
     connection (NOT the shared write-locked one) so a big-window scan can't stall the collector —
     the starvation class entries 102-105 removed. Cell keys use that SAME fixed offset as the backend,
     so days across a DST boundary don't blank out (only the ~1h-near-midnight bucket edge remains).
     Also: custom-styled checkboxes + removed number-input spinners; "seeded from edge (edge)" text fixed.

136. Motion-tuning cards gained per-type job queues + per-row Cancel (spec: docs/specs/2026-07-26-tuning-queue-views.md).
     Each AnalysisManager job now carries a stable `job_id` and UI descriptors — `category` ("mog2" vs "coverage"),
     `since_ts`, and a frame-count `total` estimate — all computed at enqueue OFF the manager lock; `/api/analysis/status`
     reports them on the running + each queued job. New `cancel(job_id)` + `POST /api/analysis/cancel/{job_id}` cancels the
     running job OR drops one specific pending job (serial-drain + FIFO invariant intact). Frontend: a shared, manager-agnostic
     `renderQueue` table (Status · Name · % · x/y frames · FPS · Time to complete · Cancel) per card, filtered by category —
     coverage runs under "YOLO coverage", MOG2 reruns under "MOG2 candidate params", both VIEWS of the one serial FIFO (never
     parallel). Only the running row shows FPS + a finish ETA (ported `/admin` etaAnchor); queued rows show neither, so no
     borrowed cross-type rate is ever displayed. `count_in_range` gained a `motion_only` filter for the reanalyze estimate.

137. Fixed admin-next Run baseline/candidate (400). The buttons sent the PREFIXED analyzer name (`mog2:candidate`) as the
     `/api/tuning/rerun` `slot`, which `MogAnalyzer` rejects — that field is the BARE slot (`candidate`) and the analyzer adds
     the `mog2:` prefix itself. Now sends the bare slot, matching the old `/admin`. Pre-existing since the Wave 2 scaffold, so
     MOG2 re-runs never worked from admin-next until now.

138. Always-on YOLO-oracle worker keeps FULL-coverage (motion + non-motion) `yolo-serial` verdicts pre-computed, so a
     motion-tuning scorecard no longer waits on a per-day manual sweep. New `YoloOracleManager` ticks every 30s over the
     un-swept tail through the SAME `run_analysis` path (verdicts byte-identical to a manual sweep), in 256-frame chunks
     re-checking `is_busy` between them — `run_analysis` honors only `stop_event`, so a chunk boundary is the ONLY place a
     mid-tick operator job can win. Detect-only, so it needs no promoted gallery. Toggle: Start page "YOLO all".
     Spec: docs/specs/2026-07-26-yolo-oracle-worker.md.

139. The two always-on YOLO loops (oracle + live-naming) deliberately do NOT yield to each other — only to manual jobs.
     Safe because a same-frame detect is idempotent (`analysis` is `INSERT OR REPLACE` on `(frame_id, analyzer)`, all writes
     serialized on the store lock), so an overlap wastes a little GPU and never corrupts. Feeding the oracle's `running` into
     live-identify's `is_busy` would have been WRONG, not just conservative: `running` is an INTENT flag (true whenever the
     toggle is on), so it would have suppressed naming for as long as the oracle was enabled.

140. Worker on/off intent now survives a clean restart: `stop()` gained `persist` (default True) and the shutdown hook passes
     `persist=False`. The hook's `stop()` previously wrote the OFF intent on every clean exit, so the launch-time `restore`
     could never fire — fully exposed by the new oracle, latent in `live_identify` (masked by its
     auto-start-when-a-model-is-promoted clause). An operator stop still remembers the off.

141. `/api/clear` re-seeds both workers' frame watermarks to the post-wipe horizon. `clear()` keeps the settings KV while
     frame rowids restart at 1, so a pre-wipe watermark sat AHEAD of every new frame and the worker looked enabled while
     covering nothing. The idempotent resume queries can't save this — they are only consulted INSIDE the watermark's window.

142. Oracle coverage is forward-only by construction: first enable seeds the watermark to the frame horizon (a full back-sweep
     would hold the GPU for hours), and it fills only MISSING verdicts. So earlier days still need a manual sweep, and a
     broadened detector still needs a manual `reanalyze`. It also idles under motion-only capture, where the non-motion
     frames a gate miss lives in are never stored.

143. Both always-on workers now self-heal from a frame-id REGRESSION, which previously stranded them silently.
     `frames.id` is INTEGER PRIMARY KEY with NO AUTOINCREMENT, so SQLite REUSES rowids once the max row is deleted —
     and the non-motion purge deletes THROUGH the current max id, almost always a non-motion frame at ~5 fps. A
     watermark left above the horizon made every later tick a no-op: hours of zero oracle coverage, and (user-visible)
     the Activity feed silently stopping naming. A tick now clamps `watermark > latest_id()` down and logs it.

144. `reset_watermark` is epoch-guarded and no longer consumes the first-enable seed. `/api/clear` does NOT stop the
     workers, so a tick in flight used to write its pre-wipe watermark back over the reset — undoing it and stranding
     the worker exactly as the reset exists to prevent. It is also now a NO-OP on a never-enabled worker: writing the
     key there consumed `_seed_horizon` permanently (it is re-derived as "no persisted key"), so clear → collect a day →
     first enable would back-sweep the whole store — the hours-long GPU hold the seed exists to prevent.

145. Start-page "YOLO all" UX hardening: a sticky `last_error` is appended as history rather than replacing the state
     line (branching on it first hid "Idle"/"Sweeping" for the rest of the process's life after one transient fault);
     the toggle can always be switched OFF, only ON is blocked in motion-only capture (a control you can't switch off
     is a trap); and the disabled checkbox is dimmed once, not twice — stacked opacities rendered it invisible.

146. Start-page "YOLO all" toggle no longer fights its own 3s poll: an `oracleSubmitting`
     in-flight flag makes `renderStats` skip the checkbox while a start/stop POST is pending.
     The handler's `disabled = true` guard was being unlocked by the very next poll (which
     re-derives `disabled`/`checked` from stale server state), so the double-click race it
     exists to prevent was still reachable, with a visible checkbox flicker.

147. The internal analyzer slug `yolo-serial` is no longer shown to operators: `/admin`'s
     tooltips, help text, empty states, and its oracle-picker label now say "YOLO". The slug
     stays the registry/DB analyzer name (it is the value in `analysis.analyzer`, half of
     `PRIMARY KEY (frame_id, analyzer)`), so this is presentation-only — renaming the
     identifier would orphan every stored verdict without a migration. Deliberately not done.

148. The oracle picker labels the two YOLO personas distinctly — "YOLO" (serial, the trusted
     oracle) vs "YOLO (batched)". Dropping the slug had briefly made both read "YOLO", which
     is worse than the slug: the picker chooses what a visit-recall scorecard is scored
     against, and the batched persona over-detects (entry 54), so an indistinguishable pair
     invites reading a scorecard against the wrong oracle.

149. The always-on YOLO-oracle worker NEVER backfills: every `start` — an operator switch-on and the
     launch-time `restore` — seeds the watermark to the current frame horizon, superseding entry 138/142's
     first-enable-only seed. A re-enable no longer drains the frames captured while it was off (an
     unbounded GPU hold that delays coverage of *today*, the thing the worker exists to keep current).
     Accepted cost: a restart forgets an un-drained tail (e.g. one left by a long manual job) — those
     frames need a manual sweep. Catch-up within a running worker is unchanged.

150. `_seed_watermark` is now the single write path for a watermark that did NOT come from a completed
     sweep (`start`'s horizon seed + `/api/clear`'s `reset_watermark`), so both carry the epoch bump that
     stops an in-flight tick writing its stale derived value back. `_seed_horizon` is gone from the oracle
     — with every start seeding, there is no first-enable-only flag left to accidentally consume.
     `live_identify` keeps its own flag: it back-identifies visit spans, a different contract.

151. admin-next queue tables share one fixed column geometry (`table-layout:fixed` + a `<colgroup>`):
     Name widest, then Frames, then Status, with % done / FPS / ETA narrowest. Auto-widths had sized each
     card's table to its own rows, so the same columns landed in different places per card. "Time to
     complete" is now "ETA". Cleanup's Drop/Sweep lost their `…` and share one width. Presentation only.

152. admin-next Activity now NAMES the cat it identified, and "Hide our cats" actually hides them.
     `events()`'s identity carries no `resolved` key (only the per-frame overlay does), so the
     `id.resolved` test never matched: every visit fell through to its subject chip and the
     resident filter was a silent no-op. A shared `identityKind` derives the kind from
     `cat_id`/`is_resident` when `resolved` is absent, so both feeds agree.

153. Frame review and Activity now render ONE shared overview tile — same fonts, same 12px chips,
     same box colours — so the two grids can't drift. Detection boxes and labels use the USER
     dashboard's traffic light (red <40% · amber <65% · green) and its identity palette, duplicated
     into admin-next per the entry-80 no-shared-CSS convention: change them together.
     The YOLO label distinguishes "no det" (swept, nothing found) from "unswept" (never analysed).
     Chips sit in a WRAPPING row, not pinned to opposite corners, which silently overlapped on a
     narrow tile or a long cat name.

154. Frame review is scoped by a start instant + a 1/5/10/15/30/60-min timespan (the corruption
     page's picker shape) instead of a whole day, so an operator can land on the minute a visit
     happened. The frame-count field is gone; the window's own width bounds the grid.
     It now reports `N of M loaded (sampled)` from the window's real frame total — the sample is
     decimated above 500 and complete below it, and the response alone can't tell the two apart —
     plus how much of the window YOLO has swept, which is what the "unswept" chips reflect.

155. Activity opens a visit on a SINGLE click (matching the user dashboard; the double-click of
     entry 47 stays on the old `/admin`), and its player shows the same three per-visit aggregates
     the user dashboard does — rate / peak / mean, with `—` for not-measured — under a band-coloured
     box whose class + confidence caption sits at the box's own corner.

156. admin-next playback no longer draws a GHOST detection box: `show()` cleared `fb` per frame but
     left `img.onload` pointing at the previous frame's placement closure, so a box-less frame — which
     never reassigns it — had the stale handler fire on its own load and paint the prior frame's box
     under a "no detection" caption. `onload` is now reset before each `src` swap.
     Also: the annotation rep-frame box lost its colour when `.fbox.warn` was replaced by the
     `tl-*` traffic light; it now bands by the rep detection's own score and reuses `placeBoxes`.
     That score is shown as "rep %" beside the visit's "peak %" — the rep can sit far below the
     peak, so otherwise the border colour is a number the operator has no way to read.

157. `GET /api/frames/sample?flags=1` attaches the two per-frame REVIEW markers a grid outlines tiles by:
     `motion` (off `frames.motion`) and a TRI-STATE `corrupt` — True/False from a stored `corruption`
     verdict, `None` where no corruption sweep has reached the frame. `None` is deliberately not False:
     showing unmeasured as clean is the "an empty danger set reads as safe" trap of entry 97.
     Additive — without the flag the payload is byte-identical.

158. `GET /api/tuning/compare` now reports the params BEHIND each column (`params.{live,baseline,candidate}`),
     so a UI shows what a scorecard was produced by instead of whatever is currently typed in the fields —
     those diverge the moment the operator edits after a re-run. All three are normalised to the EDGE
     vocabulary: a slot stores `MotionParams.downscale`, and without the rename a key-for-key diff would
     silently drop `motion_downscale`. A slot that never ran reports null, not the edge config as if it were its own.

159. admin-next Frame review outlines each tile by those markers — blue = the gate saw motion, violet =
     corrupt, and corruption WINS (a corrupt frame's motion verdict isn't trustworthy). The legend says
     an unmarked frame is unmeasured, not proven clean.
     The YOLO pill moved to the tile's lower right, all tile labels are caps, and "unswept" is now
     "NOT ANALYSED" (dimmed and dot-less — the least interesting state, and the one that fills every tile
     of a window YOLO hasn't reached), still distinct from a measured "NO DETECTION".

160. admin-next Activity playback no longer flickers its detection box. It hid the box and re-drew it on
     every frame's load — a hide/show cycle 5× a second. Natural dimensions are now learned ONCE and
     cached (every stored frame shares the camera's post-transform size), so the box is drawn
     synchronously per frame, as the user dashboard already did. Measured: 2 visibility transitions across
     ~15 played frames (the one genuinely box-less frame) instead of one pair per frame.
     This also makes a stale box structurally impossible — the box is drawn from the frame being shown,
     not from a closure the previous frame left on `onload` (entry 156's fix, now unnecessary).

161. admin-next Activity cards trade the "det NN%" pill for the user dashboard's traffic-light circle
     beside the label (same weakest-of-three band; the three numbers stay in the tooltip and in the
     playback stats), and caps their labels.

162. admin-next Motion tuning: MOG2 params are now displayed as "Var threshold", not `var_threshold`, in
     ONE priority order everywhere — fields, per-scorecard readouts, and a new bottom reference card that
     ranks each knob with which way to turn it and what it does (ported from `/admin`'s hints). Keys on the
     wire stay snake_case; only the display changed.
     Each scorecard now ends with the params that produced it, with every value differing from the live
     gate marked amber — so "what did I change" is answerable without diffing two columns by eye. The
     copy-to-edge param line is gone (redundant), stat labels are small-caps, and the headings are
     "Last 4 weeks" / "YOLO" / "Scorecards"; calendar cells spell out YOLO/BASE/CAND now that the
     header carries no legend.

163. Scorecard stat labels are sentence case ("Recall", "Missed") — five short all-caps words
     shouted over the numbers they label. The `.stat .k` column titles keep their caps.

164. A scorecard whose compare response carries NO `params` now says so ("this compute build
     predates the per-column params field; pull + restart it") instead of silently rendering
     nothing, which read as the feature being absent. Distinct from a slot that HAS no recorded
     params because it never re-ran. The case is real, not hypothetical: the dev proxy serves a
     LOCAL admin-next against a REMOTE compute, so the page routinely runs ahead of its backend.

165. Each admin-next scorecard can now SHOW the visits it missed, not just count them:
     `gate_scorecard(missed_visits=True)` returns per-visit records for the wholly-missed spans it
     already clusters, so `len(missed_visits)` IS `visits.wholly_missed` and the two cannot drift.
     Deliberately not `Store.visits`, which judges caught against the LIVE gate only and ignores
     warm-up + `oracle_floor` — under a Baseline/Candidate heading it would list the wrong column's
     misses. `interesting` rows grew `f.id`/`o.score`; the counting paths now read them positionally.
     Spec: docs/specs/2026-07-27-tuning-missed-visits.md.

166. Missed-visit records are CHRONOLOGICAL, not `visits()`' worst-first. One column's panel is open
     at a time, so a stable time order makes toggling Live gate → Candidate a visual diff: rows hold
     their place and a recovered visit simply disappears. Worst-first reshuffles every row per column
     and defeats the comparison the page exists for. The list is also uncapped — a reader must never
     wonder whether they are looking at a sample.

167. `/api/tuning/compare?missed=1` carries `motion_only_spans` alongside the records, and the panel
     banners them even when the list is EMPTY. Across a motion-only or purge span the non-motion
     frames a miss lives in were never stored, so a short list there is an absence of evidence, not
     good recall — the "an empty danger set reads as safe" trap of entries 97/126, whose sharpest
     instance is a page whose whole job is showing what was missed.

168. admin-next tuning: each missed visit's filmstrip loads only when its row scrolls into view
     (`IntersectionObserver`), which is what makes the uncapped list affordable — an eager panel
     would fire one `/api/frames/sample` per miss on open. The strip covers the visit's id SPAN, so
     it is labelled against the span width, not the detection count: `sample_frames` decimates by a
     STRIDE and returns fewer frames than the `count` asked for, so "did it sample?" can't be tested
     against that count.

169. Clicking a frame in a missed-visit filmstrip enlarges it, and the enlarge modal is now ONE
     module-level `openFrameModal` shared with Frame review rather than a second copy — the pages
     differ only in the meta line they pass (Frame review's adds identity + the motion/corrupt
     markers; a tuning strip has just the detection). Escape is one app-level listener now that the
     modal outlives any single view; the Activity player keeps its own, since it also stops playback.

170. A missed visit now says WHY the gate dropped it and which knob to turn. Each record carries its
     frames' MOG2 blob `area` bucketed into the gate's four actual reject paths — `near_zero`,
     `below_min`, `above_max`, `in_band` — with the dominant one as `reason` and a `{param, direction}`
     `fix` (e.g. above_max → raise max_area_fraction, the common top-down "cat fills the ROI" case).
     `in_band` means the area PASSED the band and only the persistence debounce dropped it.

171. That attribution uses each column's OWN area — `f.area` for Live, the slot's `analysis.score` for
     a re-run — so Live and Candidate can name different knobs for the same visit. Buckets are
     per-visit and EXCLUSIVE (they sum to `n_present`), unlike `gate_scorecard`'s window-wide
     `area_buckets` where `near_zero` is a subset of `below_min` and one long visit dominates the
     frame-weighted totals. New `classify_miss` mirrors `MotionGate.process`'s band test.

172. `near_zero` is presented as a hedge, not a prescription: MOG2 finding ~no foreground usually means
     the cat was classified as a SHADOW (`detectShadows=True` marks it grey 127, the 254 threshold
     drops it — a dark cat on a lighter floor), which NO tunable param recovers. Lowering var_threshold
     helps only at the margin, so the UI renders that one fix dashed and says so.

173. Missed-visit review names a BEHIND BACKEND instead of vanishing. FastAPI ignores an unknown
     `missed` query param, so an un-updated compute PC answers 200 with no records — and the button
     simply not rendering reads as "no such feature" (entry 164's lesson, re-learned). Two states now
     say which half is missing: no records at all, and records without the per-frame attribution.
     Routine, not hypothetical — the dev proxy serves a LOCAL admin-next against a REMOTE compute.

174. Frame review shows each frame's MOG2 blob area, so the number behind a motion=1 frame can be
     compared by eye against a still one — how the band thresholds get read off real data.
     `GET /api/frames/sample?flags=1` now attaches `area` (the LIVE gate's `frames.area`, the same
     reading as the edge UI's Area badge), free off the row it already fetched. Additive: absent
     `flags` the payload is byte-identical.

175. The area chip moved to a tile's TOP-RIGHT and is one shared renderer across both grids. It sits
     INSIDE the wrapping `.tchips` row via `margin-left:auto`, NOT pinned to the corner — pinning
     chips to opposite corners is what silently overlapped on a narrow tile or a long cat name before
     entry 153. Frame review renders it WITHOUT a bucket dot: that page holds no min/max thresholds,
     so it states the number and makes no judgement; only the missed-visit panel colours it.

176. The root CLAUDE.md `/code-review` nudge now DEFAULTS TO SILENCE, instead of firing on any
     "significant" change at a hardcoded `medium` — gated on an adjective it fired nearly every
     turn, so the suggestion carried no signal. It fires only on a NAMED trigger (blocker fixed,
     high-consequence surface, large non-mechanical diff, multi-agent diff, uncertainty), must say
     which one, and picks `medium`/`high` by rule. `shared/` is also now described as holding
     cross-tier LOGIC (`MotionGate`, wire format), not contracts alone.

177. A scorecard readout is now ONE LINE per stat — label left, value right, the same shape as the
     param list under it. Laid out as a single wrapping flex row, every gap broke in a 210px tile,
     so Recall/Missed/False/Day/Night ate ten lines with each label stranded above its number.
     Verified at the grid's 210px minimum: no row wraps or overflows. Presentation only.

178. YOLO predict no longer passes `half=`: ultralytics >= 8.4 folded it into `quantize` and warns
     once PER CALL whenever the key is present at ANY value, so the default logged a line per frame.
     FP32 now passes no precision arg (identical — `quantize` defaults to None); FP16 passes
     `quantize=16`, or legacy `half=True` pre-8.4, probed in `prepare()` (ultralytics is unpinned).
     `analysis.detail` keeps its `half` key — the recorded REGIME, and stored rows read `$.half`.
     Not a throughput fix: the warning measured ~4 us, noise against yolo11x@1280 inference.

179. `docs/NOIR_SWAP.md` records what must happen before the NoIR module's motion re-tune.
     Load-bearing item: AWB is never touched today, and its per-frame R/B gains move the
     GRAY MOG2 runs on — so tuning before locking it calibrates against a sliding baseline.
     NoIR is the worst case (no IR-cut: daylight NIR cast varies hourly; night has no colour
     to estimate from). Also: only ONE param set exists, so day/night can't diverge yet;
     night may beat day (co-axial IR hides shadows); `motion_downscale` is a noise knob.

180. Day/night LIGHTING flag: a non-registered `lighting` sweep stores a continuous
     colourfulness statistic per frame (`analysis.score`), and the day/night cutoff is applied
     at READ time from the settings KV — so it can be swept NOW, before the NoIR camera, and
     calibrated afterwards from the recorded distribution with no re-sweep.
     Measured-but-uncalibrated reads `day` + `calibrated:false`; an UNSWEPT frame stays null —
     a sweep that never ran must not present as a measurement.
     Per-channel mean normalisation makes it blind to a constant cast, which a locked white
     balance leaves on every IR frame; the downscale is its OWN constant, never
     `motion_downscale`, so a MOG2 retune can't silently invalidate a saved cutoff.
     Spec: docs/specs/2026-07-27-lighting-flag.md.

181. Motion tuning gained a Lighting card (day-scoped sweep, a coarse histogram to pick the
     cutoff from, the cutoff field) plus LIGHT calendar/day coverage; Frame review shows a
     per-frame chip, dimmed when the label is assumed rather than measured.
     Scorecards gained an All/Day/Night selector that scopes the SCORING — the re-run still
     walks every frame, because MOG2's rolling background cannot survive a filtered input.
     Day + night is an exact partition of All: frames bucket by their own timestamp, visits
     whole by their first present frame (reusing the existing split), so neither drifts.
     Day/Night disable without a location rather than scoring nothing; the selector resets
     to All on every load, since a sticky scoring filter invites misreading a scorecard later.

182. Review hardening of the lighting flag. `POST /api/lighting` now REQUIRES `threshold`
     (nullable) and rejects booleans, so an empty body can't silently wipe a calibrated
     cutoff and `true` can't coerce to a 1.0 one that reclassifies the whole store as night.
     `lighting_histogram` + the new `lighting_staleness` run on their OWN short-lived WAL
     read connections — the operator hits them on every day click, and two unbounded
     `analysis` aggregates under the shared write lock is the starvation class 102-105 removed.
     `gate_scorecard`'s `interesting` present flag is now THREE-valued, matching SQL: a
     positive verdict with a NULL score under an `oracle_floor` is UNKNOWN and counts on
     neither side, so the Python recount can no longer invent false triggers and break the
     day + night == All partition. A scoped card also drops the unscoped `split` key.
     Staleness is matched with `json_extract`, not a LIKE — `"version": 1` is a substring
     of `"version": 10`.

183. The Day/Night selector names a BEHIND BACKEND. FastAPI ignores an unknown `regime`
     param, so an un-updated compute answers 200 with UNSCOPED numbers while the Night
     button sits selected; the page now detects the missing `regime` echo and says so
     instead of letting the selector look like it worked. Routine, not hypothetical —
     the dev proxy serves a LOCAL admin-next against a REMOTE compute.

184. Recorded a decision the NoIR swap forces: `cats`/`dataset_items` survive `clear()` by
     design, so wiping frames to "start fresh" LEAVES the old labels — and a later
     gallery-build silently mixes crops from two different sensors, whose daylight colour
     differs. Worse than ordinary bad crops, since the shift correlates with capture date
     rather than with the cat. Keep them for threshold tuning only, or retire them — but
     decide before the first build, when the mix is still visible. In docs/NOIR_SWAP.md.

185. Edge can LOCK white balance and select a libcamera tuning file — the NoIR swap's
     prerequisite (docs/NOIR_SWAP.md item 1). New `awb_gains` (null = auto, [r,b] = locked)
     and `tuning_file` settings, `POST /api/awb/lock` (settle ~10 frames, lock what AWB
     converged on, persist), and `awb` in `GET /api/capabilities`. Modelled on `focus`.
     Why it matters beyond colour accuracy: auto WB re-estimates every frame, so a STATIC
     scene's pixels drift — moving the day/night colourfulness statistic and smearing the
     day colour cue. On a NoIR sensor daylight's NIR component makes that drift hourly.
     Tuning file is applied BEFORE gains everywhere (boot, POST): it rebuilds the camera,
     so gains pushed first would be discarded — and it is re-pushed after, so a reopen
     never silently reverts a lock to auto. A bad tuning name falls back + logs, never
     bricking capture. Both re-applied on a self-heal reopen.

186. Fixed a pre-existing config-UI bug the AWB panel would have doubled: `rotButtons`
     selected `.rot-btn`, which is also the shared STYLING for the focus (and now white
     balance) "Auto" buttons. Clicking Auto therefore fired the rotation handler with
     `Number(undefined)` = NaN → serialized `null` → a spurious 400 beside the real
     request, and the active-state sync cleared the button's own highlight. Now scoped
     to `.rot-btn[data-deg]`.

187. The flip (NEW_ADMIN_PLAN.md P8): `/admin` now serves the admin-next rebuild, and the
     workbench it replaced answers `/admin-old`. Kept, not deleted — buckets and corruption
     review have no admin-next equivalent, so it is a working fallback, not only a rollback.
     `/admin-next` stays an alias (same page) so a bookmark still resolves; the dev proxy
     mirrors the mapping. Directories keep their build-time names: renaming `admin-next/`
     would churn every spec reference, and `admin/` is deleted outright when it is retired.

188. New admin `#cats` page owns the roster: rename, resident/foreign, retire, and a `notes`
     field (the schema column existed and was never written). Edits go through the existing
     `PATCH /api/cats/{id}`, which had no caller — a rename used to need SQL. Reads reuse
     `/api/cats/overview` + `/api/label/regime-coverage`; no new endpoint.
     Annotation keeps only the digit picker; add-cat and the day/night coverage card moved.
     Spec: docs/specs/2026-07-28-cats-roster-page.md.

189. "Retire" now means something — it had no working consumer. Retired cats leave the
     annotation picker, the user dashboard's Cats view, and (via `labeled_crops(active_only=)`)
     gallery build + the feasibility probe. Labels and crops are never touched, so it is
     reversible; identification only changes on the next build+promote, never from the
     checkbox itself. Consequence: retiring shifts the 1-9 digit bindings after that cat.

190. The user dashboard's retired-cat filter never worked: it tested `c.active !== 0` against
     a JSON **boolean**, and `false !== 0` is true. Latent until now only because nothing
     could set `active` false. Fixed to a truthiness test.

191. `count_identified_crops` gained the same `active_only` flag and the build/probe
     endpoints + CLI pass it. It is the PRE-CHECK guarding those jobs, so counting retired
     cats the job then filters out would wave through a build that returns
     `insufficient_labels` — the guard must count exactly what the work embeds.

192. `labeled_crops(active_only=True)` is NULL-safe (`d.cat_id IS NULL OR c.active = 1`).
     The join is a LEFT JOIN, so a catless kind (`unknown_cat`) has a NULL `c.active`; a bare
     `c.active = 1` would silently drop every catless crop, which is not a retired cat's crop.
     Default stays `False`, so the two opt-in callers are explicit and greppable.

193. Roster rows set a cat's photo — a Photo column with a round thumb (falling back to the
     name's initial) over a visible SET / CHANGE caption, both opening the file picker.
     The caption is load-bearing, not decoration: a bare clickable circle on a dense table
     does not read as a control, and the first build (thumb + tooltip only) was reported as
     having no upload at all. Reverses the spec's "no avatar management here" non-goal —
     when renaming and retiring rows, the photo is how you tell which cat a row is.

194. `/api/cats/overview` gained `avatar_uploaded`, distinct from `has_avatar` (true for the
     AUTO labelled-crop fallback too). Only an uploaded override can be deleted, so a remove
     control needs to know which of the two it is showing — otherwise it offers to remove a
     crop-derived photo and silently does nothing.

195. The roster duplicates the user dashboard's client-side EXIF normalisation before
     uploading an avatar. Not incidental: the server's cv2 re-encode DROPS the EXIF tag, so a
     phone photo POSTed raw lands permanently sideways. Duplicated per the no-shared-JS
     convention — the two front doors must both carry it or they disagree on the same upload.

196. Admin `#annotate` gained a Labelled mode: review decided visits newest-first, filter by label
     (incl. `ignored`), re-label with the same 1-9/u/x keys, or `d` one back to the queue. Fixing a
     mislabel no longer means the retired `/admin-old` — it closes P4's deferred relabel item.
     A re-label now RE-SEEDS each crop's quality grade with the queue's own formula; passing the
     stored grade through left a not-a-cat correction ungraded, so every gallery build skipped it.

197. `Store.labeled_visits` moved to its own short-lived WAL read connection, and returns per-frame
     `score` + per-visit `peak_area`/`peak_score` (the re-seed inputs above). Unbounded, it walks
     every positive oracle verdict probing `dataset_items` per row — holding the shared write lock
     for that is the collector starvation entries 102-105 removed, and the live admin now calls it.

198. New `compute/tools/ir_lamp_timeline.py` plots a night's mean luma + colourfulness with sun
     events, and spectrally tests the luma series for a RIPPLE: thermal shutdown cycles over
     minutes, foldback settles flat, and in a dark scene the two look identical by eye.
     Periods are capped to those the window holds 4+ cycles of — else leftover trend reads as a
     confident ripple. Reads on its OWN connection, never the write-locked `Store` one.

199. Diagnosed (not yet fixed): the IR illuminator collapses to a few percent output at civil
     dusk and holds there till dawn, so EVERY night captured so far is near-dark from ~22:15.
     Do not tune night MOG2 params, calibrate the lighting cutoff, or build night IR crops from
     that data — a bad night scorecard reflects a broken lamp, not the gate. Leading cause is
     thermal (3 W emitter, half-enclosed back). Separately, `catpi` runs permanently under-volted.

200. Model building's three readouts are now tables in the sweep-queue style, not one-liners.
     The running/queued TRAINING jobs render through the shared `renderQueue` — build,
     validation and identify together, since they share one FIFO — with a per-run rate + ETA;
     `renderQueue` gained optional unit-bearing headers ("Items · Rate", the manager counts
     crops not frames) and a `cancelable:false` row. A pending training job carries no job id,
     so only the running row offers Cancel and one "Clear queue" drops the pending — a per-row
     button there would have lied about what it does.
     Validation runs and gallery versions became 8-column tables of the same geometry.

201. The Model page's version + validation lists refresh once per FINISHED job instead of on
     every 3s idle poll (two whole-table reads on the shared store connection, for data that
     cannot change while the manager idles). "Finished" needs BOTH signals: running→idle misses
     a job that completes with the next already promoted behind it, and a changed `result`
     misses a canceled/failed one, which leaves the prior result untouched.

202. The validation-run list says when it is a SLICE ("showing the 8 most recent of N"). It
     always showed 8 of up to 100 with nothing marking the cap, which reads as "these are all
     the runs" — and comparing a gallery against the wrong set of runs is the mistake the page
     exists to prevent.

203. The Model page's Jobs card is the queue and nothing else — no "idle" status line, no Reload
     button (that pairing is the sweep cards' shape, which sit under their own controls). It is
     ALWAYS shown, so "is anything running" has one fixed place to look; with no jobs it carries
     an empty-state line, the convention every other list on the page already uses.

204. `[hidden] { display: none !important }` now backs the whole admin-next shell. The UA's
     `[hidden]` rule is plain `display:none` at losing specificity, so any class setting display
     — `.row{display:flex}` — silently defeated `el.hidden = true`; the Jobs card's Clear-queue
     row stayed visible with an empty queue. Every `hidden` toggle in the file means "gone".

205. A gallery version can now be DELETED — `Store.delete_model_version` + `DELETE
     /api/training/models/{id}`, a Delete button on every non-active row. `model_versions`
     survives eviction and `clear` by design, so it accumulated a row plus a `gallery.npz`
     per build with no way to clear either. The version's `identifications` go too: every
     read is scoped to a `model_version_id` and the id is AUTOINCREMENT (never reused), so
     they are unreachable once the row is gone.
     The ACTIVE version is refused (409) — identification reads it every tick; promote
     another first. Irreversible: no rollback to it, and the visits it named lose their
     names. Labels and crops are never touched, so a rebuild restores it.

206. `delete_model_version` verifies the resolved gallery dir sits under `models_root`
     before the rmtree. `gallery_dir` is a bare basename written by our own builder, but
     this is the one call that recursively deletes a directory NAMED BY A DB COLUMN — a
     hand-edited row must not turn it into "remove any path". Row + identifications are one
     locked txn; the dir goes after the commit, outside the lock (filesystem work must not
     hold the shared write lock), so a failed removal leaves an inert orphan dir rather
     than a row promising a file that is not there.

207. Recorded what "Validate" actually measures, on the page itself: the feasibility probe
     scores the LABELLED CROPS at the chosen grades with a pretrained DINOv2 + kNN — it
     never reads a built gallery. So a run belongs to no model version (`feasibility_runs`
     has no model id, by design), there is nothing to "re-validate", and a run is
     identified by what it measured: grades + crop/cat counts. The UI said only "feasibility
     probe, before promote", which read as "validates the last build".

208. Model building is ordered Validate → Build → Promote, not Build → Validate → Promote.
     Validate reads no gallery, so it is a gate on the DATA — "is this separable enough to
     build from" — answered before a build is spent, not a check on one already made. The old
     order was what made it read as "validates the last build". Validate is labelled optional:
     a build recomputes the same threshold over the crops it actually enrols, so skipping it
     costs nothing but the forecast.

209. ARCHITECTURE's learning-loop section now matches the code: validation is its own step
     BEFORE training (it scores the labelled data, reads no gallery, belongs to no version,
     and is skippable), a built version carries its own threshold + separability from the
     vectors it enrolled, and promotion covers delete + the one-active invariant.
     It had claimed "a new version is validated, then promoted" — per-version validation,
     which does not exist. Recorded as deliberately-not-done so it isn't read as a gap.

210. The gallery-version list labels `n_vectors` as "Crops", in the runs table's
     Grades · Crops · Cats order. The gallery holds exactly ONE vector per enrolled crop
     (`build_gallery` returns the same integer for both), so "Vectors" was the artifact's
     word for a number the operator already knows as crops — and the differing labels hid
     that a version built at the grades a run scored covers (about) the same crops, which is
     the only sense in which a validation run corresponds to a model. Presentation only: the
     wire/DB name stays `n_vectors`, like the `yolo-serial` slug (entry 147).

211. A validation run can be DELETED — `Store.delete_feasibility_run` + `DELETE
     /api/training/feasibility/runs/{id}`, a Delete button per row. Only `prune_feasibility_
     reports` existed, which frees the report DIRS but keeps every metrics row forever; this
     discards one run entirely. No in-use guard is needed (unlike a model version): nothing
     references a run, since validation scores the labelled data, not a gallery. Same
     discipline as the version delete — row in one locked txn, dir after the commit and
     outside the lock, and the resolved dir confirmed under `training_root` before the rmtree.

212. The validation-run "report" link is now a BUTTON — an `<a class="qbtn">`, so it keeps a
     link's affordances (middle-click, copy-link, no JS) while reading as the row's control.
     A pruned report renders a DISABLED Report button, not a "pruned" note: verified to the
     same 57x25 box as the link, so the column keeps one shape and the row still says what it
     would have offered.

213. Admin action buttons now right-align on ONE rule: a row's action goes right, while toggles,
     segmented controls and pickers stay left. Moved to the right rail: Location Save, the lighting
     cutoff Save/Clear, Frame review Load, Add cat, Validate, Build, Scorecards Compare, and
     Activity's Reload (which sat left while Annotation's and Cats' identical Reload sat right).
     Deliberately still left: the collector toggles, Reset to edge baseline, the roster digit picker.

214. That right rail is held by `grow` on the ADJACENT STATUS NOTE, never a bare spacer: flex
     line-breaking uses each item's BASE size, so a full-width note bumps the button onto a second
     line where it left-aligns, while a `grow` note has base size 0 and shrinks instead.
     Consequence for future edits: such a note's tone class must be toggled via classList —
     `className = 'note ok'` drops the `grow` and moves the button (hit and fixed in addCat).
     Location instead uses margin-left:auto, so its bottom-aligned note+button group (entry 134)
     stays right-aligned on the narrow widths where it wraps to its own line.

215. Two admin control bars are now ONE line tall, with their readout on the controls' own band.
     The Lighting cutoff field takes the `.fld.inline` caption (beside the input, not stacked
     above it), so "Night below" no longer reads as a row of its own above Save/Clear.
     Frame review's `store: … → …` note joins Load in a bottom-aligned group so it centres on the
     button: that row IS two lines tall (the Start/Timespan captions stack), and the row's
     `align-items:center` centred the note against the full height, floating it above the inputs.

216. Annotation labelling no longer waits on its own write: a keypress advances to the next visit
     at once and the POST settles behind it. A label crops one JPEG per visit frame — tens of
     frames, seconds under store-lock contention — and awaiting that read as a dropped key, so
     it got pressed twice. Writes stay STRICTLY SERIAL on one chain: a fast labeller must not
     pile parallel crop-writing threads onto the shared lock, and undo must follow its label.

217. The next two visits' rep crop + frame now PREFETCH, so the advance paints from cache instead
     of a blank stage waiting on a fresh server-side decode. `/api/label/crop` therefore answers
     with a short `Cache-Control` (it carries no validator of its own, unlike `/media`, so the
     browser would refetch and the prefetch would buy nothing). Deliberately not `immutable`:
     `frames.id` is reused after a `clear()`, so a (frame_id, box) URL is only stable per session.

218. Rails the optimistic advance needs. A failed write re-queues its visit at the END (never its
     old index — that would move `current()` under the visit on screen) and reports it on a
     persistent error line, since the status is rewritten by the next keypress; `inserted 0` names
     "already labelled" and points at Labelled review. Auto-repeat no longer labels a whole queue,
     and closing the tab mid-save asks first (losing a queued write is fail-safe but silent).

219. Queue renders are MODE-GUARDED. A write settles long after its keypress now, so a chained
     callback could paint the queue over Labelled review — leaving the stage showing one mode's
     visit while the keys dispatch to the other's, which re-labels a visit the operator cannot
     see. `loadQueue` also filters locally-decided frames PER FRAME, not per visit: a returned
     cluster can mix decided frames with fresh ones, which dropping it whole would hide.

220. `POST /api/label` resolves a visit's frames in ONE batched read (`Store.frame_sources`)
     instead of a `frame_recv_ts` + `path_for` pair each: those take the shared write lock, so a
     40-frame visit took it 80 times, every acquisition queueing behind the collector's inserts
     (the contention class entries 102-105 removed). It also closes a small race — the old pair
     could see a frame evicted between the two reads.

221. The old workbench is DELETED, completing NEW_ADMIN_PLAN P8: no `/admin-old` route, no
     `web/admin/`, and no "old admin ↗" link in the appbar. `/admin` (+ the `/admin-next`
     alias) is the only console; there is no rollback target left.
     Its two unported pages went with it BY DECISION, not oversight: buckets (superseded by
     the tuning calendar, entry 135) and corruption review. Their endpoints survive
     (`/api/corruption*`, `/api/groups`, `/api/timeline`, `/api/visits`), so a corruption
     sweep is still curl-able — but it has no UI, which matters if the NoIR module carries the
     IMX708 artifacts over. Rebuild the page from docs/specs/2026-07-23-corruption-review-page.md
     if it does.

222. The household can now MARK a visit for labelling from the user app's playback modal
     (⚑ on the card); admin `#annotate` gained a Flagged mode to work the marked set.
     New `label_flags` table keyed by the EVENT's frame span — span-keyed is what makes a
     `motion_only`/`unrecognized` visit flaggable at all, since with no YOLO box it can
     never enter the annotation queue. Spec: docs/specs/2026-07-30-user-flag-for-labelling.md.

223. A flag is a work item, not output: resolving one deletes its row (no history), and
     `clear()` drops the table since frame ids restart at 1. Eviction does NOT cascade —
     a flag whose frames aged out reads `gone` and waits to be dismissed, because a mark
     that silently disappears hides that the work was lost to retention.

224. Flag identity is span OVERLAP on both sides — the client draws the ⚑ for any flag
     overlapping an event, and `add_label_flag` dedups the same way. An event's motion
     cluster GROWS as later frames arrive, so an exact `(start_id, end_id)` key would mint
     a second flag on a re-tap and leave un-mark with no target. So un-mark takes the SPAN
     (`POST /api/label/flags/unmark`) and clears every overlapping flag.

225. Flagged spans are UNFLOORED, diverging from the queue's `_ANNOTATE_MIN_CONF` (0.3):
     YOLO runs recall-first at 0.15, so a faint 0.2 cat box is a real crop the queue hides.
     The floor keeps phantom empty-scene detections out of the BULK queue (entry 73) — a
     human pointing at one visit is not that case, and a faint crop still grades `poor`,
     so a quality-filtered gallery build skips it anyway.

226. A flagged visit's state is derived from COVERAGE (`n_swept` vs `n_live`), never from a
     verdict merely existing — partial sweeps are the norm (entries 76, 142, 149), and
     calling one `no_detection` would claim the detector rejected frames it never saw.
     Analyse is offered only where coverage is incomplete, and sweeps the span WITHOUT
     `motion_only`: coverage counts every live frame, so a motion-only sweep could never
     clear `partial` on a keep-all store.

227. A flagged decision runs on the annotation page's existing serial `writeChain` and
     clears the flag ONLY when the write recorded ≥1 row. `/api/label/relabel` answers 200
     with `inserted: 0` once a span's frames have evicted — the routine aging path, since
     flags are never pruned — so deleting unconditionally would discard both the mark and
     the decision silently. `d` dismisses the flag ALONE, leaving any label untouched.

228. The user app's flag toggle reports onto the modal readout ONLY while its own event is
     still on screen, and re-enables the button for whatever IS. Prev/Next repoints that
     shared button at another event, so a settling write repainted a lie there — "Marked ✓"
     over an unflagged visit, whose next tap then ADDED a mark the user meant to remove.
     A failed write also restores the flag delta, not a whole snapshot that could undo a
     flag made elsewhere meanwhile.

229. `_resolve_flag` hints `+analyzer` to de-index that term. With no ANALYZE stats SQLite
     preferred `idx_analysis_analyzer_verdict` over the `frame_id` range and scanned every
     verdict for the oracle — O(analysis) PER FLAG, the opposite of the per-span read the
     design chose. Measured at 1.5M verdicts: 65 ms → 0.4 ms, flat in store size.
     The same query shape elsewhere (e.g. `events()`) still carries the mis-plan.

230. Corrected the root CLAUDE.md claim that `code-review`'s verifiers "default to the mid
     model": the built-in script sets NO `model:` anywhere, so every stage runs on the
     session model. A `high` run over this ~1000-line diff spent ~743k subagent tokens and
     hit the weekly limit before ANY finder returned — and reported `findings: []`, which
     reads as "clean" rather than "never ran". Tier the verify fan-out by hand; note the
     script is a per-run copy, and re-tiering a COMPLETED agent voids its resume cache.

231. Gallery build takes an optional PER-CAT CAP (`max_per_cat`, blank = uncapped), because
     the door makes the dataset permanently skewed — a resident crosses many times a day,
     a neighbour visits occasionally — and no amount of further collection fixes a ratio the
     door itself sets. 1-NN matching makes imbalance milder than for a classifier but not
     harmless: the dominant cat's vectors blanket more of the embedding space, and the
     suggested threshold is calibrated from a distribution its pairs dominate.

232. `cap_per_cat` picks best-grade-first (gallery → ok → poor → ungraded) and, where a tier
     overflows the budget, samples it EVENLY OVER TIME rather than from the front: a
     contiguous run of crops is one visit in one light, the least useful thing to fill a
     gallery with. Time order is `src_frame_id` order (ids are assigned on receive), so no
     extra column is needed. It discards no labels — a later build may enrol different crops
     — and can never drop a cat, which is why it runs before the cold-start guard.

233. The annotation queue gained "Hide confident matches" (`uncertain_only`), the filter
     ARCHITECTURE always described but the code never had. Queue membership is UNDECIDED
     (no `dataset_items` row), NOT uncertain — so a good model does not shrink the queue at
     all, and weeks of correctly-named visits bury the few worth attention. `uncertain` was
     already computed per visit and merely PRINTED; it is now a filter, applied server-side
     BEFORE the page cap (after it, confident visits would still eat the 100 slots).
     `hidden_confident` is reported, so a short or empty queue never reads as "nothing left".

234. Gallery-build's job `params` is now the `(qualities, max_per_cat)` PAIR, so the cap is in
     the dedup key: changing only the cap and pressing Build again is genuinely different
     work, and with the cap outside the key the double-click guard silently dropped it.
     The cap also lands in the artifact dir name and on the version row's metrics — with
     `n_vectors` alone, a gallery capped at 40 is indistinguishable from one whose cats only
     ever had 40 crops.

235. Two bugs the browser test caught, both in the new toggle's own wiring: `loadQueue` had no
     sequence guard, so a slow earlier fetch overwrote a newer filtered one and the toggle
     appeared not to work (mount's own load raced it); and leaving focus on the checkbox made
     `onKey` swallow the operator's next label silently, since it ignores keystrokes from
     inputs — so the control now blurs itself. Rather than exempt checkboxes from that guard:
     Space is bound to skip AND toggles a focused checkbox, so it would fire both.

236. The after-edits review now PREFERS `/fix-code --fix` — it rates findings 1-5, has a verifier
     refute each before repairing, and is undoable — keeping the inline self-run pass as fallback:
     a missing skill changes who reviews, never whether. New pre-commit gate: a commit request
     over changes no `--fix` run has seen asks once first, delegated `/git commit` included.
     The end-of-turn nudge now points at a user-run `/fix-code`; it buys DISTANCE, not another tool.

237. `/code-review` is no longer what the nudge suggests: it needs a PR to exist, only posts a
     comment, and on this repo it spent ~743k tokens to report `findings: []` (entry 230).
     Its workflow-script tiering note stays — the user can still run it deliberately.

238. COCO `bird` (14) is DROPPED from `yolo-serial`'s class set, reverting half of entry 89:
     from the top-down door view a bird box was almost never a bird, and its own subject chip
     dressed that noise up as a named subject. `person` (0) stays; verdict/score were always
     cat-only, so no scorecard moves. Such motion now files as `unrecognized`/`motion_only`.

239. Every read path that ACTS on a class dropped bird with it — the subject ladder, the
     per-visit detection aggregates, and `_best_detection_box` (playback/grid overlays) — so a
     LEGACY stored bird box is ignored rather than half-honoured. `_subject_classes` stays
     class-agnostic (it still reports the box); the consumers name what they act on.
     Consequence: a swept bird-only visit now reads as a measured detection MISS (ratio 0.0).

240. Review is now ONE point — the commit gate — not a pass after every non-trivial edit, and the
     end-of-turn user-run `/fix-code` nudge is gone with it. A per-turn pass only ever saw its own
     turn's edits, judged from inside the context that wrote them; nothing read the accumulated
     diff as one change. Carve-out, load-bearing now that review waits: tests, build and each
     subtree's verification workflow still run PER CHANGE. A fan-out's diff is flagged AT the gate.

241. Annotation's Labelled mode gained an EVENT overview above the single-visit stage — a grid of
     the decided visits, each a rep CROP with its label chip. Finding a mislabel is a scanning
     task, and the mode offered only a stepper over one visit's crops, so it read as a frame list.
     The crop, not the frame: top-down a cat is a few percent of the ROI, unreadable at tile size.
     Rebuilds only on a SET change; selection is a class toggle, so stepping never flickers tiles.

242. The Labelled keys were reachable only until you touched the mode's own "Show label" dropdown:
     `onKey` drops any keystroke targeting a SELECT, so focus left there killed n/p AND every
     re-label key — the mode read as having no navigation at all. It now blurs on change (entry
     235's fix, same cause), and Prev/Next are visible buttons rather than hint-line-only keys.

243. Labelled review dropped its per-crop filmstrip — a visit can hold ~100 crops, so the strip
     WAS the page, and no reading is made per crop. The grades it carried are now a meta-line
     tally (`2 gallery / 3 ok / 3 poor`): what this visit contributes to a quality-filtered
     build. Suppressed for `not_cat`/`ignored`, which write no crops to grade. Flagged review
     KEEPS its strip — that span is often undecided and unswept, so seeing its crops is the point.

244. Labelled review is always scoped to ONE label — the "all labels" option is gone and the
     first cat is preselected. A homogeneous grid is what makes a mislabel visible: a cat that
     isn't Mittens leaps out of the Mittens tiles and is just another face in a mixed set.
     The set-wide total survives in the grid's "N of M labelled". A label emptied by a requeue
     drops its option and falls back to the first, since there is no all to fall back to.

245. `requeue` drops the visit it DELETED, located by identity, not whatever `labIdx` points at
     after the await. Navigation was never busy-guarded, and the overview's clickable tiles made
     the race easy: navigating mid-delete spliced a different, still-labelled visit out of the
     list while the deleted one stayed on screen looking fine. `labAll` and `dropFlagged` already
     located by identity — Labelled's `lab` was the one place left trusting a live index.

246. A settling re-label repaints ITS OWN tile (`refreshTileChip(v)`), not the selected one. Same
     root cause: the write lands long after the keypress, so if the operator has moved on, the
     tile they just corrected kept its old cat until a full rebuild — in the grid whose whole
     job is showing labels at a glance.

247. The selected tile is scrolled into view by clamping the grid's own `scrollTop`, never by
     `scrollIntoView`, which walks every scrollable ancestor: with the grid scrolled above the
     viewport each Prev/Next hop scrolled the PAGE up by ~84px, yanking away the detail stage
     being read. Measured, not theorised — the prior comment claimed the opposite.

248. The grade tally is suppressed for `not_cat` and `ignored`, which are committed crop-less
     (quality/bbox/crop_path NULL), so a tally there read "8 ungraded" about crops that were
     never written. Keyed on the label KIND, not on "every grade is null": for a real cat
     labelled before grading existed, "N ungraded" is true and worth showing.

249. Admin Activity lost its Analyze button (entries 90/91) — removed as unused. Re-detecting a
     historical window is now only a Motion-tuning sweep, which is also where its progress always
     showed; Identify stays as Activity's one backfill. `/api/analysis/run`'s `reanalyze` +
     `motion_only` are untouched (the tuning sweeps use both), so this dropped a caller, not a path.

250. User playback steps VISIT to visit: the Prev/Next pair moved out of the frame-controls row
     into the footer beside the flag, as `‹ Newer` / `Older ›` around a `12 / 40` position readout.
     They already hopped visits — sitting among Play/scrub/count they READ as frame stepping, and
     they squeezed the scrub slider to ~40px on a phone (measured 158px after). Below 480px the nav
     takes its own full-width line, arrows at the screen edges.
     Spec: docs/specs/2026-07-31-user-visit-swipe-nav.md.

251. A horizontal swipe on the played frame does the same hop, with the frame tracking the finger
     — so the gesture is self-teaching rather than a silent jump. Swipe left = older, matching the
     newest-first feed. Damped to a third at either end.
     The ease-back is the "that didn't take" cue, so it plays ONLY when the visit doesn't change
     (short drag, refusal at an end, cancel); a committed hop drops the transform instantly, since
     animating it back would read as the gesture bouncing off the swipe that worked.

252. Two rails the swipe needs. A second finger DISARMS the gesture: merely ignoring its
     `touchstart` left the first armed, so a pinch ended with a large `dx` and hopped visits.
     And there is deliberately NO `touch-action` on the stage — `pan-y` would kill pinch-zoom on
     the one image worth zooming; the axis latch calls `preventDefault` only once a drag has
     committed to horizontal, which claims the axis without taking the browser's own gestures.

253. `.stage` gained `min-height: 0` so it is the dialog column's shrinking item. A flex item's
     default `min-height: auto` won't shrink below its aspect-ratio height, so the new footer line
     overflowed `max-height: 94vh` and `overflow: hidden` CLIPPED the nav — the one control that
     must stay reachable. Not a `vh` cap: any value low enough to save a 568px phone also shrinks
     the frame on a tall desktop.

254. The visit-position readout never asserts an absolute it can't support, and there are TWO ways
     it couldn't. It carries `/api/events`' `truncated` flag (the user page dropped it) → `40+` and
     "oldest loaded"; and it hedges to "newest/oldest SHOWN" whenever a toggle is hiding events —
     the common case, since `showAll` defaults off and nav walks the FILTERED list, so a bare
     "newest" is a lie on the default view. "shown" subsumes a capped feed, so it wins over "loaded".

255. The playback dialog's `aria-label` carries the same health + flag sentences the feed rows do,
     via one shared `visitAriaExtras` so the two can't claim different things about one visit. The
     flag fragment is elsewhere called the ONLY signal a visit is marked for a screen reader, and
     the dialog is where it gets toggled.
     Known limit: a hop is still silent to a screen reader — re-labelling an already-open
     `aria-modal` dialog isn't reliably announced and focus stays on the pressed button.

256. `events()` reads its annotations PER EVENT SPAN, not over the events' overall
     [min start_id, max end_id] envelope. That envelope stretches across every frame between
     the newest and oldest event, so under continuous capture ~95% of the rows it fetched
     belonged to no event — materialised, then discarded, all under the shared write lock.
     Measured at 1.5M frames with a full YOLO sweep, on a store with NO `ANALYZE` stats
     (what the real one is): `Store.events` 1415 ms → 54 ms for the same 500 events.

257. `cats_overview` takes a new `with_subject=False` path: it reads only `identity`, and the
     subject/detection annotation it never looked at was the call's most expensive part
     (964 ms → 17 ms). The reuse-the-feed invariant is intact — identity still comes from the
     same code Activity renders, so the two views cannot name a moment differently.

258. Remaining headroom on that feed, deliberately not taken: it still fetches the newest
     `_EVENT_SCAN_FRAMES` (200k) motion frames and clusters them all, to keep only the newest
     500. Costs ~90 ms once a store's motion tail exceeds the cap (~165 ms/call measured at
     540k motion frames); an early-stop after `limit` clusters would fix it, at the price of
     reworking the `truncated`/partial-oldest-cluster semantics.

259. The Activity feed is PAGED, 100 visits per page (`/api/events` gained `limit`), on both
     dashboards. Cost scales with the page, so page 1 went ~54 ms → ~20 ms; 500 was never a
     chosen page size, just the store's `_MAX_EVENTS` cap. Below ~50 the gain flattens against
     the feed's fixed floor, so 100 is the knee.
     `Store.events`'s own default stays wide — `cats_overview` walks the feed for the newest
     event naming each cat, so a 100-event window would shorten every "last seen" it reports.

260. Paging is KEYSET, not offset: a page asks for `until_id` = (oldest loaded start_id − 1),
     so page N costs what page 1 costs (measured equal at 1.5M frames) and pages can neither
     overlap nor skip a visit. `truncated` is the "another page exists" test. The user feed
     auto-loads on scroll, admin has a "Show older" button; both report the loaded count and
     an end-stop, because a list that simply stops looks like one showing everything.

261. Appending a page never disturbs what is already on screen: appended visits are strictly
     OLDER, so they land at the end and every existing index — what a row's `data-index` and
     the player's selection mean — still addresses the same visit. That is why a page may be
     appended with playback OPEN, unlike the poll's full reload, which stays guarded off.

262. Rails the paging needed, each a real failure found in a browser. `IntersectionObserver`
     fires on a CROSSING, so a page that didn't push the sentinel off screen (every visit
     filtered out, or a short page) stalled the chain — it now re-checks by geometry and
     continues. The in-flight flag is cleared UNCONDITIONALLY: skipping it when a page-1
     reload superseded the fetch left it stuck true and blocked paging for the session.
     A superseded older page is DISCARDED (its keyset was computed against the old set).

263. Admin's loaded-count + Show-older state renders BEFORE `renderGrid`'s early returns.
     It describes the LOADED SET, not what the filter left visible — and a grid reading
     "all hidden by the filter" is exactly when an operator wants to pull older visits in,
     so freezing the button there stranded them.

264. The player's Older at the last loaded visit now fetches the next page instead of
     refusing outright, so deep swiping still reaches back. It refuses THIS hop rather than
     awaiting the page: the ease-back is the "that didn't take" cue, and holding the frame
     still mid-gesture would read as a freeze. The note clears itself when the page lands,
     since the hop it explained now works.

265. The per-span reads carry `+analyzer`, DE-INDEXING that term as `_resolve_flag` does.
     Without it entry 256 was a 36x REGRESSION, not a fix: the store runs no `ANALYZE`, so
     with no `sqlite_stat1` SQLite prefers `idx_analysis_analyzer_verdict` on the equality
     and scans the whole yolo-serial partition — once PER SPAN. One 100-event page at 1.5M
     frames: 19.5 s unhinted vs 4.8 ms hinted (the old envelope query was 538 ms).
     Entry 229 predicted exactly this and named `events()` as still carrying it.

266. Measure query-plan work on a store with NO `ANALYZE` stats. A bench that runs `ANALYZE`
     gets a plan the real store never gets, which is how entry 256's numbers came out both
     too optimistic and measured against the wrong plan — the mis-plan above was invisible
     until the same queries were re-timed without stats.

267. Paging rails the browser pass missed, all in the append path. The day-group cursor is
     reset beside `railEl.innerHTML = ''`, not in the non-empty branch the empty path returns
     before reaching — a cursor pointing at a detached rail made an appended page's rows
     invisible while still counted in `activityVisible`, so the position readout and the
     player's nav addressed rows not on screen. Appending also clears the empty-state panel,
     which otherwise kept claiming "nothing to show" above the row just added.

268. A superseded page repaints the foot/button UNCONDITIONALLY, on both dashboards. The
     winning render ran while the in-flight flag was still true, so it painted "Loading
     older…" — and with only the winner repainting, that lie stayed until an unrelated
     re-render. The flag itself was already cleared unconditionally; the paint was not.

269. Two smaller paging repairs: a FAILED page fetch triggered from the player now says
     "older visits failed" in the readout (its error note lives on the page BEHIND the modal,
     so the reader saw only a promise that never resolved); and the keyset bound uses `reduce`
     rather than `Math.min(...spread)`, since paging made those arrays unbounded and a large
     enough spread throws RangeError before the try block, as an invisible unhandled rejection.

270. Admin's Identify NAMES the span it enqueues ("over the N loaded visits"), because paging
     silently narrowed that backfill from ~500 visits to one page of 100. The scope follows the
     loaded set by design — "Show older" widens it — but nothing said so at the point of the click.

271. Known limits of the paged feed, deliberately left. An empty page carrying `truncated:true`
     (reachable when a scan-capped window's sole cluster is popped) stops paging: trusting the
     flag instead would re-request the same `until_id` forever, since no new events move the
     keyset — a real fix needs the backend to expose the scanned window's floor.
     Infinite scroll also turns entry 258's fixed floor into one 200k-frame scan per page, on
     the shared write connection; a short-lived WAL read connection (as `tuning_calendar` and
     `labeled_visits` use) is the fix if it bites. And user-page paging exists only where
     `IntersectionObserver` does, with no button fallback.

272. Day/night stays SUN-TIMES driven and the lighting cutoff is deliberately left unset.
     The IR lamp runs on the edge's astronomical schedule (sunset−1min / sunrise+1min) from
     the SAME lat/lon the compute split uses, so the two agree by construction — the spec's
     premise, an illuminator whose photocell drifts from sun times, is not this wiring.
     And were MOG2 params ever to diverge, the switch must happen LIVE ON THE EDGE, which an
     offline read-time flag could not drive regardless of how accurate it got.

273. Measured over 30 July (707,784 frames, full lighting coverage): colourfulness CANNOT
     separate the regimes at this door. Evening daylight reads 0.088–0.100, at or below IR
     night's 0.100–0.103 — the scene is achromatic in daylight too, so the statistic tracks
     direct sun rather than colour-vs-IR. Mean luma does separate cleanly (night 45.6–47.8,
     day 83.4–98.6) and is ALREADY stored in `analysis.detail`, so switching axis later
     would need no re-sweep. The separation depends on the lamp staying dim: auto-exposure
     is railed at night, so a brighter emitter would erode the gap.

274. The replacement IR lamp holds output — night luma flat at ~46 all night, unlike the
     emitter of entry 199 that collapsed at civil dusk. Night crops are readable (tabby
     striping legible at 360×267 px, YOLO 0.95), so the IR-night regime is viable for the
     gallery, not merely populated. Night luma is therefore also a usable lamp-health
     signal, which is the one job the lighting sweep still earns.

275. Edge `fps` is set to 15 but the Pi only grabs ~10.2; on 30 July it ran 7.7–9.4 with no
     outage. Nothing is lost in transit (compute stored 10.24 against the Pi's 10.19), so the
     Pi is the ceiling — though a stored-rate dip can equally be collector lock contention,
     which frame-id continuity cannot distinguish (ids are assigned at insert, so a frame
     dropped before insert leaves no gap). Matters for tuning: `persistence` counts FRAMES,
     so 2 is a ~250 ms window here, not the ~400 ms it would be at the documented 5 fps.

276. The Motion-tuning calendar ("Last 4 weeks") no longer joins `frames` per verdict, and
     carries `+analyzer` — entries 229/265's mis-plan, third instance: with no ANALYZE stats
     SQLite scanned each analyzer's WHOLE partition and fetched every row. Each day's
     verdicts are now COUNTed over that day's own id span off the `(frame_id, analyzer)` PK,
     a pure covering scan — equivalent because recv_ts is non-decreasing with id and
     `analysis` cascade-deletes with its frame.

277. Measured on a 2.5M-frame / 4.3M-verdict replica of the real store: that pass 2590 →
     880 ms, the whole call 3.0 → 1.4 s (the live endpoint was 4.8 s). Known limit — it is
     still O(store), walking every frame's recv_ts index entry (390 ms) plus every verdict
     in the window on EVERY open, and the store sits at 30% of its 1 TiB cap. Structural
     fixes are maintained per-day counters or a cache; the always-on oracle writes verdicts
     continuously, so a whole-store epoch key would never hit.

278. What entry 276 trades away: `recv_ts` monotonic with `id` is ASSUMED, not enforced —
     the collector stamps the wall clock — so a backward step across a local midnight
     overlaps two days' id spans and the earlier day counts the later one's verdicts.
     It fails LOUDLY, one direction only: coverage reads above 100%, never a silent
     under-report. Left unguarded because clamping to the next day's first id regresses
     the OPPOSITE skew into a fully-swept day reading 0%; only the join is immune.

279. New `unanalyzed` subject rung: a visit whose span carries NO `yolo-serial` row says so,
     instead of `unrecognized` — which claims YOLO looked and found nothing nameable, the
     opposite reading. It outranks `corrupted`, whose contract needs "YOLO detected NOTHING"
     that an unswept span cannot support (entry 226's rule). `swept` is free — `events()`
     already holds the rows — and BINARY: a partly-swept visit reads analysed, with
     `detection.ratio` carrying the nuance. Spec: docs/specs/2026-08-01-unanalysed-visits-analyse-identify.md.

280. `swept` counts rows over the WHOLE span, not its motion frames, matching what a
     span-scoped sweep fills — so it can disagree with `ratio`'s motion-only denominator
     (swept, yet "not measured"). Narrowing it to motion frames would call a span
     unanalyzed that a sweep cannot add one verdict to.

281. New `visit-identify` TrainingManager kind + `POST /api/identify/visit`: detect THEN
     identify over ONE visit span, the pair `LiveIdentifyManager._tick` runs per closed
     visit, on demand for a visit the always-on workers never covered. No "did YOLO find a
     cat" branch — `iter_unidentified` yields only frames with a present verdict, so an
     empty visit self-skips. A cancel mid-detect returns NO summary, which is what records
     it `canceled` rather than `done`.

282. That route diverges from `/api/identify/run` twice, both because DETECT is the half
     that resolves `unanalyzed` and is useful with no gallery: no active model is a SUCCESS
     (detect ran, `identified: false`), and the deps check follows the HALVES — the
     analyzer's always, the embedder's only when a model exists. Both span bounds are
     REQUIRED and width-capped (10k ids): elsewhere a missing bound means "whole store",
     and the household's phone calls this route on a no-auth LAN.

283. Both dashboards gained the per-visit "Analyse this visit" button (playback modal), shown
     ONLY on an `unanalyzed` visit — elsewhere the job fills nothing, and a button that does
     nothing is worse than none. `unanalyzed` is EXEMPT from the user feed's noise filter:
     it is not a low-signal reading but the absence of one, and hiding the visits whose
     button you want tapped defeats the feature.

284. The button's completion watch reads `/api/training/status` for its OWN (kind, span)
     across `running` AND `queue` — matching `running` alone calls a job done while it is
     still queued behind another. It then re-reads JUST that visit's span from `/api/events`
     and patches the event IN PLACE, since object identity is what the rail rows, the player
     and the paging state address a visit by. Closing the tab loses the repaint, never the
     analysis.

285. `activity_signal` deliberately does NOT learn about `analysis`, so a visit analysed to a
     NO-DETECTION result pushes nothing over SSE and updates on the next reload. Adding a
     verdict counter would nudge every connected client every tick — the always-on oracle
     writes continuously.

286. A successful flag toggle is now SILENT — the modal footer gained the analyse button and
     has no room to confirm in words what the button's own label already says. The readout
     is kept for DIVERGENCE only ("Already on the labelling list", "It was not on…"), which
     reports the write doing something other than the tap intended and is shown nowhere else.

287. The user dashboard has NO bare `.hidden` rule — only per-element qualified ones
     (`.note.hidden`, `.modal.hidden`, …) — so `classList.toggle('hidden')` on an element
     without its own rule is a silent NO-OP. The analyse button therefore showed on every
     visit, and clicking it on an already-analysed one enqueued a pointless GPU job.
     Verify visibility by COMPUTED STYLE, never by `classList.contains` — the class was
     present the whole time, which is what made the browser check read as passing.

288. The per-visit analyse watch reads its job's OWN history record, not `status.error`:
     that is one sticky field a later promotion clears and a later failure overwrites, so
     it can report another job's failure as this one's. Leaving the queue is not succeeding
     — a failed or canceled job now says so instead of reporting a result never produced.

289. "Analysing…" is per VISIT, not a global flag. The watch runs up to two minutes and
     Prev/Next stays live throughout, so one flag painted the busy label onto whatever
     visit the reader hopped to. Several analyses may now be in flight; the server queue is
     serial and dedups a repeat of the running span.

290. A job can succeed and still record nothing for its span (frames evicted mid-run), so
     admin only retires the analyse button once the visit actually left `unanalyzed` —
     otherwise the retry control vanished under the line "Analysed: not analysed."

291. Annotation can PLAY the visit it is deciding — click either stage image, the new
     "▶ Play frames" button, or `v`, in Queue, Flagged and Labelled alike. A cat is a few
     percent of the top-down ROI, so the stage's single rep still can leave the coat
     ambiguous; the player runs the whole visit through the same crop-beside-frame pair.
     No fetch: it plays the visit record's OWN frames, i.e. exactly the crops a decision
     labels — not Activity's sampled event span, which includes frames carrying no crop.

292. With the player open the keyboard still labels: a bound key closes it and then does
     its normal thing, so a decision always lands on the visit that was on screen (the
     player only ever plays the staged visit, and nothing navigates without closing).
     Space is the exception — play/pause, since skipping a visit because the operator
     reached for pause is the worse surprise. Any stage repaint closes it, and so does a
     mode SWITCH: that load is async and leaves the outgoing mode's list in place, so a
     key pressed over the player would have re-labelled a stale, already-decided visit.

293. The player transport is now module-level and shared with Activity, which supplies its
     own stage (one full frame + box) and stats as before — so play/pause, scrub, position,
     Escape and backdrop-close can't drift between the two. Each cell is FIXED size —
     the frame cell takes the frame's own aspect ratio — since a crop's box changes shape
     frame to frame and content-sized cells would resize 5× a second.

294. The annotation saving indicator moved out of the control row to a pulsing amber pill
     directly above the stage, in a permanently reserved slot (measured: 0px shift, so the
     images never step under the reader). A label is written BEHIND the operator, so this
     is the only thing saying one is still in flight — and it has to register from the
     crop, where the eye is. Motion is what peripheral vision picks up, not a grey word.

295. It NAMES what is in flight ("Saving Mittens · 11/07-2026 06:14…"), because the visit
     left the screen the instant it was submitted — "saving 3…" cannot answer the question
     the operator actually has, which is whether their Mittens went through. All of them
     are listed in the tooltip, which also says the buttons staying enabled is fine: the
     writes are a queue, not a race.

296. A drained queue now CONFIRMS with a brief green "Saved" instead of the chip merely
     vanishing, which looked identical to one that never appeared. Suppressed when the
     batch held a failure or a short write, so it can never contradict the error line
     those already print.

297. Labelled review's re-label and send-back are on the same indicator. A re-label deletes
     the visit's crop files and cuts them all again, making it the SLOWEST write on the
     page — and being awaited rather than optimistic, it previously left the page looking
     completely idle for seconds. Root cause of the wait, unchanged: `_commit_label`
     decodes + crops + writes one JPEG per visit frame, synchronously in the request.

298. The player's scrub is the page's THIRD focusable control to blur itself after use,
     joining the entry-235/242 pair. `onKey` drops any keystroke targeting an INPUT, so
     focus left on the slider swallowed the operator's next label — with the modal over
     the stage to hide why. Blurs on `change` AND `pointerup`: a click landing on the
     thumb's current position changes no value and fires neither.

299. One `stage` setting (`tuning` | `collecting` | `running`) is now the single intent for
     capture mode and the two always-on workers, replacing the trio of switches
     (`motion_only`, `yolo_oracle`, `live_identify`) an operator assembled by hand.
     `POST /api/stage`; the Start page picker replaces three controls. Makes real the `Mode`
     entity ARCHITECTURE had claimed since day one and the code never had.
     Spec: docs/specs/2026-08-02-operating-stages.md.

300. Fixes the hole that motivated it: between switching to motion-only capture and promoting
     a first gallery, NOTHING detected anything. The detection worker bailed under motion-only
     capture and live-identify bails without an active model, so visits landed `unanalyzed`
     and the annotation queue stayed empty — noticed only when the queue you came to work was
     blank.

301. The always-on YOLO worker's coverage now FOLLOWS what is stored instead of idling: every
     frame under keep-all, motion frames under motion-only. Renamed the DETECTION worker
     (operator-facing only — module, class, settings keys and `analysis.analyzer` unchanged,
     per entry 147), because its verdicts feed event subjects, per-visit aggregates, the queue's
     membership and the identify pass — all of which need only motion frames. Only the gate
     scorecard's MISS column ever needed the non-motion half.

302. Coverage mode is read per TICK, never applied via stop/start, and both workers now restore
     unconditionally on a live app. Load-bearing: every `start` re-seeds the watermark to the
     frame horizon (entries 149/150), so driving a stage change through a restart would
     silently discard the un-drained tail. Operator-visible on the first restart: anyone who
     had "YOLO all" off finds it running — the coverage tuning wanted anyway, but a real
     GPU-load change.

303. Eviction is stage-aware: outside `tuning` it reclaims non-motion frames FIRST, so the same
     disk holds annotatable motion history much further back. `tuning` stays byte-for-byte
     plain oldest-first — note non-motion frames are NOT spared there, merely never
     preferentially targeted. This is the auto-cleanup: driven by disk pressure, so no stage
     change is destructive and a leftover keep-all window is shed gradually, not wiped.

304. That preference strips windows PARTIALLY (motion frames present, non-motion gone), which
     plain oldest-first never did — so it advances a `nonmotion_evicted_through` marker that
     `motion_only_spans` folds in as the prefix `[1, N]`. Without it a scorecard reads
     near-perfect gate recall over frames that were deleted: entries 97/126/167's trap, in the
     one place it would be least visible.

305. Three rails that marker needs. `clear()` RESETS it — the settings KV survives a wipe while
     frame ids restart at 1, so a stale value would banner a fresh store as unmeasurable
     (entries 141/143/144, a fourth direction). It is never written via `set_setting` (which
     takes the store lock `_evict_locked` already holds — a non-reentrant deadlock), riding the
     caller's transaction instead so it can't diverge from the deletes. And `add`'s rollback
     re-reads it, or a discarded write would leave it ahead in memory and REGRESS on restart.

306. Orphaned JPEGs are collected automatically at launch — a file with no `frames` row is
     unreachable by construction, so removing it loses nothing and needs no marker,
     confirmation, or stage awareness. Launch is when they exist: the changelog-42 leak is
     created by a hard power loss.

307. The orphan sweep's referenced-row probe now goes through the `recv_ts` INDEX, not a bare
     `WHERE path = ?` — `frames.path` is unindexed, so that was a FULL TABLE SCAN per candidate
     file: O(files x rows), quadratic in store size. Measured at 20k files / 20k rows:
     11 s → 0.46 s, now walk-bound (≈1 min extrapolated to 2.5M files, vs effectively
     unbounded). `add` composes the name as `<date>/<hour>/<recv_ts_ms>_f<id>.jpg`, so the
     millisecond is recoverable; the `path` equality still runs, so the answer is identical and
     only the plan changed. Entries 229/265/276, fourth instance.

308. That was latent while the sweep was manual and cancelable — entry 306 made it automatic at
     every launch, which would have pegged the store lock for hours and starved the collector on
     the real store. Caught by measuring before upgrading, not by the test suite: correctness was
     never wrong, only the plan. An unparseable filename falls back to the slow scan rather than
     being assumed orphaned.

309. Preferential eviction orders victims by `recv_ts`, not `id`. `WHERE motion = 0 ORDER BY id`
     has NO index serving it, so SQLite temp-b-tree-sorted the whole remaining non-motion
     partition to return 64 rows — once per `add` (~10/s) under the write lock, for the whole
     multi-day shedding. Measured on the real schema with no ANALYZE: 7.5 ms at 238k non-motion
     rows → 34 ms at 950k → 63 ms at 1.9M, i.e. linear, vs a flat 0.02 ms ordered by `recv_ts`
     off the existing `idx_frames_motion_recv`. At cap that was ~0.8-2.6 s of lock hold per
     second of capture: total starvation, entries 102-105 again.

310. Chose that over adding a `(motion, id)` index — also fast — because an index costs write
     time on every insert forever to speed a maintenance path, and this one already exists.
     The trade: victims are an exact id-prefix only while `recv_ts` is non-decreasing with `id`
     (entry 278: assumed, not enforced), and if it ever stepped back the `max()`-tracked marker
     OVER-claims unmeasurability rather than under-claiming — the fail-safe direction.
     Victim lists verified byte-identical over 3000 rows.

311. The `running` health card no longer reads green over a broken worker. Both always-on
     workers keep `running` true and set a STICKY `last_error` when a tick throws, so testing
     the intent flag alone showed "verdicts current" while nothing had been analysed for days —
     in the one card that exists because nobody is expected to be watching. It now fails a row
     on `last_error` too, as the fuller readouts below it already did.

312. The orphan sweep CONFIRMS a negative with the unindexed `path` equality before deleting.
     Entry 307's fast probe rests on a filename parse, and this call removes FILES — so a parse
     returning a wrong-but-plausible millisecond would find no row and destroy live frames.
     Proven with a deliberately broken parser: 0 deleted instead of 20,000. The scan runs only
     for candidates that already look orphaned (rare — orphans exist only after a crash), so
     the normal sweep is unchanged at 0.49 s/20k files. Made structural because the failure
     would be silent, total, and is newly AUTOMATIC at launch.

313. The retroactive non-motion purge stays MANUAL by decision. Outside `tuning` no non-motion
     frames are being stored, so every frame that job removes comes from a previous tuning
     window — it is retroactive by construction, and auto-running it on a stage change would
     make a mode toggle irreversibly destructive. The Start page instead links to it whenever
     leftover non-motion frames exist, keyed on `count − motion_count` so the hint appears from
     state and disappears once there is nothing to reclaim.

314. Feasibility run 6's headline "100% kNN" is INFLATED and must not be read as recognition
     accuracy: the leave-one-out masks only the diagonal, so a crop's nearest neighbour is
     usually the adjacent frame of its OWN visit — a near-duplicate. The separation AUC (0.878)
     and the gallery's stamped `threshold_balanced_acc` (0.804) are the numbers that survive.
     A visit-held-out probe is the fix; until it exists, no report scores the real task.

315. Cat SIZE is measurable signal the identify path discards: the embedder resizes every crop
     to 224x224, dropping absolute size AND bbox aspect before DINOv2 sees it. Per-visit peak
     bbox area alone separates Sultan/Store Sultan at AUC 0.84 (Jhinie/Store Jihn 0.78), every
     foreign lookalike measuring bigger — orthogonal to appearance, and aimed at exactly the
     pair DINOv2 is worst at. So it is a missing MECHANISM, not a data gap. Queued in docs/TODO.md.

316. Validation now reports a VISIT-held-out number: each visit is hidden whole, matched
     against the other visits, and named by Run's own rule (`Store._aggregate_identity`,
     injected so the probe cannot drift from what Run decides). Outcomes are correct /
     wrong / **declined** — kept apart because for a resident at the door those mean
     opposite things. The report leads with it; crop-level kNN stays, demoted, as the one
     number comparable with earlier runs.
     Spec: docs/specs/2026-08-03-visit-held-out-validation.md.

317. It rides the SAME embed and the SAME N×N distance matrix `run_feasibility` already
     builds — held-out scoring is that matrix with a visit's own columns masked — so the
     honest number costs numpy, not GPU. Additive: absent `visit_groups` the result dict
     is byte-identical, so nothing that consumed the old shape moved.

318. The visit threshold is calibrated on CROSS-VISIT same-cat pairs, never the crop-level
     one. `_best_threshold` over all same-cat pairs is dominated by same-visit
     near-duplicates at near-zero distance: measured 0.00063 vs 0.99934 on a real-shaped
     fixture (1587x), at which every visit is DECLINED and the report reads 100% unknown.
     Data-dependent — with separable cats the two coincide — so it cannot be assumed benign.

319. Held-out visits are grouped PER CAT, at a coarse 60 s gap of their own
     (`_HELDOUT_GAP_MS`), not the store's 2 s `_VISIT_GAP_MS`. Coarse is fail-safe here:
     over-merging removes more leakage, under-merging splits one physical visit across the
     boundary and lets the near-duplicates back in. A global time-sort would also merge two
     cats at the door in one minute into a group with no true `cat_id` — tailgating is
     expected here, and `dataset_items`' UNIQUE only stops two cats sharing ONE frame.

320. A cat with a single visit is UNSCOREABLE, not wrong: the correct answer is absent from
     the gallery, so it could only ever fail. Excluded from the denominator and named in the
     report (Store Kali today). Degenerate runs — no cross-visit pair to calibrate, or fewer
     than two visits — report `available: false` with a reason instead of numbers, since
     "0 correct / 100% unknown" reads as catastrophe where nothing was measured.

321. Day/night is scored separately, plus a CROSS-REGIME matrix (night visits against a
     day-only gallery and vice versa) that answers whether one gallery spans both regimes:
     night→night strong but night→day collapsed means separate galleries, not more night
     data. Bucketed whole by each visit's first crop, the same rule `gate_scorecard`'s split
     uses. No location or no astral disables it rather than guessing a boundary.

322. `feasibility_runs` gained a `metrics` JSON column (mirroring `model_versions.metrics`),
     carrying the visit block so the runs table can rank by the honest number. This is the
     repo's FIRST additive migration: the schema is `CREATE TABLE IF NOT EXISTS` only, which
     never adds a column to an existing table, so `_migrate_schema` probes `PRAGMA
     table_info` and `ALTER TABLE ADD COLUMN`s. Pre-existing rows read NULL = "not measured",
     which the runs table renders as `—`, distinct from an uncalibrated run's `n/a`.

323. `labeled_crops` now returns `src_recv_ts` + `labeled_ts`. Both come off
     `dataset_items`, NOT `frames`, so grouping and day/night bucketing cover every label
     including those whose frames have aged out. `labeled_ts` is stamped once per commit, so
     distinct values ≈ label keypresses — a free cross-check on the re-derived grouping,
     printed in the report beside it.

324. Probe charts now pin matplotlib's Agg backend (one `_plt()` accessor). They render in
     TrainingManager's WORKER thread, where matplotlib warns a GUI backend "will likely
     fail"; Agg is correct by definition since every figure goes straight to a base64 PNG.

325. The confusion-table f-strings no longer put a BACKSLASH inside `{}`. That is a
     SyntaxError before Python 3.12 (PEP 701 lifted it) and it fails at IMPORT for the
     whole module, so `compute.api.app` would not have started at all — `compute.ps1`
     accepts 3.10+. Invisible here: this dev box runs 3.13.

326. The schema migration survives losing a cross-process race. The `PRAGMA table_info`
     probe and the `ALTER TABLE` are not atomic across processes — the store lock is
     per-instance and `busy_timeout` only bounds waiting for the write lock — and the API
     plus the CLI tools legitimately open the same `index.db`. The loser now no-ops on
     `duplicate column` instead of dying inside `Store.__init__` with what reads like
     schema corruption. Narrow on purpose: "database is locked" and disk-full stay loud.

327. An `available: false` visit block carries NO `auc`/`threshold_balanced_acc`. Both are
     computed before the availability branch, so they leaked past "nothing was measured" —
     the exact distinction the visit scoring exists to keep honest.

328. A report whose visit scoring was UNAVAILABLE keeps its crop-level verdict. `demoted`
     keyed on the visit section rendering any HTML, but it also renders an explanatory
     banner — so an unavailable run suppressed the only verdict it had AND pointed the
     reader at "the visit-level number above", which was never computed.

329. Measured, correcting the spec: visit scoring DOES raise peak memory (n=6000: 2109 →
     2439 MB), via two pair-length `int64` gathers for the cross-visit mask. The largest
     addition is gone — finding the matrix min/max no longer boolean-index-copies the whole
     matrix (~1.16 GB at 12k crops), since `1 - (unit @ unit.T)` is always finite.

330. A cat can be left out of ONE gallery build without being retired: the Model page's new
     shared checkbox list sends `exclude_cat_ids` on build AND validate. Retiring was the
     only mechanism and is the wrong one — it also drops the cat from the annotation picker,
     stopping the labelling the exclusion is waiting for.
     Spec: docs/specs/2026-08-03-gallery-build-cat-exclusion.md.

331. It is an EXCLUDE-list, never an include-list, so absent/empty means "enrol everyone"
     and a cat added to the roster later is enrolled by default — an include-list would
     silently drop a new resident from every repeated build. Per-build only, never
     persisted: a reload re-ticks everyone.

332. Filtered in SQL on `labeled_crops` AND `count_identified_crops`, the pair that must
     agree. It is the FIRST build parameter that can reduce the CAT count, so the pre-check
     applies it too — the cap's "only reduces crops per cat" premise does not hold here, and
     the two-cat floor would otherwise pass on cats the build then drops. The message names
     the exclusion: "not enough labelled data" misreads when it was merely deselected.

333. The exclusion is part of the artifact's identity — dedup key (ids SORTED, so tick order
     is not identity), a `-ex<n>` dir-slug fragment after `-max<cap>`, and the ids on the
     version's / run's `metrics`. Without it a gallery built without Store Kali is
     indistinguishable from one with it, and a second Build with a different selection is
     silently dropped. Both lists NAME the cats (a count cannot be compared between builds).

334. Validate takes the same list, from the SAME shared control — deliberately unlike the
     grade checkboxes, which stay per-panel. Validation scores the crops, not a gallery, so
     an exclusion applied only to the build is invisible to the number; two independent
     lists would let you build without a cat, validate with it, and read the unmoved score
     as "the exclusion didn't help".

335. New `GET /api/label/enrollable` returns, per ACTIVE cat, crops + `label_commits` at the
     requested grades — pre-cap, which is also what a cap is picked against. Counts follow the
     Build grades: a cat with 50 labels may have 17 at gallery grade, so 50 is the wrong
     number to decide on. `label_commits` is named for what it counts, not the visits it
     proxies (~12% high). Retired cats are omitted — unticking implies ticking would enrol.

336. Excluding a RESIDENT is allowed with no guard: a newly added resident held back until
     it has crossings is precisely the case. The row shows `is_resident` and both counts, so
     the choice is informed rather than prevented — recall tracks visits, and the cat with
     the most crops measured the worst recall (Store Jihn 2725 crops, 69%).

337. Admin Activity gained "Identify all" — a WHOLE-STORE identify pass, beside the
     Identify that is scoped to the LOADED visits. Promoting a gallery orphans EVERY
     stored identification (keyed by `model_version_id`), so all history reads unnamed
     until a pass re-runs, and `/api/events`' 500-event cap means "Show older" could
     never widen the scoped button that far. Same endpoint, both bounds omitted.

338. `count_unidentified` moved to its OWN short-lived WAL read connection. UNBOUNDED —
     the identify pass's progress denominator — it measured 4.9 s on a 4.7M-frame replica
     with no `ANALYZE`, stalling a competing writer 7.2 s on the shared connection vs
     0.1 ms off it (measured). The starvation class entries 102-105 removed; latent until
     entry 337's button made the unbounded call routine.

339. That 4.9 s is NOT the entries 229/265/276/307 mis-plan: the plan already seeks
     `idx_analysis_analyzer_verdict` to the detected minority (1 ms). The cost is the
     per-row `frames` probe, and that join is NOT removable — `iter_unidentified` selects
     `f.path` and joins identically, and the count must match its yield exactly, so an
     orphan verdict would leave progress short of 100% forever.

340. New user-dashboard page `#visits` — nav is now Activity · Visits · Cats — answering
     *how much* where Activity answers *what happened*: per-cat visit counts over the
     trailing 6 h and 24 h, each cat's day/night split and share of named traffic, and
     the household totals. `Store.door_stats` + `GET /api/door-stats`.
     Spec: docs/specs/2026-08-04-user-visits-stats-page.md.

341. It counts the events `Store.events` produces rather than querying `identifications`,
     as `cats_overview` does and for the same reason: every visit is clustered by
     `_gap_split` and named by `_aggregate_identity`, so Visits, Cats and Activity can
     never name a moment differently — and the uncalibrated fail-safe (a NULL threshold
     names nobody) carries over for free.

342. The widest window is read ONCE and each visit folded into every window its
     `start_ts` falls inside, so the two columns cannot disagree at their shared
     boundary. `events` is walked in KEYSET pages under a `_MAX_STATS_PAGES` (8) budget
     rather than widening `_MAX_EVENTS`: that cap bounds a FEED RESPONSE, while a busy
     day exceeds 500 visits and this caller returns only counts.

343. A `since_id` of None means the window holds no frame — returned as the zeroed shape,
     never as an unbounded `events` read, which would count the newest visits in the
     WHOLE store as though they fell inside the window. Reachable whenever the newest
     frame predates the window (a stopped collector).

344. Seven EXCLUSIVE totals buckets summing to `door_events`, so every figure the page
     shows is derivable from the published totals. `unidentified` is cat-subject only —
     the number rendered as "couldn't put a name to" — with `person`/`corrupted` in
     `other`; a person at the door gets no figure of its own. A wind trigger is not a
     visit, so `cat_visits` excludes `noise`, `other` and `unanalyzed`.

345. Three honesty flags the page renders rather than swallowing: `covered` false (the
     ring buffer does not reach back that far, so the count is PARTIAL — banner);
     `truncated` (the page budget was spent); and `unanalyzed` separate from
     `unidentified`, since "nothing looked" and "looked, couldn't name" are different
     claims (entries 226/279). Without a usable gallery the per-cat rows are replaced by
     an explanation, not a column of zeroes.

346. `share` divides by NAMED visits (resident + neighbour), and is None — never 0.0 —
     when there are none: no named traffic means unmeasured, not zero. A retired cat's
     visits still count toward the totals (the active gallery may predate its
     retirement), so the listed shares need not sum to 1.

347. A measured zero renders as a dimmed DIGIT, not `—`, diverging from the spec's own
     wording: across these dashboards `—` means "not measured" (entries 106/108/113/322),
     and spending that distinction on a real zero would regress it. Day/night is omitted
     entirely without a location — including from the section's lead, which otherwise
     promised a split the rows did not carry.

348. `last_seen` deliberately does NOT come from `door_stats`. The page already fetches
     `/api/cats/overview` for avatars, and that field spans the whole retained feed while
     anything computed here would span 24 h — two fields of one name meaning different
     things on one page.

349. The retired `Who's home` placeholder is GONE, not relocated: it was blocked on
     direction detection and showed nothing. `#home` falls through the existing
     unknown-route fallback to Activity (verified) — no alias, since redirecting it to
     Visits would misrepresent what it asked for. Occupancy returns as its own page when
     it can answer.

350. Paging stops when the keyset bound `(oldest start_id − 1)` falls below the window's
     own `since_id`, WITHOUT setting `truncated`. That bound crosses the floor whenever
     the oldest cluster starts at the window's first frame; continuing asked for an
     inverted range, got nothing, and reported truncation — putting a "figures are
     incomplete" banner on a complete reading.

351. Review repairs. The page now reports `truncated` — the budget-exhausted UNDERCOUNT —
     as its own banner, distinct from `covered`'s retention partial. It was returned and
     silently dropped, so a busy day (exactly when the figures matter) rendered an
     undercount as complete: entries 97/126/167/304's trap in a new place.

352. `.kicker` is no longer scoped to `.cats-section`: the Visits tally card uses the same
     caption beside its own `h2`, where the descendant rule never applied and it rendered
     as bold heading text (measured 18.4px/700 vs the intended 13.6px/600 muted).

353. The `since_id is None` guard's test asserts `events` is NEVER CALLED, not that the
     counts are zero. The per-event `start_ts < since_ts` filter zeroes those totals with
     or without the guard, so the count-based test passed with the guard deleted — proven,
     then re-proven failing after the repair. What the guard buys is not reading at all.

354. Known limit, left deliberately: `door_stats` pages `events()` on the store's SHARED
     write connection, unlike `tuning_calendar`/`lighting_histogram`/`labeled_visits`,
     which hold their own short-lived WAL connections for exactly this reason. With the
     Visits page open, the SSE nudge refetches every ~3 s while a cat lingers. Two
     defensible fixes — a read connection under `events()` (wide blast radius: Activity
     and `cats_overview` share it) or debouncing the client refetch — so it needs a
     decision, not a patch. Per-span reads are ~5 ms, so this is added latency, not the
     19.5 s class of entries 102-105.
