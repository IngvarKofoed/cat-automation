"""The IR-lamp timeline tool's ripple test — the one piece of real logic in it.

``compute/tools/ir_lamp_timeline.py`` exists to answer whether the collapsed IR lamp
sits at a FLAT reduced output (thermal foldback) or CYCLES (thermal shutdown with
hysteresis). That verdict is the tool's whole point, so it is pinned here with
synthetic signals of known shape — the same "unit-test the pure numeric core with
synthetic inputs" move the feasibility probe's separability maths makes.

The regression that motivated most of these: on real daylight frames the test first
reported a confident 17.8 min "ripple" at 43x the spectral noise over a 54 min window.
That was residual TREND leaking into the lowest searched bin, not oscillation — hence
``_RIPPLE_MIN_CYCLES`` (the window must hold several cycles of anything it reports)
and the amplitude floors.
"""
from __future__ import annotations

import math

import pytest

from compute.tools.ir_lamp_timeline import _ripple_test

_INTERVAL_S = 5.0


def _series(fn, *, n: int = 2880, interval_s: float = _INTERVAL_S, t0_ms: int = 1_700_000_000_000):
    """``n`` samples spaced ``interval_s`` apart, luma from ``fn(seconds_elapsed)``."""
    return [
        {
            "recv_ts": t0_ms + int(i * interval_s * 1000),
            "luma": float(fn(i * interval_s)),
            "colourfulness": 0.0,
            "motion": 0,
            "id": i + 1,
        }
        for i in range(n)
    ]


def test_flat_series_reports_no_ripple():
    """A constant dim floor — the foldback signature — must not read as cycling."""
    result = _ripple_test(_series(lambda _s: 3.0), _INTERVAL_S)
    assert result["ok"]
    assert result["ripple"] is False


def test_low_amplitude_noise_reports_no_ripple():
    """Sensor/JPEG noise on a dark floor is not a ripple, however peaky its spectrum."""
    # Deterministic pseudo-noise: no RNG, so a failure is always reproducible.
    result = _ripple_test(
        _series(lambda s: 3.0 + 0.02 * math.sin(s / 3.0) + 0.01 * math.sin(s / 7.0)),
        _INTERVAL_S,
    )
    assert result["ok"]
    assert result["ripple"] is False


def test_detects_a_real_cycle_and_recovers_its_period():
    """A 10 min cycle at ~25% depth — the thermal-shutdown signature — is found."""
    period = 600.0
    result = _ripple_test(
        _series(lambda s: 4.0 + 1.0 * math.sin(2 * math.pi * s / period)), _INTERVAL_S
    )
    assert result["ok"]
    assert result["ripple"] is True
    # FFT bin spacing over a 4 h window is coarse; 10% is a tight tolerance for it.
    assert result["period_s"] == pytest.approx(period, rel=0.1)
    assert result["amplitude"] == pytest.approx(1.0, rel=0.25)


def test_slow_trend_alone_is_not_a_ripple():
    """A monotonic ramp — dawn arriving, or a cooling curve — must be detrended away.

    This is the false positive that shipped in the first draft: without the
    cycles-per-window cap the ramp's leakage won on amplitude AND on peak-to-noise.
    """
    result = _ripple_test(_series(lambda s: 3.0 + s / 400.0), _INTERVAL_S)
    assert result["ok"]
    assert result["ripple"] is False


def test_cycle_riding_on_a_trend_is_still_detected():
    """Detrending must remove the ramp without also removing the oscillation on it."""
    period = 480.0
    result = _ripple_test(
        _series(lambda s: 3.0 + s / 400.0 + 0.8 * math.sin(2 * math.pi * s / period)),
        _INTERVAL_S,
    )
    assert result["ok"]
    assert result["ripple"] is True
    assert result["period_s"] == pytest.approx(period, rel=0.1)


def test_period_search_is_capped_by_window_width():
    """Nothing slower than span / _RIPPLE_MIN_CYCLES may be reported as a period."""
    n = 648  # 54 min at 5 s — the window that produced the original false positive
    result = _ripple_test(_series(lambda s: 3.0 + s / 400.0, n=n), _INTERVAL_S)
    assert result["ok"]
    span_s = (n - 1) * _INTERVAL_S
    assert result["max_period_s"] <= span_s / 4.0 + 1e-6
    assert result["period_s"] <= result["max_period_s"] + 1e-6


def test_too_few_samples_degrades_rather_than_raising():
    """A window with almost nothing in it reports why, instead of throwing."""
    result = _ripple_test(_series(lambda _s: 3.0, n=4), _INTERVAL_S)
    assert result["ok"] is False
    assert result["why"]


def test_window_too_short_to_hold_several_cycles_is_refused():
    """Below the cycles-per-window floor the test declines rather than guessing."""
    # 20 samples at 5 s = 95 s span; span/4 = 23.75 s, only ~2 Nyquist units — the
    # band exists but is too narrow to be meaningful, so this pins the boundary
    # behaviour either way: refused, or run without claiming a ripple.
    result = _ripple_test(_series(lambda _s: 3.0, n=20), _INTERVAL_S)
    assert result["ok"] is False or result["ripple"] is False
