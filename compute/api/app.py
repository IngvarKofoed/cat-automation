"""FastAPI app for the compute tier: browse collected frames, serve media, clear.

The integration hub for the frame-collection browser (the compute analogue of
the edge's Flask app + background grabber). It wires three things together behind
one HTTP server: the bounded ``Store``, the background collector that fills it off
the edge stream, and a small JSON+media API a vanilla-JS page browses with. See
``docs/specs/2026-07-09-frame-collection-browser.md``.

Two runtime controls sit on top of that base (the motion-gate-oracles spec):

- **Collector start/stop.** The collector is owned by a ``CollectorManager`` (not
  a bare thread), so the browse UI can *freeze* the store — stop ingest — for a
  clean offline pass, then resume it. ``app.state.collector_manager`` is that
  handle; ``/api/collector/{start,stop}`` toggle it and ``/api/stats`` reports it.
- **Offline analysis.** A stronger, slower oracle (YOLO / BSUV) is swept over the
  *stored* frames on demand to validate the edge's cheap MOG2 gate, its verdicts
  landing in the store's ``analysis`` table. ``AnalysisManager`` runs one such job
  at a time (``/api/analysis/{run,cancel,status}``), and ``/api/frames`` grows a
  disagreement view (``analyzer`` + ``disagree=missed|false``) that surfaces the
  frames where MOG2 and a chosen oracle disagree.

``create_app`` is the injection seam, mirroring the edge's
``create_app(source_factory, start_grabber)``: tests pass an explicit ``store``
and ``start_collector=False`` to exercise the routes with no edge and no thread,
and can inject an ``analysis_manager`` whose resolver returns a fake analyzer so
the analysis routes run with no real model (and none of its heavy deps). There is
deliberately NO module-level app instance that would start a collector thread on
import — ``compute.sh`` launches ``uvicorn --factory ...:create_app``.
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from compute.analysis import ANALYZER_NAMES
from compute.analysis.mog2 import MogAnalyzer
from compute.analysis.runner import AnalysisManager
from compute.collection.collector import CollectorManager
from compute.collection.store import Store
from shared.motion import MotionParams

_WEB_DIR = Path(__file__).resolve().parent / "web"
_INDEX_HTML = _WEB_DIR / "index.html"

# Config via environment variables (the edge's style; the compute tier has no
# config store yet). CAT_PI_URL is read by EdgeClient itself, not here.
_ENV_DIR = "CAT_COLLECT_DIR"
_ENV_MAX_BYTES = "CAT_COLLECT_MAX_BYTES"
_DEFAULT_DIR = "./data/collection"
_DEFAULT_MAX_BYTES = 5368709120  # 5 GiB — ~2 h at 10 fps, a testing window

# Browse-page limit: default 200 rows, hard-capped so one request can't ask the
# server to marshal an unbounded page. The cap is generous because the browse
# grid lazy-loads images off-screen — the per-row JSON is tiny and only visible
# thumbnails actually fetch, so a big page is cheap to serve.
_DEFAULT_LIMIT = 200
_MAX_LIMIT = 1000

class AnalysisRunRequest(BaseModel):
    """Body of ``POST /api/analysis/run``: which oracle to sweep, and whether to redo.

    ``reanalyze`` clears the analyzer's prior verdicts first, so the next sweep
    re-verdicts the whole store (e.g. after swapping the model or its threshold)
    rather than the stateless default of skipping already-analyzed frames.
    """

    analyzer: str
    reanalyze: bool = False


# The six motion-gate params, in the edge's own vocabulary — the exact keys the Pi
# persists and that ``EdgeClient.get_config`` returns (``max_area_fraction`` /
# ``motion_downscale``, not the ``MotionParams`` field names). Every /api/edge/config
# and /api/tuning/* body speaks THIS vocabulary so the UI round-trips a config
# straight from the Pi into a re-run without renaming; only ``_motion_params_from``
# translates ``motion_downscale`` -> ``MotionParams.downscale`` at the boundary.
_MOTION_PARAM_KEYS = (
    "var_threshold",
    "learning_rate",
    "min_area",
    "max_area_fraction",
    "persistence",
    "motion_downscale",
)

# Fallback params when the edge is unreachable (or the collector holds no client).
# Mirrors edge/config/settings.py's motion-gate DEFAULTS BY HAND — the compute tier
# must NOT import from edge/ (that would invert the thin-edge/smart-core layering),
# so these are copied and kept in sync manually. Tagged source="defaults" so the UI
# shows the user they are not the Pi's live settings.
_EDGE_MOTION_DEFAULTS = {
    "var_threshold": 16.0,
    "learning_rate": 0.001,
    "min_area": 0.01,
    "max_area_fraction": 0.6,
    "persistence": 2,
    "motion_downscale": 320,
}

# The two re-run slots a tuning compare diffs; also the valid ``slot`` values a
# rerun request may name. ``MogAnalyzer`` re-validates on construction (its
# ValueError -> 400), so this is only the analysis-table analyzer names.
_BASELINE_SLOT = "mog2:baseline"
_CANDIDATE_SLOT = "mog2:candidate"


class TuningRerunRequest(BaseModel):
    """Body of ``POST /api/tuning/rerun``: which slot to (re)run and its params.

    ``params`` is a plain dict validated by ``_motion_params_from`` rather than a
    typed model, so a missing/ill-typed field surfaces as a 400 with a clear
    message (not FastAPI's 422 field-error blob) and the param vocabulary lives in
    one place (``_MOTION_PARAM_KEYS``).
    """

    slot: str
    params: dict


def _motion_params_from(params: dict) -> MotionParams:
    """Build a ``MotionParams`` from an edge-vocabulary param dict; raise on bad input.

    Translates the edge key ``motion_downscale`` to ``MotionParams.downscale`` (the
    only name that differs). A missing key or a non-numeric value raises
    ``ValueError`` so the route maps it to a 400.
    """
    missing = [k for k in _MOTION_PARAM_KEYS if k not in params]
    if missing:
        raise ValueError(f"missing motion params: {missing}")
    try:
        return MotionParams(
            var_threshold=float(params["var_threshold"]),
            learning_rate=float(params["learning_rate"]),
            min_area=float(params["min_area"]),
            max_area_fraction=float(params["max_area_fraction"]),
            persistence=int(params["persistence"]),
            downscale=int(params["motion_downscale"]),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid motion params: {exc}") from exc


def _edge_config_params(client) -> "tuple[str, dict]":
    """Fetch the Pi's six motion params, or fall back to the hardcoded defaults.

    Returns ``(source, params)`` where ``source`` is ``"edge"`` when
    ``client.get_config()`` succeeded and ``"defaults"`` on ANY failure or a
    ``None`` client. A partial/older Pi config (a key absent) keeps ``source ==
    "edge"`` but fills the missing key from the defaults, so the proxy never
    crashes on a thin config. ``params`` is always the full six-key edge-vocabulary
    dict.
    """
    if client is not None:
        try:
            cfg = client.get_config()
            if not isinstance(cfg, dict):
                raise ValueError("edge config is not a JSON object")
            return "edge", {k: cfg.get(k, _EDGE_MOTION_DEFAULTS[k]) for k in _MOTION_PARAM_KEYS}
        except Exception:
            # Edge unreachable / bad body / a client with no get_config — degrade to
            # the defaults rather than 500. The user still gets a usable, labelled seed.
            pass
    return "defaults", dict(_EDGE_MOTION_DEFAULTS)


def _read_slot_params(store: Store, slot: str) -> "dict | None":
    """Best-effort read of the ``MotionParams`` a slot's re-run recorded.

    Each ``MogAnalyzer`` verdict stores ``detail = {"bbox": ..., "params":
    params._asdict()}`` (see ``compute/analysis/mog2.py``), so the slot's latest
    detail recovers the params it ran with. Returns that params sub-dict, or ``None``
    when the slot has no row, no detail, or an unparseable one.
    """
    detail = store.latest_analysis_detail(slot)
    if not detail:
        return None
    params = detail.get("params")
    return params if isinstance(params, dict) else None


def _slot_thresholds(store: Store, slot: str, fallback: dict) -> "tuple[float, float, int]":
    """The ``(min_area, max_area, persistence)`` a slot's re-run used, for area bucketing.

    Reads the slot's stored params (``_read_slot_params``); any that are missing
    fall back to ``fallback`` (the edge config), so a slot that hasn't run yet — or
    a detail row missing a key — still yields usable bucket thresholds. These only
    label the missed-frame area buckets; they don't change recall/false counts.
    """
    params = _read_slot_params(store, slot) or {}

    def pick(key: str):
        val = params.get(key)
        return val if val is not None else fallback[key]

    return float(pick("min_area")), float(pick("max_area_fraction")), int(pick("persistence"))


def _compare_deltas(baseline: dict, candidate: dict) -> "dict | None":
    """Baseline→candidate change in the two headline metrics, or ``None``.

    ``None`` when either scorecard is ``{needs_rerun: True}`` (a slot not yet run —
    nothing to diff). Otherwise negative ``missed``/``false`` mean the candidate
    improved (fewer misses / fewer false triggers).
    """
    if baseline.get("needs_rerun") or candidate.get("needs_rerun"):
        return None
    return {
        "missed": candidate["recall"]["missed"] - baseline["recall"]["missed"],
        "false": candidate["false_triggers"]["count"] - baseline["false_triggers"]["count"],
    }


def _store_from_env() -> Store:
    """Build a ``Store`` under ``CAT_COLLECT_DIR`` with the ``index.db`` + media/ split.

    The DB lives beside the media dir but NOT inside it, so ``Store.clear`` (which
    only touches media files) can never race the DB file.
    """
    root = os.environ.get(_ENV_DIR, _DEFAULT_DIR)
    try:
        max_bytes = int(os.environ.get(_ENV_MAX_BYTES, _DEFAULT_MAX_BYTES))
    except ValueError:
        max_bytes = _DEFAULT_MAX_BYTES
    return Store(
        db_path=os.path.join(root, "index.db"),
        media_root=os.path.join(root, "media"),
        max_bytes=max_bytes,
    )


def create_app(
    *, store=None, client=None, start_collector: bool = True, analysis_manager=None
) -> FastAPI:
    """Build the FastAPI app.

    ``store`` defaults to a ``Store`` built from the environment. The collector is
    always wrapped in a ``CollectorManager`` (on ``app.state.collector_manager``)
    so the UI can start/stop it at runtime; when ``start_collector`` is true it is
    started here, and ``client`` defaults to an ``EdgeClient()`` built from
    ``CAT_PI_URL``. Tests pass an explicit ``store`` and ``start_collector=False``
    so no edge connection and no thread are created — the manager then holds a
    ``None`` client and simply stays stopped.

    ``analysis_manager`` defaults to a fresh ``AnalysisManager()`` (whose resolver
    is the package registry ``get_analyzer``); a test injects one whose resolver
    returns a fake analyzer, exercising the analysis routes with no real model.
    """
    store = store if store is not None else _store_from_env()
    app = FastAPI()
    app.state.store = store

    # Build the EdgeClient only for a live collector: importing it lazily keeps the
    # module (and tests) loadable without the ingest client's transitive deps
    # (requests) when the collector is off. A test app (start_collector=False,
    # client=None) leaves the manager with a None client — it never runs, so that
    # is fine, and a test that does exercise /api/collector/start injects a client.
    if start_collector and client is None:
        from compute.ingest import EdgeClient

        client = EdgeClient()

    collector_manager = CollectorManager(client, store)
    app.state.collector_manager = collector_manager

    if start_collector:
        collector_manager.start()

        @app.on_event("shutdown")
        def _stop_collector() -> None:
            # Wind the collector down between frames on process exit; the manager's
            # thread is a daemon so this is best-effort tidiness, not a hard join.
            collector_manager.stop()

    analysis_manager = analysis_manager if analysis_manager is not None else AnalysisManager()
    app.state.analysis_manager = analysis_manager

    @app.get("/")
    def index():
        # Served by path (the frontend agent owns web/index.html); 404 until it
        # exists so a missing frontend is an obvious not-found, not a crash.
        if not _INDEX_HTML.is_file():
            raise HTTPException(status_code=404, detail="browse UI not built")
        return FileResponse(_INDEX_HTML, media_type="text/html")

    @app.get("/api/frames")
    def api_frames(
        cursor: "str | None" = Query(default=None),
        limit: int = Query(default=_DEFAULT_LIMIT),
        motion: str = Query(default="all"),
        order: str = Query(default="time"),
        analyzer: "str | None" = Query(default=None),
        disagree: "str | None" = Query(default=None),
    ):
        # cursor is an OPAQUE keyset token from a prior page's next_cursor (the
        # store parses it per order/mode; a malformed one → 400 via the ValueError
        # path below). Clamp the limit rather than reject it — a client asking
        # for more just gets the cap.
        limit = max(1, min(limit, _MAX_LIMIT))
        if disagree is not None:
            # Disagreement view: MOG2 vs. a chosen oracle. An analyzer is required
            # here (the store can't default it). The disagree MODE is validated by
            # the store alone — query_disagreements raises ValueError for a bad mode,
            # mapped to 400 below — so the set of valid modes has a single source
            # (the store) rather than being re-listed here. Same keyset/token
            # contract as the plain feed.
            if analyzer is None or analyzer not in ANALYZER_NAMES:
                raise HTTPException(
                    status_code=400,
                    detail=f"disagree requires analyzer in {ANALYZER_NAMES}, got {analyzer!r}",
                )
            try:
                rows, next_cursor = store.query_disagreements(analyzer, disagree, cursor, limit)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            return {"frames": rows, "next_cursor": next_cursor}

        # Plain browse feed (unchanged): motion filter + order, keyset-paginated.
        try:
            rows, next_cursor = store.query(cursor, limit, motion, order)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"frames": rows, "next_cursor": next_cursor}

    @app.get("/api/stats")
    def api_stats():
        # Fold the collector's live run state into the store summary so the UI can
        # render its start/stop badge from the same poll it already makes.
        return {**store.stats(), "collector_running": collector_manager.running}

    @app.get("/media/{frame_id}")
    def media(frame_id: int):
        path = store.path_for(frame_id)
        # Unknown row OR an evicted/missing file → 404. path_for resolves a stale
        # row to where its file would be, so existence is checked here.
        if path is None or not os.path.isfile(path):
            raise HTTPException(status_code=404, detail="frame not found")
        return FileResponse(path, media_type="image/jpeg")

    @app.post("/api/clear")
    def api_clear():
        deleted = store.clear()
        return JSONResponse({"ok": True, "deleted": deleted})

    @app.post("/api/collector/start")
    def api_collector_start():
        # The manager owns idempotency/thread-replacement; the route just toggles
        # and reports the resulting state so the UI badge follows the truth.
        collector_manager.start()
        return {"running": collector_manager.running}

    @app.post("/api/collector/stop")
    def api_collector_stop():
        collector_manager.stop()
        return {"running": collector_manager.running}

    @app.post("/api/analysis/run")
    def api_analysis_run(req: AnalysisRunRequest):
        # Validate the name before touching anything: an unknown analyzer is a
        # client mistake (400), not a 500 out of the store/registry.
        if req.analyzer not in ANALYZER_NAMES:
            raise HTTPException(
                status_code=400,
                detail=f"unknown analyzer {req.analyzer!r}; known: {ANALYZER_NAMES}",
            )
        if analysis_manager.running:
            raise HTTPException(status_code=409, detail="analysis already running")
        try:
            # start() resolves the backend AND calls ensure_available() synchronously, so
            # a backend with missing optional deps/hardware surfaces HERE as ImportError
            # (→ 503 with the install hint) instead of vanishing into the worker thread as
            # a delayed status().error; a race that slipped past the check above →
            # RuntimeError (→ 409). reanalyze rides into the worker, where the verdict
            # clear happens only after a successful prepare() (see run_analysis), so a
            # deps-missing run can't wipe an analyzer's verdicts with no replacement.
            analysis_manager.start(store, req.analyzer, reanalyze=req.reanalyze)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except ImportError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        return analysis_manager.status()

    @app.post("/api/analysis/cancel")
    def api_analysis_cancel():
        # Idempotent: safe when no job runs (the next start replaces the event).
        analysis_manager.cancel()
        return analysis_manager.status()

    @app.get("/api/analysis/status")
    def api_analysis_status():
        # Job state + per-oracle coverage (analyzed/present). The coverage
        # DENOMINATOR (the store's frame count) is deliberately NOT recomputed here:
        # the UI already polls it via /api/stats, so duplicating the frames COUNT(*)
        # on this hot 4 s poll would only contend with ingest for information the UI
        # already has.
        return {
            **analysis_manager.status(),
            "summaries": {name: store.analysis_summary(name) for name in ANALYZER_NAMES},
        }

    # --- Edge-config proxy + offline MOG2 tuning (motion-gate-diagnostic spec) ------
    #
    # Kept SIBLING to /api/analysis/* on purpose: the /api/analysis oracles are fixed
    # ground-truth references, while these run the Pi's own (parameterized, slotted)
    # MOG2 gate offline for tuning. A separate surface keeps the oracle machinery clean
    # (see the spec's "The tuning flow & endpoints"). ``client`` here is the collector's
    # edge client (the same handle the collector streams from); a None client — a test
    # app with no edge — degrades to the hardcoded defaults.

    @app.get("/api/edge/config")
    def api_edge_config():
        # Read-only proxy of the Pi's six motion params, so the UI can seed a baseline
        # re-run from the live settings. Never writes to the Pi. Unreachable edge /
        # no client -> the defaults, tagged so the UI shows they aren't the Pi's.
        source, params = _edge_config_params(client)
        return {"source": source, "params": params}

    @app.post("/api/tuning/rerun")
    def api_tuning_rerun(req: TuningRerunRequest):
        # Validate slot + params FIRST (400) — a client mistake, before touching the
        # manager — mirroring /api/analysis/run's name-before-busy ordering. Bad params
        # (missing/ill-typed) and a bad slot both surface as ValueError -> 400.
        try:
            params = _motion_params_from(req.params)
            analyzer = MogAnalyzer(params, req.slot)
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if analysis_manager.running:
            raise HTTPException(status_code=409, detail="analysis already running")
        try:
            # reanalyze=True: a re-run always re-verdicts its whole slot with the new
            # params (the clear happens in the worker only after a successful prepare,
            # so a cv2-missing run can't wipe a prior slot). start_analyzer runs
            # ensure_available() synchronously, so a missing/broken OpenCV surfaces HERE
            # as ImportError (-> 503) instead of a delayed status().error; a race past
            # the busy check above -> RuntimeError (-> 409).
            analysis_manager.start_analyzer(store, analyzer, reanalyze=True)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except ImportError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        return analysis_manager.status()

    @app.get("/api/tuning/compare")
    def api_tuning_compare(oracle: str = Query(default="yolo")):
        # The oracle is the fixed ground truth both scorecards score against; only a
        # registered one is valid (400 otherwise — the same gate /api/frames uses).
        if oracle not in ANALYZER_NAMES:
            raise HTTPException(
                status_code=400,
                detail=f"unknown oracle {oracle!r}; known: {ANALYZER_NAMES}",
            )
        # Area-bucket thresholds are per-source: each scorecard buckets its misses by
        # the params THAT source ran with. Live -> the Pi's current config; a slot ->
        # the params stored in its analysis.detail, falling back to the edge config.
        _source, edge_params = _edge_config_params(client)

        live = store.gate_scorecard(
            "live",
            oracle,
            min_area=float(edge_params["min_area"]),
            max_area=float(edge_params["max_area_fraction"]),
            persistence=int(edge_params["persistence"]),
        )
        b_min, b_max, b_pers = _slot_thresholds(store, _BASELINE_SLOT, edge_params)
        baseline = store.gate_scorecard(
            _BASELINE_SLOT, oracle, min_area=b_min, max_area=b_max, persistence=b_pers
        )
        c_min, c_max, c_pers = _slot_thresholds(store, _CANDIDATE_SLOT, edge_params)
        candidate = store.gate_scorecard(
            _CANDIDATE_SLOT, oracle, min_area=c_min, max_area=c_max, persistence=c_pers
        )

        # Fidelity is the baseline re-run vs. stored frames.motion — only meaningful
        # once baseline has run; null until then. Deltas need both slots (null if either
        # is unrun).
        fidelity = None if baseline.get("needs_rerun") else store.gate_fidelity(_BASELINE_SLOT)
        return {
            "oracle": oracle,
            "live": live,
            "baseline": baseline,
            "candidate": candidate,
            "fidelity": fidelity,
            "deltas": _compare_deltas(baseline, candidate),
        }

    return app
