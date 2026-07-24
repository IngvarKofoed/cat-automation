"""Tests for the autonomous night-light scheduler (edge/server/night_light.py).

Nothing here needs ``astral``, real GPIO, or the wall clock: the sun-times
provider and ``now`` are injected (deterministic instants), and the GPIO effect
is a ``GpioOutputs`` wired to the fake ``PinBackend`` from test_gpio. The pure
decision (``should_be_on``) and one reconcile step (``tick``) are exercised
directly; the endpoints go through ``create_app(start_scheduler=False)`` with a
fake-backed GPIO, mirroring test_gpio's pattern.

See docs/specs/2026-07-24-edge-night-light-scheduler.md.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from edge.actuators.gpio import GPIO_OUTPUTS, GpioOutputs
from edge.capture.fake_source import FakeCaptureSource
from edge.config import settings
from edge.server.app import create_app
from edge.server.night_light import NightLight

# Pin numbers behind the channel names (see edge/actuators/gpio.py): channel1→26,
# channel2→21, channel3→20. Used to assert which pin a tick actually drove.
_PIN = {o["name"]: o["pin"] for o in GPIO_OUTPUTS}

# Synthetic sun times for a single non-polar day, all tz-aware UTC. With the
# default 30-min offsets: morning-off ends 07:30, evening-on starts 15:30.
_SUNRISE = datetime(2026, 1, 15, 7, 0, tzinfo=timezone.utc)
_SUNSET = datetime(2026, 1, 15, 16, 0, tzinfo=timezone.utc)
_ON_END = _SUNRISE + timedelta(minutes=30)  # 07:30 — morning off boundary
_ON_START = _SUNSET - timedelta(minutes=30)  # 15:30 — evening on boundary


class FakeBackend:
    """Records every pin write so a test can count and inspect commanded levels."""

    def __init__(self, pins: "list[int]") -> None:
        self.writes: "list[tuple[int, bool]]" = []
        self.levels = {pin: True for pin in pins}  # boot HIGH, like the real board

    def write(self, pin: int, high: bool) -> None:
        self.writes.append((pin, high))
        self.levels[pin] = high

    def close(self) -> None:  # pragma: no cover - not exercised here
        pass


def _fake_gpio():
    """A GpioOutputs on a fresh FakeBackend (available=True). Returns (gpio, backend)."""
    backend = FakeBackend([o["pin"] for o in GPIO_OUTPUTS])
    gpio = GpioOutputs(backend_factory=lambda pins: backend)
    return gpio, backend


def _cfg(**overrides) -> dict:
    """A night_light config block off the defaults, with any field overridden."""
    return {**settings.DEFAULTS["night_light"], **overrides}


def _fixed_sun(_lat, _lon, _date):
    """Sun-times provider that ignores its args and returns the synthetic day."""
    return (_SUNRISE, _SUNSET)


# --- should_be_on: across the day ---------------------------------------


def test_should_be_on_before_sunrise_is_on():
    # Pre-dawn (before morning-off ends) is still night → ON.
    now = datetime(2026, 1, 15, 6, 0, tzinfo=timezone.utc)
    assert NightLight.should_be_on(_SUNRISE, _SUNSET, 30, 30, now) is True


def test_should_be_on_midday_is_off():
    now = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
    assert NightLight.should_be_on(_SUNRISE, _SUNSET, 30, 30, now) is False


def test_should_be_on_after_sunset_offset_is_on():
    # Just past evening-on start → ON again for the night.
    now = _ON_START + timedelta(minutes=1)
    assert NightLight.should_be_on(_SUNRISE, _SUNSET, 30, 30, now) is True


# --- should_be_on: exact boundary instants (< vs >= semantics) ----------


def test_should_be_on_morning_boundary_is_exclusive():
    # now == on_end uses `now < on_end` → False at the instant, True just before.
    assert NightLight.should_be_on(_SUNRISE, _SUNSET, 30, 30, _ON_END) is False
    just_before = _ON_END - timedelta(microseconds=1)
    assert NightLight.should_be_on(_SUNRISE, _SUNSET, 30, 30, just_before) is True


def test_should_be_on_evening_boundary_is_inclusive():
    # now == on_start uses `now >= on_start` → True at the instant, False just before.
    assert NightLight.should_be_on(_SUNRISE, _SUNSET, 30, 30, _ON_START) is True
    just_before = _ON_START - timedelta(microseconds=1)
    assert NightLight.should_be_on(_SUNRISE, _SUNSET, 30, 30, just_before) is False


# --- tick: write-on-change only + LOW-on-configured-channel -------------


def test_tick_writes_low_on_configured_channel_once():
    # A non-default channel proves the tick honors cfg["channel"], and LOW (False)
    # is what "on" drives on an active-low board.
    gpio, backend = _fake_gpio()
    nl = NightLight(gpio, lambda: _cfg(enabled=True, channel="channel2"),
                    sun_provider=_fixed_sun)
    night = datetime(2026, 1, 15, 2, 0, tzinfo=timezone.utc)  # ON

    for _ in range(4):  # several ticks, desired unchanged
        nl.tick(night)

    # Exactly ONE write: the first ON assertion; the rest are no-ops.
    assert backend.writes == [(_PIN["channel2"], False)]
    assert nl._last_level is False  # commanded LOW = on


def test_tick_manual_flip_holds_until_desired_changes():
    # The manual-override property: a hand flip between transitions is NOT
    # overwritten until the astronomical state actually flips.
    gpio, backend = _fake_gpio()
    nl = NightLight(gpio, lambda: _cfg(enabled=True), sun_provider=_fixed_sun)
    night = datetime(2026, 1, 15, 2, 0, tzinfo=timezone.utc)  # ON
    day = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)  # OFF (same date)
    pin = _PIN["channel1"]

    nl.tick(night)  # scheduler drives LOW (on)
    assert backend.writes == [(pin, False)]

    # Operator flips the switch HIGH by hand (recorded on the same backend).
    gpio.set("channel1", True)
    assert backend.writes[-1] == (pin, True)

    # More ON ticks: desired == last commanded level → NO scheduler write, so the
    # manual HIGH survives.
    nl.tick(night)
    nl.tick(night)
    assert backend.writes == [(pin, False), (pin, True)]  # only the manual flip
    assert backend.levels[pin] is True  # manual level held

    # Now the desired state actually changes (day → off) → the scheduler writes.
    nl.tick(day)
    assert backend.writes[-1] == (pin, True)  # desired HIGH (off) re-asserted
    assert nl._last_level is True


# --- tick: no-op paths --------------------------------------------------


def test_tick_disabled_does_not_write_and_resets_last_level():
    gpio, backend = _fake_gpio()
    cfg = {"box": _cfg(enabled=True)}
    nl = NightLight(gpio, lambda: cfg["box"], sun_provider=_fixed_sun)
    night = datetime(2026, 1, 15, 2, 0, tzinfo=timezone.utc)  # ON

    nl.tick(night)  # asserts on → _last_level = False
    assert nl._last_level is False
    n_writes = len(backend.writes)

    cfg["box"] = _cfg(enabled=False)  # UI toggles the schedule off
    nl.tick(night)
    assert len(backend.writes) == n_writes  # disabled → no write
    assert nl._last_level is None  # reset so a later enable re-asserts

    cfg["box"] = _cfg(enabled=True)  # re-enable
    nl.tick(night)
    # Re-asserts on the very next tick despite the desired state being unchanged
    # from before it was disabled (because _last_level was reset to None).
    assert len(backend.writes) == n_writes + 1
    assert backend.writes[-1] == (_PIN["channel1"], False)


def test_tick_unavailable_gpio_is_a_silent_noop():
    # No gpiozero backend (the dev Mac): available=False → no write, no exception.
    def _raising_factory(_pins):
        raise RuntimeError("no gpiochip")

    gpio = GpioOutputs(backend_factory=_raising_factory)
    assert gpio.available is False
    nl = NightLight(gpio, lambda: _cfg(enabled=True), sun_provider=_fixed_sun)
    night = datetime(2026, 1, 15, 2, 0, tzinfo=timezone.utc)

    nl.tick(night)  # must not raise
    assert nl._last_level is None  # unavailable path resets like disabled


def test_tick_sun_provider_error_leaves_pin_as_is():
    gpio, backend = _fake_gpio()

    def _boom(_lat, _lon, _date):
        raise RuntimeError("astral missing / polar date")

    nl = NightLight(gpio, lambda: _cfg(enabled=True), sun_provider=_boom)
    pin = _PIN["channel1"]

    # Put the pin at a known level first, then a raising tick must not touch it.
    gpio.set("channel1", False)
    assert backend.writes == [(pin, False)]

    nl.tick(datetime(2026, 1, 15, 2, 0, tzinfo=timezone.utc))  # must not raise
    assert backend.writes == [(pin, False)]  # no further write
    assert backend.levels[pin] is False  # pin left exactly as-is


def test_tick_set_failure_does_not_advance_last_level_and_retries():
    # A gpio.set() that raises (here: a channel absent from GPIO_OUTPUTS → KeyError)
    # must be caught, leave _last_level unadvanced, and be retried next tick — so a
    # failed/bad-config write never makes the scheduler believe the pin is set.
    gpio, backend = _fake_gpio()
    cfg = {"box": _cfg(enabled=True, channel="bogus")}  # not a known output name
    nl = NightLight(gpio, lambda: cfg["box"], sun_provider=_fixed_sun)
    night = datetime(2026, 1, 15, 2, 0, tzinfo=timezone.utc)  # ON

    nl.tick(night)  # gpio.set("bogus", ...) raises KeyError → caught, no-op
    assert backend.writes == []  # nothing landed on any pin
    assert nl._last_level is None  # NOT advanced → the next tick retries

    cfg["box"] = _cfg(enabled=True, channel="channel1")  # fix the channel
    nl.tick(night)
    assert backend.writes == [(_PIN["channel1"], False)]  # the write finally lands
    assert nl._last_level is False


# --- status: schedule snapshot for the UI -------------------------------


def _two_day_sun(_lat, _lon, date):
    """Date-aware provider: sunrise 07:00 / sunset 16:00 UTC ON the given date, so
    status()'s today-and-tomorrow transition set differs per day (unlike _fixed_sun,
    which would make tomorrow's transitions duplicate today's)."""
    sr = datetime(date.year, date.month, date.day, 7, 0, tzinfo=timezone.utc)
    ss = datetime(date.year, date.month, date.day, 16, 0, tzinfo=timezone.utc)
    return sr, ss


def test_status_none_when_disabled():
    gpio, _ = _fake_gpio()
    nl = NightLight(gpio, lambda: _cfg(enabled=False), sun_provider=_two_day_sun)
    now = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
    assert nl.status(now) is None


def test_status_none_when_sun_provider_raises():
    gpio, _ = _fake_gpio()

    def _boom(_lat, _lon, _date):
        raise RuntimeError("astral missing / polar date")

    nl = NightLight(gpio, lambda: _cfg(enabled=True), sun_provider=_boom)
    now = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
    assert nl.status(now) is None


def test_status_daytime_next_change_is_tonights_on():
    # Midday → off; the next flip is tonight's ON (sunset − 30 = 15:30), the earliest
    # transition strictly after now.
    gpio, _ = _fake_gpio()
    nl = NightLight(gpio, lambda: _cfg(enabled=True), sun_provider=_two_day_sun)
    now = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
    st = nl.status(now)
    assert st["on_now"] is False
    assert st["desired_level"] == "HIGH"
    assert st["sunrise"] == datetime(2026, 1, 15, 7, 0, tzinfo=timezone.utc).isoformat()
    assert st["sunset"] == datetime(2026, 1, 15, 16, 0, tzinfo=timezone.utc).isoformat()
    assert st["next_change"] == datetime(2026, 1, 15, 15, 30, tzinfo=timezone.utc).isoformat()


def test_status_evening_next_change_rolls_to_tomorrow_morning():
    # Evening → on; today's transitions are past, so next_change is TOMORROW's
    # morning OFF (sunrise + 30 = 07:30 next day) — exercises the two-day set.
    gpio, _ = _fake_gpio()
    nl = NightLight(gpio, lambda: _cfg(enabled=True), sun_provider=_two_day_sun)
    now = datetime(2026, 1, 15, 20, 0, tzinfo=timezone.utc)
    st = nl.status(now)
    assert st["on_now"] is True
    assert st["desired_level"] == "LOW"
    assert st["next_change"] == datetime(2026, 1, 16, 7, 30, tzinfo=timezone.utc).isoformat()


# --- endpoints ----------------------------------------------------------


@pytest.fixture
def nl_client(tmp_path, monkeypatch):
    """A test client with a fake-backed (available) GPIO and no scheduler thread."""
    monkeypatch.setenv("CAT_EDGE_CONFIG", str(tmp_path / "settings.json"))
    gpio, backend = _fake_gpio()
    app = create_app(
        source_factory=FakeCaptureSource,
        start_grabber=False,
        start_scheduler=False,
        gpio=gpio,
    )
    return app.test_client(), backend


def test_get_night_light_returns_config_available_and_status(nl_client):
    client, _ = nl_client
    resp = client.get("/api/night-light")
    assert resp.status_code == 200
    data = resp.get_json()
    # The full config block is present with its defaults.
    assert data["enabled"] is False
    assert data["channel"] == "channel1"
    assert data["on_before_sunset_min"] == 30
    assert data["off_after_sunrise_min"] == 30
    assert data["latitude"] == pytest.approx(55.676)
    assert data["longitude"] == pytest.approx(12.568)
    # Hardware availability is surfaced, and status is null while disabled.
    assert data["available"] is True
    assert data["status"] is None


@pytest.mark.parametrize(
    "body",
    [
        {"channel": "nope"},  # not a known GPIO output name
        {"channel": ""},  # empty
        {"on_before_sunset_min": 30.5},  # non-integer minutes
        {"off_after_sunrise_min": True},  # bool rejected (int subclass)
        {"on_before_sunset_min": 241},  # out of [0, 240]
        {"on_before_sunset_min": -1},  # out of [0, 240]
        {"latitude": 91},  # out of [-90, 90]
        {"latitude": "north"},  # not a number
        {"longitude": 181},  # out of [-180, 180]
        {"longitude": -181},
        {"enabled": "yes"},  # non-bool
        {"enabled": 1},  # bool required, int rejected
        "notjson",  # not a JSON object
    ],
)
def test_post_night_light_rejects_bad_field_400(nl_client, body):
    client, _ = nl_client
    if isinstance(body, str):
        resp = client.post("/api/night-light", data=body, content_type="application/json")
    else:
        resp = client.post("/api/night-light", json=body)
    assert resp.status_code == 400


def test_post_night_light_persists_and_round_trips(nl_client):
    client, _ = nl_client
    resp = client.post(
        "/api/night-light",
        json={
            "enabled": True,
            "channel": "channel2",
            "on_before_sunset_min": 45,
            "off_after_sunrise_min": 10,
            "latitude": 40.0,
            "longitude": -73.0,
        },
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["enabled"] is True
    assert data["channel"] == "channel2"
    assert data["on_before_sunset_min"] == 45
    assert data["off_after_sunrise_min"] == 10
    assert data["latitude"] == pytest.approx(40.0)
    assert data["longitude"] == pytest.approx(-73.0)
    # status is left unasserted: with enabled=True the payload calls the DEFAULT
    # (astral) sun provider, which returns None here if astral is absent — the key
    # exists either way.
    assert "status" in data

    # Round-trips through a fresh GET (from the live state)...
    got = client.get("/api/night-light").get_json()
    assert got["channel"] == "channel2"
    assert got["on_before_sunset_min"] == 45
    assert got["latitude"] == pytest.approx(40.0)

    # ...and is durably persisted to disk.
    saved = settings.load_settings()["night_light"]
    assert saved["enabled"] is True
    assert saved["channel"] == "channel2"
    assert saved["on_before_sunset_min"] == 45


def test_camera_config_post_does_not_wipe_night_light(nl_client):
    # The assembly-point property: night_light is carried through the whole-config
    # write, so a camera-settings POST /api/config never silently clobbers it.
    client, _ = nl_client
    client.post("/api/night-light", json={"enabled": True, "latitude": 12.34})

    # A camera change rewrites settings.json wholesale.
    resp = client.post("/api/config", json={"rotation": 90})
    assert resp.status_code == 200

    got = client.get("/api/night-light").get_json()
    assert got["enabled"] is True
    assert got["latitude"] == pytest.approx(12.34)
    # And it survives on disk too.
    saved = settings.load_settings()["night_light"]
    assert saved["enabled"] is True
    assert saved["latitude"] == pytest.approx(12.34)


def test_malformed_stored_night_light_falls_back_to_default(tmp_path, monkeypatch):
    # A hand-edited / partial night_light block must not crash boot or feed the
    # scheduler junk — _valid_night_light rejects it and the load falls back to the
    # default block wholesale (same fail-safe posture as fps/focus/motion).
    monkeypatch.setenv("CAT_EDGE_CONFIG", str(tmp_path / "settings.json"))
    (tmp_path / "settings.json").write_text(
        json.dumps(
            {
                **settings.DEFAULTS,
                "night_light": {  # enabled non-bool AND latitude out of range
                    "enabled": "yes",
                    "channel": "channel1",
                    "on_before_sunset_min": 30,
                    "off_after_sunrise_min": 30,
                    "latitude": 999,
                    "longitude": 12.568,
                },
            }
        )
    )
    gpio, _ = _fake_gpio()
    app = create_app(
        source_factory=FakeCaptureSource,
        start_grabber=False,
        start_scheduler=False,
        gpio=gpio,
    )
    got = app.test_client().get("/api/night-light").get_json()
    for key, value in settings.DEFAULTS["night_light"].items():
        assert got[key] == value
