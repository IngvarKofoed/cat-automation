# Edge host CPU & memory metrics on `/status`

Report the edge device's host CPU utilization and memory usage so we can see, at
a glance in the config UI, whether the Pi (or a dev Mac) is struggling. The edge
measures both with `psutil`, exposes them under a new `system` object on the
existing `GET /status` snapshot, and the config UI renders two more badges in the
status row it already polls. Cross-platform (Linux Pi OS + macOS) falls out of
`psutil` doing the per-OS work for us. No new endpoint, no new thread, no change
to the frame/motion path.

## Key decisions

- **`psutil` dependency** (new). Add `psutil` to `edge/requirements.txt`. It is
  the standard portable way to read CPU% and memory and works identically on
  Linux and macOS, replacing what would otherwise be two hand-rolled per-OS code
  paths. Chosen over stdlib `os.getloadavg()` + `/proc`/`sysctl` parsing (a clean
  0–100% figure and one code path beat zero dependencies here).
- **CPU% is host-wide, non-blocking, cadence-independent** (new). A small
  `SystemMetrics` helper gates `psutil.cpu_percent(interval=None)` behind a
  monotonic window (recompute at most once every ~2 s; return the cached value in
  between). This avoids both a blocking `interval=…` call inside the request
  handler and a dedicated sampler thread, while keeping the number meaningful no
  matter how often `/status` is polled.
- **Metrics live under a nested `system` object on `/status`** (extends). The
  existing flat camera/motion fields (`motion`, `camera_ok`, …) stay untouched;
  host health is grouped under `system` so the two concerns stay visually
  distinct and unknown-field-tolerant clients (the compute tier) are unaffected.
  This is an **additive, backward-compatible** change to the `/status` contract
  documented in `ARCHITECTURE.md`.
- **Memory reported consistently across OSes** (new). `mem_percent` and
  `mem_used_mb` are both derived from `total - available`, not psutil's platform-
  dependent `.used`, so the Pi and a Mac report comparable numbers.
- **Reuse the existing `/status` poll + badge row** (reuses). The UI adds two
  badges to `#motionStatus` and fills them inside the existing `pollStatus()`
  loop (`index.html:970`) — no new timer, no new fetch.
- **Fail soft, never 500** (extends). If `psutil` is missing or a reading throws,
  `system` is `null` and `/status` still returns 200 with camera/motion intact;
  the UI shows `—`. Mirrors how the UI already degrades camera health.

## Goals

- Show host CPU% and memory usage of the edge device in the config UI, updating
  live alongside the motion/camera badges.
- Work unchanged on Raspberry Pi OS (Linux) and macOS.
- Keep the edge a thin pure server: measuring its own load is local, read-only,
  and never gates or slows the frame/motion path.

## Non-goals

- Per-process metrics for the edge server itself (we want "is the *device* busy",
  not the Python process). Host-wide is the useful signal.
- History, graphs, or alerting on load — just a current-value badge.
- Metrics for the compute tier, or surfacing edge metrics anywhere but the config
  UI. (The `system` object is on the wire, so the compute tier *could* read it
  later; that's not built here.)
- CPU temperature / throttling, disk, network, per-core breakdown — a later add
  if wanted.

## Design

### `SystemMetrics` helper (`edge/server/metrics.py`)

A tiny class owning all sampling state; one instance per app (like `Grabber`).

```python
class SystemMetrics:
    def sample(self) -> "dict | None":
        # Returns {cpu_percent, mem_percent, mem_used_mb, mem_total_mb}
        # or None if psutil is unavailable / a reading fails.
```

- **Memory** is a single instantaneous call, `psutil.virtual_memory()`:
  - `mem_total_mb = round(total / 1024**2)`
  - `mem_used_mb  = round((total - available) / 1024**2)`
  - `mem_percent  = round(vm.percent, 1)` (psutil's `.percent` is already
    `(total-available)/total`, consistent with the two MB figures above).
- **CPU%** uses `psutil.cpu_percent(interval=None)` (non-blocking; returns the
  busy fraction since the previous call), gated by a monotonic window so callers
  can poll as fast as they like without distorting it:
  - Store `_last_cpu_mono` and `_last_cpu_pct`.
  - On `sample()`, if `monotonic() - _last_cpu_mono >= CPU_WINDOW_S`
    (`CPU_WINDOW_S = 2.0`), re-read `cpu_percent(interval=None)`, update both
    stored fields. Otherwise reuse `_last_cpu_pct`.
  - `cpu_percent` is `None` until the *first* window has elapsed (psutil's first
    reading is meaningless), so the UI shows `—` for ~2 s after boot. Rounded to
    one decimal once real.
- `psutil` is imported at module load inside a `try`; if the import fails,
  `sample()` returns `None`. Any exception during a reading is caught and also
  yields `None` (or `cpu_percent: None` for a CPU-only hiccup) — `/status` must
  never fail because of metrics.

### `/status` (`edge/server/app.py`)

Construct one `SystemMetrics` in `create_app()` (alongside `grabber`), then add
its reading to the existing JSON:

```python
return jsonify(
    frame_id=snap.frame_id,
    ...,
    version=_VERSION,
    system=metrics.sample(),   # dict, or None on failure
)
```

No other route changes. `sample()` is cheap (two psutil calls at most once/sec,
cached otherwise), so calling it per `/status` poll is fine.

### Config UI (`edge/server/ui/index.html`)

- Add two badges to the existing `#motionStatus` row:
  `<span id="cpuLoad" class="badge">CPU: —</span>` and
  `<span id="memLoad" class="badge">Mem: —</span>`.
- In `pollStatus()`, after the camera-health block, read `data.system`:
  - present → `CPU: 12.3%` and `Mem: 41% (412 MB)`
    (`mem_percent` + `mem_used_mb`).
  - `null`/missing → both show `—`.
- In the existing `catch` (server unreachable) block, set both to `—` too.

No new polling, timer, or fetch — this rides the loop that already runs while the
tab is visible.

### Cross-tier docs

Update the `/status` shape in `docs/ARCHITECTURE.md` (Communication and data
flow) to include `system: {cpu_percent, mem_percent, mem_used_mb, mem_total_mb} |
null`, since that section is the source of truth for the contract the compute
tier reads. Additive and backward-compatible.

## Alternatives considered

- **Background sampler thread (Approach B).** A daemon sampling every ~2 s into a
  lock-guarded slot, mirroring `Grabber`. Always has a fresh value regardless of
  who polls, but adds a second thread + lifecycle for one number. The on-demand
  cache gets the same result for the continuously-polling config UI without it.
- **Stdlib only, no dependency (Approach C).** `os.getloadavg()` + per-OS memory
  parsing (`/proc/meminfo`, `sysctl`/`vm_stat`). Zero deps, but two memory code
  paths, load-average isn't a clean 0–100% utilization figure, and more to get
  right and test. Rejected for the fragility; revisit only if `psutil` proves a
  packaging problem on 32-bit Pi OS (where it may build from source, needing
  `python3-dev`).
