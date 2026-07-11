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

    def prepare(self, store, since_id: "int | None" = None) -> None:
        # since_id is the frame-range-groups scope run_analysis now always passes
        # (see Analyzer.prepare); this fake is stateless and ignores it.
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


class SpyAnalysisManager(AnalysisManager):
    """A real ``AnalysisManager`` that also records each ``start_analyzer()`` call's args.

    Used only by the frame-range-groups scoping tests below, to verify that
    ``POST /api/tuning/rerun`` forwards its ``since_id``/``until_id`` straight through to
    the manager, unmodified. Delegates to ``super().start_analyzer()`` so the re-run
    still actually executes — every other assertion (status, written verdicts) behaves
    exactly as the non-spy tests above; only the call args are captured.
    """

    def __init__(self, resolver) -> None:
        super().__init__(resolver=resolver)
        self.start_analyzer_calls: "list[dict]" = []

    def start_analyzer(
        self,
        store,
        analyzer,
        reanalyze: bool = False,
        since_id: "int | None" = None,
        until_id: "int | None" = None,
    ) -> None:
        self.start_analyzer_calls.append(
            {"analyzer": analyzer.name, "reanalyze": reanalyze, "since_id": since_id, "until_id": until_id}
        )
        super().start_analyzer(store, analyzer, reanalyze=reanalyze, since_id=since_id, until_id=until_id)


def _make_app_with_manager(tmp_path, manager, client=None) -> "tuple[TestClient, Store]":
    """Build a ``TestClient`` wired to a caller-supplied ``AnalysisManager``.

    ``make_app`` (the fixture below) always builds its own manager internally, so the
    scoping-forwarding tests — which need to inspect a spy manager *after* the request
    completes — construct the app directly here instead, mirroring ``make_app``'s own
    ``Store``/``create_app`` wiring.
    """
    from compute.api.app import create_app

    store = Store(
        db_path=str(tmp_path / "index.db"),
        media_root=str(tmp_path / "media"),
        max_bytes=10_000_000,
    )
    app = create_app(store=store, client=client, start_collector=False, analysis_manager=manager)
    return TestClient(app), store


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


def test_tuning_rerun_inverted_range_is_400(make_app):
    # An impossible window (since_id > until_id) selects no frames; a scoped run would
    # silently no-op, so the endpoint rejects it as a client error instead.
    client, _store = make_app()
    resp = client.post(
        "/api/tuning/rerun",
        json={"slot": "candidate", "params": _edge_params(), "since_id": 90, "until_id": 10},
    )
    assert resp.status_code == 400


def test_tuning_compare_inverted_range_is_400(make_app):
    client, _store = make_app(client=NoConfigClient())
    resp = client.get("/api/tuning/compare", params={"oracle": "yolo", "since_id": 90, "until_id": 10})
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


def _seed(store: Store, *, slots) -> "list[int]":
    """Add 4 frames + a 'yolo' oracle verdict each; optionally seed the named mog2 slots
    (verdict = frames.motion so fidelity is perfect, with MogAnalyzer-shaped detail).

    Returns the 4 assigned frame row ids in creation order, so a scoped-compare test
    can build ``since_id``/``until_id`` windows against them.
    """
    ids = []
    for i in range(4):
        present = i % 2 == 0
        fid = store.add(
            _frame(frame_id=i, ts=i, motion=present, area=0.05 if present else 0.0),
            recv_ts_ms=1_700_000_000_000 + i,
        )
        store.write_analysis(fid, "yolo", present, 0.9 if present else 0.1, None)
        for slot in slots:
            store.write_analysis(fid, slot, present, 0.05 if present else 0.0, _slot_detail())
        ids.append(fid)
    return ids


# Mirrors the compare endpoint's _WARMUP_FRAMES: the pre-window frame count at which a
# scoped window is treated as fully primed (warmup drops to 0). Kept here so a change to
# the app-side constant is caught by these tests rather than silently diverging.
_COMPARE_WARMUP = 500


def _seed_primed(store: Store, *, pre_fill: int, slots) -> "list[int]":
    """Add ``pre_fill`` verdict-less filler frames, THEN the 4-frame windowed set ``_seed``
    builds, and return the 4 window ids.

    The filler frames are what a scoped windowed re-run warm-starts from
    (``recent_before(window_start, N)``): with ``pre_fill >= _COMPARE_WARMUP`` the window
    enters FULLY primed, so a scoped compare over it derives warmup=0 and scores the whole
    window — the production case a 4-frame-only fixture can't reach (see the compare's
    graduated-warmup rule). The filler carry no verdicts, so they never enter a scored set
    and (being id < the window) never fall inside the [since_id, until_id] scope.
    """
    base = 1_700_000_000_000
    for j in range(pre_fill):
        store.add(_frame(frame_id=j, ts=j, motion=False, area=0.0), recv_ts_ms=base + j)
    ids = []
    for i in range(4):
        present = i % 2 == 0
        fid = store.add(
            _frame(frame_id=pre_fill + i, ts=pre_fill + i, motion=present, area=0.05 if present else 0.0),
            recv_ts_ms=base + pre_fill + i,
        )
        store.write_analysis(fid, "yolo", present, 0.9 if present else 0.1, None)
        for slot in slots:
            store.write_analysis(fid, slot, present, 0.05 if present else 0.0, _slot_detail())
        ids.append(fid)
    return ids


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


# --- GET /api/tuning/compare: the warmup/scope rule -----------------------------
#
# Unscoped: warmup=_COMPARE_WARMUP, no bounds — exactly today. Scoped: the bounds ride
# to every gate_scorecard/gate_fidelity call (see the spec's "Scorecard fairness"), and
# the warmup is GRADUATED by how well the window was primed — a scoped re-run warm-starts
# from up to _COMPARE_WARMUP frames just before the window, so:
#   - fully primed (>= _COMPARE_WARMUP frames precede it) -> warmup 0, whole window scored;
#   - under-primed (fewer precede it) -> only the shortfall dropped;
#   - at the store's very start (nothing precedes it) -> the full _COMPARE_WARMUP, like a
#     cold unscoped start — so a store-start window is NOT scored on a cold model.
# The 4-frame set sits inside the warmup prefix, so _seed_primed prepends filler frames to
# reach the fully-primed case a 4-frame-only fixture can't.


def test_tuning_compare_unscoped_uses_warmup_500_and_drops_the_small_store(make_app):
    client, store = make_app(client=NoConfigClient())
    _seed(store, slots=("mog2:baseline", "mog2:candidate"))

    body = client.get("/api/tuning/compare", params={"oracle": "yolo"}).json()
    for key in ("live", "baseline", "candidate"):
        assert body[key]["warmup"] == 500, key
        # warmup=500 over a 4-row scored set drops everything (nothing survives
        # past OFFSET 500) — the same cold-start assumption today's unscoped
        # compare has always made.
        assert body[key]["analyzed"] == 0, key
        assert body[key]["present"] == 0, key


def test_tuning_compare_scoped_fully_primed_window_uses_warmup_0_and_scores(make_app):
    # A window with >= _COMPARE_WARMUP frames before it is fully primed: warmup drops to
    # 0 and the whole 4-frame window is scored (the production case — a mid-store group).
    client, store = make_app(client=NoConfigClient())
    ids = _seed_primed(store, pre_fill=_COMPARE_WARMUP, slots=("mog2:baseline", "mog2:candidate"))

    body = client.get(
        "/api/tuning/compare",
        params={"oracle": "yolo", "since_id": ids[0], "until_id": ids[-1]},
    ).json()
    for key in ("live", "baseline", "candidate"):
        assert body[key]["warmup"] == 0, key
        assert body[key]["analyzed"] == 4, key
    assert body["deltas"] == {"missed": 0, "false": 0}
    # gate_fidelity is scoped too (same since_id/until_id) — perfect over exactly the
    # 4 in-window frames, NOT the 500 verdict-less filler.
    assert body["fidelity"] == {"compared": 4, "agree": 4, "rate": 1.0}


def test_tuning_compare_scoped_bounds_actually_narrow_the_scored_set(make_app):
    # Narrowing until_id to a sub-range of the (fully-primed) window changes the scored-set
    # size, proving the bounds reach store.gate_scorecard (not merely the warmup flag):
    # only ids[0..1] are in range here, so analyzed must drop to 2.
    client, store = make_app(client=NoConfigClient())
    ids = _seed_primed(store, pre_fill=_COMPARE_WARMUP, slots=("mog2:baseline", "mog2:candidate"))

    body = client.get(
        "/api/tuning/compare",
        params={"oracle": "yolo", "since_id": ids[0], "until_id": ids[1]},
    ).json()
    for key in ("live", "baseline", "candidate"):
        assert body[key]["warmup"] == 0, key
        assert body[key]["analyzed"] == 2, key
    assert body["fidelity"] == {"compared": 2, "agree": 2, "rate": 1.0}


def test_tuning_compare_one_sided_bound_also_counts_as_scoped(make_app):
    # Only since_id set (until_id absent) must still take the scoped path. With the window
    # fully primed, since_id=ids[1] leaves ids[1..3] in range -> warmup 0, analyzed 3.
    client, store = make_app(client=NoConfigClient())
    ids = _seed_primed(store, pre_fill=_COMPARE_WARMUP, slots=("mog2:baseline", "mog2:candidate"))

    body = client.get(
        "/api/tuning/compare", params={"oracle": "yolo", "since_id": ids[1]}
    ).json()
    for key in ("live", "baseline", "candidate"):
        assert body[key]["warmup"] == 0, key
        assert body[key]["analyzed"] == 3, key  # ids[1], ids[2], ids[3]


def test_tuning_compare_scoped_window_at_store_start_keeps_full_warmup(make_app):
    # A window that starts at the store's OLDEST frame has nothing before it to prime
    # from, so the re-run enters cold and the compare keeps the FULL warmup — the exact
    # early-window frames the owner selected are not scored on a still-adapting model.
    # (The 4-frame window then sits entirely inside the prefix -> analyzed 0.)
    client, store = make_app(client=NoConfigClient())
    ids = _seed(store, slots=("mog2:baseline", "mog2:candidate"))  # no filler: ids[0] is oldest

    body = client.get(
        "/api/tuning/compare",
        params={"oracle": "yolo", "since_id": ids[0], "until_id": ids[-1]},
    ).json()
    for key in ("live", "baseline", "candidate"):
        assert body[key]["warmup"] == _COMPARE_WARMUP, key
        assert body[key]["analyzed"] == 0, key


def test_tuning_compare_scoped_underprimed_window_drops_only_the_shortfall(make_app):
    # A window with FEWER than _COMPARE_WARMUP frames before it is under-primed: the
    # warmup is the shortfall (_COMPARE_WARMUP - pre_window), not 0 and not the full drop.
    client, store = make_app(client=NoConfigClient())
    pre = 3
    ids = _seed_primed(store, pre_fill=pre, slots=("mog2:baseline", "mog2:candidate"))

    body = client.get(
        "/api/tuning/compare",
        params={"oracle": "yolo", "since_id": ids[0], "until_id": ids[-1]},
    ).json()
    for key in ("live", "baseline", "candidate"):
        assert body[key]["warmup"] == _COMPARE_WARMUP - pre, key


# --- POST /api/tuning/rerun forwards since_id/until_id to the manager -----------


@_requires_cv
def test_tuning_rerun_forwards_since_id_until_id_to_manager(tmp_path):
    manager = SpyAnalysisManager(_make_resolver({}))
    client, store = _make_app_with_manager(tmp_path, manager)
    ids = [
        store.add(_frame(frame_id=i, ts=i, body=_jpeg_gray(0)), recv_ts_ms=1_700_000_000_000 + i)
        for i in range(3)
    ]

    resp = client.post(
        "/api/tuning/rerun",
        json={
            "slot": "candidate",
            "params": _edge_params(),
            "since_id": ids[0],
            "until_id": ids[1],
        },
    )
    assert resp.status_code == 200
    assert manager.start_analyzer_calls == [
        {"analyzer": "mog2:candidate", "reanalyze": True, "since_id": ids[0], "until_id": ids[1]}
    ]
    body = resp.json()
    assert body["since_id"] == ids[0]
    assert body["until_id"] == ids[1]

    status = _poll_until_done(client)
    assert status["since_id"] == ids[0]
    assert status["until_id"] == ids[1]
    # Only the two in-scope frames were verdicted, not the third.
    assert store.analysis_summary("mog2:candidate")["analyzed"] == 2


@_requires_cv
def test_tuning_rerun_absent_scope_forwards_none(tmp_path):
    # The strict-superset case: an absent since_id/until_id in the request body must
    # forward as None — a whole-slot re-run, exactly as before this feature existed.
    manager = SpyAnalysisManager(_make_resolver({}))
    client, store = _make_app_with_manager(tmp_path, manager)
    for i in range(3):
        store.add(_frame(frame_id=i, ts=i, body=_jpeg_gray(0)), recv_ts_ms=1_700_000_000_000 + i)

    resp = client.post("/api/tuning/rerun", json={"slot": "baseline", "params": _edge_params()})
    assert resp.status_code == 200
    assert manager.start_analyzer_calls == [
        {"analyzer": "mog2:baseline", "reanalyze": True, "since_id": None, "until_id": None}
    ]
    body = resp.json()
    assert body["since_id"] is None
    assert body["until_id"] is None

    _poll_until_done(client)
    assert store.analysis_summary("mog2:baseline")["analyzed"] == 3
