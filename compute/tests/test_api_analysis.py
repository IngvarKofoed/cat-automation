"""Tests for the analysis + collector-toggle routes on ``compute/api/app.py``
(the motion-gate-oracles spec's API layer).

``compute/tests/test_analysis.py`` already exercises the lower layers directly —
``Store``'s analysis methods, ``run_analysis``, and ``AnalysisManager`` — with no
HTTP involved. This file is the layer above that: it drives the same machinery
*through* ``create_app`` + ``fastapi.testclient.TestClient``, so what's actually
under test is the routing/validation ``app.py`` adds on top — the 400/503 mapping,
the enqueue-while-running queue behavior (a second run no longer 409s — it waits),
the ``reanalyze`` clear-then-resweep sequencing, the disagreement query wired to
``GET /api/frames``, the collector start/stop/motion-only toggles + persisted
resume, the clear re-seed, and the density-timeline / visit-inbox read endpoints —
none of which the lower-layer tests can see.

No real edge, no real model: ``create_app(store=..., client=FakeClient(),
start_collector=False, analysis_manager=AnalysisManager(resolver=...))`` is the
same injection-seam pattern ``test_collection.py`` uses, extended with the two
runtime controls this spec adds. ``FakeAnalyzer`` derives its verdict from a
frame's own mean gray level (mirroring ``test_analysis.py``'s fake exactly, for
the same reason: the runner genuinely ``cv2.imdecode``s stored bytes, so a
scenario needs a real, decodable JPEG, not a placeholder). A ``gate`` lets a test
freeze a sweep mid-frame — deterministic concurrency control, no sleep-and-hope.
"""
from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from compute.analysis.base import AnalysisResult
from compute.analysis.runner import AnalysisManager
from compute.collection.store import Store
from compute.ingest import StreamFrame
from shared.wire import StreamFrameMeta

# A minimal but genuinely valid JPEG (SOI ... EOI) for tests that never reach a
# real decode (the collector routes, and the 400/503 validation paths that fail
# before any sweep starts). Sweep tests need real, decodable images instead — see
# ``_jpeg_gray`` below — so they don't use this.
_JPEG_BODY = b"\xff\xd8\xff\xe0" + b"fake-jpeg-body" + b"\xff\xd9"

# cv2/numpy back the real sweep tests (the runner decodes stored bytes); they are
# a base dependency of compute/requirements.txt (StreamFrame.image needs them
# too), but guard the import anyway so a box without the CV stack merely skips
# those tests rather than failing to collect this whole module.
try:
    import cv2
    import numpy as np

    _HAVE_CV = True
except Exception:  # pragma: no cover - exercised only where cv2 is absent
    _HAVE_CV = False

_requires_cv = pytest.mark.skipif(not _HAVE_CV, reason="cv2/numpy required for the sweep tests")


def _frame(
    frame_id: int = 1,
    ts: int = 1_000,
    motion: bool = False,
    bbox=None,
    area: float = 0.0,
    body: bytes = _JPEG_BODY,
) -> StreamFrame:
    """Build a ``StreamFrame`` directly — the shape ``Store.add`` consumes."""
    meta = StreamFrameMeta(frame_id=frame_id, ts=ts, motion=motion, bbox=bbox, area=area)
    return StreamFrame(meta, body)


def _jpeg_gray(level: int) -> bytes:
    """A real solid-gray JPEG at ``level`` (0..255); decodable by the runner.

    The analysis runner genuinely ``cv2.imdecode``s stored bytes (unlike the
    plain collector, which writes JPEGs verbatim and never opens them), so a
    sweep test needs a real image whose brightness ``FakeAnalyzer`` can read back
    out. Solid colours round-trip through JPEG exactly, matching
    ``test_analysis.py``'s identical helper.
    """
    img = np.full((16, 16, 3), level, dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", img)
    assert ok
    return buf.tobytes()


class FakeAnalyzer:
    """A controllable stand-in oracle: verdict = the frame's own mean gray level.

    Satisfies the ``Analyzer`` protocol with zero model weights: ``analyze``
    derives ``verdict`` from the decoded image's brightness (bright >= 127 ->
    present), so a test fixes the outcome purely by which ``_jpeg_gray`` level it
    stores. The optional ``gate`` lets a test freeze the sweep mid-frame — every
    call blocks on it before returning — which is what makes the concurrent-run,
    cancel, and reanalyze tests deterministic instead of racing a background
    thread against sleeps.
    """

    def __init__(
        self,
        name: str = "yolo",
        windowed: bool = False,
        gate: "threading.Event | None" = None,
        unavailable: bool = False,
    ) -> None:
        self.name = name
        self.windowed = windowed
        self.gate = gate
        self.unavailable = unavailable
        self.prepared_with = None
        self.prepared_since_id = None

    def ensure_available(self) -> None:
        # The synchronous dep gate the runner calls in start(); set unavailable=True
        # to simulate a backend whose optional deps/hardware are missing (→ 503) —
        # how the real yolo/bsuv backends surface an absent torch/CUDA at request time.
        if self.unavailable:
            raise ImportError("optional analysis deps not installed (fake)")

    def prepare(self, store, since_id: "int | None" = None) -> None:
        # since_id is the frame-range-groups scope run_analysis now always passes
        # (see Analyzer.prepare); FakeAnalyzer is stateless so it just records what it
        # was called with, for the scoped-run tests below to assert against.
        self.prepared_with = store
        self.prepared_since_id = since_id

    def analyze(self, image) -> AnalysisResult:
        if self.gate is not None:
            self.gate.wait(timeout=5)
        level = float(image.mean())
        return AnalysisResult(verdict=bool(level >= 127.0), score=level, detail={"level": level})


class FakeClient:
    """A stand-in edge connection: no network, no real Pi.

    ``iter_stream_reconnecting`` returns a genuinely empty iterator, so a
    ``CollectorManager``-driven ``run_collector`` loop has nothing to consume and
    returns almost immediately once started — good test hygiene (no live
    background thread lingering past the test), and orthogonal to the manager's
    own ``running`` flag, which ``start``/``stop`` toggle synchronously (see
    ``compute/api/app.py``'s "the route just toggles" contract) rather than
    tracking whether the underlying thread happens to still be alive.
    """

    def iter_stream_reconnecting(self):
        return iter(())


class SpyAnalysisManager(AnalysisManager):
    """A real ``AnalysisManager`` that also records each ``enqueue_named()`` call's args.

    Used only by the frame-range-groups scoping tests below, to verify that
    ``POST /api/analysis/run`` forwards its ``since_id``/``until_id`` straight
    through to the manager, unmodified. Delegates to ``super().enqueue_named()`` so
    the job still actually runs — every other assertion (status, written verdicts)
    behaves exactly as the non-spy tests above; only the call args are captured.
    """

    def __init__(self, resolver) -> None:
        super().__init__(resolver=resolver)
        self.enqueue_calls: "list[dict]" = []

    def enqueue_named(
        self,
        store,
        name: str,
        reanalyze: bool = False,
        since_id: "int | None" = None,
        until_id: "int | None" = None,
    ) -> dict:
        self.enqueue_calls.append(
            {"name": name, "reanalyze": reanalyze, "since_id": since_id, "until_id": until_id}
        )
        return super().enqueue_named(
            store, name, reanalyze=reanalyze, since_id=since_id, until_id=until_id
        )


def _make_app_with_manager(tmp_path, manager, client=None) -> "tuple[TestClient, Store]":
    """Build a ``TestClient`` wired to a caller-supplied ``AnalysisManager``.

    ``make_app`` (the fixture below) always builds its own manager internally, so
    the scoping-forwarding tests — which need to inspect a spy manager *after* the
    request completes — construct the app directly here instead, mirroring
    ``make_app``'s own ``Store``/``create_app`` wiring.
    """
    from compute.api.app import create_app

    store = Store(
        db_path=str(tmp_path / "index.db"),
        media_root=str(tmp_path / "media"),
        max_bytes=10_000_000,
    )
    app = create_app(
        store=store, client=client or FakeClient(), start_collector=False, analysis_manager=manager
    )
    return TestClient(app), store


def _make_resolver(analyzers: dict, import_error_for: "frozenset[str]" = frozenset()):
    """Build a fake ``AnalysisManager`` resolver: name -> a pre-built test double.

    Mirrors ``compute.analysis.get_analyzer``'s contract (an unknown name raises
    ``ValueError``) but hands back ``FakeAnalyzer`` instances instead of real
    models, and can be told to raise ``ImportError`` for a given name — simulating
    a backend whose optional deps (``compute/requirements-analysis.txt``) aren't
    installed, which the API maps to a 503 instead of a bare 500.
    """

    def resolve(name: str):
        if name in import_error_for:
            raise ImportError(f"optional analysis deps not installed for {name!r}")
        try:
            return analyzers[name]
        except KeyError:
            raise ValueError(f"unknown analyzer {name!r}") from None

    return resolve


@pytest.fixture
def make_app(tmp_path):
    """Factory for a ``TestClient`` over a fresh ``Store``, with test-controlled
    collector + analysis.

    Mirrors ``test_collection.py``'s ``api_client`` fixture (an explicit
    ``Store``, ``start_collector=False`` so no real edge/thread is created) and
    adds the two runtime controls this spec's app layer grows on top: a
    ``FakeClient`` standing in for the edge (so the collector routes have
    something to start/stop with no network) and an ``AnalysisManager`` whose
    resolver is test-supplied (so the analysis routes run with no real model and
    none of its heavy deps). ``create_app`` is imported lazily here — not at
    module top — so a not-yet-implemented piece it depends on fails each test
    that actually builds an app, not collection of this whole module.
    """

    def _make(analyzers: "dict | None" = None, import_error_for: "frozenset[str]" = frozenset()):
        from compute.api.app import create_app

        store = Store(
            db_path=str(tmp_path / "index.db"),
            media_root=str(tmp_path / "media"),
            max_bytes=10_000_000,
        )
        manager = AnalysisManager(resolver=_make_resolver(analyzers or {}, import_error_for))
        app = create_app(store=store, client=FakeClient(), start_collector=False, analysis_manager=manager)
        return TestClient(app), store

    return _make


def _poll_until_done(client: TestClient, timeout: float = 5.0, interval: float = 0.01) -> dict:
    """Poll ``GET /api/analysis/status`` until the job stops running; return it.

    Every sweep under test runs a handful of frames through a trivial fake
    analyzer, so this returns in well under the timeout; the deadline only
    guards against hanging the suite if a regression leaves ``running`` stuck.
    """
    deadline = time.monotonic() + timeout
    status = client.get("/api/analysis/status").json()
    while status["running"] and time.monotonic() < deadline:
        time.sleep(interval)
        status = client.get("/api/analysis/status").json()
    assert not status["running"], f"analysis job did not finish within {timeout}s: {status}"
    return status


# --- POST /api/analysis/run + GET /api/analysis/status -------------------------


@_requires_cv
def test_analysis_run_starts_a_job_and_completes(make_app):
    fake = FakeAnalyzer(name="yolo")
    client, store = make_app(analyzers={"yolo": fake})
    for i in range(3):
        store.add(_frame(frame_id=i, ts=i, body=_jpeg_gray(0)), recv_ts_ms=1_700_000_000_000 + i)

    resp = client.post("/api/analysis/run", json={"analyzer": "yolo"})
    assert resp.status_code == 200
    assert resp.json()["analyzer"] == "yolo"

    status = _poll_until_done(client)
    assert status["error"] is None
    assert status["done"] == status["total"] == 3
    assert store.analysis_summary("yolo") == {"analyzed": 3, "present": 0}
    assert fake.prepared_with is store


@_requires_cv
def test_analysis_disagreement_views_return_expected_ids(make_app):
    fake = FakeAnalyzer(name="yolo")
    client, store = make_app(analyzers={"yolo": fake})

    # motion=0 (still), oracle sees the subject (bright) -> a genuine miss.
    id_missed = store.add(
        _frame(frame_id=1, ts=1, motion=False, body=_jpeg_gray(255)), recv_ts_ms=1_700_000_000_001
    )
    # motion=1 (fired), oracle sees nothing (dark) -> a false trigger.
    id_false = store.add(
        _frame(frame_id=2, ts=2, motion=True, bbox=(0.0, 0.0, 0.1, 0.1), area=0.05, body=_jpeg_gray(0)),
        recv_ts_ms=1_700_000_000_002,
    )
    # The two agreeing quadrants must appear in NEITHER disagreement view.
    id_agree_present = store.add(
        _frame(frame_id=3, ts=3, motion=True, bbox=(0.0, 0.0, 0.1, 0.1), area=0.05, body=_jpeg_gray(255)),
        recv_ts_ms=1_700_000_000_003,
    )
    id_agree_absent = store.add(
        _frame(frame_id=4, ts=4, motion=False, body=_jpeg_gray(0)), recv_ts_ms=1_700_000_000_004
    )

    resp = client.post("/api/analysis/run", json={"analyzer": "yolo"})
    assert resp.status_code == 200
    _poll_until_done(client)

    resp = client.get("/api/frames", params={"analyzer": "yolo", "disagree": "missed"})
    assert resp.status_code == 200
    frames = resp.json()["frames"]
    assert [f["id"] for f in frames] == [id_missed]
    assert "score" in frames[0]  # disagreement rows carry the oracle's score

    resp = client.get("/api/frames", params={"analyzer": "yolo", "disagree": "false"})
    assert resp.status_code == 200
    assert [f["id"] for f in resp.json()["frames"]] == [id_false]

    # Sanity: the agreeing frames never show up in either disagreement view.
    disagreeing_ids = {id_missed, id_false}
    assert id_agree_present not in disagreeing_ids
    assert id_agree_absent not in disagreeing_ids


@_requires_cv
def test_analysis_run_while_running_enqueues(make_app):
    # The 409 refusal is gone: a second run while one is active ENQUEUES behind it and
    # both drain serially. Two DISTINCT windows keep the jobs from deduping.
    gate = threading.Event()  # cleared: the first frame blocks until we release it
    fake = FakeAnalyzer(name="yolo", gate=gate)
    client, store = make_app(analyzers={"yolo": fake})
    ids = [
        store.add(_frame(frame_id=i, ts=i, body=_jpeg_gray(0)), recv_ts_ms=1_700_000_000_000 + i)
        for i in range(2)
    ]

    r1 = client.post("/api/analysis/run", json={"analyzer": "yolo", "since_id": ids[0], "until_id": ids[0]})
    assert r1.status_code == 200
    assert r1.json()["running"] is True  # its one frame is stuck in flight on the gate
    assert r1.json()["position"] == 0

    # Second request no longer 409s — it enqueues at position 1.
    r2 = client.post("/api/analysis/run", json={"analyzer": "yolo", "since_id": ids[1], "until_id": ids[1]})
    assert r2.status_code == 200
    assert r2.json()["position"] == 1
    assert r2.json()["deduped"] is False

    gate.set()
    status = _poll_until_done(client)
    # Both windows verdicted, and both jobs recorded "done" (most-recent first).
    assert store.analysis_summary("yolo")["analyzed"] == 2
    assert [h["state"] for h in status["history"][:2]] == ["done", "done"]


def test_analysis_run_unknown_analyzer_is_400(make_app):
    client, _store = make_app(analyzers={})
    resp = client.post("/api/analysis/run", json={"analyzer": "bogus"})
    assert resp.status_code == 400


def test_analysis_run_inverted_range_is_400(make_app):
    # since_id > until_id is an impossible window (selects no frames): reject it as a
    # client error rather than launch a sweep that silently verdicts nothing.
    client, _store = make_app()
    resp = client.post(
        "/api/analysis/run", json={"analyzer": "yolo", "since_id": 90, "until_id": 10}
    )
    assert resp.status_code == 400


def test_analysis_run_missing_deps_is_503(make_app):
    # "yolo" is a valid analyzer name (passes the ANALYZER_NAMES gate), but the
    # resolver raises ImportError for it, simulating requirements-analysis.txt
    # not being installed. No cv2/sweep involved: start() fails before any decode.
    client, _store = make_app(analyzers={}, import_error_for=frozenset({"yolo"}))
    resp = client.post("/api/analysis/run", json={"analyzer": "yolo"})
    assert resp.status_code == 503


@_requires_cv
def test_analysis_cancel_stops_the_job_early(make_app):
    gate = threading.Event()
    fake = FakeAnalyzer(name="yolo", gate=gate)
    client, store = make_app(analyzers={"yolo": fake})
    for i in range(3):
        store.add(_frame(frame_id=i, ts=i, body=_jpeg_gray(200)), recv_ts_ms=1_700_000_000_000 + i)

    resp = client.post("/api/analysis/run", json={"analyzer": "yolo"})
    assert resp.status_code == 200
    assert resp.json()["running"] is True  # frame 1 is blocked on the gate

    resp = client.post("/api/analysis/cancel")
    assert resp.status_code == 200

    # Release the one in-flight frame; the loop must see the cancel at the NEXT
    # frame boundary and stop, leaving 1 verdict written, not 3.
    gate.set()
    status = _poll_until_done(client)
    assert status["done"] == 1
    assert store.analysis_summary("yolo")["analyzed"] == 1


@_requires_cv
def test_analysis_run_reanalyze_clears_prior_rows(make_app):
    gate = threading.Event()
    gate.set()  # first run: don't block, run straight to completion
    fake = FakeAnalyzer(name="yolo", gate=gate)
    client, store = make_app(analyzers={"yolo": fake})
    store.add(_frame(frame_id=1, body=_jpeg_gray(0)), recv_ts_ms=1_700_000_000_000)
    store.add(_frame(frame_id=2, body=_jpeg_gray(255)), recv_ts_ms=1_700_000_000_001)

    resp = client.post("/api/analysis/run", json={"analyzer": "yolo"})
    assert resp.status_code == 200
    _poll_until_done(client)
    assert store.analysis_summary("yolo") == {"analyzed": 2, "present": 1}

    # Second run with reanalyze=True: the clear now happens on the WORKER thread,
    # after a successful prepare() and before the (gated) first analyze() — so a run
    # that fails at start() can't wipe verdicts (see run_analysis). Clearing the gate
    # first freezes the sweep at its first frame, so once the clear lands the count
    # stays 0 until we release it; poll briefly for the clear to be observed.
    gate.clear()
    resp = client.post("/api/analysis/run", json={"analyzer": "yolo", "reanalyze": True})
    assert resp.status_code == 200
    deadline = time.monotonic() + 5.0
    while store.analysis_summary("yolo")["analyzed"] != 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert store.analysis_summary("yolo") == {"analyzed": 0, "present": 0}

    gate.set()
    _poll_until_done(client)
    assert store.analysis_summary("yolo") == {"analyzed": 2, "present": 1}


def test_analysis_run_unavailable_backend_is_503(make_app):
    # The REAL missing-deps mechanism: the resolver succeeds (the name is valid) but
    # the backend's ensure_available() raises ImportError (torch/CUDA absent). start()
    # calls it synchronously, so the API returns 503 at request time — not a 200
    # followed only by a background status().error.
    fake = FakeAnalyzer(name="yolo", unavailable=True)
    client, _store = make_app(analyzers={"yolo": fake})
    resp = client.post("/api/analysis/run", json={"analyzer": "yolo"})
    assert resp.status_code == 503


def test_analysis_reanalyze_missing_deps_preserves_verdicts(make_app):
    # reanalyze clears verdicts only in the worker, AFTER a successful prepare(); a run
    # that fails synchronously at start() (missing deps → 503) must leave the prior
    # verdicts intact rather than wiping them for a sweep that never runs.
    client, store = make_app(analyzers={}, import_error_for=frozenset({"yolo"}))
    fid = store.add(_frame(frame_id=1), recv_ts_ms=1_700_000_000_000)
    store.write_analysis(fid, "yolo", True, 1.0, None)
    assert store.analysis_summary("yolo")["analyzed"] == 1

    resp = client.post("/api/analysis/run", json={"analyzer": "yolo", "reanalyze": True})
    assert resp.status_code == 503
    assert store.analysis_summary("yolo")["analyzed"] == 1  # NOT wiped


# --- GET /api/stats + collector start/stop --------------------------------------


def test_api_stats_includes_collector_running(make_app):
    client, store = make_app()
    store.add(_frame(frame_id=1), recv_ts_ms=1_700_000_000_000)

    resp = client.get("/api/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert body["collector_running"] is False
    assert body["count"] == 1  # the existing store fields are still there alongside it


def test_collector_start_then_stop_flips_running(make_app):
    client, _store = make_app()

    assert client.get("/api/stats").json()["collector_running"] is False

    resp = client.post("/api/collector/start")
    assert resp.status_code == 200
    assert resp.json()["running"] is True
    assert client.get("/api/stats").json()["collector_running"] is True

    resp = client.post("/api/collector/stop")
    assert resp.status_code == 200
    assert resp.json()["running"] is False
    assert client.get("/api/stats").json()["collector_running"] is False


def _live_app(tmp_path, *, autostart) -> TestClient:
    """A production-shaped app (``start_collector=True``) with a Fake edge client.

    Passing an explicit ``FakeClient`` keeps the real ``EdgeClient`` (and its
    ``requests``/network) out of the wiring path while still exercising
    ``start_collector=True`` — the branch a bare ``compute.sh`` launch takes.
    ``autostart`` is passed explicitly so ambient ``CAT_COLLECT_AUTOSTART`` can't
    flip the assertion.
    """
    from compute.api.app import create_app

    store = Store(
        db_path=str(tmp_path / "index.db"),
        media_root=str(tmp_path / "media"),
        max_bytes=10_000_000,
    )
    return TestClient(create_app(store=store, client=FakeClient(), start_collector=True, autostart=autostart))


def test_live_app_wires_collector_but_does_not_autostart(tmp_path):
    # The fresh-launch contract: start_collector=True wires the collector (so the UI
    # can start it) but does NOT begin collecting — the store isn't written until the
    # operator clicks Start. Verify it starts stopped AND is genuinely startable.
    client = _live_app(tmp_path, autostart=False)
    assert client.get("/api/stats").json()["collector_running"] is False
    assert client.post("/api/collector/start").json()["running"] is True
    client.post("/api/collector/stop")


def test_autostart_true_begins_collecting(tmp_path):
    # The CAT_COLLECT_AUTOSTART opt-in: autostart=True restores begin-immediately, so
    # the collector is already running before any /api/collector/start call.
    client = _live_app(tmp_path, autostart=True)
    assert client.get("/api/stats").json()["collector_running"] is True
    client.post("/api/collector/stop")


def test_autostart_resolves_from_env_when_unset(tmp_path, monkeypatch):
    # autostart=None (the factory default) reads CAT_COLLECT_AUTOSTART: unset/empty →
    # off, a truthy spelling → on. This is what `uvicorn --factory create_app` gets,
    # since it calls create_app() with no args.
    from compute.api.app import create_app

    def build(name):
        store = Store(
            db_path=str(tmp_path / name / "index.db"),
            media_root=str(tmp_path / name / "media"),
            max_bytes=10_000_000,
        )
        return TestClient(create_app(store=store, client=FakeClient(), start_collector=True))

    monkeypatch.delenv("CAT_COLLECT_AUTOSTART", raising=False)
    assert build("off").get("/api/stats").json()["collector_running"] is False

    monkeypatch.setenv("CAT_COLLECT_AUTOSTART", "yes")
    c = build("on")
    assert c.get("/api/stats").json()["collector_running"] is True
    c.post("/api/collector/stop")


# --- Frame-range groups: POST/GET/DELETE /api/groups, GET /api/range/count -----
#
# The name->bounds bookmark layer (see the frame-range-groups spec). No cv2/sweep
# involved — group CRUD and range counting are pure store reads/writes over the
# `frames`/`groups` tables.


def test_groups_create_list_delete_roundtrip(make_app):
    client, store = make_app()
    ids = [
        store.add(_frame(frame_id=i, ts=i), recv_ts_ms=1_700_000_000_000 + i) for i in range(5)
    ]

    resp = client.post(
        "/api/groups", json={"name": "dusk visit", "start_id": ids[1], "end_id": ids[3]}
    )
    assert resp.status_code == 200
    created = resp.json()
    assert created["name"] == "dusk visit"
    assert created["start_id"] == ids[1]
    assert created["end_id"] == ids[3]
    assert created["count"] == 3  # ids[1], ids[2], ids[3]
    group_id = created["id"]

    resp = client.get("/api/groups")
    assert resp.status_code == 200
    groups = resp.json()["groups"]
    assert len(groups) == 1
    assert groups[0]["id"] == group_id
    assert groups[0]["name"] == "dusk visit"
    assert groups[0]["count"] == 3

    resp = client.delete(f"/api/groups/{group_id}")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "deleted": 1}

    # Removing the bookmark never touches the frames it spanned.
    assert client.get("/api/groups").json()["groups"] == []
    assert client.get("/api/range/count", params={"since_id": ids[1], "until_id": ids[3]}).json() == {
        "count": 3
    }


def test_groups_create_normalizes_endpoint_order(make_app):
    # The two endpoint clicks can arrive in either order; create_group normalizes
    # to start_id=min/end_id=max regardless of which one the caller names "start".
    client, store = make_app()
    ids = [store.add(_frame(frame_id=i, ts=i), recv_ts_ms=1_700_000_000_000 + i) for i in range(3)]

    resp = client.post("/api/groups", json={"name": "reversed", "start_id": ids[2], "end_id": ids[0]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["start_id"] == ids[0]
    assert body["end_id"] == ids[2]


def test_groups_delete_unknown_id_is_idempotent(make_app):
    client, _store = make_app()
    resp = client.delete("/api/groups/999")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "deleted": 0}


def test_groups_create_unknown_endpoint_is_400(make_app):
    client, store = make_app()
    fid = store.add(_frame(frame_id=1), recv_ts_ms=1_700_000_000_000)

    resp = client.post("/api/groups", json={"name": "bad", "start_id": fid, "end_id": fid + 999})
    assert resp.status_code == 400


def test_range_count_scoped_and_unscoped(make_app):
    client, store = make_app()
    ids = [store.add(_frame(frame_id=i, ts=i), recv_ts_ms=1_700_000_000_000 + i) for i in range(5)]

    resp = client.get("/api/range/count")
    assert resp.status_code == 200
    assert resp.json()["count"] == 5  # absent bounds = whole store

    resp = client.get("/api/range/count", params={"since_id": ids[1], "until_id": ids[3]})
    assert resp.status_code == 200
    assert resp.json()["count"] == 3

    # One-sided bounds are each unbounded on the other side.
    assert client.get("/api/range/count", params={"since_id": ids[3]}).json()["count"] == 2
    assert client.get("/api/range/count", params={"until_id": ids[1]}).json()["count"] == 2


# --- /api/frames?since_id=&until_id= scopes the browse + disagreement feeds ----


def test_frames_since_id_until_id_scopes_the_feed(make_app):
    client, store = make_app()
    ids = [store.add(_frame(frame_id=i, ts=i), recv_ts_ms=1_700_000_000_000 + i) for i in range(5)]

    resp = client.get("/api/frames", params={"since_id": ids[1], "until_id": ids[3]})
    assert resp.status_code == 200
    got_ids = {f["id"] for f in resp.json()["frames"]}
    assert got_ids == set(ids[1:4])

    # Absent bounds still return the whole store, byte-for-byte today's behavior.
    resp = client.get("/api/frames")
    assert {f["id"] for f in resp.json()["frames"]} == set(ids)


@_requires_cv
def test_frames_disagreement_view_scoped_by_since_until(make_app):
    fake = FakeAnalyzer(name="yolo")
    client, store = make_app(analyzers={"yolo": fake})
    # Five "missed" frames: motion=0 (still), oracle bright -> present -> a genuine miss.
    ids = [
        store.add(
            _frame(frame_id=i, ts=i, motion=False, body=_jpeg_gray(255)),
            recv_ts_ms=1_700_000_000_000 + i,
        )
        for i in range(5)
    ]

    resp = client.post("/api/analysis/run", json={"analyzer": "yolo"})
    assert resp.status_code == 200
    _poll_until_done(client)

    resp = client.get(
        "/api/frames",
        params={"analyzer": "yolo", "disagree": "missed", "since_id": ids[1], "until_id": ids[3]},
    )
    assert resp.status_code == 200
    got_ids = {f["id"] for f in resp.json()["frames"]}
    assert got_ids == set(ids[1:4])


# --- POST /api/analysis/run forwards since_id/until_id to the manager ----------


@_requires_cv
def test_analysis_run_forwards_since_id_until_id_to_manager(tmp_path):
    fake = FakeAnalyzer(name="yolo")
    manager = SpyAnalysisManager(_make_resolver({"yolo": fake}))
    client, store = _make_app_with_manager(tmp_path, manager)
    ids = [
        store.add(_frame(frame_id=i, ts=i, body=_jpeg_gray(0)), recv_ts_ms=1_700_000_000_000 + i)
        for i in range(3)
    ]

    resp = client.post(
        "/api/analysis/run",
        json={"analyzer": "yolo", "since_id": ids[0], "until_id": ids[1]},
    )
    assert resp.status_code == 200
    assert manager.enqueue_calls == [
        {"name": "yolo", "reanalyze": False, "since_id": ids[0], "until_id": ids[1]}
    ]
    body = resp.json()
    assert body["since_id"] == ids[0]
    assert body["until_id"] == ids[1]

    status = _poll_until_done(client)
    assert status["since_id"] == ids[0]
    assert status["until_id"] == ids[1]
    # Only the in-scope frames were verdicted, not the third (id ids[2]).
    assert store.analysis_summary("yolo")["analyzed"] == 2


@_requires_cv
def test_analysis_run_absent_scope_forwards_none(tmp_path):
    # The strict-superset case: an absent since_id/until_id in the request body must
    # forward as None — a whole-store sweep, exactly as before this feature existed.
    fake = FakeAnalyzer(name="yolo")
    manager = SpyAnalysisManager(_make_resolver({"yolo": fake}))
    client, store = _make_app_with_manager(tmp_path, manager)
    store.add(_frame(frame_id=1, body=_jpeg_gray(0)), recv_ts_ms=1_700_000_000_000)

    resp = client.post("/api/analysis/run", json={"analyzer": "yolo"})
    assert resp.status_code == 200
    assert manager.enqueue_calls == [
        {"name": "yolo", "reanalyze": False, "since_id": None, "until_id": None}
    ]
    body = resp.json()
    assert body["since_id"] is None
    assert body["until_id"] is None

    _poll_until_done(client)
    assert store.analysis_summary("yolo")["analyzed"] == 1


# --- Queue control routes: cancel / clear / stop-all ----------------------------


@_requires_cv
def test_analysis_queue_clear_route_drops_pending(make_app):
    # POST /api/analysis/queue/clear empties the pending deque; the running job finishes.
    gate = threading.Event()  # cleared: head job blocks
    fake = FakeAnalyzer(name="yolo", gate=gate)
    client, store = make_app(analyzers={"yolo": fake})
    ids = [
        store.add(_frame(frame_id=i, ts=i, body=_jpeg_gray(0)), recv_ts_ms=1_700_000_000_000 + i)
        for i in range(3)
    ]

    client.post("/api/analysis/run", json={"analyzer": "yolo", "since_id": ids[0], "until_id": ids[0]})
    client.post("/api/analysis/run", json={"analyzer": "yolo", "since_id": ids[1], "until_id": ids[1]})
    client.post("/api/analysis/run", json={"analyzer": "yolo", "since_id": ids[2], "until_id": ids[2]})
    assert len(client.get("/api/analysis/status").json()["queue"]) == 2

    resp = client.post("/api/analysis/queue/clear")
    assert resp.status_code == 200
    assert resp.json()["queue"] == []      # pending dropped
    assert resp.json()["running"] is True  # running job untouched

    gate.set()
    _poll_until_done(client)
    assert store.analysis_summary("yolo")["analyzed"] == 1  # only the running job ran


@_requires_cv
def test_analysis_queue_stop_all_route_clears_and_cancels(make_app):
    # POST /api/analysis/queue/stop-all clears pending AND cancels the running job.
    gate = threading.Event()
    fake = FakeAnalyzer(name="yolo", gate=gate)
    client, store = make_app(analyzers={"yolo": fake})
    ids = [
        store.add(_frame(frame_id=i, ts=i, body=_jpeg_gray(0)), recv_ts_ms=1_700_000_000_000 + i)
        for i in range(3)
    ]

    client.post("/api/analysis/run", json={"analyzer": "yolo", "since_id": ids[0], "until_id": ids[2]})
    client.post("/api/analysis/run", json={"analyzer": "yolo", "since_id": ids[1], "until_id": ids[1]})
    assert len(client.get("/api/analysis/status").json()["queue"]) == 1

    resp = client.post("/api/analysis/queue/stop-all")
    assert resp.status_code == 200
    assert resp.json()["queue"] == []

    gate.set()
    status = _poll_until_done(client)
    assert status["history"][0]["state"] == "canceled"


# --- Collector motion-only route + /api/stats fields ----------------------------


def test_collector_motion_only_route_toggles_and_persists(make_app):
    client, store = make_app()
    assert client.get("/api/stats").json()["motion_only"] is False

    resp = client.post("/api/collector/motion-only", json={"motion_only": True})
    assert resp.status_code == 200
    assert resp.json() == {"motion_only": True}
    assert client.get("/api/stats").json()["motion_only"] is True
    assert store.get_setting("motion_only") == "1"  # persisted for restore across restart

    resp = client.post("/api/collector/motion-only", json={"motion_only": False})
    assert resp.json() == {"motion_only": False}
    assert store.get_setting("motion_only") == "0"


def _fresh_app(tmp_path, name: str = "") -> "tuple[TestClient, Store]":
    """A TestClient + Store over ``tmp_path[/name]``, wired like ``make_app`` but built
    directly so two apps can share ONE db file (to test persistence across a restart)."""
    from compute.api.app import create_app

    root = tmp_path / name if name else tmp_path
    store = Store(
        db_path=str(root / "index.db"),
        media_root=str(root / "media"),
        max_bytes=10_000_000,
    )
    app = create_app(store=store, client=FakeClient(), start_collector=False)
    return TestClient(app), store


def test_motion_only_persists_and_restores_across_create_app(tmp_path):
    # A first app flips motion-only on (persisted to the store's settings KV). A fresh
    # app over the SAME db restores that flag into memory at launch — but leaves the
    # collector STOPPED (a bare launch never auto-starts; changelog 28's safety property).
    c1, _s1 = _fresh_app(tmp_path)
    c1.post("/api/collector/motion-only", json={"motion_only": True})
    assert c1.get("/api/stats").json()["motion_only"] is True

    c2, _s2 = _fresh_app(tmp_path)  # "restart" over the same db
    stats = c2.get("/api/stats").json()
    assert stats["motion_only"] is True          # restored from settings
    assert stats["collector_running"] is False   # still stopped, not auto-started


def test_resume_available_reflects_persisted_collector_intent(tmp_path):
    # resume_available is (persisted collector_running == "1") AND (collector stopped).
    # It is False while actually running, True after a mid-run "restart", and cleared by
    # a one-click resume or by an operator stop.
    c1, _s1 = _fresh_app(tmp_path)
    c1.post("/api/collector/start")  # persists collector_running=1
    assert c1.get("/api/stats").json()["resume_available"] is False  # running now → not "resume"

    c2, _s2 = _fresh_app(tmp_path)  # restart: intent was on, collector starts stopped
    stats = c2.get("/api/stats").json()
    assert stats["collector_running"] is False
    assert stats["resume_available"] is True

    # One-click resume clears the prompt.
    assert c2.post("/api/collector/start").json()["running"] is True
    assert c2.get("/api/stats").json()["resume_available"] is False

    # An operator stop clears the persisted intent, so a later launch won't offer Resume.
    c2.post("/api/collector/stop")
    assert c2.get("/api/stats").json()["resume_available"] is False


def test_clear_reseeds_mode_when_collector_running(make_app):
    # clear() drops mode_changes (keyed to frame ids), but the /api/clear route re-seeds
    # the CURRENT mode when collection is live — otherwise a motion-only run after a clear
    # would sit in an empty log and read as reliable full capture. The re-seed leaves
    # exactly one ON boundary (stamped at the post-wipe latest id 0).
    client, store = make_app()
    client.post("/api/collector/start")  # running (FakeClient → empty stream, flag stays on)
    client.post("/api/collector/motion-only", json={"motion_only": True})

    resp = client.post("/api/clear")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    # Re-seeded: a single motion-only ON span at the store start (id 0), not an empty log.
    assert store.motion_only_spans() == [{"start_id": 0, "end_id": 0}]
    client.post("/api/collector/stop")


def test_clear_does_not_reseed_when_collector_stopped(make_app):
    # With collection stopped, /api/clear does NOT re-seed — the log is simply empty after
    # the wipe (nothing is silently written to a store whose collector isn't running).
    client, store = make_app()
    client.post("/api/collector/motion-only", json={"motion_only": True})  # a mode row exists
    assert store.motion_only_spans() != []

    client.post("/api/clear")
    assert store.motion_only_spans() == []  # dropped, and not re-seeded (collector stopped)


# --- New read endpoints: resolve / sample / timeline / visits -------------------


def test_frames_resolve_and_sample_endpoints(make_app):
    client, store = make_app()
    base = 1_700_000_000_000
    ids = [store.add(_frame(frame_id=i), recv_ts_ms=base + i * 100) for i in range(5)]

    resolved = client.get("/api/frames/resolve", params={"start_ts": base, "end_ts": base + 400}).json()
    assert resolved == {
        "since_id": ids[0],
        "until_id": ids[4],
        "since_ts": base,
        "until_ts": base + 400,
    }

    # A None-ish window (no bounds) still resolves both sides to null.
    assert client.get("/api/frames/resolve").json() == {
        "since_id": None,
        "until_id": None,
        "since_ts": None,
        "until_ts": None,
    }

    sampled = client.get("/api/frames/sample", params={"count": 2}).json()["frames"]
    assert sampled[0]["id"] == ids[0]  # first frame always included
    assert set(sampled[0].keys()) == {"id", "recv_ts", "url"}


def test_timeline_and_visits_endpoints_shape_and_validation(make_app):
    client, store = make_app()
    base = 1_700_000_000_000
    f1 = store.add(_frame(frame_id=1, motion=False), recv_ts_ms=base)
    store.write_analysis(f1, "yolo", True, 0.9, None)  # a miss (motion 0, present)

    tl = client.get("/api/timeline", params={"oracle": "yolo", "bins": 4}).json()
    assert "bins" in tl and "motion_only_spans" in tl
    assert tl["motion_only_spans"] == []  # no motion-only span in this window

    vis = client.get("/api/visits", params={"oracle": "yolo", "mode": "missed"}).json()
    assert "visits" in vis and "motion_only_spans" in vis
    assert len(vis["visits"]) == 1  # the single missed frame is one visit

    # Validation: a bad oracle (400) on both, a bad mode (400) on visits.
    assert client.get("/api/timeline", params={"oracle": "bogus"}).status_code == 400
    assert client.get("/api/visits", params={"oracle": "bogus"}).status_code == 400
    assert client.get("/api/visits", params={"oracle": "yolo", "mode": "bogus"}).status_code == 400


def test_timeline_visits_attach_motion_only_spans_over_a_motion_only_window(make_app):
    # When the window overlaps a motion-only span, both endpoints hand the UI the span
    # list so it can flag "misses unmeasurable here" rather than reading an empty missed
    # set as perfect recall.
    client, store = make_app()
    store.record_mode_change(True)  # motion-only ON from the store start
    base = 1_700_000_000_000
    f1 = store.add(_frame(frame_id=1, motion=True, bbox=(0, 0, 0.1, 0.1), area=0.05), recv_ts_ms=base)
    store.write_analysis(f1, "yolo", False, 0.1, None)

    tl = client.get("/api/timeline", params={"oracle": "yolo"}).json()
    assert tl["motion_only_spans"] == [{"start_id": 0, "end_id": 1}]
    vis = client.get("/api/visits", params={"oracle": "yolo", "mode": "false"}).json()
    assert vis["motion_only_spans"] == [{"start_id": 0, "end_id": 1}]


@_requires_cv
def test_windowed_enqueue_flags_motion_only_overlap_but_yolo_does_not(make_app):
    # BSUV is windowed → flagged at enqueue over a motion-only span (its contiguity
    # assumption is broken there). YOLO is per-frame → never flagged, even over the same
    # span. Both drain (gate pre-set), each over the whole store.
    gate = threading.Event()
    gate.set()  # don't block — we only care about the enqueue-time overlap flag
    client, store = make_app(
        analyzers={
            "yolo": FakeAnalyzer(name="yolo", gate=gate),
            "bsuv": FakeAnalyzer(name="bsuv", windowed=True, gate=gate),
        }
    )
    store.record_mode_change(True)  # motion-only ON from the store start
    for i in range(2):
        store.add(_frame(frame_id=i, body=_jpeg_gray(0)), recv_ts_ms=1_700_000_000_000 + i)

    ry = client.post("/api/analysis/run", json={"analyzer": "yolo"})
    assert ry.status_code == 200
    assert ry.json()["motion_only_overlap"] is False
    _poll_until_done(client)

    rb = client.post("/api/analysis/run", json={"analyzer": "bsuv"})
    assert rb.status_code == 200
    assert rb.json()["motion_only_overlap"] is True
    _poll_until_done(client)
