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
  Collection is OFF at launch by default — the operator clicks Start in the UI;
  ``CAT_COLLECT_AUTOSTART=1`` opts into begin-immediately for an unattended run.
- **Offline analysis.** A stronger, slower oracle (YOLO / BSUV) is swept over the
  *stored* frames on demand to validate the edge's cheap MOG2 gate, its verdicts
  landing in the store's ``analysis`` table. ``AnalysisManager`` drains an in-memory
  FIFO of such jobs one at a time — a second request *enqueues* rather than being
  refused, so several buckets × oracles can be queued and left to run unattended
  (``/api/analysis/{run,cancel,queue/clear,queue/stop-all,status}``) — and
  ``/api/frames`` grows a disagreement view (``analyzer`` + ``disagree=missed|false``)
  that surfaces the frames where MOG2 and a chosen oracle disagree. On top of that,
  ``/api/timeline`` and ``/api/visits`` summarize a bucket's disagreements as a density
  strip and a worst-first visit inbox for review at 24-hour scale.

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
_ENV_AUTOSTART = "CAT_COLLECT_AUTOSTART"
_DEFAULT_DIR = "./data/collection"
_DEFAULT_MAX_BYTES = 5368709120  # 5 GiB — ~2 h at 10 fps, a testing window

# Browse-page limit: default 200 rows, hard-capped so one request can't ask the
# server to marshal an unbounded page. The cap is generous because the browse
# grid lazy-loads images off-screen — the per-row JSON is tiny and only visible
# thumbnails actually fetch, so a big page is cheap to serve.
_DEFAULT_LIMIT = 200
_MAX_LIMIT = 1000

# Density-timeline binning: the default bin count when the caller omits ``bins``, and a
# hard cap so a direct API caller can't ask ``/api/timeline`` to shard an 864k-frame
# window into a bin-per-frame result. ``timeline_bins`` returns only NON-empty bins, so
# the real result is bounded by the window's frame count regardless; the cap bounds the
# JSON size and keeps the strip's cell count in the "few hundred" the spec intends.
_DEFAULT_TIMELINE_BINS = 200
_MAX_TIMELINE_BINS = 2000

class AnalysisRunRequest(BaseModel):
    """Body of ``POST /api/analysis/run``: which oracle to sweep, and whether to redo.

    ``reanalyze`` clears the analyzer's prior verdicts first, so the next sweep
    re-verdicts the whole store (e.g. after swapping the model or its threshold)
    rather than the stateless default of skipping already-analyzed frames.

    ``since_id`` / ``until_id`` optionally scope the sweep to a group's inclusive id
    window (``None`` on either side = unbounded, so an absent scope sweeps the whole
    store exactly as before — see the frame-range-groups spec).
    """

    analyzer: str
    reanalyze: bool = False
    since_id: "int | None" = None
    until_id: "int | None" = None


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

# The oracles whose sweep is WINDOWED/stateful — BSUV replays temporally-contiguous
# frames to build its background — so a motion-only span's multi-minute gaps make their
# verdicts (and any scorecard built from them) unreliable across it. A run over a bucket
# overlapping such a span therefore returns a ``motion_only_overlap`` warning at enqueue
# so the UI can flag "verdicts unreliable across the motion-only span" rather than
# presenting a clean scorecard. YOLO is per-frame and unaffected, so it is NOT here; a
# MOG2 re-run is windowed too and ``/api/tuning/rerun`` flags it directly.
_WINDOWED_ORACLES = {"bsuv"}

# The warm-up prefix length the tuning compare drops from a scorecard. Mirrors
# ``MogAnalyzer._WARMUP`` (the number of recent frames a windowed re-run primes its
# background over) BY HAND — same discipline as ``_EDGE_MOTION_DEFAULTS`` mirrors the
# edge defaults — rather than importing a private from mog2. See ``api_tuning_compare``
# for how a scoped compare derives its warmup from this and the pre-window frame count.
_WARMUP_FRAMES = 500


class TuningRerunRequest(BaseModel):
    """Body of ``POST /api/tuning/rerun``: which slot to (re)run and its params.

    ``params`` is a plain dict validated by ``_motion_params_from`` rather than a
    typed model, so a missing/ill-typed field surfaces as a 400 with a clear
    message (not FastAPI's 422 field-error blob) and the param vocabulary lives in
    one place (``_MOTION_PARAM_KEYS``).

    ``since_id`` / ``until_id`` optionally scope the re-run to a group's inclusive id
    window (``None`` = unbounded on that side); absent, the re-run covers the whole
    store as before. The intended workflow scopes baseline/candidate re-runs *and* the
    compare to the same window so the scorecards score exactly it.
    """

    slot: str
    params: dict
    since_id: "int | None" = None
    until_id: "int | None" = None


class GroupCreateRequest(BaseModel):
    """Body of ``POST /api/groups``: name a contiguous frame window ``[start_id, end_id]``.

    The two endpoint ids may arrive in either click order — ``Store.create_group``
    normalizes them to ``min``/``max`` — and both must be current frame rows, else the
    range can't be anchored (a client-input error the route maps to 400).
    """

    name: str
    start_id: int
    end_id: int


class CollectorMotionOnlyRequest(BaseModel):
    """Body of ``POST /api/collector/motion-only``: the desired motion-only capture flag.

    ``motion_only`` True switches the collector to persist only frames the edge saw
    motion in — a compact false-triggers-only / cat-crop capture mode; False (the
    default) stores every frame. ``CollectorManager.set_motion_only`` records the
    mode-change boundary (only on a real flip) and persists the setting. The
    load-bearing caveat: a *miss* is a non-motion frame that held a cat, so dropping
    non-motion frames makes recall/misses unmeasurable in that store — which the UI
    labels and which every overlapping window is flagged for (see *Motion-only spans*).
    """

    motion_only: bool


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


def _validate_bounds(since_id: "int | None", until_id: "int | None") -> None:
    """Reject an inverted explicit range (both bounds set, ``since_id > until_id``).

    Such a window selects no frames, so a scoped run would silently no-op (0 total,
    0 verdicts, "completed") rather than fail — a confusing outcome for what is an
    impossible window. The UI never sends one (it normalizes to min/max), so this is
    a guard for a direct API caller. Either bound ``None`` is fine (unbounded side).
    Raises ``HTTPException`` 400.
    """
    if since_id is not None and until_id is not None and since_id > until_id:
        raise HTTPException(
            status_code=400,
            detail=f"since_id ({since_id}) must be <= until_id ({until_id})",
        )


# Truthy spellings for the autostart env flag. Collection is OFF at launch by
# default — the operator clicks Start in the browse UI — so a fresh compute run
# never begins writing to the store until asked. Set CAT_COLLECT_AUTOSTART to one
# of these to restore begin-immediately (e.g. an unattended long run).
_AUTOSTART_TRUTHY = {"1", "true", "yes", "on"}


def _autostart_from_env() -> bool:
    """Whether the collector should begin at launch, read from ``CAT_COLLECT_AUTOSTART``.

    Absent/empty/anything not in ``_AUTOSTART_TRUTHY`` → ``False`` (the default:
    start stopped, let the UI start it).
    """
    return os.environ.get(_ENV_AUTOSTART, "").strip().lower() in _AUTOSTART_TRUTHY


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
    *, store=None, client=None, start_collector: bool = True, autostart: "bool | None" = None, analysis_manager=None
) -> FastAPI:
    """Build the FastAPI app.

    ``store`` defaults to a ``Store`` built from the environment. The collector is
    always wrapped in a ``CollectorManager`` (on ``app.state.collector_manager``)
    so the UI can start/stop it at runtime. ``start_collector`` *wires* the live
    collector — builds the default ``EdgeClient()`` from ``CAT_PI_URL`` and registers
    the shutdown hook — but does NOT begin collecting on its own: a fresh launch
    starts stopped and the operator clicks Start in the browse UI. ``autostart``
    (default from ``CAT_COLLECT_AUTOSTART``, off) restores begin-immediately for an
    unattended run. Tests pass an explicit ``store`` and ``start_collector=False``
    so no edge connection and no thread are created — the manager then holds a
    ``None`` client and simply stays stopped.

    ``analysis_manager`` defaults to a fresh ``AnalysisManager()`` (whose resolver
    is the package registry ``get_analyzer``); a test injects one whose resolver
    returns a fake analyzer, exercising the analysis routes with no real model.
    """
    store = store if store is not None else _store_from_env()
    autostart = _autostart_from_env() if autostart is None else autostart
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

    # Restore the persisted motion-only capture flag into memory WITHOUT starting the
    # collector or writing the store: a bare launch must never silently write (changelog
    # 28), and the collector still starts stopped unless ``autostart``. The operator's
    # last motion-only choice thus survives a restart; the separate collector-running
    # intent is read (not restored here) only to drive the Start-page Resume prompt (see
    # /api/stats.resume_available).
    collector_manager.restore_motion_only(store.get_setting("motion_only") == "1")

    if start_collector:
        # The collector is now WIRED (live client above, shutdown hook below) but
        # begins only if autostart is set — otherwise a fresh launch stays stopped
        # and the operator starts it from the browse UI (POST /api/collector/start).
        # This keeps a bare `compute.sh` run from silently writing to the store;
        # CAT_COLLECT_AUTOSTART=1 restores begin-immediately for an unattended run.
        if autostart:
            collector_manager.start()

        @app.on_event("shutdown")
        def _stop_collector() -> None:
            # Wind the collector down between frames on process exit — whether it was
            # autostarted or started later from the UI; the manager's thread is a
            # daemon so this is best-effort tidiness, not a hard join. Idempotent when
            # already stopped.
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
        since_id: "int | None" = Query(default=None),
        until_id: "int | None" = Query(default=None),
    ):
        # cursor is an OPAQUE keyset token from a prior page's next_cursor (the
        # store parses it per order/mode; a malformed one → 400 via the ValueError
        # path below). Clamp the limit rather than reject it — a client asking
        # for more just gets the cap. since_id/until_id are the optional inclusive
        # id-range scope a selected group expands to (absent = whole store); both the
        # disagreement view and the plain feed thread them straight to the store,
        # which ANDs them with the keyset predicate so paging is unaffected.
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
                rows, next_cursor = store.query_disagreements(
                    analyzer, disagree, cursor, limit, since_id, until_id
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            return {"frames": rows, "next_cursor": next_cursor}

        # Plain browse feed: motion filter + order, keyset-paginated, optionally scoped.
        try:
            rows, next_cursor = store.query(cursor, limit, motion, order, since_id, until_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"frames": rows, "next_cursor": next_cursor}

    @app.get("/api/stats")
    def api_stats():
        # Fold the collector's live run state into the store summary so the UI can render
        # its start/stop badge from the same poll it already makes. Also expose the
        # motion-only capture flag (so the Start toggle reflects persisted state) and
        # ``resume_available`` — the persisted collector-running intent was on yet the
        # collector is currently stopped, i.e. the process restarted mid-run — which
        # drives the Start-page one-click Resume prompt.
        return {
            **store.stats(),
            "collector_running": collector_manager.running,
            "motion_only": collector_manager.current_motion_only,
            "resume_available": (
                store.get_setting("collector_running") == "1" and not collector_manager.running
            ),
        }

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
        # clear() drops mode_changes (keyed to frame ids → the same rowid-reuse hazard as
        # groups) but KEEPS the settings KV (config). If collection is LIVE across the
        # clear, the current motion-only mode would then sit in an EMPTY mode_changes log
        # — reading later as reliable full capture even mid motion-only run — so re-seed
        # one boundary row with the current mode (stamped at the store's post-wipe latest
        # id). clear() can't do this itself: it doesn't know whether collection is running.
        deleted = store.clear()
        if collector_manager.running:
            store.record_mode_change(collector_manager.current_motion_only)
        return JSONResponse({"ok": True, "deleted": deleted})

    # --- Frame-range groups (the name->bounds bookmark layer) -----------------------
    #
    # A group is a saved (name, [start_id, end_id]) window; the frontend expands the
    # selected one into since_id/until_id before calling the scoped feeds/runs, so the
    # backend stays group-agnostic (see the frame-range-groups spec). Only this CRUD is
    # new surface; scoping rides the existing endpoints as optional bounds.

    @app.get("/api/groups")
    def api_groups_list():
        # Saved groups newest-first, each with a LIVE count of the frames still in its
        # window (a group whose endpoints aged out reads a smaller/zero count — the
        # bounds stay valid, the window just holds fewer live frames).
        return {"groups": store.list_groups()}

    @app.post("/api/groups")
    def api_groups_create(req: GroupCreateRequest):
        # Resolve the endpoints' timestamps and save the window; a start/end id that
        # isn't a live frame row can't anchor the range (ValueError -> 400, the same
        # client-input mapping the rest of the app uses). Returns the created group.
        try:
            return store.create_group(req.name, req.start_id, req.end_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.delete("/api/groups/{group_id}")
    def api_groups_delete(group_id: int):
        # Removes only the bookmark row — never touches frames (a group is id bounds,
        # not a membership set). ``deleted`` is 0 for an unknown id (idempotent).
        return {"ok": True, "deleted": store.delete_group(group_id)}

    @app.get("/api/range/count")
    def api_range_count(
        since_id: "int | None" = Query(default=None),
        until_id: "int | None" = Query(default=None),
    ):
        # The live "N frames in range" readout the UI shows while picking a pending
        # range, before it is saved as a group. Both bounds optional (absent = whole
        # store), matching the scope params the feeds and re-runs take.
        return {"count": store.count_in_range(since_id, until_id)}

    # --- Windowed reads for the Buckets + Motion-Detection views --------------------
    #
    # The clock→id resolution, the density-viewer sample, and the density timeline +
    # visit inbox that back the motion-detection workflow (see the
    # motion-detection-workflow spec). All scope by the same optional since_id/until_id
    # id bounds a bucket (group) expands to; timeline/visits additionally attach the
    # window's motion_only_spans so the UI can flag stretches where a miss is
    # unmeasurable (a non-motion frame that held a cat was never stored — see the
    # only-save-motion mode) rather than reading the empty missed set as perfect recall.

    @app.get("/api/frames/resolve")
    def api_frames_resolve(
        start_ts: "int | None" = Query(default=None),
        end_ts: "int | None" = Query(default=None),
    ):
        # Clock-picker → id bounds: the ONLY time-domain input in the app resolves ONCE
        # here (nearest frame at-or-after start_ts, at-or-before end_ts) to the id window
        # every scoped read then shares. A None bound — or a bound matching no frame —
        # stays null on that side, which the UI reads as "no frames in that window".
        since_id, until_id = store.resolve_ts_range(start_ts, end_ts)
        return {"since_id": since_id, "until_id": until_id}

    @app.get("/api/frames/sample")
    def api_frames_sample(
        count: int = Query(default=_DEFAULT_LIMIT),
        per_ms: "int | None" = Query(default=None),
        since_id: "int | None" = Query(default=None),
        until_id: "int | None" = Query(default=None),
    ):
        # The density viewer's decimated preview, so a wide bucket is never dumped as tens
        # of thousands of thumbs. Two strategies:
        #  - per_ms set → one frame per that many ms of recv_ts (a TRUE per-minute/hour
        #    time rate that holds regardless of capture fps, clock-window width, or gaps);
        #  - else → ~count frames evenly spread by frame INDEX.
        # Both clamp their density server-side (interval raised / count capped) so the
        # result can't exceed _MAX_SAMPLE thumbnails.
        if per_ms is not None:
            frames = store.sample_frames_by_interval(since_id, until_id, per_ms)
        else:
            frames = store.sample_frames(since_id, until_id, count)
        return {"frames": frames}

    @app.get("/api/timeline")
    def api_timeline(
        oracle: str = Query(default="yolo"),
        bins: int = Query(default=_DEFAULT_TIMELINE_BINS),
        since_id: "int | None" = Query(default=None),
        until_id: "int | None" = Query(default=None),
    ):
        # The density overview: per-bin disagreement counts across the bucket, plus the
        # window's motion_only_spans so the strip can shade stretches where misses aren't
        # measurable. The oracle is the fixed ground truth (400 if unknown — the same gate
        # /api/frames uses); bins are clamped so a caller can't shard bin-per-frame.
        if oracle not in ANALYZER_NAMES:
            raise HTTPException(
                status_code=400,
                detail=f"unknown oracle {oracle!r}; known: {ANALYZER_NAMES}",
            )
        _validate_bounds(since_id, until_id)
        bins = max(1, min(int(bins), _MAX_TIMELINE_BINS))
        return {
            "bins": store.timeline_bins(since_id, until_id, oracle, bins),
            "motion_only_spans": store.motion_only_spans(since_id, until_id),
        }

    @app.get("/api/visits")
    def api_visits(
        oracle: str = Query(default="yolo"),
        mode: str = Query(default="missed"),
        since_id: "int | None" = Query(default=None),
        until_id: "int | None" = Query(default=None),
    ):
        # The ranked visit inbox for one error mode over the bucket, worst-first, plus the
        # window's motion_only_spans (so the missed tab is marked unreliable rather than
        # reassuringly empty across a motion-only span). The oracle is validated here
        # (400); the MODE is validated by the store alone (ValueError → 400), so the set
        # of valid modes has a single source — mirroring /api/frames' disagree handling.
        # ``conflict`` mode ignores the oracle, but it is still validated for a uniform
        # contract (the default "yolo" is valid, so conflict works without naming one).
        if oracle not in ANALYZER_NAMES:
            raise HTTPException(
                status_code=400,
                detail=f"unknown oracle {oracle!r}; known: {ANALYZER_NAMES}",
            )
        _validate_bounds(since_id, until_id)
        try:
            visits = store.visits(since_id, until_id, oracle, mode)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {
            "visits": visits,
            "motion_only_spans": store.motion_only_spans(since_id, until_id),
        }

    @app.post("/api/collector/start")
    def api_collector_start():
        # The manager owns idempotency/thread-replacement; the route just toggles and
        # reports the resulting state so the UI badge follows the truth. Persist the
        # collector-running INTENT here (not in the manager) so it is written only on an
        # operator-initiated start — never by the process-exit shutdown hook, which calls
        # the bare stop() — letting the Start-page Resume prompt distinguish "was
        # collecting when the process died" from "operator stopped it" (see the spec's
        # Persistence section).
        collector_manager.start()
        store.set_setting("collector_running", "1")
        return {"running": collector_manager.running}

    @app.post("/api/collector/stop")
    def api_collector_stop():
        # Symmetric to start: clear the persisted intent on an operator stop so a later
        # launch does NOT offer Resume. The shutdown hook deliberately does not come
        # through here — it calls collector_manager.stop() directly, leaving the intent
        # untouched so a mid-run restart still surfaces Resume.
        collector_manager.stop()
        store.set_setting("collector_running", "0")
        return {"running": collector_manager.running}

    @app.post("/api/collector/motion-only")
    def api_collector_motion_only(req: CollectorMotionOnlyRequest):
        # Flip motion-only capture at runtime. The manager records the mode-change
        # boundary (only on a real flip) and persists the setting; the route just reports
        # the resulting flag. Missed cats become unmeasurable while this is on — the UI
        # labels that caveat and shades any overlapping window (see Motion-only spans).
        collector_manager.set_motion_only(bool(req.motion_only))
        return {"motion_only": collector_manager.current_motion_only}

    @app.post("/api/analysis/run")
    def api_analysis_run(req: AnalysisRunRequest):
        # Validate the name before touching anything: an unknown analyzer is a
        # client mistake (400), not a 500 out of the store/registry.
        if req.analyzer not in ANALYZER_NAMES:
            raise HTTPException(
                status_code=400,
                detail=f"unknown analyzer {req.analyzer!r}; known: {ANALYZER_NAMES}",
            )
        _validate_bounds(req.since_id, req.until_id)
        try:
            # enqueue_named resolves the backend AND calls ensure_available()
            # synchronously (both while the HTTP handler is still on the stack), so a bad
            # name surfaces as ValueError (→ 400) and a backend with missing optional
            # deps/hardware as ImportError (→ 503 with the install hint) — instead of
            # vanishing into the worker as a delayed status().error. There is no
            # busy-refusal anymore: a second request ENQUEUES (position ≥ 1) rather than
            # 409ing — the queue drains serially. reanalyze rides into the worker, where
            # the verdict clear happens only after a successful prepare() (see
            # run_analysis), so a deps-missing run can't wipe verdicts with no
            # replacement. since_id/until_id scope the sweep to a group's window (None =
            # whole store).
            result = analysis_manager.enqueue_named(
                store,
                req.analyzer,
                reanalyze=req.reanalyze,
                since_id=req.since_id,
                until_id=req.until_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except ImportError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        # BSUV (windowed) verdicts are unreliable across a motion-only span's multi-minute
        # gaps; warn at enqueue over an overlapping bucket so the UI can flag the resulting
        # scorecard rather than present it as clean. YOLO is per-frame → never flagged.
        overlap = (
            bool(store.motion_only_spans(req.since_id, req.until_id))
            if req.analyzer in _WINDOWED_ORACLES
            else False
        )
        return {**result, "motion_only_overlap": overlap}

    @app.post("/api/analysis/cancel")
    def api_analysis_cancel():
        # Cancel the RUNNING job → the worker's finally records it "canceled" and promotes
        # the next pending job. Idempotent: a no-op when idle (cancel() only sets
        # stop_event under the lock while running — it does NOT arm a future job).
        analysis_manager.cancel()
        return analysis_manager.status()

    @app.post("/api/analysis/queue/clear")
    def api_analysis_queue_clear():
        # Drop every PENDING job; the running job finishes normally then, finding an empty
        # deque, promotes nothing and the manager goes idle. "Clear the queue."
        analysis_manager.clear_pending()
        return analysis_manager.status()

    @app.post("/api/analysis/queue/stop-all")
    def api_analysis_queue_stop_all():
        # Clear pending AND cancel the running job, atomically under the manager lock so
        # no pending job is promoted between the two — the unsurprising "stop everything".
        analysis_manager.stop_all()
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
        # manager — mirroring /api/analysis/run's name-before-enqueue ordering. Bad params
        # (missing/ill-typed) and a bad slot both surface as ValueError -> 400.
        try:
            params = _motion_params_from(req.params)
            analyzer = MogAnalyzer(params, req.slot)
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        _validate_bounds(req.since_id, req.until_id)
        try:
            # reanalyze=True: a re-run always re-verdicts its whole slot with the new
            # params (the clear happens in the worker only after a successful prepare, so
            # a cv2-missing run can't wipe a prior slot). enqueue_analyzer runs
            # ensure_available() synchronously, so a missing/broken OpenCV surfaces HERE
            # as ImportError (→ 503) instead of a delayed status().error. No busy-refusal:
            # a re-run ENQUEUES behind any running sweep (position ≥ 1) rather than
            # 409ing, so interactive tuning re-runs queue on the one GPU. A re-run with
            # DIFFERENT params over the same window is NOT a dedup (that is the tune
            # loop); an identical same-params re-run is dropped. since_id/until_id scope
            # the re-run to a group's window (None = whole store).
            result = analysis_manager.enqueue_analyzer(
                store, analyzer, reanalyze=True, since_id=req.since_id, until_id=req.until_id
            )
        except ImportError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        # A MOG2 re-run is windowed/stateful like BSUV, so it too is unreliable across a
        # motion-only span — flag an overlapping window at enqueue (mog2 is always
        # windowed, so unlike /api/analysis/run there is no per-oracle guard here).
        overlap = bool(store.motion_only_spans(req.since_id, req.until_id))
        return {**result, "motion_only_overlap": overlap}

    @app.get("/api/tuning/compare")
    def api_tuning_compare(
        oracle: str = Query(default="yolo"),
        since_id: "int | None" = Query(default=None),
        until_id: "int | None" = Query(default=None),
    ):
        # The oracle is the fixed ground truth both scorecards score against; only a
        # registered one is valid (400 otherwise — the same gate /api/frames uses).
        if oracle not in ANALYZER_NAMES:
            raise HTTPException(
                status_code=400,
                detail=f"unknown oracle {oracle!r}; known: {ANALYZER_NAMES}",
            )
        _validate_bounds(since_id, until_id)
        # Area-bucket thresholds are per-source: each scorecard buckets its misses by
        # the params THAT source ran with. Live -> the Pi's current config; a slot ->
        # the params stored in its analysis.detail, falling back to the edge config.
        _source, edge_params = _edge_config_params(client)

        # Scope: when a group's bounds are present, all four columns (live / baseline /
        # candidate / oracle) score the SAME window so the denominators match. The
        # warm-up prefix drop then depends on how well the window was primed. A scoped
        # windowed re-run warm-starts from up to _WARMUP_FRAMES frames immediately BEFORE
        # the window (recent_before(since_id, N)):
        #  - If at least that many precede it, the model enters fully warm → drop NOTHING
        #    (warmup=0); dropping would discard the very frames you selected to study.
        #  - If FEWER precede it (a window at/near the store's oldest frame — the extreme
        #    being since_id absent, i.e. the window starts at the very beginning), the
        #    model entered under-primed, so drop only the still-adapting shortfall
        #    (_WARMUP_FRAMES - available), matching what an unscoped cold-start drops.
        # Unscoped (both None): the full _WARMUP_FRAMES, exactly as today.
        scoped = since_id is not None or until_id is not None
        if not scoped or since_id is None:
            warmup = _WARMUP_FRAMES
        else:
            # Frames strictly before the window are what a scoped re-run primed from
            # (capped at _WARMUP_FRAMES by recent_before's LIMIT); drop only the shortfall.
            pre_window = store.count_in_range(until_id=since_id - 1)
            warmup = max(0, _WARMUP_FRAMES - pre_window)

        live = store.gate_scorecard(
            "live",
            oracle,
            warmup=warmup,
            min_area=float(edge_params["min_area"]),
            max_area=float(edge_params["max_area_fraction"]),
            persistence=int(edge_params["persistence"]),
            since_id=since_id,
            until_id=until_id,
        )
        b_min, b_max, b_pers = _slot_thresholds(store, _BASELINE_SLOT, edge_params)
        baseline = store.gate_scorecard(
            _BASELINE_SLOT,
            oracle,
            warmup=warmup,
            min_area=b_min,
            max_area=b_max,
            persistence=b_pers,
            since_id=since_id,
            until_id=until_id,
        )
        c_min, c_max, c_pers = _slot_thresholds(store, _CANDIDATE_SLOT, edge_params)
        candidate = store.gate_scorecard(
            _CANDIDATE_SLOT,
            oracle,
            warmup=warmup,
            min_area=c_min,
            max_area=c_max,
            persistence=c_pers,
            since_id=since_id,
            until_id=until_id,
        )

        # Fidelity is the baseline re-run vs. stored frames.motion — only meaningful
        # once baseline has run; null until then, and scoped to the same window. Deltas
        # need both slots (null if either is unrun).
        fidelity = (
            None if baseline.get("needs_rerun") else store.gate_fidelity(_BASELINE_SLOT, since_id, until_id)
        )
        return {
            "oracle": oracle,
            "live": live,
            "baseline": baseline,
            "candidate": candidate,
            "fidelity": fidelity,
            "deltas": _compare_deltas(baseline, candidate),
        }

    return app
