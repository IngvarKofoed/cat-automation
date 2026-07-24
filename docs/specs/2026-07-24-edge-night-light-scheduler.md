# Edge night-light scheduler

An autonomous scheduler on the Pi that drives a GPIO relay channel to power a
1000-lumen LED lamp for night camera illumination: **on** at `sunset − 30 min`,
**off** at `sunrise + 30 min`. Sun times are computed offline with `astral`
(no network — the Pi never dials out) from a config location defaulting to
Copenhagen. It runs as a small poll-reconcile daemon thread that writes the pin
only when the desired state changes, so it survives a compute-PC outage and a
manual flip of the switch holds until the next scheduled transition.

## Key decisions

- **Edge-side autonomous actuation** (diverges). The Pi drives the lamp on a
  fixed astronomical schedule rather than via compute-emitted intents
  (ARCHITECTURE's `DENY_ENTRY`→lock model). Justified because this is **ambient
  camera-illumination** — a capture concern that must keep working when the
  compute tier or network is down — not access-control actuation. The deferred
  intent-based Control API (lock / sound / deterrent-light) is untouched. The
  build adds a one-line note to `docs/ARCHITECTURE.md` recording that Channel 1 =
  fixed illumination, partially answering its deferred "light's role" open
  question and keeping it distinct from the deterrent-light intent.
- **Poll-reconcile loop, write-on-change** (reuses + new). A daemon thread built
  on the existing `Watchdog` idiom (`edge/server/watchdog.py`) wakes every
  `_POLL_S` (~60 s), computes the desired pin level, and calls `gpio.set()`
  **only when it differs from the last commanded level**. This yields the free
  manual-override property (an unchanged desired state never re-writes, so a hand
  flip holds until the next real transition) and self-corrects against the Pi's
  clock — it has no RTC, boots wrong, and NTP-steps later, which a per-minute
  recompute simply absorbs.
- **`astral` for offline sun times** (new). Pure-Python, no network, no API key.
  Added to `edge/requirements.txt` as a normal pip dep (unlike `gpiozero` /
  `picamera2`, which are apt + system-site-packages). Imported lazily and
  degraded gracefully, matching the lazy-import pattern used for optional/heavy
  deps across the repo.
- **Injected sun-times provider** (reuses). The scheduler takes a
  `sun_provider(lat, lon, date) -> (sunrise, sunset)` callable, defaulting to an
  `astral`-backed one. Mirrors the `source_factory` / `backend_factory` /
  `on_stall` injection seams and keeps the window logic hermetically unit-testable
  without `astral` installed.
- **Config threaded through the one whole-config assembly point** (extends).
  `night_light` is a key in `settings.json`, carried through
  `_config_snapshot_locked()` and `set_config`'s `next_config` so a camera-settings
  `POST /api/config` can't silently wipe it (the file is rewritten wholesale). Edits
  go through a dedicated `GET`/`POST /api/night-light`.
- **Scheduler reads live config each tick** (reuses). Constructed with a
  `read_night_light()` closure over the locked `state`, exactly like
  `Grabber(read_config)`. A UI enable/offset change takes effect on the next tick
  with no restart and no explicit poke.
- **Graceful no-op when unavailable or disabled** (reuses). If the schedule is
  disabled, or `gpio.available` is False (no `gpiozero` — the dev Mac), or the sun
  computation raises, the tick does nothing and leaves the pin alone. Safe to run
  everywhere; catches `GpioUnavailable`.
- **UTC-internal time math** (new). Everything is tz-aware UTC — `astral` returns
  UTC instants and offsets are `timedelta`s on them, so our code contains no DST
  logic. Valid for non-polar mid-latitudes (Copenhagen always has a sunrise and a
  sunset); the polar edge case is handled by the graceful no-op above.

## Goals

- Drive the configured channel LOW (= on, active-low board) from `sunset − N` to
  `sunrise + M`, automatically, on the Pi, day after day.
- Keep working during a compute-PC or network outage (edge autonomy).
- No outbound network from the Pi (sun times computed locally).
- Preserve the manual GPIO switch: a manual flip holds until the next scheduled
  transition.
- Enable and configure from the config UI; a safe no-op off a Pi.

## Non-goals

- Access control, deterrence, or the intent-based Control API (lock / sound /
  deterrent-light) — deferred, untouched here.
- Light-level / brightness sensing — this is a pure astronomical schedule.
- More than one schedule, or scheduling multiple channels.
- Persisting manual GPIO levels across restart (unchanged — pins still boot HIGH).
- Automatic location detection — location is config, defaulting to Copenhagen.

## Design

### New module: `edge/server/night_light.py`

A `NightLight` class, sibling to `Watchdog` — a server-managed daemon thread with
a pure, testable decision and an injected effect. Shape:

```python
class NightLight:
    def __init__(self, gpio, read_config, *, sun_provider=None,
                 poll_s=_POLL_S, now_fn=lambda: datetime.now(timezone.utc)): ...

    # pure ---------------------------------------------------------------
    @staticmethod
    def should_be_on(sunrise, sunset, on_before_sunset_min,
                     off_after_sunrise_min, now) -> bool:
        on_end   = sunrise + timedelta(minutes=off_after_sunrise_min)   # morning
        on_start = sunset  - timedelta(minutes=on_before_sunset_min)    # evening
        return now < on_end or now >= on_start   # ON overnight; OFF during the day

    # one reconcile step (testable) --------------------------------------
    def tick(self, now) -> None: ...

    # lifecycle (mirrors Watchdog) ---------------------------------------
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def status(self, now) -> "dict | None": ...   # for GET /api/night-light
```

`tick(now)`:

1. `cfg = read_config()`. If `not cfg["enabled"]` or `not gpio.available`: set
   `_last_level = None` (so a later enable re-asserts immediately) and return.
2. `sunrise, sunset = sun_provider(cfg["latitude"], cfg["longitude"], now.date())`,
   wrapped in `try/except` — on failure (astral missing, or a polar date raising)
   log throttled and return, leaving the pin as-is.
3. `desired_on = should_be_on(...)`; `desired_level = not desired_on` (LOW = on).
4. If `_last_level is None or desired_level != _last_level`: `gpio.set(cfg["channel"],
   desired_level)` (catch `KeyError` / `GpioUnavailable` → log, return) and set
   `_last_level = desired_level`.

`_run()` loops `tick(now_fn())` **immediately**, then `while not self._stop.wait(poll_s)`
— so a boot in the middle of the night lights the lamp at once rather than after
one poll interval. Daemon thread, `threading.Event` stop, `stop()` joins briefly —
all copied from `Watchdog`.

`_last_level` tracks the **commanded pin level**, not wall-clock transitions: since
the tick re-writes only on a change, the ~60 s poll never disturbs a manual flip
between transitions, and re-asserts correctly the minute the astronomical state
actually flips.

Default `sun_provider` lazily imports `astral` (`Observer(latitude, longitude)`,
`astral.sun.sun(obs, date=date)` → `sunrise`/`sunset` as UTC instants). Import
failure propagates to the `tick` guard → graceful no-op.

### Wiring in `create_app` (`edge/server/app.py`)

- New param `start_scheduler: bool = True` (parallel to `start_grabber` /
  `start_watchdog`; tests pass False).
- After `gpio` is built, add `read_night_light()` returning `dict(state["night_light"])`
  under `lock`, construct `NightLight(gpio, read_night_light)`, expose as
  `app.night_light`, and if `start_scheduler` start it + `atexit.register(stop)`.
- The thread starts regardless of `enabled`; it self-gates per tick, so the UI
  toggle needs no thread restart.

### Config (`edge/config/settings.py`)

Add to `DEFAULTS`:

```python
"night_light": {
    "enabled": False,
    "channel": "channel1",
    "on_before_sunset_min": 30,
    "off_after_sunrise_min": 30,
    "latitude": 55.676,
    "longitude": 12.568,
},
```

Add `night_light` to `state`, to `_config_snapshot_locked()`, and to `set_config`'s
`next_config` (carried from `state`, like the camera keys) so every whole-config
write preserves it. On load, validate the stored value and fall back to the default
block if malformed (same defensive posture as fps/focus/motion params).

### Endpoints

- `GET /api/night-light` → the `night_light` config + `available` + a `status`
  block from `NightLight.status(now)`: `{on_now, desired_level, sunrise, sunset,
  next_change}` (ISO UTC), or `null` when disabled / uncomputable. `status`
  reuses `should_be_on`; `next_change` is the earliest of today/tomorrow's
  `on_start`/`on_end` after `now`.
- `POST /api/night-light` → validate, persist via the assembly point, update
  `state["night_light"]` under `lock` (the scheduler picks it up next tick).
  Validation, 400 on the first bad field: `enabled` bool; `channel` in
  `gpio.names()`; `on_before_sunset_min` / `off_after_sunrise_min` **integer
  minutes** in `[0, 240]` (bool rejected, matching `_is_int`); `latitude` in
  `[-90, 90]`; `longitude` in `[-180, 180]`.

### Config UI (`edge/server/ui/index.html`)

A "Night light" section near the GPIO switches: enable checkbox, channel dropdown
(from `/api/gpio` names, default `channel1`), the two offset fields
(`on_before_sunset_min` / `off_after_sunrise_min`, integer-minute inputs — the
`30`s are editable here, not baked in), latitude / longitude fields, a Save
button, and a one-line status readout ("Lamp ON now —
next OFF ≈ 06:12") rendered from the `status` block in local browser time. A short
note states that a manual switch flip holds until the next scheduled transition.
Off a Pi (`available:false`) the section explains the schedule can't drive the pin
here, matching the existing GPIO-note behavior.

### Dependencies & tests

- `astral` added to `edge/requirements.txt` (pip; pure-Python).
- `edge/tests/test_night_light.py`: unit-test `should_be_on` across the day
  (pre-dawn, mid-day, post-sunset, the two offset boundaries) with synthetic sun
  times; `tick` writes-on-change only (manual-override preservation); no-op when
  disabled / `gpio.available` False / provider raises; correct level on enable.
  Endpoint validation via `create_app(start_scheduler=False)` + a fake GPIO
  backend, following `test_gpio.py`.

## Alternatives considered

- **Sleep-until-next-transition loop.** Wakes only twice a day, but needs an
  explicit next-transition calc *and* a safety cap (an NTP step mid-sleep would
  wake it at the wrong wall-clock time on the RTC-less Pi). More code for behavior
  identical to the poll-reconcile loop. Rejected.
- **IP-geolocation / OS location.** The only "automatic" location source would
  make the Pi dial out — violating the pure-server principle — and adds a failure
  mode (no internet → no sun times). `astral` gives the same two timestamps
  offline. Rejected.
- **Light-level sensing** (frame luminance / camera lux). More robust to weather
  and shade, but needs tuning and per-scene calibration. Deferred; the
  astronomical schedule is deterministic and the simplest thing that works.
- **Compute-driven via the Control API.** The correct long-term home for
  *actuation policy*, but the intent API is deferred and illumination must survive
  a compute outage — so the schedule lives on the edge for now.
