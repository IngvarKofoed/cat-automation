# Edge grab-stall recovery

Make a camera-grab stall non-fatal. Today a wedged CSI camera (a hung
`capture_array()`, or an error-thrash that reopens forever but never succeeds)
freezes the grabber's `frame_id`; compute then reconnects `/stream` every few
seconds, and each reconnect leaks a Werkzeug handler thread + FD because the
frozen generator never writes and so never notices the client left — until the
process exhausts FDs/threads and dies, requiring a manual restart. This spec adds
three coupled fixes: **(1)** bound the `/stream` generator so it exits on a stall,
**(2)** a watchdog that detects a frozen `frame_id` and cleanly exits the process
so **(3)** a committed systemd unit (`Restart=always`) restarts it fresh, plus
grab-failure logging so the underlying camera fault is finally visible.

## Key decisions

- **Recover by process replacement, not in-place** (new). On a frozen `frame_id`
  the watchdog logs and calls `os._exit()`; systemd restarts a fresh process. A
  hung `capture_array()` blocks while holding `PicameraCaptureSource._lock`, so
  `close()` can't interrupt it from another thread — and libcamera/DMA state is
  process-global, so only a new process reliably clears a wedge. Rejected an
  in-process source swap: it leaks the stuck thread + camera handle every wedge
  and risks device-busy on reopen (Alternatives).
- **One `frame_id`-freshness rule covers both failure modes** (extends). The
  watchdog fires on "no successful grab for `WATCHDOG_S`", which is true whether
  the read *hangs* or *raises-and-retries forever* — both leave `frame_id` frozen.
  Reuses the monotonic-`mono` staleness concept already in `_frame_is_stale`
  (`app.py:158`); no wall-clock (the Pi has no RTC).
- **Watchdog is a separate helper, not part of `Grabber`** (new). Lives in
  `edge/server/watchdog.py`, started by `create_app` only when
  `start_grabber=True`, with an injectable `on_stall` callback (default:
  log + `os._exit`). Keeps process-exit policy out of the pure `Grabber` and out
  of the thread-free `grab_once()` test path. It acts only while
  `Grabber.is_running()`, so stopping the grabber (shutdown, or a test) disarms
  it — no caller can be surprised by a stray `os._exit`.
- **Bound the `/stream` generator on stall** (extends). The generator returns
  after `STREAM_STALL_EXIT_S` with no newly-sent frame. Compute already reconnects
  (`client.py`), so a bounded stream is transparent — this is what stops the
  per-reconnect thread/FD leak.
- **Reliability knobs are env-overridable constants, not persisted settings**
  (new). `CAT_EDGE_WATCHDOG_S`, `CAT_EDGE_BOOT_GRACE_S`, `CAT_EDGE_STREAM_STALL_S`
  are read from the environment with defaults in code — not added to
  `settings.py`/`settings.json`, because they are operational tuning, not camera
  config, and don't belong in the config UI.
- **First committed deployment artifact** (new). `deploy/cat-edge.service`
  (`Restart=always` + a start-limit that caps a *fast startup* crash-loop — not a
  camera wedge, which restarts on the far-slower watchdog cycle and so keeps
  self-healing). Aligns with ARCHITECTURE.md, which already states the Pi runs
  under systemd; `edge.sh` stays the dev launcher and becomes the unit's
  `ExecStart`.
- **Grab failures logged via a module logger** (extends). `_grab_once_internal`
  logs on the success→failure transition, throttled while continuously failing,
  and on recovery; the watchdog emits a periodic heartbeat. Today these go only to
  the invisible `last_error` slot field.
- **Suppress routine `/stream` access-log lines** (diverges). A logging filter on
  the `werkzeug` logger drops successful (2xx) `GET /stream` access lines, so a
  reconnect storm can't bury the new grabber logs. Non-2xx and all other requests
  still log — we lose per-frame stream chatter, not error signal.
- **Crash forensics live in journald, not a separate log file** (new). The unit's
  `journal` output records every restart with its exit code — including crashes
  the app can't self-log (unhandled exception, SIGKILL/OOM, SIGSEGV) — so
  `journalctl -u cat-edge.service` *is* the crash record. A separate on-disk crash
  log would only duplicate it and miss the app-can't-log cases. One install step:
  make journald persistent (volatile by default on Pi OS → lost on a full reboot).
  See Design §5.

## Goals

- A camera-grab stall (hang *or* error-thrash) no longer kills the edge; it
  self-recovers unattended — ~`WATCHDOG_S` + restart (~25 s) once a first frame
  has been seen, or ~`BOOT_GRACE_S` + restart (~65 s) for a camera that never
  produced one.
- The `/stream` generator can never leak a handler thread/FD per reconnect while
  frames aren't advancing.
- Grab failures, camera liveness, and every restart/crash (with its cause) are
  visible in journald — so you can spot *that* it went down and *why* after the
  fact, and the later libcamera root-cause hunt has data to work from.

## Non-goals

- Fixing the green-stripe/purple artifacts or changing the sensor config (the
  possible revert of `4194b79`). That is investigation **#4**, done interactively
  against the live Pi — this change is config-agnostic.
- Interrupting or aborting a hung `capture_array()` in place. We can't (the lock),
  and we don't try — we replace the process.
- Changing compute-side reconnect/liveness logic — it already behaves correctly
  (stall → `EdgeUnavailable` → reconnect with backoff).
- Any actuator / fail-safe-door behavior (no actuators exist yet).

## Design

### 1. Bound the `/stream` generator (`app.py`)

The leak is structural: during a freeze `grabber.wait_next(...)` returns every
`_STREAM_WAIT_S` (5 s) with no new frame, so `gen()` yields nothing, never writes
to the socket, and so never raises `BrokenPipe`/`GeneratorExit` when the client
disconnects (`app.py:405-417`). The fix is to bound the generator's idle life.

Track the monotonic time of the last *sent* part; if it exceeds
`STREAM_STALL_EXIT_S` (default 15 s) with nothing new sent, `return` (ends the
multipart response cleanly). Sketch inside `gen()`:

```python
last_sent_mono = time.monotonic()
...
while True:
    snap = grabber.wait_next(last_sent_id, timeout=_STREAM_WAIT_S)
    if snap.frame_id > last_sent_id and snap.frame is not None:
        part = _build_part(snap, overlay=overlay)
        if part is not None:
            last_sent_id = snap.frame_id
            last_sent_mono = time.monotonic()
            yield part
    # Unconditional (not elif): a frame that advances but fails to encode
    # (_build_part -> None) still doesn't reset the timer, so a persistent
    # encode failure is bounded too, not just a frozen frame_id.
    if time.monotonic() - last_sent_mono > STREAM_STALL_EXIT_S:
        return  # nothing sent for the bound; shed this handler, compute reconnects
```

When frames *are* flowing this branch never runs, and a dead client is still
caught by the existing `yield`→`BrokenPipe`→`OSError` path — the bound only
addresses the frozen-frame case where nothing is ever written. Each handler now
lives at most `STREAM_STALL_EXIT_S` into a freeze, so the leak is fully bounded
even before the watchdog restarts the process.

### 2. Watchdog + clean exit (`edge/server/watchdog.py`, new)

A small `Watchdog` that periodically reads `grabber.snapshot()` and decides
whether the grab loop is stalled. The decision is a pure, testable method; the
action is an injectable callback.

- **Stall rule.** Evaluated only while `grabber.is_running()` (see Lifecycle) —
  a stopped grabber freezes `mono` by design and is never a stall. Compute
  `since = time.monotonic() - snap.mono`.
  - *Armed* once `snap.frame_id > 0` (a first frame was produced): stalled if
    `since > WATCHDOG_S` (default 20 s ≈ 100 healthy frames at 5 fps).
  - *Boot grace*: if `snap.frame_id == 0` still, stalled once
    `time.monotonic() - start_mono > BOOT_GRACE_S` (default 60 s) — catches a
    camera that never produces a first frame.
- **Action (`on_stall(reason)`).** Log `CRITICAL` with context (last `frame_id`,
  `since`, `last_error`), then `os._exit` with a code that encodes the cause —
  **70** for an armed wedge, **71** for the boot-grace never-first-frame case — so
  `systemctl status` / `journalctl` tell the two apart (and apart from `1` =
  exception or a kill signal = OOM/segfault) without parsing the app log. The
  `CRITICAL` record is flushed before exit (logging's `StreamHandler` flushes per
  record), so `os._exit` skipping atexit loses nothing; there's nothing else to
  flush (settings write synchronously, no DB). `os._exit`, not `sys.exit`: the
  watchdog runs in a non-main thread where `SystemExit` would only end that
  thread, and the grabber thread may be stuck in libcamera — we must terminate the
  whole wedged process immediately, without joining it. The callback is injected so
  tests substitute a recorder instead of exiting.
- **Lifecycle.** A daemon thread polling every ~2 s, started by `create_app` only
  when `start_grabber=True` (so `grab_once()`-driven tests never spawn it) and
  exposed as `app.watchdog`. It acts **only while the grabber is running** — via a
  new `Grabber.is_running()` (started, and its stop `Event` clear). A stopped
  grabber freezes `mono`/`frame_id` by design (`grabber.py:230`), so the watchdog
  must treat "grabber stopped" as *not a stall*: it neither fires nor heartbeats.
  This makes it robust to **any** caller that stops the grabber directly —
  including the existing real-edge test fixture (`compute/tests/test_ingest.py`
  builds `create_app(start_grabber=True)` and calls `grabber.stop()` in teardown),
  which therefore needs **no change** and can't be killed by a stray `os._exit`.
  `create_app` also registers `watchdog.stop()` on the app shutdown hook alongside
  the grabber. Every ~30 s the watchdog emits an `INFO` heartbeat — `frame_id`,
  seconds-since-last-success, `motion`, `last_error` (all read off the snapshot;
  no new fields) — so the log shows liveness between events.

### 3. systemd unit (`deploy/cat-edge.service`, new)

```ini
[Unit]
Description=Cat Automation edge (camera node)
# StartLimit* are [Unit] directives (systemd >= 230; Pi OS ships 247+) — under
# [Service] they are silently ignored. This caps a FAST startup crash-loop: an
# import/config error that dies before the first frame (~3-4 s/cycle with
# RestartSec=2) trips 5-in-60 s and the unit stops as `failed` (visible via
# `systemctl status`). A camera *wedge* restarts on the ~20-65 s watchdog cycle —
# far outside this window — so a genuinely dead camera keeps slow-retrying and
# self-heals when it returns, the intended behavior for an unattended door node.
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
Type=simple
User=ingvar
WorkingDirectory=/home/ingvar/cat-automation
ExecStart=/home/ingvar/cat-automation/edge.sh
Restart=always
RestartSec=2
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

`ExecStart=edge.sh` reuses the existing bootstrap (venv + `git describe` version
bake) and its final `exec`, so systemd tracks the real Python process. `edge.sh`
itself is unchanged and remains the dev launcher. The `User=`/paths are this Pi's
confirmed install values (`ingvar` @ `/home/ingvar/cat-automation`); re-confirm
them if the checkout ever moves, since a wrong path fails silently (only
`systemctl status` shows it). `After=network-online.target` is intentionally
omitted: the edge is a pure inbound listener that binds `0.0.0.0` and never dials
out (ARCHITECTURE.md), so it needs no network-up ordering.

### 4. Grab-failure logging (`grabber.py`)

Introduce `log = logging.getLogger("edge.grabber")` and configure
`logging.basicConfig(level=INFO)` in `app.py`'s `__main__` so records reach
stdout/journald. In `_grab_once_internal`'s failure branch:

- On the **success→failure transition**, log `WARNING` with the exception.
- While **continuously failing**, throttle to one line per ~10 s (a per-loop
  failure counter + last-log monotonic timestamp) so an error-thrash doesn't spam.
- On the **first success after failures**, log `INFO` ("camera recovered after N
  failed grabs").

This finally distinguishes, in the log, a silent hang (no failure lines, watchdog
fires) from an error-thrash (repeated failure lines, then watchdog fires) from
plain corruption — the signal investigation #4 needs.

Because the grabber logs would otherwise be buried under the reconnect storm's
`GET /stream` access lines, attach a `logging.Filter` to the `werkzeug` logger
that drops successful `GET /stream` records (match the request line + a 2xx
status in `record.getMessage()`). Everything else — non-2xx `/stream`, and every
other route — still logs, so a genuinely failing stream is still visible.

### 5. Spotting a crash after the fact (journald)

No separate crash file is needed — three layers already land in journald and
survive the process restart:

1. **Watchdog stall** — the `CRITICAL` line (§2) names the cause, and the exit
   code (70 wedge / 71 never-first-frame) is recorded by systemd.
2. **A crash the app can't log** — an unhandled exception (traceback → stderr →
   journal, exit 1), or a `SIGKILL`/`SIGSEGV` (OOM, native crash). systemd logs
   `Main process exited, code=…, status=…` plus the scheduled restart for every
   one; a kernel OOM kill also shows in `journalctl -k`. This is the case a
   hand-rolled crash log would *miss* (a hard-killed process can't write its own
   log), which is why journald is the better record.
3. **Liveness + camera fault** — the 30 s heartbeat and grab-failure lines (§4),
   no longer buried under the suppressed `/stream` access spam.

Reading it: `journalctl -u cat-edge.service` (`-b` for this boot, `-f` to follow);
`systemctl status cat-edge` shows the restart count and last exit code/signal at a
glance.

**One install step — persistent journald.** Pi OS keeps the journal in tmpfs by
default, so a full Pi *reboot* (power loss) erases history; a process restart (our
normal case) does not. To keep crash history across reboots, enable persistent
storage with a size cap (SD-card wear): `sudo mkdir -p /var/log/journal &&
sudo systemctl restart systemd-journald`, plus `SystemMaxUse=200M` in
`journald.conf`. An install step, not code.

## Open questions

_None — resolved during review._

## Alternatives considered

- **In-process source swap, no exit (Approach B).** Keeps HTTP up, but the hung
  grabber thread never returns from libcamera, so it (and the camera/DMA handle)
  leaks on every wedge — reintroducing the exhaustion we're fixing — and a second
  `Picamera2` open while the first is still held often fails device-busy. Only
  helps transient read errors, which already self-heal.
- **Hybrid: reopen a few times, then exit (Approach C).** The cheap path doesn't
  address a true hang (same B problems), and transient errors self-heal without
  it, so it mostly adds moving parts around what reduces to Approach A.
