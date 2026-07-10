"""The background collector loop: edge stream → store, always on — and its manager.

Two pieces:

- ``run_collector`` — a single function run as a daemon thread. It consumes the
  existing auto-reconnecting frame feed (``EdgeClient.iter_stream_reconnecting()``,
  which already owns reconnection/backoff — no logic to re-invent here) and writes
  each frame verbatim into the ``Store``. Between frames it checks a stop event so
  the app can ask it to wind down.
- ``CollectorManager`` — wraps that loop so the web app can *freeze* the store
  (stop ingest) for a clean offline analysis pass and resume it later, instead of
  the collector being a fire-once thread bolted on at app start. It owns the
  ``(thread, stop_event, running)`` triple and lets the UI toggle it at runtime
  (see the motion-gate-oracles spec and ``compute/api/app.py``).

No dedup is needed: the stream delivers each frame once, and the store's row
``id`` is unique even across an edge restart (where ``frame_id`` repeats but the
compute-side insertion order does not).
"""
from __future__ import annotations

import logging
import threading
import time

from compute.collection.store import Store

logger = logging.getLogger(__name__)

# Log a progress line every N stored frames — enough to confirm the collector is
# alive and see the store growing, without flooding at 10 fps.
_LOG_EVERY = 500


def run_collector(client, store: Store, stop_event: threading.Event) -> None:
    """Loop the reconnecting stream into ``store`` until ``stop_event`` is set.

    ``recv_ts`` is stamped from the compute clock here (``int(time.time()*1000)``)
    — the reliable time axis, since the Pi has no RTC — while ``edge_ts`` and
    ``frame_id`` ride along in ``frame.meta`` for reference only.
    """
    saved = 0
    errors = 0
    logger.info("collector started")
    for frame in client.iter_stream_reconnecting():
        if stop_event.is_set():
            break
        try:
            store.add(frame, int(time.time() * 1000))
        except Exception:
            # A per-frame store failure — a transient disk-full/permission error,
            # a momentarily locked DB — must not kill the always-on collector: the
            # stream keeps flowing and the next frame may well succeed. Log it and
            # move on, but throttle (first, then every _LOG_EVERY) so a persistent
            # fault can't flood the log at frame rate. Reconnection is the client's
            # job; surviving a bad write is ours.
            errors += 1
            if errors == 1 or errors % _LOG_EVERY == 0:
                logger.exception("collector: store.add failed (%d dropped this run)", errors)
            continue
        saved += 1
        if saved % _LOG_EVERY == 0:
            st = store.stats()
            logger.info(
                "collector: %d frames saved this run; store %d frames, %.1f/%.1f MB",
                saved,
                st["count"],
                st["bytes"] / 1e6,
                st["cap_bytes"] / 1e6,
            )
    logger.info("collector stopped after %d frames this run", saved)


# How long ``start`` waits for a just-stopped collector thread to actually exit
# before spawning its replacement. It bounds the one real overlap window (see
# ``start``): a thread told to stop is usually blocked inside
# ``iter_stream_reconnecting`` waiting for the next frame, so it only notices the
# stop between frames — a few hundred ms at ~5–10 fps. We wait a little longer than
# that, but never indefinitely, so a stalled/reconnecting edge can't wedge the HTTP
# handler that called ``start``.
_STOP_JOIN_TIMEOUT_S = 2.0


class CollectorManager:
    """Runtime start/stop control for the always-on collector loop.

    Wraps ``run_collector`` so the browse UI can freeze the store for a clean
    offline analysis pass and resume it afterwards. It owns the single active run's
    ``(thread, stop_event)`` and an authoritative ``running`` flag.

    ``running`` is a flag the manager sets *synchronously* in ``start``/``stop`` —
    deliberately NOT derived from ``thread.is_alive()``. Two reasons: the API
    contract is "the route just toggles and reports the resulting state" (see
    ``compute/api/app.py``), so the flag must flip the instant the call returns;
    and thread liveness is a poor proxy anyway — a collector fed an empty/finished
    stream returns at once yet the operator's intent was still "running", while a
    thread blocked on a socket read looks alive long after ``stop`` asked it to
    quit. The flag tracks *intent*; the thread winds down on its own schedule.

    A ``None`` client (a test app built with ``start_collector=False`` and no edge)
    means there is nothing to stream: ``start`` refuses rather than spawning a
    thread that would immediately ``AttributeError`` on ``None.iter_stream_*`` —
    keeping ``running`` honestly False. Every real caller injects a client.
    """

    def __init__(self, client, store: Store) -> None:
        self._client = client
        self._store = store
        # Guards the (thread, stop_event, running) triple so a status read and a
        # concurrent start/stop toggle never see a torn state. Held only for the
        # quick bookkeeping — never across the collector loop, which runs in its
        # own thread and touches only the (separately locked) store.
        self._lock = threading.Lock()
        self._thread: "threading.Thread | None" = None
        self._stop_event: "threading.Event | None" = None
        self._running = False

    def start(self) -> None:
        """Start collecting; idempotent, and a no-op when already running or client-less.

        A stopped run is replaced by a *fresh* thread + stop event — a
        ``threading.Thread`` and a set ``Event`` are both one-shot, so resuming
        means new objects, not reusing the old ones. Before spawning the
        replacement we best-effort join any prior thread (bounded by
        ``_STOP_JOIN_TIMEOUT_S``) so a rapid stop→start doesn't leave the previous
        collector — likely still blocked on its final stream read — briefly running
        a second, overlapping connection. If the join times out we proceed anyway:
        the old thread carries the *old* (set) stop event and will exit at its next
        frame boundary, so the overlap is transient and self-healing, and bounding
        the wait matters more than eliminating that rare window.
        """
        with self._lock:
            if self._running:
                return
            if self._client is None:
                # Nothing to stream from — stay honestly stopped (see class docstring).
                return
            stale = self._thread
            stop_event = threading.Event()
            thread = threading.Thread(
                target=run_collector,
                args=(self._client, self._store, stop_event),
                name="collector",
                daemon=True,
            )
            self._stop_event = stop_event
            self._thread = thread
            self._running = True
        # Join the previous thread and start the new one OUTSIDE the lock: the join
        # can block up to the timeout (a status poll must not stall behind it), and
        # thread spin-up needs nothing the lock protects.
        if stale is not None and stale.is_alive():
            stale.join(timeout=_STOP_JOIN_TIMEOUT_S)
        thread.start()

    def stop(self) -> None:
        """Stop collecting; idempotent. Signals the loop and flips ``running`` at once.

        Sets the current stop event and clears ``running`` synchronously so the
        caller (and the next ``/api/stats`` poll) sees "stopped" immediately. It
        does NOT join: the thread is usually parked in a blocking stream read and
        would only notice the flag at the next frame, so joining here could stall
        the HTTP handler for a frame interval; the daemon thread winds down on its
        own, and ``start`` handles any leftover before spawning a replacement.
        """
        with self._lock:
            if not self._running:
                return
            if self._stop_event is not None:
                self._stop_event.set()
            self._running = False

    @property
    def running(self) -> bool:
        """Whether collection is currently on (lock-guarded read of the intent flag)."""
        with self._lock:
            return self._running
