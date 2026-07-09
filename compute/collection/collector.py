"""The background collector loop: edge stream → store, always on.

A single function run as a daemon thread by the web app. It consumes the
existing auto-reconnecting frame feed (``EdgeClient.iter_stream_reconnecting()``,
which already owns reconnection/backoff — no logic to re-invent here) and writes
each frame verbatim into the ``Store``. Between frames it checks a stop event so
the app can ask it to wind down.

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
