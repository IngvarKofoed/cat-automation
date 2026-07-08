"""Host CPU & memory metrics for the edge device's ``/status`` snapshot.

The Pi stays a thin, pure server: this reports the *host's* own load (CPU
utilization and memory usage), not per-process figures — the useful signal is
"is the device busy", not "how big is the Python process". The read is local,
read-only, and never touches the frame/motion path or dials out.

Cross-platform (Raspberry Pi OS + macOS dev) falls out of ``psutil`` doing the
per-OS work for us. It is fail-soft by design: if ``psutil`` is missing or a
reading raises, ``sample()`` returns ``None`` so ``/status`` still answers 200
with camera/motion intact — metrics must never break the health endpoint. See
docs/specs/2026-07-08-edge-system-metrics.md.
"""
from __future__ import annotations

import threading
import time

try:
    import psutil
except ImportError:  # metrics degrade to None rather than breaking /status
    psutil = None

# Recompute host CPU% at most once per this window; return the cached value in
# between. Chosen for a load indicator: the UI polls /status faster than this,
# but the CPU figure only needs to refresh ~every 2 s to stay meaningful.
CPU_WINDOW_S = 2.0


class SystemMetrics:
    """Owns the CPU sampling state; one instance per app (like ``Grabber``)."""

    def __init__(self) -> None:
        self._last_cpu_mono = 0.0
        self._last_cpu_pct: float | None = None
        # Bool sentinel for "never sampled" — a monotonic value can't stand in
        # for it, since monotonic() can legitimately be 0.0 near boot.
        self._cpu_primed = False
        # /status runs under Flask threaded=True and is polled concurrently by the
        # config UI and the compute tier, so guard the CPU sampling state (and
        # psutil's own process-global cpu_percent delta) against racing readers —
        # else two threads clear the window together and cache a spurious ~0%.
        self._cpu_lock = threading.Lock()

    def sample(self) -> dict | None:
        """Return the host metrics dict, or ``None`` if psutil is unavailable.

        Keys: ``cpu_percent`` (float rounded to 1 dp, or None until the first
        CPU window has elapsed / on a CPU-only read hiccup), ``mem_percent``,
        ``mem_used_mb``, ``mem_total_mb``.
        """
        if psutil is None:
            return None

        # Memory: a single instantaneous read. Derive both the percent and the
        # used-MB from total-available (not psutil's platform-dependent .used)
        # so the Pi and a Mac report comparable numbers. A failure here loses
        # the whole dict — but memory is the one field we can't degrade.
        try:
            vm = psutil.virtual_memory()
            mem_total_mb = round(vm.total / 1024**2)
            mem_used_mb = round((vm.total - vm.available) / 1024**2)
            mem_percent = round(vm.percent, 1)
        except Exception:
            return None

        # CPU: non-blocking and cadence-independent. cpu_percent(interval=None)
        # reports the busy fraction since the previous call, so we gate re-reads
        # behind a monotonic window — callers can poll as fast as they like
        # without distorting the figure. A CPU-only hiccup degrades to None
        # while still returning the good memory reading.
        with self._cpu_lock:
            now = time.monotonic()
            try:
                if not self._cpu_primed:
                    # psutil's first reading is meaningless; call once to prime its
                    # internal delta and discard the value, leaving cpu_percent None
                    # (the UI shows "—") until the first real window elapses.
                    psutil.cpu_percent(interval=None)
                    self._cpu_primed = True
                    self._last_cpu_mono = now
                elif now - self._last_cpu_mono >= CPU_WINDOW_S:
                    self._last_cpu_pct = round(psutil.cpu_percent(interval=None), 1)
                    self._last_cpu_mono = now
                # else: keep the cached _last_cpu_pct
            except Exception:
                # A read hiccup degrades cpu_percent to None, but still advance the
                # window (and mark primed) so we retry at most once per CPU_WINDOW_S
                # rather than hammering psutil on every poll during the failure.
                self._last_cpu_pct = None
                self._cpu_primed = True
                self._last_cpu_mono = now
            cpu_pct = self._last_cpu_pct

        return {
            "cpu_percent": cpu_pct,
            "mem_percent": mem_percent,
            "mem_used_mb": mem_used_mb,
            "mem_total_mb": mem_total_mb,
        }
