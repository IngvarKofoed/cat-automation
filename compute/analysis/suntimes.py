"""Offline sunrise/sunset for the day/night tuning-scorecard split.

The motion-gate scorecard (``Store.gate_scorecard``) can be split into a **Day**
and a **Night** column so an IR-night gate regression can't hide behind a good
daytime number (see the admin-next redesign spec, P2). Deciding which side of
sunrise/sunset a frame falls on needs sun times for arbitrary *past* dates, with
no network call — so it is computed offline with ``astral``, the same
pure-Python dependency the edge already trusts for its night-light scheduler
(``edge/server/night_light.py``).

This module is deliberately import-light: ``astral`` is imported **lazily**
inside :func:`sun_times`, so importing ``compute.analysis.suntimes`` (or anything
that transitively imports it, like ``compute.api.app``) never requires the
dependency. Callers that want to know up front whether the split can be computed
probe :func:`astral_available`.

All datetimes are tz-aware **UTC** — ``astral.sun.sun`` returns UTC instants, and
``frames.recv_ts`` (epoch milliseconds) is likewise UTC.
"""
from __future__ import annotations

from datetime import date as _date
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, Tuple

_ONE_DAY = timedelta(days=1)

# A provider maps ``(lat, lon, day)`` to that local date's ``(sunrise, sunset)``
# as tz-aware UTC instants. The default is :func:`sun_times` (astral-backed); the
# seam exists so the bucketing logic can be unit-tested with a synthetic provider
# and no astral install (see compute/tests/test_suntimes.py).
SunProvider = Callable[[float, float, _date], Tuple[datetime, datetime]]


def astral_available() -> bool:
    """Whether ``astral`` (and its ``sun`` module) can be imported.

    The day/night split is only offered when this is true; otherwise the caller
    reports the split *unavailable* rather than silently classifying every frame
    as day. Any import failure — the package absent, or a broken partial
    install — reads as unavailable.
    """
    try:
        import astral  # noqa: F401
        import astral.sun  # noqa: F401
    except Exception:  # noqa: BLE001 - any import problem means "cannot compute"
        return False
    return True


def sun_times(lat: float, lon: float, day: _date) -> "Tuple[datetime, datetime]":
    """``(sunrise, sunset)`` as tz-aware UTC instants for ``day`` at ``(lat, lon)``.

    ``astral`` is imported lazily inside the call so importing this module never
    needs the dependency (matching the edge's ``_astral_sun_provider``). Raises
    if ``astral`` is absent (``ImportError``) or the date has no sunrise/sunset —
    a polar day / polar night at high latitude (``ValueError`` from astral); the
    caller (:func:`night_classifier`) decides how to degrade.
    """
    from astral import Observer
    from astral.sun import sun

    s = sun(Observer(latitude=lat, longitude=lon), date=day)
    return (s["sunrise"], s["sunset"])


def _as_utc(t: datetime) -> datetime:
    """Coerce ``t`` to tz-aware UTC (a naive datetime is assumed already UTC)."""
    return t if t.tzinfo is not None else t.replace(tzinfo=timezone.utc)


def is_night(t: datetime, sunrise: datetime, sunset: datetime) -> bool:
    """Pure boundary test: is instant ``t`` outside the ``[sunrise, sunset)`` day.

    Night is ``t < sunrise`` (before dawn) or ``t >= sunset`` (after dusk); the
    daytime window is half-open so a frame exactly at sunrise counts as day and
    one exactly at sunset counts as night. All three arguments are normalized to
    UTC first, so a caller may pass naive-UTC datetimes.
    """
    t, sunrise, sunset = _as_utc(t), _as_utc(sunrise), _as_utc(sunset)
    return t < sunrise or t >= sunset


def night_classifier(
    lat: float,
    lon: float,
    *,
    sun_provider: "Optional[SunProvider]" = None,
) -> "Callable[[int], bool]":
    """Build ``is_night(recv_ts_ms) -> bool`` for ``(lat, lon)``.

    ``recv_ts_ms`` is epoch **milliseconds** (UTC), exactly as stored in
    ``frames.recv_ts``. A frame is classified by the **most recent sun event at or
    before it**: a sunrise means the sun is up (day), a sunset that it is down
    (night). The classifier gathers the sunrise/sunset events of the day *before*,
    *of*, and *after* the frame's UTC date and picks the latest one ``<= t``.

    Why not simply test ``t`` against its own UTC date's ``(sunrise, sunset)``: at
    longitudes far from the prime meridian those two events belong to *different*
    local days and come back out of order (sunset numerically earlier than
    sunrise), so a naive ``[sunrise, sunset)`` test flags all 24 h as night. The
    event-ordering test is correct at any longitude. (The naive form was dormant
    at the Copenhagen deployment, where sunrise < sunset within the UTC date.)

    Sun times per date come from ``sun_provider`` (default: the astral-backed
    :func:`sun_times`) and are **cached per calendar date**, so the 3-day window is
    computed at most once per date. A date whose computation raises — a polar
    day/night, or ``astral`` failing after :func:`astral_available` passed — is
    cached as unresolvable; if no date in the window resolves, the frame is
    classified **day** (``False``) so the split never crashes. Callers that need
    the honest "cannot compute" signal probe :func:`astral_available` first.
    """
    provider = sun_provider or sun_times
    cache: "dict[_date, Optional[Tuple[datetime, datetime]]]" = {}

    def _times_for(day: _date) -> "Optional[Tuple[datetime, datetime]]":
        if day not in cache:
            try:
                cache[day] = provider(lat, lon, day)
            except Exception:  # noqa: BLE001 - polar date / astral hiccup → unresolvable
                cache[day] = None
        return cache[day]

    def classify(recv_ts_ms: int) -> bool:
        t = datetime.fromtimestamp(recv_ts_ms / 1000.0, tz=timezone.utc)
        base = t.date()
        events: "list[Tuple[datetime, bool]]" = []
        resolved = False
        for day in (base - _ONE_DAY, base, base + _ONE_DAY):
            times = _times_for(day)
            if times is None:
                continue
            resolved = True
            events.append((_as_utc(times[0]), True))   # sunrise
            events.append((_as_utc(times[1]), False))  # sunset
        if not resolved:
            return False  # polar / unresolvable around t → treat as day (never crash)
        prior = [e for e in events if e[0] <= t]
        if not prior:
            return False  # t precedes every event in the 3-day window → treat as day
        prior.sort()
        return not prior[-1][1]  # night iff the latest event <= t was a sunset

    return classify


__all__ = ["astral_available", "sun_times", "is_night", "night_classifier", "SunProvider"]
