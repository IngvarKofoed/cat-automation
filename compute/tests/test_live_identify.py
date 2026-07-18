"""Tests for the always-on live-identification worker (compute/learning/live_identify).

``LiveIdentifyManager`` is the compute analogue of ``CollectorManager`` — an always-on
tick loop, not a FIFO job queue — so these tests drive the *tick* directly rather than
through the FIFO lifecycle the analysis/training-manager tests exercise. Every seam that
would touch torch or the GPU is injected, so the whole worker runs with fakes and NO
torch, NO model, no CUDA:

- ``_FakeStore`` — an in-memory stand-in exposing only the four methods the tick uses:
  ``active_model`` (the promoted gallery, or ``None``), ``closed_visits`` (the settled-
  tail visit spans, filtered by the watermark so a processed span is not re-returned),
  and the ``get_setting``/``set_setting`` KV the watermark + intent persist through.
- ``_FakeDetect`` / ``_FakeIdentify`` — record their ``[lo, hi]`` calls (and the resident
  embedder identify is handed); ``_FakeIdentify`` can be told to raise on one span to
  prove the worker survives a per-span fault without advancing the watermark past it.
- ``_FakeEmbedderFactory`` / ``_FakeEmbedder`` — expose the ``backbone``/``imgsz``
  properties + ``prepare()`` the tick keys residency on, and count builds so an
  active-model backbone change can be shown to rebuild exactly once.
- controllable ``now_ms`` and ``is_busy`` closures — a fixed clock makes ``last_tick_ts``
  deterministic, and a busy flag proves the tick yields the GPU.

The single background-loop test uses a tiny ``tick_seconds`` and asserts ``stop()`` winds
the daemon down (``running`` false, thread joined, intent persisted "0").
"""
from __future__ import annotations

import threading
import time

from compute.identification.embed import EmbedCancelled
from compute.learning.live_identify import _MAX_SPANS_PER_TICK, LiveIdentifyManager

# A resolved active-model dict, shaped like ``Store.active_model()`` (id/backbone/imgsz/
# gallery_path/threshold). The tick reads backbone+imgsz (embedder residency) and
# gallery_path (passed to identify); the rest rides along.
_MODEL_A = {
    "id": 1,
    "backbone": "dinov2_vits14",
    "imgsz": 224,
    "gallery_path": "/models/a/gallery.npz",
    "threshold": 0.4,
}
# A differently-configured model — both backbone AND imgsz differ, so a promotion to it
# must rebuild the resident embedder into its feature space.
_MODEL_B = {
    "id": 2,
    "backbone": "dinov2_vitb14",
    "imgsz": 392,
    "gallery_path": "/models/b/gallery.npz",
    "threshold": 0.5,
}


def _wait(pred, timeout: float = 5.0, interval: float = 0.005) -> bool:
    """Poll ``pred`` until true or ``timeout`` elapses; return its final value."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return bool(pred())


class _FakeStore:
    """In-memory store exposing only what the tick touches: active_model, closed_visits,
    and the settings KV. ``closed_visits`` filters ``spans`` to those with ``lo >
    since_id`` (oldest-first), so a span already folded into the watermark is not handed
    back — the same "only new spans" property the real keyset read gives."""

    def __init__(self, model=None, spans=None, latest_id=0) -> None:
        self._model = model
        self._spans = sorted(spans or [])
        self._latest_id = latest_id
        self.settings: "dict[str, str]" = {}
        self.active_model_calls = 0
        self.closed_visits_calls: "list[tuple]" = []

    def active_model(self):
        self.active_model_calls += 1
        return self._model

    def latest_id(self):
        # The frame horizon start() seeds the watermark to on a first-ever enable.
        return self._latest_id

    def closed_visits(self, since_id, now_ms, *, gap_ms: int = 2000):
        self.closed_visits_calls.append((since_id, now_ms))
        floor = since_id or 0
        return [(lo, hi) for (lo, hi) in self._spans if lo > floor]

    def get_setting(self, key):
        return self.settings.get(key)

    def set_setting(self, key, value):
        self.settings[key] = value


class _FakeDetect:
    """Records each ``[since_id, until_id]`` detect call and the analyzer/manager it got,
    so a test can assert the spans, their order, and that ONE analyzer instance is reused."""

    def __init__(self) -> None:
        self.calls: "list[tuple]" = []
        self.analyzers: "list" = []

    def __call__(self, store, analyzer, manager, since_id=None, until_id=None):
        self.calls.append((since_id, until_id))
        self.analyzers.append(analyzer)


class _FakeIdentify:
    """Records each identify call (span + the resident embedder AND gallery handed to it);
    optionally raises a fault (``fail_on``) or an ``EmbedCancelled`` (``cancel_on``) on one
    span, so a per-span fault or a mid-pass stop-cancel can be injected mid-tick."""

    def __init__(self, fail_on=None, cancel_on=None) -> None:
        self.calls: "list[tuple]" = []
        self.embedders: "list" = []
        self.galleries: "list" = []
        self._fail_on = fail_on
        self._cancel_on = cancel_on

    def __call__(
        self, store, model, gallery_path, since_id=None, until_id=None,
        embedder=None, gallery=None, progress=None,
    ):
        self.calls.append((since_id, until_id))
        self.embedders.append(embedder)
        self.galleries.append(gallery)
        if self._fail_on is not None and (since_id, until_id) == self._fail_on:
            raise RuntimeError("identify boom")
        if self._cancel_on is not None and (since_id, until_id) == self._cancel_on:
            raise EmbedCancelled("identify cancelled")
        return {"n_identified": 1}


class _FakeEmbedder:
    """Exposes the ``backbone``/``imgsz`` the tick keys residency on, and counts prepare()."""

    def __init__(self, model, imgsz) -> None:
        self._model = model
        self._imgsz = imgsz
        self.prepared = 0

    @property
    def backbone(self):
        return self._model

    @property
    def imgsz(self):
        return self._imgsz

    def prepare(self):
        self.prepared += 1


class _FakeEmbedderFactory:
    """Builds ``_FakeEmbedder``s, recording the ``(backbone, imgsz)`` each build was asked
    for so a rebuild-on-model-change can be asserted precisely."""

    def __init__(self) -> None:
        self.calls: "list[tuple]" = []
        self.instances: "list[_FakeEmbedder]" = []

    def __call__(self, model, imgsz):
        self.calls.append((model, imgsz))
        e = _FakeEmbedder(model, imgsz)
        self.instances.append(e)
        return e


class _FakeAnalyzerFactory:
    """Returns a sentinel analyzer, counting builds so single-construction can be asserted."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return object()


class _FakeGalleryLoader:
    """Returns a fresh sentinel gallery per call, recording the paths it was asked to load
    so a reload-on-model-change can be asserted precisely — and a same-model reuse shown to
    NOT reload (the worker caches, so a correct worker calls this once per distinct path)."""

    def __init__(self) -> None:
        self.paths: "list[str]" = []
        self.returned: "list" = []

    def __call__(self, path):
        self.paths.append(path)
        g = object()
        self.returned.append(g)
        return g


def _manager(store, *, is_busy=lambda: False, now_ms=lambda: 1000, tick_seconds=5.0):
    """Build a LiveIdentifyManager wired to fresh fakes; returns (manager, parts)."""
    detect = _FakeDetect()
    identify = _FakeIdentify()
    analyzer_factory = _FakeAnalyzerFactory()
    embedder_factory = _FakeEmbedderFactory()
    gallery_loader = _FakeGalleryLoader()
    mgr = LiveIdentifyManager(
        store,
        is_busy=is_busy,
        detect=detect,
        identify=identify,
        analyzer_factory=analyzer_factory,
        embedder_factory=embedder_factory,
        gallery_loader=gallery_loader,
        tick_seconds=tick_seconds,
        now_ms=now_ms,
    )
    return mgr, {
        "detect": detect,
        "identify": identify,
        "analyzer_factory": analyzer_factory,
        "embedder_factory": embedder_factory,
        "gallery_loader": gallery_loader,
    }


# --- yield: a busy manual job holds the GPU, so the tick does nothing but note it ran ---


def test_tick_skips_when_busy():
    # is_busy True → the tick returns after the active-model check, before any GPU work:
    # no detect/identify, no embedder or analyzer build, watermark untouched and NOT
    # persisted. But the tick still recorded its run (last_tick_ts) — it's alive, yielding.
    store = _FakeStore(model=_MODEL_A, spans=[(1, 5)])
    mgr, parts = _manager(store, is_busy=lambda: True, now_ms=lambda: 4242)
    mgr._tick(threading.Event())

    assert parts["detect"].calls == []
    assert parts["identify"].calls == []
    assert parts["embedder_factory"].calls == []  # yielded before building anything
    assert parts["analyzer_factory"].calls == 0
    st = mgr.status()
    assert st["watermark"] == 0
    assert "live_identify_watermark" not in store.settings
    assert st["last_tick_ts"] == 4242  # tick ran and yielded


# --- no model: nothing promoted yet, so there is nothing to identify against ---


def test_tick_skips_when_no_active_model():
    # active_model None → idle this tick (before the busy check, before any build).
    store = _FakeStore(model=None, spans=[(1, 5)])
    mgr, parts = _manager(store, now_ms=lambda: 77)
    mgr._tick(threading.Event())

    assert parts["detect"].calls == []
    assert parts["identify"].calls == []
    assert parts["embedder_factory"].calls == []
    assert parts["analyzer_factory"].calls == 0
    assert mgr.status()["watermark"] == 0
    assert mgr.status()["last_tick_ts"] == 77  # ran, found no model, recorded the tick


# --- happy path: closed spans detected+identified in order, watermark advanced+persisted -


def test_tick_processes_spans_in_order_and_persists_watermark():
    store = _FakeStore(model=_MODEL_A, spans=[(1, 5), (10, 15)])
    mgr, parts = _manager(store, now_ms=lambda: 9000)
    mgr._tick(threading.Event())

    # Both closed spans detected then identified, oldest-first.
    assert parts["detect"].calls == [(1, 5), (10, 15)]
    assert parts["identify"].calls == [(1, 5), (10, 15)]
    # closed_visits queried once, from the starting watermark (0) with the tick's clock.
    assert store.closed_visits_calls == [(0, 9000)]

    # Watermark advanced to the last span end and persisted as a string.
    st = mgr.status()
    assert st["watermark"] == 15
    assert store.settings["live_identify_watermark"] == "15"
    assert st["last_error"] is None

    # The embedder + analyzer are built exactly once and reused across both spans.
    assert parts["embedder_factory"].calls == [("dinov2_vits14", 224)]
    assert parts["embedder_factory"].instances[0].prepared == 1
    assert parts["analyzer_factory"].calls == 1
    resident = parts["embedder_factory"].instances[0]
    assert parts["identify"].embedders == [resident, resident]  # same resident handed each span
    assert parts["detect"].analyzers[0] is parts["detect"].analyzers[1]  # one analyzer reused

    # The gallery is loaded once and the SAME resident copy handed to both spans (no
    # per-span .npz re-read), mirroring the embedder's residency.
    assert parts["gallery_loader"].paths == ["/models/a/gallery.npz"]
    resident_gallery = parts["gallery_loader"].returned[0]
    assert parts["identify"].galleries == [resident_gallery, resident_gallery]


# --- residency: the embedder rebuilds only when the active model's (backbone,imgsz) changes -


def test_tick_rebuilds_embedder_only_when_model_changes():
    # No spans — this isolates residency: ensure_resident runs before the (empty) span loop.
    store = _FakeStore(model=_MODEL_A, spans=[])
    mgr, parts = _manager(store)
    factory = parts["embedder_factory"]

    mgr._tick(threading.Event())  # first tick builds embedder A
    assert factory.calls == [("dinov2_vits14", 224)]

    mgr._tick(threading.Event())  # same model → reuse, no rebuild
    assert factory.calls == [("dinov2_vits14", 224)]

    store._model = _MODEL_B  # a promotion to a differently-configured model
    mgr._tick(threading.Event())  # backbone+imgsz changed → rebuild into the new space
    assert factory.calls == [("dinov2_vits14", 224), ("dinov2_vitb14", 392)]
    assert len(factory.instances) == 2
    assert factory.instances[1].prepared == 1


# --- resilience: a per-span fault stops the tick without advancing the watermark past it -


def test_tick_survives_span_exception_without_advancing_watermark():
    # identify raises on the SECOND span. The first span completes fully (watermark → 5,
    # persisted); the second's detect runs, its identify raises, the tick is caught, the
    # watermark stays 5 (never skips the un-identified span), and the error is surfaced.
    store = _FakeStore(model=_MODEL_A, spans=[(1, 5), (10, 15)])
    detect = _FakeDetect()
    identify = _FakeIdentify(fail_on=(10, 15))
    mgr = LiveIdentifyManager(
        store,
        is_busy=lambda: False,
        detect=detect,
        identify=identify,
        analyzer_factory=_FakeAnalyzerFactory(),
        embedder_factory=_FakeEmbedderFactory(),
        gallery_loader=_FakeGalleryLoader(),
        now_ms=lambda: 1000,
    )

    mgr._tick(threading.Event())  # must NOT raise — the worker survives

    assert detect.calls == [(1, 5), (10, 15)]  # detect precedes identify, so span 2 detected
    assert identify.calls == [(1, 5), (10, 15)]  # identify attempted span 2 and raised
    st = mgr.status()
    assert st["watermark"] == 5  # only the first span's end persisted
    assert store.settings["live_identify_watermark"] == "5"
    assert "identify boom" in (st["last_error"] or "")

    # And the worker is still usable — a subsequent tick runs without raising (it re-tries
    # the still-failing span, watermark still parked at 5).
    mgr._tick(threading.Event())
    assert mgr.status()["watermark"] == 5


# --- lifecycle: start ticks in the background; stop winds the daemon down and persists off -


def test_start_ticks_then_stop_ends_loop():
    store = _FakeStore(model=_MODEL_A, spans=[(1, 5)])
    # A persisted watermark makes this a RESUME (not a first-ever enable), so start() does
    # not seed to the horizon and the background tick processes the existing span — the
    # first-enable seeding is covered separately below.
    store.settings["live_identify_watermark"] = "0"
    mgr, parts = _manager(store, tick_seconds=0.01, now_ms=lambda: 1000)

    mgr.start()
    assert mgr.running is True
    assert store.settings["live_identify"] == "1"  # start persisted the on intent

    # The single closed span gets processed by a background tick (watermark → 5); after
    # that closed_visits(since=5) filters it out, so ticks stop advancing.
    assert _wait(lambda: mgr.status()["watermark"] == 5), "background tick never processed the span"
    assert parts["identify"].calls == [(1, 5)]

    mgr.stop()
    assert mgr.running is False
    assert store.settings["live_identify"] == "0"  # stop persisted the off intent

    mgr.join(timeout=5)
    assert _wait(lambda: not mgr._thread.is_alive()), "worker thread did not exit after stop"


# --- first enable: seed the watermark to the horizon, don't back-identify the whole store -


def test_start_seeds_watermark_to_horizon_on_first_enable():
    # Fresh store (no persisted watermark) that already holds days of visits, horizon at
    # id 100. Enabling the worker must NOT back-identify that history (the manual Identify
    # pass owns history): start() jumps the watermark to 100, so background ticks find no
    # spans beyond it and identify nothing — the worker names only NEW visits from here.
    store = _FakeStore(model=_MODEL_A, spans=[(1, 5), (10, 15)], latest_id=100)
    mgr, parts = _manager(store, tick_seconds=0.01, now_ms=lambda: 9000)

    mgr.start()
    # Seeded to the horizon and persisted, synchronously, before any tick ran.
    assert mgr.status()["watermark"] == 100
    assert store.settings["live_identify_watermark"] == "100"

    assert _wait(lambda: store.closed_visits_calls != []), "no background tick ran"
    mgr.stop()
    mgr.join(timeout=5)
    assert parts["identify"].calls == []  # nothing historical was identified
    assert parts["detect"].calls == []


# --- yield mid-tick: a manual job arriving after the tick started makes the worker stop ---


def test_tick_yields_when_busy_arrives_mid_tick():
    # is_busy is False at tick start and for span 1's pre-check, then True — a manual job
    # arrived. The worker must yield BEFORE span 2 rather than contend for the shared GPU:
    # span 1 fully processed, span 2 skipped, watermark parked at span 1's end.
    calls = {"n": 0}

    def is_busy():
        calls["n"] += 1
        return calls["n"] >= 3  # tick-top check + span-1 pre-check are False; span-2 on: True

    store = _FakeStore(model=_MODEL_A, spans=[(1, 5), (10, 15)])
    mgr, parts = _manager(store, is_busy=is_busy, now_ms=lambda: 9000)
    mgr._tick(threading.Event())

    assert parts["detect"].calls == [(1, 5)]
    assert parts["identify"].calls == [(1, 5)]
    assert mgr.status()["watermark"] == 5
    assert store.settings["live_identify_watermark"] == "5"


# --- partial detect on stop: don't identify or advance past a span detect left unfinished -


def test_tick_bails_after_partial_detect_on_stop():
    # A stop fires DURING a span's detect (run_analysis returns normally between batches,
    # leaving the span partially detected). The worker must not identify or advance past
    # it — else the span's undetected tail is never revisited. identify is never called and
    # the watermark stays before the span, so the next run re-detects it whole.
    stop_event = threading.Event()

    class _StopDuringDetect:
        def __init__(self):
            self.calls = []

        def __call__(self, store, analyzer, manager, since_id=None, until_id=None):
            self.calls.append((since_id, until_id))
            stop_event.set()  # a stop arrives mid-detect

    detect = _StopDuringDetect()
    identify = _FakeIdentify()
    store = _FakeStore(model=_MODEL_A, spans=[(1, 5)])
    mgr = LiveIdentifyManager(
        store,
        is_busy=lambda: False,
        detect=detect,
        identify=identify,
        analyzer_factory=_FakeAnalyzerFactory(),
        embedder_factory=_FakeEmbedderFactory(),
        gallery_loader=_FakeGalleryLoader(),
        now_ms=lambda: 9000,
    )
    mgr._tick(stop_event)

    assert detect.calls == [(1, 5)]  # detect ran
    assert identify.calls == []  # identify did NOT (stop seen right after detect)
    st = mgr.status()
    assert st["watermark"] == 0  # not advanced past the partially-detected span
    assert "live_identify_watermark" not in store.settings
    assert st["last_error"] is None  # a stop is not a fault


# --- backlog bound: one tick processes at most the cap; the next tick continues ---


def test_tick_caps_spans_per_tick():
    # More closed spans than the per-tick cap (e.g. re-enabled after a long off-period):
    # one tick processes exactly the cap and parks the watermark at that span's end; the
    # next tick drains the rest — so a big backlog can't monopolize the GPU in one run.
    n = _MAX_SPANS_PER_TICK + 5
    spans = [(i * 10 + 1, i * 10 + 5) for i in range(n)]
    store = _FakeStore(model=_MODEL_A, spans=spans)
    mgr, parts = _manager(store, now_ms=lambda: 10 ** 9)

    mgr._tick(threading.Event())
    assert len(parts["identify"].calls) == _MAX_SPANS_PER_TICK
    assert mgr.status()["watermark"] == spans[_MAX_SPANS_PER_TICK - 1][1]

    mgr._tick(threading.Event())  # the remaining 5 drain next tick
    assert len(parts["identify"].calls) == n
    assert mgr.status()["watermark"] == spans[-1][1]


# --- identify cancel: a stop's batch-boundary cancel parks the watermark, not an error ---


def test_tick_cancel_during_identify_parks_watermark():
    # identify raises EmbedCancelled (a stop hit its batch-boundary cancel hook, via the
    # progress callback the worker now passes) on the FIRST span. A cancel is an
    # intentional stop, not a fault: the watermark stays before the span (idempotent resume
    # finishes it next run) and last_error is NOT set.
    store = _FakeStore(model=_MODEL_A, spans=[(1, 5), (10, 15)])
    detect = _FakeDetect()
    identify = _FakeIdentify(cancel_on=(1, 5))
    mgr = LiveIdentifyManager(
        store,
        is_busy=lambda: False,
        detect=detect,
        identify=identify,
        analyzer_factory=_FakeAnalyzerFactory(),
        embedder_factory=_FakeEmbedderFactory(),
        gallery_loader=_FakeGalleryLoader(),
        now_ms=lambda: 9000,
    )
    mgr._tick(threading.Event())

    assert identify.calls == [(1, 5)]  # cancelled on span 1; span 2 never attempted
    st = mgr.status()
    assert st["watermark"] == 0  # parked before the cancelled span
    assert "live_identify_watermark" not in store.settings
    assert st["last_error"] is None  # a cancel is not an error


# --- gallery residency: loaded once, reused across spans, reloaded only on a promotion ---


def test_tick_reuses_resident_gallery_and_reloads_on_model_change():
    store = _FakeStore(model=_MODEL_A, spans=[(1, 5), (10, 15)])
    mgr, parts = _manager(store, now_ms=lambda: 9000)
    loader = parts["gallery_loader"]

    mgr._tick(threading.Event())  # two spans, ONE load, the same object handed to both
    assert loader.paths == ["/models/a/gallery.npz"]
    g_a = loader.returned[0]
    assert parts["identify"].galleries == [g_a, g_a]

    store._spans = [(20, 25)]  # a fresh visit beyond the watermark, same model
    mgr._tick(threading.Event())
    assert loader.paths == ["/models/a/gallery.npz"]  # gallery reused, no reload

    store._model = _MODEL_B  # a promotion → different gallery_path → reload once
    store._spans = [(30, 35)]
    mgr._tick(threading.Event())
    assert loader.paths == ["/models/a/gallery.npz", "/models/b/gallery.npz"]
