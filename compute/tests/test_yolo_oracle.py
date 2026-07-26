"""Tests for the always-on YOLO-oracle worker (compute/learning/yolo_oracle).

``YoloOracleManager`` is an always-on tick loop (like ``CollectorManager`` /
``LiveIdentifyManager``), not a FIFO job queue, so these tests drive the *tick* directly.
Every seam that would touch torch or the GPU is injected, so the whole worker runs with
fakes and NO torch, NO model, no CUDA:

- ``_FakeStore`` — exposes only what the tick touches: ``latest_id`` (the frame horizon the
  window is capped to) and the ``get_setting``/``set_setting`` KV the watermark + intent
  persist through.
- ``_FakeDetect`` — records each ``(since_id, until_id)`` chunk AND the full kwargs, so the
  chunk boundaries, their order, and the load-bearing *absence* of ``motion_only`` (full
  coverage) can all be asserted.
- controllable ``now_ms`` / ``is_busy`` / ``motion_only`` closures — a fixed clock makes
  ``last_tick_ts`` deterministic; the two predicates prove the tick yields the GPU to a
  manual job and idles under motion-only capture.

The load-bearing behaviors these cover: full-coverage sweeping (no ``motion_only``), the
per-chunk ``is_busy`` re-check that lets an operator's job win mid-tick, the per-tick frame
cap that stops a backlog monopolizing the GPU, and the watermark only ever advancing past a
COMPLETED chunk.
"""
from __future__ import annotations

import threading
import time

from compute.learning.yolo_oracle import (
    _CHUNK,
    _MAX_FRAMES_PER_TICK,
    YoloOracleManager,
)


def _wait(pred, timeout: float = 5.0, interval: float = 0.005) -> bool:
    """Poll ``pred`` until true or ``timeout`` elapses; return its final value."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return bool(pred())


class _FakeStore:
    """In-memory store exposing only what the tick touches: latest_id + the settings KV."""

    def __init__(self, latest_id=0) -> None:
        self._latest_id = latest_id
        self.settings: "dict[str, str]" = {}
        self.latest_id_calls = 0

    def latest_id(self):
        self.latest_id_calls += 1
        return self._latest_id

    def get_setting(self, key):
        return self.settings.get(key)

    def set_setting(self, key, value):
        self.settings[key] = value


class _FakeDetect:
    """Records each chunk's ``(since_id, until_id)``, its kwargs, and the analyzer it got."""

    def __init__(self, fail_on=None, on_call=None) -> None:
        self.calls: "list[tuple]" = []
        self.kwargs: "list[dict]" = []
        self.analyzers: "list" = []
        self.managers: "list" = []
        self._fail_on = fail_on
        self._on_call = on_call

    def __call__(self, store, analyzer, manager, **kwargs):
        self.calls.append((kwargs.get("since_id"), kwargs.get("until_id")))
        self.kwargs.append(dict(kwargs))
        self.analyzers.append(analyzer)
        self.managers.append(manager)
        if self._on_call is not None:
            self._on_call(self.calls[-1])
        if self._fail_on is not None and self.calls[-1] == self._fail_on:
            raise RuntimeError("detect boom")


class _FakeAnalyzerFactory:
    """Returns a sentinel analyzer, counting builds so single-construction can be asserted."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return object()


def _manager(
    store,
    *,
    is_busy=lambda: False,
    motion_only=lambda: False,
    now_ms=lambda: 1000,
    tick_seconds=5.0,
    detect=None,
):
    """Build a YoloOracleManager wired to fresh fakes; returns (manager, parts)."""
    detect = detect if detect is not None else _FakeDetect()
    analyzer_factory = _FakeAnalyzerFactory()
    mgr = YoloOracleManager(
        store,
        is_busy=is_busy,
        motion_only=motion_only,
        detect=detect,
        analyzer_factory=analyzer_factory,
        tick_seconds=tick_seconds,
        now_ms=now_ms,
    )
    return mgr, {"detect": detect, "analyzer_factory": analyzer_factory}


# --- yield: a busy manual job holds the GPU, so the tick does nothing but note it ran ---


def test_tick_skips_when_busy():
    # is_busy True → the tick returns before any GPU work: no detect, no analyzer build,
    # watermark untouched and NOT persisted. But the tick still recorded its run — it's
    # alive, yielding. Operator work always wins.
    store = _FakeStore(latest_id=500)
    mgr, parts = _manager(store, is_busy=lambda: True, now_ms=lambda: 4242)
    mgr._tick(threading.Event())

    assert parts["detect"].calls == []
    assert parts["analyzer_factory"].calls == 0  # yielded before building anything
    st = mgr.status()
    assert st["watermark"] == 0
    assert "yolo_oracle_watermark" not in store.settings
    assert st["last_tick_ts"] == 4242  # tick ran and yielded


# --- motion-only capture: the non-motion frames aren't stored, so coverage is unattainable -


def test_tick_idles_when_capture_is_motion_only():
    # The whole point of this worker is covering NON-motion frames (a gate miss is only
    # visible there). Under motion-only capture those frames are never stored, so the tick
    # must idle — enforced in the backend, not merely by greying the UI toggle.
    store = _FakeStore(latest_id=500)
    mgr, parts = _manager(store, motion_only=lambda: True, now_ms=lambda: 77)
    mgr._tick(threading.Event())

    assert parts["detect"].calls == []
    assert parts["analyzer_factory"].calls == 0
    st = mgr.status()
    assert st["watermark"] == 0
    assert "yolo_oracle_watermark" not in store.settings
    assert st["last_tick_ts"] == 77  # ran, idled, recorded the tick
    assert mgr.running is False  # (intent untouched by an idle tick)


# --- happy path: the tail is swept in chunks, full-coverage, watermark advanced+persisted --


def test_tick_sweeps_tail_in_chunks_and_persists_watermark():
    # A window wider than one chunk must be issued as several bounded run_analysis calls
    # (the yield granularity), contiguous and gapless, oldest-first.
    total = _CHUNK * 2 + 10
    store = _FakeStore(latest_id=total)
    mgr, parts = _manager(store, now_ms=lambda: 9000)
    mgr._tick(threading.Event())

    assert parts["detect"].calls == [
        (1, _CHUNK),
        (_CHUNK + 1, _CHUNK * 2),
        (_CHUNK * 2 + 1, total),
    ]
    # Full coverage is load-bearing: motion_only must be absent/False so NON-motion frames
    # get verdicts too — the only way a gate miss is measurable.
    assert all(not kw.get("motion_only", False) for kw in parts["detect"].kwargs)
    # ...and no reanalyze: the worker fills MISSING verdicts, it never re-verdicts (that
    # stays a manual sweep, so a broadened detector needs an explicit backfill).
    assert all(not kw.get("reanalyze", False) for kw in parts["detect"].kwargs)

    st = mgr.status()
    assert st["watermark"] == total
    assert store.settings["yolo_oracle_watermark"] == str(total)
    assert st["last_error"] is None

    # One analyzer built and reused across every chunk (no per-chunk weight reload).
    assert parts["analyzer_factory"].calls == 1
    assert len({id(a) for a in parts["detect"].analyzers}) == 1


def test_tick_hands_detect_an_adapter_carrying_this_ticks_stop_event():
    # The cancel path depends entirely on this: run_analysis aborts between batches by polling
    # `manager.stop_event`, so if the tick handed over the wrong (or a fresh) event, a stop
    # could never interrupt an in-flight sweep. Also pins the no-op progress hooks, since
    # run_analysis calls set_total/record unconditionally.
    from compute.analysis.runner import DetectAdapter

    store = _FakeStore(latest_id=10)
    store.settings["yolo_oracle_watermark"] = "0"
    mgr, parts = _manager(store)
    stop_event = threading.Event()

    mgr._tick(stop_event)

    adapter = parts["detect"].managers[0]
    assert isinstance(adapter, DetectAdapter)
    assert adapter.stop_event is stop_event
    adapter.set_total(0)      # must be callable no-ops, not AttributeError
    adapter.record(True)


def test_tick_resumes_when_capture_returns_to_keep_all():
    # An operator's mid-run capture flip must take effect on the worker WITHOUT touching its
    # intent: idle while motion-only, sweeping again when keep-all returns. The getter is read
    # fresh each tick, so this is what makes "intent survives a temporary mode flip" true.
    mode = {"motion_only": True}
    store = _FakeStore(latest_id=10)
    store.settings["yolo_oracle_watermark"] = "0"
    mgr, parts = _manager(store, motion_only=lambda: mode["motion_only"])

    mgr._tick(threading.Event())
    assert parts["detect"].calls == []  # idled

    mode["motion_only"] = False  # operator flips capture back to keep-all
    mgr._tick(threading.Event())
    assert parts["detect"].calls == [(1, 10)]  # resumed, no re-enable needed


def test_tick_resumes_from_persisted_watermark():
    # A restart must resume where it left off, not re-sweep from 1.
    store = _FakeStore(latest_id=100)
    store.settings["yolo_oracle_watermark"] = "60"
    mgr, parts = _manager(store)
    mgr._tick(threading.Event())

    assert parts["detect"].calls == [(61, 100)]  # strictly beyond the watermark
    assert mgr.status()["watermark"] == 100


def test_tick_does_nothing_when_no_new_frames():
    # Watermark already at the horizon → no work, no detect, no wasted analyzer build.
    store = _FakeStore(latest_id=50)
    store.settings["yolo_oracle_watermark"] = "50"
    mgr, parts = _manager(store)
    mgr._tick(threading.Event())

    assert parts["detect"].calls == []
    assert mgr.status()["watermark"] == 50


def test_tick_handles_empty_store():
    # A fresh/emptied store (latest_id 0) must be a clean no-op, not a (1, 0) inverted window.
    store = _FakeStore(latest_id=0)
    mgr, parts = _manager(store)
    mgr._tick(threading.Event())

    assert parts["detect"].calls == []
    assert mgr.status()["watermark"] == 0


# --- backlog bound: one tick sweeps at most the cap; the next tick continues ---


def test_tick_caps_frames_per_tick():
    # Re-enabled against a big un-swept tail: one tick must sweep exactly the cap and park
    # the watermark there, so a backlog drains gradually instead of monopolizing the GPU.
    store = _FakeStore(latest_id=_MAX_FRAMES_PER_TICK * 2)
    mgr, parts = _manager(store)

    mgr._tick(threading.Event())
    assert mgr.status()["watermark"] == _MAX_FRAMES_PER_TICK
    assert parts["detect"].calls[0] == (1, _CHUNK)
    assert parts["detect"].calls[-1][1] == _MAX_FRAMES_PER_TICK

    mgr._tick(threading.Event())  # the rest drains on later ticks
    assert mgr.status()["watermark"] == _MAX_FRAMES_PER_TICK * 2


# --- yield mid-tick: a manual job arriving after the tick started parks the watermark ---


def test_tick_yields_when_busy_arrives_mid_tick():
    # is_busy is False at tick start and for chunk 1's pre-check, then True — a manual job
    # arrived. Because run_analysis honors only stop_event (never is_busy), the per-chunk
    # re-check is the ONLY thing that lets the operator's job win mid-tick: chunk 1 completes,
    # chunk 2 is never issued, and the watermark parks at chunk 1's end.
    calls = {"n": 0}

    def is_busy():
        calls["n"] += 1
        return calls["n"] >= 3  # tick-top + chunk-1 pre-check are False; chunk-2 on: True

    store = _FakeStore(latest_id=_CHUNK * 3)
    mgr, parts = _manager(store, is_busy=is_busy)
    mgr._tick(threading.Event())

    assert parts["detect"].calls == [(1, _CHUNK)]
    assert mgr.status()["watermark"] == _CHUNK
    assert store.settings["yolo_oracle_watermark"] == str(_CHUNK)


# --- partial chunk on stop: never advance past a chunk detect left unfinished ---


def test_tick_bails_after_partial_chunk_on_stop():
    # A stop fires DURING a chunk's detect (run_analysis returns normally between batches,
    # leaving the chunk half-swept). Advancing then would strand that chunk's tail forever
    # un-swept, so the watermark must stay before it and the next run re-sweep it whole.
    stop_event = threading.Event()
    detect = _FakeDetect(on_call=lambda _call: stop_event.set())
    store = _FakeStore(latest_id=_CHUNK * 3)
    mgr, parts = _manager(store, detect=detect)

    mgr._tick(stop_event)

    assert detect.calls == [(1, _CHUNK)]  # one chunk attempted, then the stop was seen
    st = mgr.status()
    assert st["watermark"] == 0  # not advanced past the partially-swept chunk
    assert "yolo_oracle_watermark" not in store.settings
    assert st["last_error"] is None  # a stop is not a fault


# --- resilience: a chunk fault stops the tick without advancing past it, worker survives ---


def test_tick_survives_detect_exception_without_advancing_watermark():
    # detect raises on the SECOND chunk: the first completes (watermark → _CHUNK, persisted),
    # the tick is caught, the watermark never skips the failed chunk, and the error surfaces.
    detect = _FakeDetect(fail_on=(_CHUNK + 1, _CHUNK * 2))
    store = _FakeStore(latest_id=_CHUNK * 3)
    mgr, parts = _manager(store, detect=detect)

    mgr._tick(threading.Event())  # must NOT raise — the worker survives

    st = mgr.status()
    assert st["watermark"] == _CHUNK  # only the first chunk's end persisted
    assert store.settings["yolo_oracle_watermark"] == str(_CHUNK)
    assert "detect boom" in (st["last_error"] or "")

    # And the worker is still usable — a later tick runs without raising (retrying the
    # still-failing chunk, watermark still parked).
    mgr._tick(threading.Event())
    assert mgr.status()["watermark"] == _CHUNK


# --- first enable: seed to the horizon, don't back-sweep the whole store ---


def test_start_seeds_watermark_to_horizon_on_first_enable():
    # A fresh enable against a store already holding days of frames must NOT back-sweep that
    # history (a full back-sweep would hold the GPU for hours; history stays the manual
    # sweep's job): start() jumps the watermark to the horizon, so ticks find nothing behind.
    store = _FakeStore(latest_id=100_000)
    mgr, parts = _manager(store, tick_seconds=0.01)

    mgr.start()
    # Seeded and persisted synchronously, before any tick ran.
    assert mgr.status()["watermark"] == 100_000
    assert store.settings["yolo_oracle_watermark"] == "100000"

    assert _wait(lambda: store.latest_id_calls > 1), "no background tick ran"
    mgr.stop()
    mgr.join(timeout=5)
    assert parts["detect"].calls == []  # nothing historical was swept


def test_start_does_not_reseed_when_watermark_persisted():
    # A restart (persisted watermark present) is a RESUME, not a first enable — start() must
    # leave the watermark alone so the un-swept tail is still covered.
    store = _FakeStore(latest_id=900)
    store.settings["yolo_oracle_watermark"] = "100"
    mgr, _ = _manager(store, tick_seconds=60.0)

    mgr.start()
    assert mgr.status()["watermark"] == 100  # untouched by start
    mgr.stop()
    mgr.join(timeout=5)


# --- lifecycle: start ticks in the background; stop winds it down and persists off ---


def test_start_ticks_then_stop_ends_loop():
    store = _FakeStore(latest_id=10)
    store.settings["yolo_oracle_watermark"] = "0"  # a RESUME, so no horizon seeding
    mgr, parts = _manager(store, tick_seconds=0.01)

    mgr.start()
    assert mgr.running is True
    assert store.settings["yolo_oracle"] == "1"  # start persisted the on intent

    assert _wait(lambda: mgr.status()["watermark"] == 10), "background tick never swept"
    assert parts["detect"].calls == [(1, 10)]

    mgr.stop()
    assert mgr.running is False
    assert store.settings["yolo_oracle"] == "0"  # stop persisted the off intent

    mgr.join(timeout=5)
    assert _wait(lambda: not mgr._thread.is_alive()), "worker thread did not exit after stop"


def test_shutdown_stop_preserves_persisted_intent():
    # The restore contract hinges on this: the shutdown hook also calls stop(), so an
    # unconditional persist-"0" would clear the operator's on-intent on every clean exit and
    # the worker could never come back at the next launch. An operator stop DOES remember.
    store = _FakeStore(latest_id=10)
    mgr, _ = _manager(store, tick_seconds=60.0)

    mgr.start()
    assert store.settings["yolo_oracle"] == "1"

    mgr.stop(persist=False)  # a PROCESS exit
    mgr.join(timeout=5)
    assert mgr.running is False
    assert store.settings["yolo_oracle"] == "1"  # intent survives, so restore can fire

    mgr.start()
    mgr.stop()  # an OPERATOR stop (default)
    mgr.join(timeout=5)
    assert store.settings["yolo_oracle"] == "0"  # deliberate off is remembered


def test_restore_only_starts_when_intent_on():
    store = _FakeStore(latest_id=10)
    mgr, _ = _manager(store, tick_seconds=60.0)

    mgr.restore(False)  # persisted intent off → stay stopped
    assert mgr.running is False

    mgr.restore(True)
    assert mgr.running is True
    mgr.stop()
    mgr.join(timeout=5)


# --- clear(): re-seeding the watermark is what keeps the worker from being stranded ---


def test_reset_watermark_repoints_and_persists():
    # /api/clear wipes frames (rowids restart at 1) but KEEPS the settings KV, so a pre-wipe
    # watermark would sit far ahead of every new frame and the worker would silently cover
    # nothing. reset_watermark re-points it AND blocks a later first-start from re-seeding.
    store = _FakeStore(latest_id=0)
    store.settings["yolo_oracle_watermark"] = "500000"
    mgr, parts = _manager(store, tick_seconds=60.0)
    assert mgr.status()["watermark"] == 500_000  # loaded the stale value

    mgr.reset_watermark(0)  # post-wipe horizon
    assert mgr.status()["watermark"] == 0
    assert store.settings["yolo_oracle_watermark"] == "0"

    # A fresh store then grows; the next tick must actually sweep the new low ids.
    store._latest_id = 40
    mgr._tick(threading.Event())
    assert parts["detect"].calls == [(1, 40)]

    # And start() must NOT re-seed over the explicit reset (would strand it again).
    mgr.start()
    assert mgr.status()["watermark"] == 40
    mgr.stop()
    mgr.join(timeout=5)


def test_tick_clamps_watermark_when_frame_ids_regress():
    # `frames.id` is INTEGER PRIMARY KEY with NO AUTOINCREMENT, so SQLite REUSES rowids once
    # the max row is deleted — and the non-motion purge deletes THROUGH the current max id,
    # which at ~5 fps is almost always a non-motion frame. So latest_id() really does go DOWN
    # under the watermark in normal operation. Without a clamp every later tick sees an empty
    # window and the worker reports running + error-free while sweeping NOTHING until the store
    # regrows past the stale id — hours of silent zero coverage.
    store = _FakeStore(latest_id=1000)
    store.settings["yolo_oracle_watermark"] = "1000"
    mgr, parts = _manager(store)

    store._latest_id = 300  # a whole-store non-motion purge dropped the newest frames
    mgr._tick(threading.Event())
    assert mgr.status()["watermark"] == 300, "stale watermark left above the horizon"
    assert store.settings["yolo_oracle_watermark"] == "300"  # clamp persisted

    store._latest_id = 340  # ids 301.. are REUSED by fresh frames
    mgr._tick(threading.Event())
    assert parts["detect"].calls == [(301, 340)], "coverage did not self-heal after the purge"


def test_reset_watermark_mid_tick_is_not_clobbered():
    # /api/clear does NOT stop the workers, so a reset can land while a tick is inside its
    # chunk loop — and that tick then writes its own derived `hi` back. Without a guard the
    # reset is undone and the watermark sits far ABOVE every post-wipe frame, so `until <
    # start` on every later tick: the worker reports running with no error and silently
    # sweeps nothing, forever. Exactly the stranding the reset exists to prevent.
    store = _FakeStore(latest_id=_CHUNK * 3)
    store.settings["yolo_oracle_watermark"] = "0"
    holder = {}

    def on_call(call):
        if call == (1, _CHUNK):  # a clear lands DURING the first chunk's detect
            store._latest_id = 0
            holder["mgr"].reset_watermark(0)

    detect = _FakeDetect(on_call=on_call)
    mgr, _parts = _manager(store, detect=detect)
    holder["mgr"] = mgr

    mgr._tick(threading.Event())

    st = mgr.status()
    assert st["watermark"] == 0, "the in-flight tick resurrected the pre-clear watermark"
    assert store.settings["yolo_oracle_watermark"] == "0"
    assert st["last_error"] is None  # abandoning the tick is not a fault


def test_reset_watermark_preserves_first_enable_seed():
    # /api/clear calls reset_watermark on workers that may NEVER have been enabled. A
    # never-seeded worker has no stale watermark to strand (it is 0, and first start() seeds
    # it to the then-current horizon), so the reset must not consume the seed — else the very
    # common "clear → collect for a day → enable for the first time" flow back-sweeps the
    # whole store, the hours-long GPU hold _seed_horizon exists to prevent.
    store = _FakeStore(latest_id=0)  # freshly cleared
    mgr, parts = _manager(store, tick_seconds=60.0)

    mgr.reset_watermark(0)  # what /api/clear does to a never-enabled worker

    store._latest_id = 432_000  # ...then a day of collection
    mgr.start()  # the FIRST ever enable

    assert mgr.status()["watermark"] == 432_000, "first enable back-sweeps the whole store"
    mgr.stop()
    mgr.join(timeout=5)
    assert parts["detect"].calls == []
