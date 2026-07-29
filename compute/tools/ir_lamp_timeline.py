"""Plot the IR lamp's output across a night — brightness, colourfulness, motion. Read-only.

Built to answer three questions about the illuminator dropout diagnosed 2026-07-29
(the lamp collapses to a few percent output around civil dusk and stays there until
dawn), none of which can be settled by scrubbing frames by eye:

1. **Flat floor or ripple?** A thermal *shutdown* with hysteresis cycles — cut, cool,
   restart — on a period of minutes. A thermal *foldback* (current tapering
   continuously) settles at a stable reduced output and does not oscillate. A slow
   ripple in an already-dark scene is invisible when paging through frames, so
   "it was dark all night" is what BOTH look like until you plot it. This script runs
   an explicit spectral test and prints the dominant period if there is one.
2. **How dark, exactly?** Mean luma says whether the lamp is off or merely starved.
3. **When did it move?** With the window widened past dusk/dawn, the sun-event
   overlay shows whether the transitions track civil twilight (ambient-driven) or a
   fixed interval after turn-on (thermal).

Colourfulness is plotted beside luma because it separates the two illuminants:
IR floods all three channels roughly equally (grey), daylight does not. Brightness
alone conflates "lamp dimmed" with "night got darker"; the pair does not.

Why it does NOT reuse ``Store``
-------------------------------
It opens its own short-lived SQLite connection instead — the move
``Store.lighting_histogram`` / ``tuning_calendar`` make, for the same reason. The
compute PC collects continuously, and ``Store`` funnels every read through one
shared write-locked connection; a windowed scan there is the collector starvation
that changelog 102-105 removed. Nothing here writes, and ``Store.__init__`` would
also run schema init against a live DB for no benefit.

Sun times come from ``astral`` directly rather than
``compute.analysis.suntimes.sun_times``, which returns only ``(sunrise, sunset)`` —
the interesting boundaries here are civil **dusk** and **dawn**, since that is where
the lamp was observed to move.

Usage (from the repo root, in the compute venv):
    python -m compute.tools.ir_lamp_timeline
    python -m compute.tools.ir_lamp_timeline --from "2026-07-28 21:30" --to "2026-07-28 23:30"
    python -m compute.tools.ir_lamp_timeline --interval 2 --out dusk
    CAT_COLLECT_DIR=D:/cat/data python -m compute.tools.ir_lamp_timeline

Times are LOCAL (the OS timezone) — naive strings, no tz suffix. The CSV is always
written; the PNG needs matplotlib (an analysis extra, see requirements-analysis.txt).
"""
from __future__ import annotations

import argparse
import csv
import math
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone

from shared.motion import lighting_measure

# Same env vars/defaults as compute/api/app.py's _store_from_env and the other
# tools, so every entry point points at one store without a shared config module.
_ENV_DIR = "CAT_COLLECT_DIR"
_DEFAULT_DIR = "./data/collection"

# Below this mean luma (0-255) shared.motion.lighting_measure reports colourfulness
# 0.0 by construction — an unlit scene has no colour to measure. Restated here (the
# constant itself is private) so the summary can WARN when most of the window sits
# under it, rather than letting a flat zero read as "measured monochrome".
_DARK_LUMA = 4.0

# Ripple search bounds. The floor is two sample intervals (Nyquist); the ceiling is
# 30 min, above which a "period" over a night-length window is drift, not cycling.
_RIPPLE_MAX_PERIOD_S = 1800.0
# ...but the ceiling is ALSO capped so the window holds at least this many cycles. A
# "period" a third the width of the window is indistinguishable from leftover trend —
# without this the test called a 17.8 min ripple on a 54 min window of ordinary dawn
# brightening, at 43x the spectral noise. Trend leaks into the lowest searched bin and
# swamps the band median, so a huge ratio there is expected, not evidence.
_RIPPLE_MIN_CYCLES = 4.0
# High-pass window for detrending, as a multiple of the longest period searched: the
# moving median removes anything slower than the ripples we care about (cooling
# curves, dawn) so the FFT sees oscillation and not trend.
_DETREND_FACTOR = 2.0
# A spectral peak counts as a ripple only if it stands this far above the median
# spectral magnitude, clears _RIPPLE_MIN_ABS luma (else it is quantization noise in a
# dark frame), and is _RIPPLE_MIN_PCT of the mean (else it is real but irrelevant).
# Heuristics — every number behind them is printed so the call stays the reader's.
_RIPPLE_PEAK_RATIO = 4.0
_RIPPLE_MIN_ABS = 0.15
_RIPPLE_MIN_PCT = 2.0


def _resolve_root(root_override: "str | None") -> str:
    """The store root: ``--dir`` if given, else ``$CAT_COLLECT_DIR``, else the default."""
    return root_override if root_override is not None else os.environ.get(_ENV_DIR, _DEFAULT_DIR)


def _parse_local(text: str) -> datetime:
    """Parse ``YYYY-MM-DD HH:MM[:SS]`` as a LOCAL instant, returned tz-aware.

    ``astimezone()`` on a naive datetime reads the OS timezone, which works on
    Windows without the ``tzdata`` package — unlike ``zoneinfo``, whose named zones
    need it and would make the compute PC (the box that actually has the frames) the
    one machine this fails on.
    """
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt).astimezone()
        except ValueError:
            continue
    raise SystemExit(f"could not parse {text!r} — expected 'YYYY-MM-DD HH:MM'")


def _default_window() -> "tuple[datetime, datetime]":
    """Yesterday 23:00 → today 03:00, local — the collapsed-state window."""
    today = date.today()
    yesterday = today - timedelta(days=1)
    return (
        datetime(yesterday.year, yesterday.month, yesterday.day, 23, 0).astimezone(),
        datetime(today.year, today.month, today.day, 3, 0).astimezone(),
    )


def _sun_events(lat: float, lon: float, days: "list[date]") -> "list[tuple[str, datetime]]":
    """``[(label, instant)]`` for dawn/sunrise/sunset/dusk over ``days``, tz-aware UTC.

    Returns ``[]`` if ``astral`` is missing or a day has no such events (polar) —
    the overlay is context, never a reason to fail the run.
    """
    try:
        from astral import Observer
        from astral.sun import sun
    except Exception:  # noqa: BLE001 - astral absent → no overlay, not an error
        return []
    obs = Observer(latitude=lat, longitude=lon)
    out: "list[tuple[str, datetime]]" = []
    for day in days:
        try:
            s = sun(obs, date=day)
        except Exception:  # noqa: BLE001 - polar date → skip that day
            continue
        for key, label in (
            ("dawn", "civil dawn"),
            ("sunrise", "sunrise"),
            ("sunset", "sunset"),
            ("dusk", "civil dusk"),
        ):
            if key in s:
                out.append((label, s[key]))
    return sorted(out, key=lambda p: p[1])


def _read_location(db_path: str) -> "tuple[float, float] | None":
    """(lat, lon) from the store's settings KV, or ``None`` if unset/unparseable.

    Mirrors ``Store.get_location``'s contract (including its non-finite guard) on
    this tool's own connection; a bad value degrades to "never configured" so the
    plot simply loses its sun lines.
    """
    conn = sqlite3.connect(db_path)
    try:
        rows = dict(
            conn.execute(
                "SELECT key, value FROM settings WHERE key IN ('location_lat', 'location_lon')"
            ).fetchall()
        )
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    try:
        lat, lon = float(rows["location_lat"]), float(rows["location_lon"])
    except (KeyError, TypeError, ValueError):
        return None
    return (lat, lon) if math.isfinite(lat) and math.isfinite(lon) else None


def _sample_rows(db_path: str, start_ms: int, end_ms: int, interval_ms: int) -> "list[tuple]":
    """One row per ``interval_ms`` of recv_ts across the window: ``(id, recv_ts, path, motion)``.

    Same bucket-and-take-earliest decimation as ``Store.sample_frames_by_interval``
    (a true wall-clock rate, unaffected by capture fps or collector gaps), extended
    with ``path``/``motion`` and run on this tool's own connection. Empty intervals
    yield no row, so a collector gap shows as a gap rather than a stretched line.
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        row = conn.execute(
            "SELECT MIN(recv_ts), COUNT(*) FROM frames WHERE recv_ts >= ? AND recv_ts <= ?",
            (start_ms, end_ms),
        ).fetchone()
        if not row or not row[1]:
            return []
        origin = int(row[0])
        return conn.execute(
            "SELECT id, recv_ts, path, motion FROM ("
            "  SELECT id, recv_ts, path, motion,"
            "    ROW_NUMBER() OVER ("
            "      PARTITION BY (recv_ts - ?) / ? ORDER BY recv_ts ASC, id ASC"
            "    ) AS rn"
            "  FROM frames WHERE recv_ts >= ? AND recv_ts <= ?"
            ") WHERE rn = 1 ORDER BY recv_ts ASC, id ASC",
            (origin, max(1, interval_ms), start_ms, end_ms),
        ).fetchall()
    finally:
        conn.close()


def _measure(rows: "list[tuple]", media_root: str) -> "tuple[list[dict], int]":
    """Decode each sampled frame and measure it. Returns ``(samples, n_missing)``.

    ``n_missing`` counts rows whose JPEG is gone or undecodable (evicted mid-run, or
    an orphaned row) — reported rather than silently dropped, since a large count
    would mean the plot covers less of the window than it appears to.
    """
    import cv2

    samples: "list[dict]" = []
    missing = 0
    # Decoding dominates the runtime and a fine --interval over a long window can mean
    # tens of thousands of frames, so report progress rather than going silent for
    # minutes. Deliberately no cap on the sample count: the interval is the operator's
    # explicit choice, and refusing it would just hide the cost behind a second flag.
    total = len(rows)
    for i, (row_id, recv_ts, rel_path, motion) in enumerate(rows, 1):
        if i % 2000 == 0:
            print(f"         {i}/{total}…", flush=True)
        img = cv2.imread(os.path.join(media_root, rel_path), cv2.IMREAD_COLOR)
        if img is None:
            missing += 1
            continue
        colourfulness, luma = lighting_measure(img)
        samples.append(
            {
                "id": int(row_id),
                "recv_ts": int(recv_ts),
                "luma": float(luma),
                "colourfulness": float(colourfulness),
                "motion": int(motion or 0),
            }
        )
    return samples, missing


def _ripple_test(samples: "list[dict]", interval_s: float) -> dict:
    """Spectral test for a periodic ripple in the luma series.

    Resamples onto a uniform grid (empty buckets interpolated), high-pass filters
    with a moving median far longer than the periods searched — so a cooling curve
    or the approach of dawn is removed as trend rather than fitted as a very long
    "period" — then takes the FFT and reports the strongest component between the
    Nyquist floor (two sample intervals) and a ceiling that is BOTH
    ``_RIPPLE_MAX_PERIOD_S`` and ``span / _RIPPLE_MIN_CYCLES``, whichever is smaller.

    Returns the numbers AND the verdict, so a reader who disagrees with the
    thresholds can apply their own.
    """
    import numpy as np

    if len(samples) < 16:
        return {"ok": False, "why": "too few samples"}

    t = np.array([s["recv_ts"] for s in samples], dtype=float) / 1000.0
    y = np.array([s["luma"] for s in samples], dtype=float)
    # Uniform grid at the requested interval; gaps (stopped collector, empty
    # buckets) are linearly interpolated so the FFT sees even spacing.
    grid = np.arange(t[0], t[-1] + interval_s, interval_s)
    if grid.size < 16:
        return {"ok": False, "why": "window too short"}
    uniform = np.interp(grid, t, y)

    # Longest period worth searching: capped both absolutely and by the window's own
    # width, so we never report a "period" the window can't show repeating.
    span_s = float(grid[-1] - grid[0])
    max_period = min(_RIPPLE_MAX_PERIOD_S, span_s / _RIPPLE_MIN_CYCLES)
    if max_period < 2.0 * interval_s:
        return {
            "ok": False,
            "why": f"window too short — needs >= {_RIPPLE_MIN_CYCLES:g} cycles of a "
                   f"period above the {2 * interval_s:g}s Nyquist floor",
        }

    # Moving-median high pass, twice the longest searched period. Odd, >= 3 samples,
    # and shorter than the series (else it flattens everything to a constant).
    win = max(3, int(_DETREND_FACTOR * max_period / interval_s) | 1)
    if win >= uniform.size:
        win = max(3, (uniform.size // 2) | 1)
    pad = win // 2
    padded = np.pad(uniform, pad, mode="edge")
    trend = np.array(
        [np.median(padded[i : i + win]) for i in range(uniform.size)], dtype=float
    )
    detrended = uniform - trend

    spectrum = np.abs(np.fft.rfft(detrended * np.hanning(detrended.size)))
    freqs = np.fft.rfftfreq(detrended.size, d=interval_s)
    # Amplitude scaling: Hann halves the coherent gain, hence the 4/N rather than 2/N.
    amps = spectrum * 4.0 / detrended.size

    band = (freqs > 0) & (freqs >= 1.0 / max_period) & (freqs <= 0.5 / interval_s)
    band_idx = np.flatnonzero(band)
    if band_idx.size == 0:
        return {"ok": False, "why": "no usable frequency band"}

    # Pick the peak from the band's OWN indices, never by argmax over a masked full
    # spectrum: when the detrend leaves a near-perfectly flat residual (a constant or
    # a clean linear ramp) every in-band amplitude is 0.0, and argmax then returns
    # index 0 — the DC bin, outside the band — whose zero frequency made period_s inf.
    idx = int(band_idx[np.argmax(amps[band_idx])])
    peak_amp = float(amps[idx])
    noise = float(np.median(amps[band_idx])) or 1e-12
    mean_luma = float(np.mean(uniform))
    ratio = peak_amp / noise
    pct = (100.0 * peak_amp / mean_luma) if mean_luma > 0 else 0.0
    return {
        "ok": True,
        "period_s": float(1.0 / freqs[idx]),
        "max_period_s": max_period,
        "amplitude": peak_amp,
        "amplitude_pct": pct,
        "noise": noise,
        "ratio": ratio,
        "mean_luma": mean_luma,
        "ripple": bool(
            ratio >= _RIPPLE_PEAK_RATIO
            and peak_amp >= _RIPPLE_MIN_ABS
            and pct >= _RIPPLE_MIN_PCT
        ),
    }


def _write_csv(path: str, samples: "list[dict]") -> None:
    """Raw per-sample readings — written unconditionally, so the PNG is optional."""
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["frame_id", "recv_ts_ms", "local_time", "luma", "colourfulness", "motion"])
        for s in samples:
            local = datetime.fromtimestamp(s["recv_ts"] / 1000.0).strftime("%Y-%m-%d %H:%M:%S")
            writer.writerow(
                [s["id"], s["recv_ts"], local, f"{s['luma']:.4f}", f"{s['colourfulness']:.6f}", s["motion"]]
            )


def _write_png(path: str, samples: "list[dict]", events: "list[tuple[str, datetime]]") -> "str | None":
    """Two stacked panels (luma, colourfulness) with sun events. ``None`` if unplottable."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
    except Exception as exc:  # noqa: BLE001 - matplotlib is an opt-in analysis extra
        return f"matplotlib unavailable ({exc}) — CSV only"

    times = [datetime.fromtimestamp(s["recv_ts"] / 1000.0) for s in samples]
    luma = [s["luma"] for s in samples]
    colour = [s["colourfulness"] for s in samples]

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 8), sharex=True)
    ax1.plot(times, luma, lw=0.9, color="#d08030")
    motion_t = [t for t, s in zip(times, samples) if s["motion"]]
    motion_y = [s["luma"] for s in samples if s["motion"]]
    if motion_t:
        ax1.scatter(motion_t, motion_y, s=8, color="#cc3333", zorder=3, label="motion")
        ax1.legend(loc="upper right", fontsize=8)
    ax1.set_ylabel("mean luma (0-255)")
    ax1.set_title("IR lamp output over time")
    ax1.axhline(_DARK_LUMA, ls=":", lw=0.8, color="#888")

    ax2.plot(times, colour, lw=0.9, color="#3080b0")
    ax2.set_ylabel("colourfulness")
    ax2.set_xlabel("local time")

    lo, hi = min(times), max(times)
    for label, when in events:
        local = when.astimezone().replace(tzinfo=None)
        if lo <= local <= hi:
            for ax in (ax1, ax2):
                ax.axvline(local, ls="--", lw=0.9, color="#666")
            ax1.annotate(
                label, (local, ax1.get_ylim()[1]), fontsize=8, rotation=90,
                va="top", ha="right", color="#666",
            )

    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    for ax in (ax1, ax2):
        ax.grid(alpha=0.25)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return None


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    default_from, default_to = _default_window()
    ap.add_argument("--dir", default=None, help=f"store root (default: ${_ENV_DIR} or {_DEFAULT_DIR!r})")
    ap.add_argument("--from", dest="start", default=None,
                    help=f"local start 'YYYY-MM-DD HH:MM' (default {default_from:%Y-%m-%d %H:%M})")
    ap.add_argument("--to", dest="end", default=None,
                    help=f"local end 'YYYY-MM-DD HH:MM' (default {default_to:%Y-%m-%d %H:%M})")
    ap.add_argument("--interval", type=float, default=5.0, help="seconds between samples (default 5)")
    ap.add_argument("--out", default="ir-lamp-timeline", help="output prefix for .csv/.png")
    ap.add_argument("--lat", type=float, default=None, help="override the store's latitude")
    ap.add_argument("--lon", type=float, default=None, help="override the store's longitude")
    args = ap.parse_args()

    root = _resolve_root(args.dir)
    db_path = os.path.join(root, "index.db")
    if not os.path.exists(db_path):
        raise SystemExit(f"no store DB at {db_path!r} (set --dir or {_ENV_DIR} to its parent)")
    media_root = os.path.join(root, "media")

    start = _parse_local(args.start) if args.start else default_from
    end = _parse_local(args.end) if args.end else default_to
    if end <= start:
        raise SystemExit(f"--to ({end}) must be after --from ({start})")
    interval_s = max(0.2, float(args.interval))

    print(f"store    {root}")
    print(f"window   {start:%Y-%m-%d %H:%M} -> {end:%Y-%m-%d %H:%M} local  ({interval_s:g}s sampling)")
    # Flush before the first step that can SystemExit: piped stdout is block-buffered
    # while stderr is not, so an error would otherwise print ABOVE its own context.
    sys.stdout.flush()

    rows = _sample_rows(
        db_path, int(start.timestamp() * 1000), int(end.timestamp() * 1000), int(interval_s * 1000)
    )
    if not rows:
        raise SystemExit("no frames in that window — check the dates and the store root")
    print(f"sampled  {len(rows)} frames, decoding…")

    samples, missing = _measure(rows, media_root)
    if not samples:
        raise SystemExit(f"none of the {len(rows)} sampled frames could be read from {media_root!r}")
    if missing:
        print(f"WARNING  {missing} sampled frame(s) unreadable (evicted or orphaned) — excluded")

    lumas = sorted(s["luma"] for s in samples)
    n = len(lumas)
    median = lumas[n // 2]
    dark = sum(1 for v in lumas if v < _DARK_LUMA)
    n_motion = sum(s["motion"] for s in samples)

    print()
    print("--- brightness (mean luma, 0-255) ---")
    print(f"  samples      {n}   ({n_motion} with motion)")
    print(f"  min/med/max  {lumas[0]:.2f} / {median:.2f} / {lumas[-1]:.2f}")
    print(f"  p05/p95      {lumas[int(0.05 * n)]:.2f} / {lumas[int(0.95 * n)]:.2f}")
    if dark:
        pct = 100.0 * dark / n
        print(f"  NOTE  {dark} sample(s) ({pct:.0f}%) below luma {_DARK_LUMA} — "
              "lighting_measure reports colourfulness 0.0 there BY CONSTRUCTION,")
        print("        so a flat zero in the lower panel over those frames is not a measurement.")

    print()
    print("--- ripple test (is the collapsed state flat, or cycling?) ---")
    r = _ripple_test(samples, interval_s)
    if not r.get("ok"):
        print(f"  not run: {r.get('why')}")
    else:
        print(f"  searched periods   {2 * interval_s:g}s .. {r['max_period_s'] / 60.0:.1f} min"
              f"  (window must hold {_RIPPLE_MIN_CYCLES:g}+ cycles)")
        print(f"  strongest period   {r['period_s'] / 60.0:.1f} min")
        print(f"  its amplitude      {r['amplitude']:.3f} luma  ({r['amplitude_pct']:.1f}% of mean {r['mean_luma']:.2f})")
        print(f"  vs spectral noise  {r['ratio']:.1f}x median")
        print(f"  thresholds         {_RIPPLE_PEAK_RATIO:g}x noise, >{_RIPPLE_MIN_ABS} luma, >{_RIPPLE_MIN_PCT:g}% of mean")
        if r["ripple"]:
            print(f"  => RIPPLE: cycling on a ~{r['period_s'] / 60.0:.1f} min period.")
            print("     Consistent with thermal SHUTDOWN + hysteresis (cut, cool, restart),")
            print("     or a slow thermal feedback loop through the photocell. That period IS")
            print("     the thermal time constant.")
        else:
            print("  => FLAT: no significant periodicity.")
            print("     Consistent with thermal FOLDBACK — current tapering to a stable")
            print("     equilibrium — and against a cycling shutdown, which would oscillate.")

    location = (args.lat, args.lon) if args.lat is not None and args.lon is not None else _read_location(db_path)
    events: "list[tuple[str, datetime]]" = []
    if location:
        days = sorted({start.astimezone(timezone.utc).date(), end.astimezone(timezone.utc).date()})
        events = _sun_events(location[0], location[1], days)
        if events:
            print()
            print(f"--- sun events at {location[0]:.4f}, {location[1]:.4f} ---")
            for label, when in events:
                mark = "  <-- in window" if start <= when <= end else ""
                print(f"  {label:<12} {when.astimezone():%Y-%m-%d %H:%M:%S}{mark}")
        else:
            print("\n  (no sun overlay — astral not installed, or a polar date)")
    else:
        print("\n  (no sun overlay — no location set in the store; pass --lat/--lon)")

    csv_path = f"{args.out}.csv"
    png_path = f"{args.out}.png"
    _write_csv(csv_path, samples)
    print()
    print(f"wrote  {csv_path}")
    problem = _write_png(png_path, samples, events)
    print(f"wrote  {png_path}" if problem is None else f"no PNG: {problem}")


if __name__ == "__main__":
    main()
