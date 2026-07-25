"""Tests for the offline sun-times helper (compute/analysis/suntimes.py) — the
day/night boundary the tuning-scorecard split (admin-next P2) buckets visits by.

Two layers:

- The pure boundary math (``is_night``) and the ``night_classifier`` factory are
  tested with a **synthetic** sun-times provider, so they run with no ``astral``
  install and no network — the same hermetic style the rest of the suite uses.
- ``sun_times`` (the real astral path) is exercised behind a skip guard, so the
  file passes whether or not the optional dependency is present.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from compute.analysis import suntimes

UTC = timezone.utc


def _ms(dt: datetime) -> int:
    """Epoch milliseconds (UTC) for a tz-aware datetime — the classifier's input."""
    return int(dt.timestamp() * 1000)


def _fixed_provider(sunrise_hour: int = 8, sunset_hour: int = 16):
    """A synthetic sun provider: the same UTC hour window on every date."""

    def provider(lat: float, lon: float, day: date):
        return (
            datetime(day.year, day.month, day.day, sunrise_hour, 0, tzinfo=UTC),
            datetime(day.year, day.month, day.day, sunset_hour, 0, tzinfo=UTC),
        )

    return provider


# --- is_night: pure boundary -------------------------------------------------


def test_is_night_day_window_is_half_open():
    sunrise = datetime(2026, 7, 25, 3, 0, tzinfo=UTC)
    sunset = datetime(2026, 7, 25, 19, 0, tzinfo=UTC)
    # Before sunrise and after sunset -> night; the window itself is [sunrise, sunset):
    # exactly sunrise counts as day, exactly sunset counts as night.
    assert suntimes.is_night(datetime(2026, 7, 25, 2, 59, tzinfo=UTC), sunrise, sunset) is True
    assert suntimes.is_night(sunrise, sunrise, sunset) is False
    assert suntimes.is_night(datetime(2026, 7, 25, 12, 0, tzinfo=UTC), sunrise, sunset) is False
    assert suntimes.is_night(sunset, sunrise, sunset) is True
    assert suntimes.is_night(datetime(2026, 7, 25, 20, 0, tzinfo=UTC), sunrise, sunset) is True


def test_is_night_treats_naive_datetimes_as_utc():
    # A naive datetime must not raise (no tz-aware vs naive comparison error) — it is
    # assumed already UTC, matching how astral / recv_ts are handled.
    sunrise = datetime(2026, 7, 25, 3, 0)
    sunset = datetime(2026, 7, 25, 19, 0)
    assert suntimes.is_night(datetime(2026, 7, 25, 12, 0), sunrise, sunset) is False
    assert suntimes.is_night(datetime(2026, 7, 25, 1, 0), sunrise, sunset) is True


# --- night_classifier: recv_ts -> day/night, cached per date -----------------


def test_night_classifier_classifies_by_injected_sun_times():
    classify = suntimes.night_classifier(55.6, 12.5, sun_provider=_fixed_provider())
    # 08:00-16:00 UTC is day; dawn/evening are night; sunrise itself is day.
    assert classify(_ms(datetime(2023, 11, 15, 12, 0, tzinfo=UTC))) is False
    assert classify(_ms(datetime(2023, 11, 15, 6, 0, tzinfo=UTC))) is True
    assert classify(_ms(datetime(2023, 11, 15, 20, 0, tzinfo=UTC))) is True
    assert classify(_ms(datetime(2023, 11, 15, 8, 0, tzinfo=UTC))) is False


def test_night_classifier_caches_sun_times_per_date():
    calls: "list[date]" = []

    def provider(lat, lon, day):
        calls.append(day)
        return _fixed_provider()(lat, lon, day)

    classify = suntimes.night_classifier(1.0, 2.0, sun_provider=provider)
    # The classifier reads a 3-day window (day-1, day, day+1) so the correct
    # sunrise/sunset pair is available at any longitude; each date is computed
    # AT MOST ONCE (cached). Three lookups on the same UTC date -> the window's
    # three dates, each computed once.
    for hour in (6, 12, 20):
        classify(_ms(datetime(2023, 11, 15, hour, 0, tzinfo=UTC)))
    assert calls == [date(2023, 11, 14), date(2023, 11, 15), date(2023, 11, 16)]
    # An adjacent date reuses two cached dates and computes only the new one.
    classify(_ms(datetime(2023, 11, 16, 12, 0, tzinfo=UTC)))
    assert calls == [
        date(2023, 11, 14), date(2023, 11, 15), date(2023, 11, 16), date(2023, 11, 17),
    ]


def test_night_classifier_far_longitude_out_of_order_sun_times():
    # At longitudes far from the prime meridian a single UTC date's astral
    # (sunrise, sunset) come back OUT OF ORDER (sunset earlier than sunrise) — they
    # belong to different local days. A naive [sunrise, sunset) test would then flag
    # every hour as night. Model that shape (as real astral returns for e.g. Los
    # Angeles): sunrise 15:00, sunset 01:00 UTC on each date.
    def out_of_order(lat, lon, day):
        return (
            datetime(day.year, day.month, day.day, 15, 0, tzinfo=UTC),  # sunrise
            datetime(day.year, day.month, day.day, 1, 0, tzinfo=UTC),   # sunset (earlier!)
        )

    classify = suntimes.night_classifier(34.0, -118.0, sun_provider=out_of_order)
    # 20:00 UTC = local noon -> DAY (must not be misclassified as night).
    assert classify(_ms(datetime(2026, 1, 15, 20, 0, tzinfo=UTC))) is False
    # 05:00 UTC (after the 01:00 sunset, before the 15:00 sunrise) -> NIGHT.
    assert classify(_ms(datetime(2026, 1, 15, 5, 0, tzinfo=UTC))) is True


def test_night_classifier_unresolvable_date_falls_back_to_day_without_raising():
    calls: "list[date]" = []

    def boom(lat, lon, day):
        calls.append(day)
        raise ValueError("polar day: sun never sets")

    classify = suntimes.night_classifier(78.0, 15.0, sun_provider=boom)
    # A polar / failing window must not crash the scorecard: with no date in the
    # 3-day window resolvable it classifies as day (False), and each failed date is
    # cached (not retried per frame).
    assert classify(_ms(datetime(2023, 6, 21, 2, 0, tzinfo=UTC))) is False
    assert classify(_ms(datetime(2023, 6, 21, 14, 0, tzinfo=UTC))) is False
    assert calls == [date(2023, 6, 20), date(2023, 6, 21), date(2023, 6, 22)]


# --- astral_available + the real sun_times path ------------------------------


def test_astral_available_returns_bool():
    assert isinstance(suntimes.astral_available(), bool)


@pytest.mark.skipif(not suntimes.astral_available(), reason="astral not installed")
def test_sun_times_real_astral_returns_ordered_utc_instants():
    sunrise, sunset = suntimes.sun_times(55.6, 12.5, date(2026, 7, 25))
    assert sunrise.tzinfo is not None and sunset.tzinfo is not None
    assert sunrise < sunset
    # A Copenhagen summer noon is unambiguously daytime through the real astral path.
    classify = suntimes.night_classifier(55.6, 12.5)
    assert classify(_ms(datetime(2026, 7, 25, 12, 0, tzinfo=UTC))) is False
