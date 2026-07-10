"""Tests for the edge-config proxy + offline MOG2 tuning routes on ``compute/api/app.py``
(the motion-gate-diagnostic spec's API layer).

The lower layers are already covered directly: ``test_scorecard.py`` exercises
``Store.gate_scorecard`` / ``gate_fidelity``, ``test_mog2.py`` the ``MogAnalyzer`` +
``start_analyzer`` machinery, and ``test_edge_config.py`` ``EdgeClient.get_config``.
This file is the layer above: it drives ``GET /api/edge/config``,
``POST /api/tuning/rerun``, and ``GET /api/tuning/compare`` *through* ``create_app`` +
``TestClient``, so what's under test is the routing/validation/wiring ``app.py`` adds —
the defaults fallback, the param vocabulary translation, the 400/409 mapping, and how
the three scorecards + fidelity + deltas are assembled.

No real edge, no real model: ``create_app(store=..., client=<fake>,
start_collector=False, analysis_manager=AnalysisManager(resolver=...))`` — the same
injection-seam pattern ``test_api_analysis.py`` uses. Clients here vary by whether they
expose ``get_config`` (an edge with settings vs. one that can't answer -> defaults).
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

# A minimal valid JPEG for the store (written verbatim, never decoded by these tests —
# only the real sweeps decode, and those build real images via ``_jpeg_gray``).
_JPEG_BODY = b"\xff\xd8\xff\xe0" + b"fake-jpeg-body" + b"\xff\xd9"

try:
    import cv2
    import numpy as np

    _HAVE_CV = True
except Exception:  # pragma: no cover - exercised only where cv2 is absent
    _HAVE_CV = False

_requires_cv = pytest.mark.skipif(not _HAVE_CV, reason="cv2/numpy required for the sweep tests")

# The defaults the app falls back to when the edge can't answer (must mirror
# ``compute/api/app.py``'s ``_EDGE_MOTION_DEFAULTS``, which mirrors edge/config/settings.py).
_DEFAULTS = {
    "var_threshold": 16.0,
    "learning_rate": 0.001,
    "min_area": 0.01,
    "max_area_fraction": 0.6,
    "persistence": 2,
    "motion_downscale": 320,
}


def _frame(
    frame_id: int = 1,
    ts: int = 1_000,
    motion: bool = False,
    bbox=None,
    area: float = 0.0,
    body: bytes = _JPEG_BODY,
) -> StreamFrame:
    meta = StreamFrameMeta(frame_id=frame_id, ts=ts, motion=motion, bbox=bbox, area=area)
    return StreamFrame(meta, body)


def _jpeg_gray(level: int) -> bytes:
    """A real solid-gray JPEG at ``level`` (0..255); decodable by a real sweep."""
    img = np.full((16, 16, 3), level, dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", img)
    assert ok
    return buf.tobytes()


def _edge_params(persistence: int = 1) -> dict:
    """A full edge-vocabulary param dict (the shape /api/tuning/rerun accepts)."""
    return {
        "var_threshold": 16.0,
        "learning_rate": 0.01,
        "min_area": 0.01,
        "max_area_fraction": 0.6,
        "persistence": persistence,
        "motion_downscale": 320,
    }


def _slot_detail() -> dict:
    """A ``MogAnalyzer``-shaped detail blob: ``{"bbox", "params"}`` with the params in
    ``MotionParams`` field names (``downscale``, not ``motion_downscale``)."""
    return {
        "bbox": None,
        "params": {
            "var_threshold": 20.0,
            "learning_rate": 0.02,
            "min_area": 0.02,
            "max_area_fraction": 0.5,
            "persistence": 3,
            "downscale": 256,
        },
    }


class FakeAnalyzer:
    """A controllable stand-in oracle: verdict = the frame's own mean gray level."""

    def __init__(self, name: str = "yolo", windowed: bool = False, gate=None) -> None:
        self.name = name
        self.windowed = windowed
        self.gate = gate

    def ensure_available(self) -> None:
        pass

    def prepare(self, store) -> None:
        pass

    def analyze(self, image) -> AnalysisResult:
        if self.gate is not None:
            self.gate.wait(timeout=5)
        level = float(image.mean())
        return AnalysisResult(verdict=bool(level >= 127.0), score=level, detail={"level": level})


class ConfigClient:
    """A stand-in edge that answers ``get_config`` with a fixed body (a live Pi)."""

    def __init__(self, config: dict) -> None:
        self._config = config

    def get_config(self) -> dict:
        return dict(self._config)

    def iter_stream_reconnecting(self):
        return iter(())


class NoConfigClient:
    """A stand-in edge with NO ``get_config`` — calling it raises AttributeError, which
    the proxy treats like any other failure and degrades to defaults."""

    def iter_stream_reconnecting(self):
        return iter(())


def _make_resolver(analyzers: dict):
    def resolve(name: str):
        try:
            return analyzers[name]
        except KeyError:
            raise ValueError(f"unknown analyzer {name!r}") from None

    return resolve


@pytest.fixture
def make_app(tmp_path):
    """Factory: (client, analyzers) -> (TestClient, Store) over a fresh store."""

    def _make(client=None, analyzers: "dict | None" = None):
        from compute.api.app import create_app

        store = Store(
            db_path=str(tmp_path / "index.db"),
            media_root=str(tmp_path / "media"),
            max_bytes=10_000_000,
        )
        manager = AnalysisManager(resolver=_make_resolver(analyzers or {}))
        app = create_app(
            store=store, client=client, start_collector=False, analysis_manager=manager
        )
        return TestClient(app), store

    return _make


def _poll_until_done(client: TestClient, timeout: float = 5.0, interval: float = 0.01) -> dict:
    deadline = time.monotonic() + timeout
    status = client.get("/api/analysis/status").json()
    while status["running"] and time.monotonic() < deadline:
        time.sleep(interval)
        status = client.get("/api/analysis/status").json()
    assert not status["running"], f"tuning job did not finish within {timeout}s: {status}"
    return status


# --- GET /api/edge/config -------------------------------------------------------


def test_edge_config_from_edge(make_app):
    # A live edge (config includes camera fields too) -> source "edge", only the six
    # motion params surface, verbatim.
    cfg = {**_edge_params(persistence=4), "device": "csi:0", "rotation": 90, "fps": 5, "focus": None}
    client, _store = make_app(client=ConfigClient(cfg))
    resp = client.get("/api/edge/config")
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "edge"
    assert body["params"] == _edge_params(persistence=4)
    assert "device" not in body["params"]  # camera fields are not motion params


def test_edge_config_partial_config_fills_defaults(make_app):
    # An older/thin Pi config missing a key stays source "edge" but fills the gap from
    # the defaults rather than 500-ing.
    partial = {**_edge_params(), "device": "csi:0"}
    del partial["motion_downscale"]
    client, _store = make_app(client=ConfigClient(partial))
    body = client.get("/api/edge/config").json()
    assert body["source"] == "edge"
    assert body["params"]["motion_downscale"] == _DEFAULTS["motion_downscale"]


def test_edge_config_defaults_when_client_cannot_answer(make_app):
    # A client with no get_config -> the failure path -> defaults.
    client, _store = make_app(client=NoConfigClient())
    body = client.get("/api/edge/config").json()
    assert body["source"] == "defaults"
    assert body["params"] == _DEFAULTS


def test_edge_config_defaults_when_no_client(make_app):
    # No edge client at all (a test/offline app) -> defaults, no crash.
    client, _store = make_app(client=None)
    body = client.get("/api/edge/config").json()
    assert body["source"] == "defaults"
    assert body["params"] == _DEFAULTS


# --- POST /api/tuning/rerun -----------------------------------------------------


def test_tuning_rerun_bad_slot_is_400(make_app):
    client, _store = make_app()
    resp = client.post("/api/tuning/rerun", json={"slot": "bogus", "params": _edge_params()})
    assert resp.status_code == 400


def test_tuning_rerun_missing_params_is_400(make_app):
    client, _store = make_app()
    bad = _edge_params()
    del bad["var_threshold"]
    resp = client.post("/api/tuning/rerun", json={"slot": "baseline", "params": bad})
    assert resp.status_code == 400


def test_tuning_rerun_non_numeric_param_is_400(make_app):
    client, _store = make_app()
    bad = {**_edge_params(), "var_threshold": "not-a-number"}
    resp = client.post("/api/tuning/rerun", json={"slot": "candidate", "params": bad})
    assert resp.status_code == 400


@_requires_cv
def test_tuning_rerun_while_running_is_409(make_app):
    # A gated oracle sweep occupies the single job slot; a rerun must be refused (409).
    gate = threading.Event()  # cleared: the one frame blocks until released
    client, store = make_app(analyzers={"yolo": FakeAnalyzer(name="yolo", gate=gate)})
    store.add(_frame(frame_id=1, body=_jpeg_gray(0)), recv_ts_ms=1_700_000_000_000)

    assert client.post("/api/analysis/run", json={"analyzer": "yolo"}).status_code == 200
    resp = client.post("/api/tuning/rerun", json={"slot": "baseline", "params": _edge_params()})
    assert resp.status_code == 409

    gate.set()
    _poll_until_done(client)


@_requires_cv
def test_tuning_rerun_runs_mog2_into_slot(make_app):
    # End-to-end: a real MogAnalyzer sweep lands verdicts in the mog2:candidate slot,
    # reported live through the shared analysis status, and untouched siblings stay empty.
    client, store = make_app()
    for i in range(3):
        store.add(_frame(frame_id=i, ts=i, body=_jpeg_gray(0)), recv_ts_ms=1_700_000_000_000 + i)

    resp = client.post("/api/tuning/rerun", json={"slot": "candidate", "params": _edge_params()})
    assert resp.status_code == 200
    assert resp.json()["analyzer"] == "mog2:candidate"

    status = _poll_until_done(client)
    assert status["error"] is None
    assert store.analysis_summary("mog2:candidate")["analyzed"] == 3
    # The sibling slot was never touched by this run.
    assert store.analysis_summary("mog2:baseline")["analyzed"] == 0


# --- GET /api/tuning/compare ----------------------------------------------------


def test_tuning_compare_bad_oracle_is_400(make_app):
    client, _store = make_app(client=None)
    resp = client.get("/api/tuning/compare", params={"oracle": "bogus"})
    assert resp.status_code == 400


def _seed(store: Store, *, slots) -> None:
    """Add 4 frames + a 'yolo' oracle verdict each; optionally seed the named mog2 slots
    (verdict = frames.motion so fidelity is perfect, with MogAnalyzer-shaped detail)."""
    for i in range(4):
        present = i % 2 == 0
        fid = store.add(
            _frame(frame_id=i, ts=i, motion=present, area=0.05 if present else 0.0),
            recv_ts_ms=1_700_000_000_000 + i,
        )
        store.write_analysis(fid, "yolo", present, 0.9 if present else 0.1, None)
        for slot in slots:
            store.write_analysis(fid, slot, present, 0.05 if present else 0.0, _slot_detail())


def test_tuning_compare_needs_rerun_when_slots_empty(make_app):
    # No mog2 rows at all -> both slots report needs_rerun; deltas + fidelity are null;
    # the live gate still scores (and the edge-config threshold seed degrades to defaults).
    client, store = make_app(client=NoConfigClient())
    _seed(store, slots=())

    body = client.get("/api/tuning/compare", params={"oracle": "yolo"}).json()
    assert body["oracle"] == "yolo"
    assert body["baseline"] == {"source": "mog2:baseline", "oracle": "yolo", "needs_rerun": True}
    assert body["candidate"] == {"source": "mog2:candidate", "oracle": "yolo", "needs_rerun": True}
    assert body["deltas"] is None
    assert body["fidelity"] is None
    assert "recall" in body["live"]  # live is a full scorecard regardless


def test_tuning_compare_candidate_unrun_gives_null_deltas(make_app):
    # Baseline has run, candidate hasn't -> baseline is a full card + fidelity computed,
    # candidate needs_rerun, and deltas stay null (nothing to diff).
    client, store = make_app(client=NoConfigClient())
    _seed(store, slots=("mog2:baseline",))

    body = client.get("/api/tuning/compare", params={"oracle": "yolo"}).json()
    assert "recall" in body["baseline"]
    assert body["candidate"].get("needs_rerun") is True
    assert body["deltas"] is None
    assert body["fidelity"] == {"compared": 4, "agree": 4, "rate": 1.0}


def test_tuning_compare_populated_assembles_cards_deltas_fidelity(make_app):
    # Both slots run (verdict = motion) -> full cards, computed deltas, perfect fidelity.
    client, store = make_app(client=NoConfigClient())
    _seed(store, slots=("mog2:baseline", "mog2:candidate"))

    body = client.get("/api/tuning/compare", params={"oracle": "yolo"}).json()
    for key in ("live", "baseline", "candidate"):
        assert "recall" in body[key], key
    assert body["deltas"] == {"missed": 0, "false": 0}
    assert body["fidelity"] == {"compared": 4, "agree": 4, "rate": 1.0}
