"""Autonomous night-light scheduler: drive a GPIO channel on an astronomical clock.

A small poll-reconcile daemon thread, sibling to :class:`edge.server.watchdog.Watchdog`.
It powers a camera-illumination lamp wired to one relay channel **on** at
``sunset − on_before_sunset_min`` and **off** at ``sunrise + off_after_sunrise_min``,
day after day, entirely on the Pi. Sun times are computed offline with ``astral``
from a config location (defaulting to Copenhagen), so the edge still never dials
out — this is ambient camera illumination, a capture concern that must keep
working when the compute tier or network is down, NOT access-control actuation
(the deferred intent-based Control API is untouched). See
docs/specs/2026-07-24-edge-night-light-scheduler.md.

The loop wakes every ``_POLL_S`` (~60 s), computes the desired pin level, and calls
``gpio.set()`` **only when it differs from the last commanded level**. Two
properties fall out of write-on-change:

* **Manual override holds.** An unchanged desired state never re-writes, so a hand
  flip of the switch survives until the next real astronomical transition.
* **Self-correcting clock.** The RTC-less Pi boots with a wrong clock and NTP-steps
  it later; a per-minute recompute simply absorbs the step, with no next-transition
  math to invalidate.

The decision (:meth:`should_be_on`) is pure and unit-testable; the sun-times source
and the GPIO effect are both injected so the window logic can be tested without
``astral`` installed and without a Pi.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

from edge.actuators.gpio import GpioUnavailable
from edge.server.watchdog import env_positive_float

_log = logging.getLogger("edge.night_light")

# Poll cadence (seconds). ~60 s is plenty: transitions are minute-granular and the
# loop only re-writes the pin on a change, so a tighter poll buys nothing.
_POLL_S = env_positive_float("CAT_EDGE_NIGHT_LIGHT_POLL_S", 60.0)

# Throttle interval (seconds) shared by the repeating failure paths (sun compute,
# gpio set, unexpected tick error). The loop runs forever, so a persistent fault
# (astral missing, a polar date, a bad channel) must log once and then stay quiet
# rather than flood journald every poll. Each path throttles INDEPENDENTLY via its
# own _last_*_mono field; only the interval is shared. First failure logs at once.
_ERROR_LOG_INTERVAL_S = 300.0


def _astral_sun_provider(lat, lon, date) -> "tuple[datetime, datetime]":
    """Default sun-times source: ``(sunrise, sunset)`` as tz-aware UTC instants.

    ``astral`` is imported lazily inside the call so importing this module never
    needs the dependency, and any import/compute failure (astral absent, or a polar
    date with no sunrise/sunset) PROPAGATES to :meth:`NightLight.tick`'s guard,
    which degrades to a graceful no-op. ``astral.sun.sun`` returns UTC datetimes.
    """
    from astral import Observer
    from astral.sun import sun

    s = sun(Observer(latitude=lat, longitude=lon), date=date)
    return (s["sunrise"], s["sunset"])


class NightLight:
    """Poll-reconcile scheduler that drives one GPIO channel on an astronomical clock.

    ``gpio`` is a :class:`edge.actuators.gpio.GpioOutputs`; ``read_config`` returns
    the live ``night_light`` config dict each tick (a closure over the app's locked
    state, exactly like ``Grabber(read_config)``), so a UI enable/offset change takes
    effect on the next tick with no restart. ``sun_provider(lat, lon, date) ->
    (sunrise, sunset)`` and ``now_fn() -> datetime`` are injected for testability.
    """

    def __init__(
        self,
        gpio,
        read_config: "Callable[[], dict]",
        *,
        sun_provider: "Callable[..., tuple[datetime, datetime]] | None" = None,
        poll_s: float = _POLL_S,
        now_fn: "Callable[[], datetime]" = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._gpio = gpio
        self._read_config = read_config
        self._sun_provider = sun_provider or _astral_sun_provider
        self._poll_s = poll_s
        self._now_fn = now_fn

        # The pin LEVEL last commanded by this scheduler (True=HIGH, False=LOW), or
        # None when nothing has been asserted yet (initial, or after a
        # disabled/unavailable tick) so a later enable re-asserts immediately.
        self._last_level: "bool | None" = None

        # Throttled-log state (monotonic seconds of the last emitted log per path;
        # 0.0 = never, which logs on first occurrence). See the _INTERVAL constants.
        self._last_sun_error_mono = 0.0
        self._last_set_error_mono = 0.0
        self._last_tick_error_mono = 0.0

        # Thread lifecycle, mirroring Watchdog.
        self._stop = threading.Event()
        self._thread: "threading.Thread | None" = None

    # -- decision (pure) ---------------------------------------------------

    @staticmethod
    def should_be_on(
        sunrise: datetime,
        sunset: datetime,
        on_before_sunset_min: int,
        off_after_sunrise_min: int,
        now: datetime,
    ) -> bool:
        """True when the lamp should be ON at ``now`` (all args tz-aware UTC).

        ON overnight, OFF during the day: on until ``sunrise + off_after_sunrise_min``
        (morning), then off until ``sunset − on_before_sunset_min`` (evening). The
        two windows are computed from the SAME day's sunrise/sunset, so the test is
        ``now < morning_off_end OR now >= evening_on_start`` — true across the two
        night halves that bracket the day.
        """
        on_end = sunrise + timedelta(minutes=off_after_sunrise_min)  # morning off
        on_start = sunset - timedelta(minutes=on_before_sunset_min)  # evening on
        return now < on_end or now >= on_start

    # -- one reconcile step (testable) -------------------------------------

    def tick(self, now: datetime) -> None:
        """Reconcile the pin once against the schedule at ``now`` (tz-aware UTC).

        No-op (and clears ``_last_level`` so a later enable re-asserts) when the
        schedule is disabled or the GPIO backend is absent. A sun-computation
        failure leaves the pin as-is (throttled log). The pin is written only when
        the desired level differs from the last commanded one — the write-on-change
        rule that preserves a manual flip between real transitions.
        """
        cfg = self._read_config()
        if not cfg["enabled"] or not self._gpio.available:
            self._last_level = None
            return

        try:
            sunrise, sunset = self._sun_provider(
                cfg["latitude"], cfg["longitude"], now.date()
            )
        except Exception as exc:  # noqa: BLE001 - astral absent / polar date → no-op
            if self._should_log("_last_sun_error_mono", _ERROR_LOG_INTERVAL_S):
                _log.warning(
                    "night-light sun computation failed (lat=%s lon=%s): %s — leaving pin as-is",
                    cfg.get("latitude"),
                    cfg.get("longitude"),
                    exc,
                )
            return
        # Success: re-arm the throttle so a later failure streak logs at once.
        self._last_sun_error_mono = 0.0

        desired_on = self.should_be_on(
            sunrise,
            sunset,
            cfg["on_before_sunset_min"],
            cfg["off_after_sunrise_min"],
            now,
        )
        desired_level = not desired_on  # LOW (False) = on, active-low board

        if self._last_level is None or desired_level != self._last_level:
            try:
                self._gpio.set(cfg["channel"], desired_level)
            except (KeyError, GpioUnavailable) as exc:
                # Do NOT advance _last_level — the write never landed, so the next
                # tick must retry rather than assume the pin reached this level.
                if self._should_log("_last_set_error_mono", _ERROR_LOG_INTERVAL_S):
                    _log.warning(
                        "night-light could not drive channel %r: %s",
                        cfg.get("channel"),
                        exc,
                    )
                return
            self._last_level = desired_level
            # A successful drive re-arms the set-error throttle (mirrors the sun-error
            # re-arm above), so a later failure streak logs at once rather than being
            # silenced by a stale timestamp from an earlier, since-recovered fault.
            self._last_set_error_mono = 0.0

    # -- status (for GET /api/night-light) ---------------------------------

    def status(self, now: datetime) -> "dict | None":
        """A schedule snapshot for the config UI, or None when uninformative.

        None when the schedule is disabled OR the sun computation raises (astral
        missing, polar date). Otherwise ``{on_now, desired_level, sunrise, sunset,
        next_change}`` with ISO-8601 UTC strings. ``next_change`` is the earliest of
        today's and tomorrow's on/off transitions strictly after ``now`` (two days
        ahead guarantees at least one future candidate).
        """
        cfg = self._read_config()
        if not cfg["enabled"]:
            return None
        try:
            sunrise, sunset = self._sun_provider(
                cfg["latitude"], cfg["longitude"], now.date()
            )
            tomorrow = (now + timedelta(days=1)).date()
            sunrise2, sunset2 = self._sun_provider(
                cfg["latitude"], cfg["longitude"], tomorrow
            )
        except Exception:  # noqa: BLE001 - same graceful no-op as tick
            return None

        before = cfg["on_before_sunset_min"]
        after = cfg["off_after_sunrise_min"]
        on_now = self.should_be_on(sunrise, sunset, before, after, now)
        transitions = (
            sunrise + timedelta(minutes=after),  # today morning off
            sunset - timedelta(minutes=before),  # today evening on
            sunrise2 + timedelta(minutes=after),  # tomorrow morning off
            sunset2 - timedelta(minutes=before),  # tomorrow evening on
        )
        future = [t for t in transitions if t > now]
        next_change = min(future) if future else None
        return {
            "on_now": on_now,
            "desired_level": "LOW" if on_now else "HIGH",
            "sunrise": sunrise.isoformat(),
            "sunset": sunset.isoformat(),
            "next_change": next_change.isoformat() if next_change is not None else None,
        }

    # -- lifecycle (mirrors Watchdog) --------------------------------------

    def start(self) -> None:
        """Start the daemon poll loop. Idempotent — a second call is a no-op."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="edge-night-light", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the loop to exit and wait briefly. Safe when not running."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)

    def _run(self) -> None:
        # Tick IMMEDIATELY, then every poll_s — so a boot in the middle of the night
        # lights the lamp at once rather than after one poll interval. stop.wait
        # doubles as the pace and returns True the instant stop is set.
        self._tick_guarded()
        while not self._stop.wait(self._poll_s):
            self._tick_guarded()

    def _tick_guarded(self) -> None:
        """Run one tick, never letting an unexpected error kill the loop.

        ``tick`` already guards the expected failure paths (sun compute, gpio.set);
        this is the last-resort net so a genuinely unforeseen error stops one tick,
        not the whole scheduler. Logged throttled, like the other loop faults.
        """
        try:
            self.tick(self._now_fn())
        except Exception as exc:  # noqa: BLE001 - the loop must never die
            if self._should_log("_last_tick_error_mono", _ERROR_LOG_INTERVAL_S):
                _log.warning("night-light tick failed: %s", exc)

    def _should_log(self, attr: str, interval: float) -> bool:
        """Rate-limit a repeating log line; True at most once per ``interval``.

        ``attr`` names a monotonic-seconds field on ``self`` (0.0 = never logged, so
        the first call always returns True). Updated to now whenever it returns True.
        """
        mono = time.monotonic()
        last = getattr(self, attr)
        if last == 0.0 or mono - last >= interval:
            setattr(self, attr, mono)
            return True
        return False
