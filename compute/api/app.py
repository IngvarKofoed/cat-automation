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

import asyncio
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from compute.analysis import ANALYZER_NAMES
from compute.analysis.corruption import CorruptionAnalyzer
from compute.analysis.mog2 import MogAnalyzer
from compute.analysis.runner import AnalysisManager
from compute.collection.collector import CollectorManager
from compute.collection.store import _QUALITIES, Store
from compute.dataset import crops
from compute.learning.runner import TrainingManager
from shared.motion import MotionParams

_WEB_DIR = Path(__file__).resolve().parent / "web"
# Two independently-styled front doors sharing only the /api + /media backend:
# the near-blank user page at `/`, the full workbench SPA at `/admin`. Separate
# files (each its own inline <style>) are what keep their CSS isolated — see
# docs/specs/2026-07-22-admin-user-area-split.md.
_USER_HTML = _WEB_DIR / "user" / "index.html"
_ADMIN_HTML = _WEB_DIR / "admin" / "index.html"
# Home-screen icon for the pinned user app (served at the root paths iOS probes).
_APPLE_TOUCH_ICON = _WEB_DIR / "user" / "apple-touch-icon.png"

# /api/events/stream (SSE) cadence: how often the server samples the feed's change
# signal, and how many quiet ticks between keepalive comments (~21 s at 3 s/tick).
_SSE_POLL_SECONDS = 3.0
_SSE_HEARTBEAT_TICKS = 7

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

    ``motion_only`` (opt-in, default off) restricts the sweep to ``frames.motion = 1``
    — the tight/fast path the Activity "Analyze" button uses to re-detect just a visit
    window's motion frames without paying for the non-motion majority. Off, every frame
    in scope is a candidate exactly as before.
    """

    analyzer: str
    reanalyze: bool = False
    since_id: "int | None" = None
    until_id: "int | None" = None
    motion_only: bool = False


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


class CorruptionRunRequest(BaseModel):
    """Body of ``POST /api/corruption/run``: enqueue a corruption sweep over a range.

    Mirrors ``AnalysisRunRequest`` minus ``analyzer`` — corruption is a single
    NON-registered analyzer built directly (never named through ``ANALYZER_NAMES``),
    so the route hands ``CorruptionAnalyzer()`` to ``enqueue_analyzer`` exactly as
    the Activity backfill / MOG2 tuning paths hand over their instances.

    ``reanalyze`` clears the window's ``corruption`` verdicts first (the re-sweep
    after a ``_CORRUPT_*`` constant change). ``since_id`` / ``until_id`` scope the
    sweep to a range (``None`` = unbounded on that side). ``motion_only`` (opt-in)
    restricts it to ``frames.motion = 1`` — corruption is stateless, so the flag
    applies exactly as for the YOLO sweep.
    """

    reanalyze: bool = False
    since_id: "int | None" = None
    until_id: "int | None" = None
    motion_only: bool = False


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


# The annotation tool labels the trustworthy serial-YOLO detections (see the memory
# note that the batched ``yolo`` oracle over-detects), so the queue defaults to it.
_LABEL_DEFAULT_ORACLE = "yolo-serial"
# The three label decisions, which are ALSO the ``dataset_items.label_kind`` values —
# the wire and the storage enum are the same set, so a decision maps to a label_kind
# 1:1. Validated in the route (400) before any crop work; the store re-validates the
# resulting ``label_kind`` as its own safety net.
_LABEL_DECISIONS = ("identified", "unknown_cat", "not_cat")


class CatCreateRequest(BaseModel):
    """Body of ``POST /api/cats``: add a roster cat.

    ``name`` is required and must be non-empty after stripping; a duplicate name is
    a client error (``Store.create_cat`` raises ``ValueError`` → 400).
    ``is_resident`` defaults False (a named neighbour/foreign cat).
    """

    name: str
    is_resident: bool = False


class CatUpdateRequest(BaseModel):
    """Body of ``PATCH /api/cats/{id}``: a partial roster edit.

    All three fields default ``None`` and only the ones the client actually SENT are
    forwarded (via ``model_dump(exclude_unset=True)``), so PATCHing just ``active``
    can't blank the name. An empty body, an empty ``name``, an unknown id, or a
    duplicate ``name`` → ``ValueError`` → 400 (``Store.update_cat``). Retire a cat by
    setting ``active`` False rather than deleting it, so its labels keep resolving.
    """

    name: "str | None" = None
    is_resident: "bool | None" = None
    active: "bool | None" = None


class LabelFrame(BaseModel):
    """One visit frame in a ``POST /api/label`` body: which frame, its box, its quality.

    ``bbox`` is ``[x1,y1,x2,y2]`` in the stored JPEG's pixel space (what the crop is
    cut to; ``None``/absent for a ``not_cat`` frame, which stores no crop).
    ``quality`` is the per-crop signal (``gallery``/``ok``/``poor``, or ``None``);
    the store validates it, so a bad value surfaces as a 400.
    """

    frame_id: int
    bbox: "list[float] | None" = None
    quality: "str | None" = None


class LabelRequest(BaseModel):
    """Body of ``POST /api/label``: assign ONE identity to a whole visit's frames.

    ``decision`` ∈ ``identified`` | ``unknown_cat`` | ``not_cat``. ``cat_id`` is
    required for ``identified`` (and must name a current roster cat) and ignored
    otherwise. Each frame in ``frames`` becomes one durable ``dataset_items`` row;
    for ``identified``/``unknown_cat`` its crop is materialised FIRST, then the row
    written — so a crash orphans a harmless crop file, never a row without its crop.
    """

    decision: str
    cat_id: "int | None" = None
    frames: "list[LabelFrame]" = []


class DeleteRequest(BaseModel):
    """Body of ``POST /api/label/delete``: send labelled frames back to the queue.

    ``frame_ids`` are source frame ids whose ``dataset_items`` rows (and materialised
    crop files) are removed, so the frames re-enter the annotation queue (undecided).
    """

    frame_ids: "list[int]" = []


class FeasibilityRunRequest(BaseModel):
    """Body of ``POST /api/training/feasibility/run``: which crop grades to embed.

    ``qualities`` is ``null`` (or ``[]``) for "all grades", or a subset of
    ``_QUALITIES`` (``gallery``/``ok``/``poor``) to A/B whether crop quality is the
    separability bottleneck. A grade outside ``_QUALITIES`` is a client mistake (400).
    Both ``null`` and ``[]`` collapse to ``None`` (no filter) at the store/manager
    boundary, so the two spell the same "all crops" request.
    """

    qualities: "list[str] | None" = None


class GalleryBuildRequest(BaseModel):
    """Body of ``POST /api/training/gallery/build``: which crop grades to enroll.

    ``qualities`` is ``null`` (or ``[]``) for "all grades", or a subset of
    ``_QUALITIES`` (``gallery``/``ok``/``poor``) — the UI defaults the checkboxes to
    ``gallery`` only (protect-the-gallery: enroll clean, representative crops and
    keep hard ones for threshold-tuning; see ``compute/CLAUDE.md``), but a build may
    widen the selection per run. A grade outside ``_QUALITIES`` is a client mistake
    (400). Both ``null`` and ``[]`` collapse to ``None`` (no filter) at the
    store/manager boundary, mirroring ``FeasibilityRunRequest``.
    """

    qualities: "list[str] | None" = None


class IdentifyRunRequest(BaseModel):
    """Body of ``POST /api/identify/run``: the id-window to identify over.

    ``since_id``/``until_id`` are the same optional inclusive bounds every windowed
    read/run takes (``None`` on a side = unbounded); the pass runs against whatever
    model is currently ``active`` — there is no model selector in the body.
    """

    since_id: "int | None" = None
    until_id: "int | None" = None


def _parse_box(box: str) -> "list[float]":
    """Parse a ``"x1,y1,x2,y2"`` query string to four floats; raise on bad input.

    The crop endpoint's box comes from a stored detection the client round-trips, so
    a malformed value is a client mistake — raised as ``ValueError`` for the route to
    map to a 400 rather than a 500.
    """
    parts = str(box).split(",")
    if len(parts) != 4:
        raise ValueError(f"box must be 'x1,y1,x2,y2', got {box!r}")
    try:
        return [float(p) for p in parts]
    except ValueError:
        raise ValueError(f"box coordinates must be numeric, got {box!r}")


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
        # Durable annotation crops live in a sibling dir of media/ so they survive
        # frame eviction/clear (see the annotation-tool spec). Passed explicitly
        # rather than relying on Store's derived default, to document the layout.
        dataset_root=os.path.join(root, "dataset"),
    )


def create_app(
    *,
    store=None,
    client=None,
    start_collector: bool = True,
    autostart: "bool | None" = None,
    analysis_manager=None,
    training_manager=None,
    live_identify_manager=None,
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

    ``training_manager`` defaults to a fresh ``TrainingManager()`` (whose probe
    runner is the real feasibility orchestrator); a test injects one with a fake
    probe runner, exercising the ``/api/training/*`` routes with no torch. It is a
    SEPARATE instance from ``analysis_manager`` on purpose — training and oracle
    sweeps are unrelated workflows and may run concurrently, so they must not share
    a dedup namespace or contend for one queue slot.

    ``live_identify_manager`` defaults to a fresh ``LiveIdentifyManager`` (the
    always-on worker that names new Activity visits — see the live-identify-worker
    spec), built AFTER the analysis/training managers because it yields the shared
    GPU to either while a manual job runs (its ``is_busy`` reads both managers'
    ``running``). Built here — not at module scope — so a test that injects a fake
    (with no thread and no torch) never imports the worker's ML deps, exactly as the
    injected ``training_manager`` keeps the training routes torch-free. Its persisted
    on/off intent is restored ONLY on a live app (``start_collector``): a test app
    must never auto-start a GPU worker.
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

    analysis_manager = analysis_manager if analysis_manager is not None else AnalysisManager()
    app.state.analysis_manager = analysis_manager

    training_manager = training_manager if training_manager is not None else TrainingManager()
    app.state.training_manager = training_manager

    # The always-on live-identify worker. Built AFTER the analysis + training managers
    # because it must yield the shared GPU/DB-connection to either while a manual job
    # runs — ``is_busy`` reads both managers' ``running`` (a @property on each, so no
    # call parens). Imported LAZILY (like ``EdgeClient`` above and ``Embedder`` in the
    # training routes) so an injected fake manager keeps this module — and its tests —
    # loadable without the worker's ML deps.
    if live_identify_manager is None:
        from compute.learning.live_identify import LiveIdentifyManager

        live_identify_manager = LiveIdentifyManager(
            store,
            is_busy=(lambda: analysis_manager.running or training_manager.running),
        )
    app.state.live_identify_manager = live_identify_manager

    # Restore live-naming at launch — but ONLY on a live app. ``restore`` start()s a GPU
    # worker thread (the compute PC is the dedicated always-on box), which a test app
    # (start_collector=False) must never do; so, unlike the collector's in-memory
    # ``restore_motion_only``, this is gated on start_collector. It starts when EITHER the
    # operator left it on (persisted "live_identify" intent) OR a model has been promoted:
    # with an active gallery, new visits should be named automatically without a manual
    # toggle. (Without an active model the worker just idles each tick, so this is a no-op
    # then.) First-ever enable still seeds the watermark to the frame horizon, so it names
    # only NEW visits — back-identifying history stays the manual Identify pass's job.
    if start_collector:
        want_live = (
            store.get_setting("live_identify") == "1"
            or store.active_model() is not None
        )
        live_identify_manager.restore(want_live)

    @app.on_event("shutdown")
    def _shutdown() -> None:
        # Wind everything down between frames/batches on process exit, THEN close the
        # store. Order is load-bearing: the collector, the analysis worker, AND the
        # training worker all write through the store's single shared connection, so all
        # three must be stopped AND joined before close() checkpoints+closes it —
        # otherwise an in-flight write (a collector add, a sweep's write_analysis_batch /
        # iter_unanalyzed fetch, or a training run's add_feasibility_run) races a closed
        # DB. stop() only signals; the bounded join() waits for the thread to actually
        # leave its write path. Threads are daemons, so the timeouts just bound how long
        # exit blocks — a still-running sweep's verdicts are resumable via iter_unanalyzed,
        # and its write is itself log-and-skip, so the residual race is a no-op, not a
        # lost/failed job. Registered UNCONDITIONALLY (not gated on start_collector) so the
        # WAL checkpoint-on-close always runs — otherwise an analysis-only / test app would
        # never checkpoint.
        collector_manager.stop()
        collector_manager.join(timeout=5.0)
        analysis_manager.stop_all()
        analysis_manager.join(timeout=10.0)
        training_manager.stop_all()
        training_manager.join(timeout=10.0)
        # The live-identify worker is a fourth writer of the shared connection (its
        # detect/identify passes write the store), so it too is stopped AND joined
        # before close() — same load-bearing ordering as the three above.
        live_identify_manager.stop()
        live_identify_manager.join(timeout=10.0)
        store.close()

    # The SPA shells are single self-contained files (inline CSS+JS), so a stale
    # cached shell means stale CODE after a redeploy — worst on a pinned home-screen
    # app that rarely cold-loads. `no-cache` forces revalidation against the ETag /
    # Last-Modified that FileResponse already sets: an unchanged shell still 304s
    # (fast), a redeployed one is picked up on the next launch. (This is the shell;
    # the running page keeps its data fresh via the foreground-refresh + SSE below.)
    _SHELL_HEADERS = {"Cache-Control": "no-cache"}

    @app.get("/")
    def index():
        # The user-facing dashboard (the "Threshold" Activity + Cats SPA). 404 until
        # the file exists so a missing frontend is an obvious not-found, not a crash.
        if not _USER_HTML.is_file():
            raise HTTPException(status_code=404, detail="user UI not built")
        return FileResponse(_USER_HTML, media_type="text/html", headers=_SHELL_HEADERS)

    @app.get("/admin")
    def admin():
        # The full workbench SPA (Start/Activity/Buckets/Sweeps/Tuning/Annotate/
        # Training). Its own document, own CSS; hash routing → /admin#activity.
        if not _ADMIN_HTML.is_file():
            raise HTTPException(status_code=404, detail="admin UI not built")
        return FileResponse(_ADMIN_HTML, media_type="text/html", headers=_SHELL_HEADERS)

    @app.get("/apple-touch-icon.png")
    @app.get("/apple-touch-icon-precomposed.png")
    def apple_touch_icon():
        # iOS auto-probes these root paths for a pinned app's home-screen icon (and the
        # user page's <link rel="apple-touch-icon"> points here). One PNG backs both the
        # plain and -precomposed names. Cache hard — it changes only on a rebuild, and
        # iOS grabs it once at add-to-home-screen time.
        if not _APPLE_TOUCH_ICON.is_file():
            raise HTTPException(status_code=404, detail="icon not built")
        return FileResponse(
            _APPLE_TOUCH_ICON,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=604800"},
        )

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
            # The live-identify worker's snapshot (running/watermark/last_tick_ts/
            # last_error), so the Activity page's live-naming toggle renders from the
            # same poll the rest of the UI already makes.
            "live_identify": live_identify_manager.status(),
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
        # Also report each bound's actual frame recv_ts so the Buckets viewer can
        # label a whole-window "Select all" selection with real frame times.
        return {
            "since_id": since_id,
            "until_id": until_id,
            "since_ts": store.frame_recv_ts(since_id),
            "until_ts": store.frame_recv_ts(until_id),
        }

    @app.get("/api/frames/sample")
    def api_frames_sample(
        count: int = Query(default=_DEFAULT_LIMIT),
        per_ms: "int | None" = Query(default=None),
        since_id: "int | None" = Query(default=None),
        until_id: "int | None" = Query(default=None),
        detections: "str | None" = Query(default=None),
    ):
        # The density viewer's decimated preview, so a wide bucket is never dumped as tens
        # of thousands of thumbs. Two strategies:
        #  - per_ms set → one frame per that many ms of recv_ts (a TRUE per-minute/hour
        #    time rate that holds regardless of capture fps, clock-window width, or gaps);
        #  - else → ~count frames evenly spread by frame INDEX.
        # Both clamp their density server-side (interval raised / count capped) so the
        # result can't exceed _MAX_SAMPLE thumbnails.
        #
        # `detections` (the count branch only) attaches each sampled frame's stored
        # per-frame detection for that analyzer (the playback filmstrip/box overlay);
        # gated against ANALYZER_NAMES like /api/frames. Not applicable to the per_ms
        # density path, so it's ignored there.
        if per_ms is not None:
            frames = store.sample_frames_by_interval(since_id, until_id, per_ms)
        else:
            if detections is not None and detections not in ANALYZER_NAMES:
                raise HTTPException(
                    status_code=400,
                    detail=f"unknown analyzer {detections!r}; known: {ANALYZER_NAMES}",
                )
            frames = store.sample_frames(since_id, until_id, count, detections=detections)
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

    @app.get("/api/events")
    def api_events(
        since_id: "int | None" = Query(default=None),
        until_id: "int | None" = Query(default=None),
        min_frames: int = Query(default=1),
    ):
        # The user-facing, oracle-free activity feed: motion frames clustered into
        # "what happened at the door" events, newest-first (see the activity-page
        # spec). Contrast /api/visits, which is oracle-driven and tuning-focused
        # (missed/false/conflict against a YOLO/BSUV sweep) — this needs no sweep
        # and is populated the moment any frames are collected. No motion_only_spans
        # caveat: this page WANTS motion frames, so motion-only capture doesn't
        # degrade it. The date filter is resolved to since_id/until_id client-side
        # via /api/frames/resolve, the same scope every windowed read takes.
        _validate_bounds(since_id, until_id)
        # min_frames is clamped to >= 1 inside Store.events (the single source of
        # truth for the clamp), so the route passes it straight through.
        return store.events(since_id, until_id, min_frames=min_frames)

    @app.get("/api/events/stream")
    async def api_events_stream(request: Request):
        # Server-Sent Events: push a lightweight "the feed changed" nudge so a pinned
        # dashboard updates in near-real-time without every client polling. We send only
        # a signal, not payloads — the client re-fetches /api/events on it, reusing its
        # existing render path. The signal is Store.activity_signal() (motion-scoped, so
        # continuous frame capture doesn't fire it every tick). Sampling and store reads
        # run in the threadpool so the sync SQLite store never blocks the event loop;
        # heartbeat comments keep the connection alive and a dropped client detectable
        # through quiet periods.
        async def gen():
            # An initial comment flushes headers so EventSource fires `open` promptly.
            yield ": connected\n\n"
            last = None
            ticks = 0
            while True:
                if await request.is_disconnected():
                    break
                try:
                    sig = await run_in_threadpool(store.activity_signal)
                except Exception:
                    # Store closing / transient read error — end the stream; the
                    # browser's EventSource reconnects on its own.
                    break
                key = (sig["motion_id"], sig["ident_rev"], sig["model_id"])
                if last is not None and key != last:
                    # Only push on a CHANGE after connect — the client already fetched
                    # on load, so the first sample just establishes the baseline.
                    yield "event: activity\ndata: 1\n\n"
                    ticks = 0
                else:
                    ticks += 1
                    if ticks % _SSE_HEARTBEAT_TICKS == 0:
                        yield ": ping\n\n"
                last = key
                await asyncio.sleep(_SSE_POLL_SECONDS)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",  # tell any reverse proxy not to buffer the stream
            },
        )

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

    @app.post("/api/live-identify/start")
    def api_live_identify_start():
        # Turn on live naming of new Activity visits. The manager owns
        # idempotency/thread-replacement and persists the on/off intent itself (unlike
        # the collector, whose intent the route persists), so the route just toggles and
        # reports the resulting state for the Activity page's badge to follow.
        live_identify_manager.start()
        return {"running": live_identify_manager.running}

    @app.post("/api/live-identify/stop")
    def api_live_identify_stop():
        # Symmetric to start: stop the worker and report the resulting (stopped) state.
        live_identify_manager.stop()
        return {"running": live_identify_manager.running}

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
                motion_only=req.motion_only,
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

    @app.get("/api/analysis/coverage")
    def api_analysis_coverage(
        since_id: "int | None" = Query(default=None),
        until_id: "int | None" = Query(default=None),
    ):
        # Per-oracle verdict coverage scoped to the SELECTED bucket's window, so the Motion
        # view shows what a scoped sweep will actually cover ("0/356 analyzed in this
        # bucket") instead of the whole-store counts /api/analysis/status carries. ``total``
        # is the window's frame count (shared across oracles — an enqueue sweeps the same
        # frames whichever oracle); analyzed/present are per oracle. Both bounds absent =
        # whole store.
        _validate_bounds(since_id, until_id)
        cov = {name: store.analysis_coverage(name, since_id, until_id) for name in ANALYZER_NAMES}
        total = next(iter(cov.values()))["total"] if cov else store.count_in_range(since_id, until_id)
        return {
            "total": total,
            "oracles": {name: {"analyzed": c["analyzed"], "present": c["present"]} for name, c in cov.items()},
        }

    # --- Corruption review (the corrupt-frame guard's calibration page) -------------
    #
    # A SIBLING surface to /api/analysis, NOT gated by ANALYZER_NAMES: corruption is a
    # non-registered analyzer (it isn't gate ground-truth about cats/motion), so it must
    # never be selectable in the scorecard/disagreement/oracle-coverage paths. Its own
    # routes below own the sweep enqueue and the range feed. See the corruption-review
    # spec and compute/analysis/corruption.py.

    @app.post("/api/corruption/run")
    def api_corruption_run(req: CorruptionRunRequest):
        # Enqueue a corruption sweep over the resolved range, mirroring the Activity
        # "Analyze" backfill (reanalyze + motion_only + scope) but on a directly-built
        # CorruptionAnalyzer() instead of a registry name — so no ANALYZER_NAMES gate.
        # enqueue_analyzer runs ensure_available() synchronously (a no-op here: numpy is
        # a base dep), and reanalyze's verdict clear happens in the worker only after a
        # successful prepare (see run_analysis). A second run ENQUEUES behind any active
        # sweep on the shared queue (no busy-refusal); progress shows on the Sweeps page.
        _validate_bounds(req.since_id, req.until_id)
        try:
            result = analysis_manager.enqueue_analyzer(
                store,
                CorruptionAnalyzer(),
                reanalyze=req.reanalyze,
                since_id=req.since_id,
                until_id=req.until_id,
                motion_only=req.motion_only,
            )
        except ImportError as exc:  # defensive: the guard needs no optional deps
            raise HTTPException(status_code=503, detail=str(exc))
        return result

    @app.get("/api/corruption")
    def api_corruption(
        filter: str = Query(default="all"),
        cursor: "str | None" = Query(default=None),
        limit: int = Query(default=_DEFAULT_LIMIT),
        since_id: "int | None" = Query(default=None),
        until_id: "int | None" = Query(default=None),
    ):
        # The range feed + header readout. The feed is keyset-paged (opaque cursor, like
        # /api/frames) and joins each frame to its corruption verdict + cat-oracle verdict,
        # filtered by all / corrupt / corrupt-and-cat (a bad filter → 400 from the store).
        # The readout carries: corruption coverage (total/analyzed/present in the window —
        # the count + rate), the staleness count (verdicts whose stamped thresholds predate
        # a constant change → "re-sweep"), and cat coverage so the UI can flag an un-swept
        # range rather than read an empty danger set as "safe".
        limit = max(1, min(limit, _MAX_LIMIT))
        _validate_bounds(since_id, until_id)
        try:
            rows, next_cursor = store.corruption_feed(filter, cursor, limit, since_id, until_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {
            "frames": rows,
            "next_cursor": next_cursor,
            "coverage": store.analysis_coverage("corruption", since_id, until_id),
            "stale": store.corruption_staleness(since_id, until_id),
            "cat_coverage": store.cat_coverage(since_id, until_id),
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
        oracle_floor: float = Query(default=0.30),
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
        # oracle_floor re-slices "present" to verdicts at or above this confidence,
        # dropping the low-conf phantom detections that inflate visit/miss counts.
        # Both oracle scores (YOLO confidence, BSUV foreground fraction) live in
        # [0, 1]; default 0.30 excludes the phantoms out of the box, 0 disables it.
        if not (0.0 <= oracle_floor <= 1.0):
            raise HTTPException(
                status_code=400,
                detail=f"oracle_floor must be in [0, 1], got {oracle_floor}",
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
            oracle_floor=oracle_floor,
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
            oracle_floor=oracle_floor,
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
            oracle_floor=oracle_floor,
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

    # --- Cat-identity annotation tool (roster CRUD + label queue + crops) -----------
    #
    # Label WHO each detected cat is, per visit, over the trustworthy yolo-serial
    # detections already in the store (see the cat-identity annotation-tool spec).
    # The roster (cats) is editable mid-annotation; the queue is virtual (live
    # oracle-present visits minus already-decided ones); a label commit materialises
    # a durable crop per frame THEN records its dataset_items row. Both cats and
    # dataset_items survive eviction/clear — they are the precious hand-made output.

    @app.get("/api/cats")
    def api_cats_list():
        # The whole roster, id-ASC (creation order) so the UI's 1-9 digit binding is
        # stable across a mid-session add; includes retired (active=0) cats.
        return {"cats": store.list_cats()}

    @app.post("/api/cats")
    def api_cats_create(req: CatCreateRequest):
        # A duplicate name is a client-input error (UNIQUE) → 400, the same mapping
        # the rest of the app uses. Returns the created cat.
        try:
            return store.create_cat(req.name, req.is_resident)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.patch("/api/cats/{cat_id}")
    def api_cats_update(cat_id: int, req: CatUpdateRequest):
        # Only the fields the client actually sent are applied (exclude_unset), so a
        # partial PATCH can't blank an omitted column. Empty body / empty name /
        # unknown id / duplicate name all → ValueError → 400.
        try:
            return store.update_cat(cat_id, req.model_dump(exclude_unset=True))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    # --- User-dashboard "Cats" view (user-activity-cats spec) -----------------------
    # The roster + live last-seen + an uploadable per-cat avatar. Display-only here
    # (add/rename/retire stays on the admin Annotate page); the one write is setting a
    # cat's photo. Avatars are a file convention (<dataset_root>/avatars/cat_<id>.jpg),
    # not a schema column — the file's presence IS the "manual avatar set" flag.

    _AVATAR_MAX_BYTES = 10 * 1024 * 1024

    @app.get("/api/cats/overview")
    def api_cats_overview():
        # Roster + per-cat last-seen (derived from the same events() feed the Activity
        # view renders). Per cat, avatar_version = the mtime (ms) of the file GET
        # .../avatar would serve (uploaded → labelled crop → none), and has_avatar = one
        # exists. The client stamps the avatar URL with avatar_version, so an unchanged
        # avatar keeps ONE cacheable URL while a replaced one gets a fresh URL that
        # auto-busts — caching (avatars are big) without a stale photo. has_avatar is now
        # derived from real file existence, so a crop row whose file is gone reads false
        # (matching what GET .../avatar would actually 404 on). active_model() read ONCE:
        # has_model = a gallery is promoted; uncalibrated = its threshold is None.
        rows = store.cats_overview()
        for row in rows:
            ver = None
            uploaded = store.avatar_path(row["id"])
            if os.path.isfile(uploaded):
                ver = int(os.path.getmtime(uploaded) * 1000)
            elif row["has_crop"]:
                crop = store.cat_avatar_crop_path(row["id"])
                if crop and os.path.isfile(crop):
                    ver = int(os.path.getmtime(crop) * 1000)
            row["avatar_version"] = ver
            row["has_avatar"] = ver is not None
        model = store.active_model()
        return {
            "cats": rows,
            "has_model": model is not None,
            "uncalibrated": model is not None and model["threshold"] is None,
        }

    @app.get("/api/cats/{cat_id}/avatar")
    def api_cats_avatar(cat_id: int):
        # Precedence: uploaded file → representative labelled crop → 404 (the client
        # then draws an initial-letter placeholder). Each candidate is isfile-guarded
        # exactly like /media, so a labelled-crop row whose file was removed
        # (relabel/undo) falls through to the next candidate rather than 500ing.
        for candidate in (store.avatar_path(cat_id), store.cat_avatar_crop_path(cat_id)):
            if candidate and os.path.isfile(candidate):
                return FileResponse(candidate, media_type="image/jpeg")
        raise HTTPException(status_code=404, detail="no avatar for this cat")

    @app.post("/api/cats/{cat_id}/avatar")
    async def api_cats_avatar_set(cat_id: int, request: Request):
        # The image is the raw request body (no multipart dependency). Reject an
        # oversized body (413) before decoding; 404 an unknown cat; normalize_avatar_bytes
        # validates + downscales + re-encodes, returning None for an undecodable image
        # (400). The file's presence at avatar_path is the "manual avatar set" flag.
        data = await request.body()
        if len(data) > _AVATAR_MAX_BYTES:
            raise HTTPException(status_code=413, detail="avatar image too large (max 10 MB)")
        if cat_id not in {c["id"] for c in store.list_cats()}:
            raise HTTPException(status_code=404, detail=f"no such cat: {cat_id}")
        out = crops.normalize_avatar_bytes(data)
        if out is None:
            raise HTTPException(status_code=400, detail="not a decodable image")
        dest = store.avatar_path(cat_id)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as fh:
            fh.write(out)
        return {"ok": True}

    @app.delete("/api/cats/{cat_id}/avatar")
    def api_cats_avatar_delete(cat_id: int):
        # Remove the uploaded override so the cat reverts to its auto crop (or the
        # placeholder). Idempotent: a missing file is not an error.
        path = store.avatar_path(cat_id)
        deleted = False
        try:
            os.remove(path)
            deleted = True
        except OSError:
            pass
        return {"ok": True, "deleted": deleted}

    @app.get("/api/label/visits")
    def api_label_visits(
        oracle: str = Query(default=_LABEL_DEFAULT_ORACLE),
        since_id: "int | None" = Query(default=None),
        until_id: "int | None" = Query(default=None),
    ):
        # The virtual annotation queue: undecided oracle-present visits, chronological,
        # plus a progress readout — all scoped to the same since_id/until_id bucket
        # window every windowed read shares. Default oracle is yolo-serial (the
        # trustworthy detector); an unknown oracle is a client mistake (400), the same
        # gate the other oracle-taking endpoints use.
        if oracle not in ANALYZER_NAMES:
            raise HTTPException(
                status_code=400,
                detail=f"unknown oracle {oracle!r}; known: {ANALYZER_NAMES}",
            )
        _validate_bounds(since_id, until_id)
        # One combined store call: the queue and the progress readout share the
        # same present-frames scan instead of each re-running the JOIN+EXISTS read.
        return store.label_queue(oracle, since_id, until_id)

    def _validate_label(req: LabelRequest) -> "int | None":
        # Validate a label / re-label request and resolve cat_id — ALL before any crop
        # I/O, so a bad request never leaves half-written crops. Decision must be one of
        # the three; an 'identified' decision needs a cat_id naming a current roster cat
        # (the fat-fingered-digit guard); every frame's quality must be a valid enum
        # (add_dataset_items re-checks it, but only AFTER the materialise loop). Returns
        # the resolved cat_id (None for unknown_cat/not_cat).
        if req.decision not in _LABEL_DECISIONS:
            raise HTTPException(
                status_code=400,
                detail=f"decision must be one of {_LABEL_DECISIONS}, got {req.decision!r}",
            )
        cat_id = None
        if req.decision == "identified":
            if req.cat_id is None:
                raise HTTPException(status_code=400, detail="cat_id is required for an 'identified' label")
            if req.cat_id not in {c["id"] for c in store.list_cats()}:
                raise HTTPException(status_code=400, detail=f"no such cat: {req.cat_id}")
            cat_id = int(req.cat_id)
        for fr in req.frames:
            if fr.quality is not None and fr.quality not in _QUALITIES:
                raise HTTPException(
                    status_code=400,
                    detail=f"quality must be one of {_QUALITIES} or None, got {fr.quality!r}",
                )
        return cat_id

    def _commit_label(decision: str, cat_id: "int | None", frames: "list[LabelFrame]") -> "tuple[int, int]":
        # Materialise each cat crop FIRST then record its row (the store's ordering
        # contract: a crash orphans a harmless crop file, never a row without its crop),
        # and write the dataset_items batch. Assumes the request was already validated
        # by _validate_label. Returns (inserted_rows, crops_written).
        dataset_root = store.dataset_root
        subdir = f"cat_{cat_id}" if decision == "identified" else "cat_unknown_cat"
        rows: "list[dict]" = []
        crops_written = 0
        for fr in frames:
            row: dict = {
                "frame_id": fr.frame_id,
                "label_kind": decision,
                "cat_id": cat_id,
                "quality": fr.quality,
                "bbox": fr.bbox,
                "source": "detector",
            }
            if decision == "not_cat":
                # A detector false positive: a row, but no crop/box/quality to keep.
                row["quality"] = None
                row["bbox"] = None
                row["crop_path"] = None
                rows.append(row)
                continue
            recv_ts = store.frame_recv_ts(fr.frame_id)
            src_path = store.path_for(fr.frame_id)
            if recv_ts is None or src_path is None or not os.path.isfile(src_path):
                continue  # frame no longer live — skipped here AND by the store
            rel_path = os.path.join(subdir, f"{fr.frame_id}_{recv_ts}.jpg")
            dest_abs = os.path.join(dataset_root, rel_path)
            if not crops.materialize(src_path, fr.bbox, dest_abs, root=dataset_root):
                continue  # crop couldn't be cut (bad box / write error) — skip its row
            crops_written += 1
            row["crop_path"] = rel_path
            rows.append(row)
        try:
            inserted = store.add_dataset_items(rows)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return inserted, crops_written

    def _delete_crop_files(removed: "list[dict]") -> int:
        # Best-effort remove the crop files for deleted dataset_items rows; return the
        # count removed. crop_path is our own DB value, but realpath-contain it under
        # dataset_root anyway before unlinking (defence in depth), and swallow OSError
        # (an already-gone file is fine).
        root = os.path.realpath(store.dataset_root)
        removed_files = 0
        for item in removed:
            rel = item.get("crop_path")
            if not rel:
                continue
            abs_path = os.path.realpath(os.path.join(store.dataset_root, rel))
            if os.path.commonpath([root, abs_path]) != root:
                continue
            try:
                os.remove(abs_path)
                removed_files += 1
            except OSError:
                pass
        return removed_files

    @app.post("/api/label")
    def api_label(req: LabelRequest):
        # Assign ONE identity to a whole visit's frames (validate → commit).
        cat_id = _validate_label(req)
        inserted, crops_written = _commit_label(req.decision, cat_id, req.frames)
        return {"inserted": inserted, "crops": crops_written}

    @app.get("/api/label/labeled")
    def api_label_labeled(
        oracle: str = Query(default=_LABEL_DEFAULT_ORACLE),
        since_id: "int | None" = Query(default=None),
        until_id: "int | None" = Query(default=None),
    ):
        # The undo / re-label feed: already-decided visits for the window, newest-
        # labelled first. Same oracle gate + bucket scope as the queue endpoint.
        if oracle not in ANALYZER_NAMES:
            raise HTTPException(
                status_code=400,
                detail=f"unknown oracle {oracle!r}; known: {ANALYZER_NAMES}",
            )
        _validate_bounds(since_id, until_id)
        visits = store.labeled_visits(oracle, since_id, until_id)
        return {"visits": visits, "total": len(visits)}

    @app.post("/api/label/relabel")
    def api_label_relabel(req: LabelRequest):
        # Change a labelled visit's decision: delete its existing rows + crop files,
        # then commit the new label (re-materialising crops). Validated up front like
        # /api/label. Deleting first keeps the (src_frame_id, src_recv_ts) UNIQUE
        # slot free so the re-commit's INSERT isn't ignored as a duplicate.
        cat_id = _validate_label(req)
        removed = store.delete_dataset_items([fr.frame_id for fr in req.frames])
        _delete_crop_files(removed)
        inserted, crops_written = _commit_label(req.decision, cat_id, req.frames)
        return {"deleted": len(removed), "inserted": inserted, "crops": crops_written}

    @app.post("/api/label/delete")
    def api_label_delete(req: DeleteRequest):
        # Undo: drop the label rows for these frames and their crop files, so the
        # frames return to the annotation queue.
        removed = store.delete_dataset_items(req.frame_ids)
        removed_files = _delete_crop_files(removed)
        return {"deleted": len(removed), "crops_removed": removed_files}

    @app.get("/api/label/crop/{frame_id}")
    def api_label_crop(frame_id: int, box: str = Query(...)):
        # Crop the stored JPEG to box on the fly (rep crop + filmstrip previews). The
        # box is a "x1,y1,x2,y2" pixel-space string the client round-trips from the
        # detection; a malformed one is a client error (400). An unknown row or an
        # evicted/missing file → 404. A degenerate box (after clamping) → 400.
        try:
            coords = _parse_box(box)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        path = store.path_for(frame_id)
        if path is None or not os.path.isfile(path):
            raise HTTPException(status_code=404, detail="frame not found")
        try:
            data = crops.crop_bytes(path, coords)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return Response(content=data, media_type="image/jpeg")

    # --- Training page: feasibility validation (learning-loop Train stage) ----------
    #
    # The Train stage's Validate step, run as a background job on a SECOND, dedicated
    # queue (``training_manager``), sibling to ``analysis_manager`` but never sharing
    # its dedup namespace or queue slot — training and oracle sweeps are unrelated
    # workflows and may run concurrently (see the training-page spec). Error mapping
    # mirrors ``/api/analysis/*``: a bad grade is a client mistake (400) and the missing
    # embed deps (torch/torchvision/OpenCV) surface SYNCHRONOUSLY as a 503 with the
    # install hint, not as a delayed ``status().error``. ``Embedder`` is imported LAZILY
    # inside the run endpoint so this module stays torch-free at import.

    @app.post("/api/training/feasibility/run")
    def api_training_feasibility_run(req: FeasibilityRunRequest):
        # Validate the grade selection FIRST (400) — a client mistake, before any deps
        # or store work. ``null`` and ``[]`` both mean "all grades" → ``None`` at the
        # store/manager boundary (no filter), so the two spell the same request.
        if req.qualities is not None:
            bad = [q for q in req.qualities if q not in _QUALITIES]
            if bad:
                raise HTTPException(
                    status_code=400,
                    detail=f"quality must be a subset of {_QUALITIES}, got {bad!r}",
                )
        qualities = list(req.qualities) if req.qualities else None

        # Cheap cold-start pre-check on the labelled-crop counts FIRST — before the
        # (seconds-long, torch-importing) ensure_available. Fewer than 2 crops or 2 distinct
        # cats is the empty-state (nothing separable to measure yet), so return the friendly
        # "label at least two cats" next-step WITHOUT enqueuing (HTTP 200, enough=False).
        # Ordering this ABOVE the dep check matters: on a box without the analysis extras a
        # first-run operator with no labels should be told to LABEL DATA (the real next step),
        # not to install torch — the dependency only blocks once there is data to embed.
        n_crops, n_cats = store.count_identified_crops(tuple(qualities) if qualities else None)
        if n_crops < 2 or n_cats < 2:
            return {
                "enough": False,
                "n_crops": n_crops,
                "n_cats": n_cats,
                "message": (
                    f"Not enough labelled data yet: {n_crops} crop(s) across {n_cats} "
                    "cat(s). Label at least two cats before validating."
                ),
            }

        # There IS data to embed — now check the heavy embed deps SYNCHRONOUSLY (mirroring
        # /api/analysis/run's ensure_available), so a missing-torch environment fails at
        # request time with the install hint (503) rather than as a delayed status().error.
        # Import Embedder here (not at module scope) so app import stays torch-free.
        from compute.identification.embed import Embedder

        try:
            Embedder().ensure_available()
        except ImportError as exc:
            raise HTTPException(status_code=503, detail=str(exc))

        # Enough to run: enqueue on the training queue. The manager dedups only a
        # double-click of the currently-RUNNING job, never a pending re-run — a
        # feasibility run reads the growing labelled set, so a deliberate re-run after
        # more labelling is not a duplicate and must enqueue.
        return {**training_manager.enqueue_feasibility(store, qualities), "enough": True}

    @app.post("/api/training/cancel")
    def api_training_cancel():
        # Cancel the RUNNING feasibility job → the embed loop aborts at the next batch
        # boundary (the probe honours the manager's stop signal), the worker's finally
        # records it "canceled" with NO feasibility_runs row, and promotes the next
        # pending job. Idempotent no-op when idle.
        training_manager.cancel()
        return training_manager.status()

    @app.post("/api/training/queue/clear")
    def api_training_queue_clear():
        # Drop every PENDING training job; the running one finishes normally then, finding
        # an empty deque, promotes nothing and the manager goes idle.
        training_manager.clear_pending()
        return training_manager.status()

    @app.post("/api/training/queue/stop-all")
    def api_training_queue_stop_all():
        # Clear pending AND cancel the running job, atomically under the manager lock so no
        # pending job is promoted between the two — "stop everything".
        training_manager.stop_all()
        return training_manager.status()

    @app.get("/api/training/status")
    def api_training_status():
        # The poll the Training page renders: the running job's kind/params + done/total
        # for the progress+ETA line, the pending queue, the finished-job history, and the
        # last successful run's summary (incl run_id) so a poll arriving after completion
        # can point the iframe at its report without a second fetch.
        return training_manager.status()

    @app.get("/api/training/feasibility/runs")
    def api_training_feasibility_runs():
        # The durable validation-run history, most-recent-first, each with its metrics,
        # run_id, and a report_available flag (false once its report dir was pruned) — the
        # source for the panel's recent-runs list. Capped at 100 rows.
        return {"runs": store.feasibility_runs(limit=100)}

    @app.get("/api/training/feasibility/report/{run_id}")
    def api_training_feasibility_report(run_id: int):
        # Serve a run's self-contained feasibility.html into the page's iframe. 404 once
        # that run's report dir has been pruned (its metrics row still lists, flagged
        # report_available=false) — the page shows a "re-run to regenerate" placeholder
        # rather than loading a 404 into the iframe.
        path = store.feasibility_run_report_path(run_id)
        if path is None:
            return Response(status_code=404)
        return FileResponse(path, media_type="text/html")

    # --- Training page: gallery build + promote; Activity/Training: identify -------
    #
    # The learning loop's remaining Train -> Run surface (see the
    # identification-gallery-activity spec): build a versioned gallery from labelled
    # crops, promote one version active, and run identify passes against it. Error
    # mapping mirrors the feasibility block above and ``/api/analysis/run``: a bad
    # grade/window is a client mistake (400/409) and the missing embed deps surface
    # SYNCHRONOUSLY as a 503 with the install hint, not a delayed ``status().error``.
    # ``Embedder`` is imported LAZILY inside each endpoint so this module stays
    # torch-free at import. Promotion itself is a synchronous status flip on the
    # store — not a queued job (see the spec's "promote is synchronous" decision).

    @app.post("/api/training/gallery/build")
    def api_training_gallery_build(req: GalleryBuildRequest):
        # Validate the grade selection FIRST (400) — a client mistake, before any
        # deps or store work. ``null``/``[]`` both mean "all grades" -> ``None`` at
        # the store/manager boundary, exactly like ``/api/training/feasibility/run``.
        if req.qualities is not None:
            bad = [q for q in req.qualities if q not in _QUALITIES]
            if bad:
                raise HTTPException(
                    status_code=400,
                    detail=f"quality must be a subset of {_QUALITIES}, got {bad!r}",
                )
        qualities = list(req.qualities) if req.qualities else None

        # Cheap cold-start pre-check on the labelled-crop counts FIRST — before the
        # (seconds-long, torch-importing) ensure_available. Same ordering rationale
        # as feasibility: an operator with no labels yet should be told to LABEL
        # DATA, not to install torch.
        n_crops, n_cats = store.count_identified_crops(tuple(qualities) if qualities else None)
        if n_crops < 2 or n_cats < 2:
            return {
                "enough": False,
                "n_crops": n_crops,
                "n_cats": n_cats,
                "message": (
                    f"Not enough labelled data: {n_crops} crop(s) across {n_cats} "
                    "cat(s). Grade representative crops as gallery, or widen the "
                    "selection."
                ),
            }

        # There IS data to embed — check the heavy embed deps SYNCHRONOUSLY (mirroring
        # /api/training/feasibility/run), so a missing-torch environment fails at
        # request time with the install hint (503) rather than as a delayed
        # status().error. Import Embedder here (not at module scope) so app import
        # stays torch-free.
        from compute.identification.embed import Embedder

        try:
            Embedder().ensure_available()
        except ImportError as exc:
            raise HTTPException(status_code=503, detail=str(exc))

        # Enough to run: enqueue on the training queue (same dedup-running-only
        # semantics as the other training enqueues — see TrainingManager).
        return {**training_manager.enqueue_gallery_build(store, qualities), "enough": True}

    @app.get("/api/training/models")
    def api_training_models():
        # The Promote panel's list (newest-first, each flagging gallery_available)
        # plus the Activity/Training "active model" readout (None if nothing has
        # been promoted yet).
        return {"models": store.list_model_versions(), "active": store.active_model()}

    @app.post("/api/training/models/{model_id}/promote")
    def api_training_models_promote(model_id: int):
        # Synchronous status flip, not a queued job — promote is a trivial single-row
        # transaction (see the spec). Accepts any existing version, including a
        # retired one (that's the rollback path). An unknown id is a 404 (nothing to
        # promote); a known id whose gallery.npz has gone missing from disk is a 409
        # (a real conflict — the artifact this row names no longer exists).
        try:
            return store.promote_model(model_id)
        except ValueError as exc:
            msg = str(exc)
            status_code = 404 if "no such model" in msg else 409
            raise HTTPException(status_code=status_code, detail=msg)

    @app.post("/api/identify/run")
    def api_identify_run(req: IdentifyRunRequest):
        # No active model -> 409: nothing to identify against. This is the same
        # "build & promote a gallery first" guard the Activity/Training UI shows as
        # a disabled control with a note, checked again here for a direct API caller.
        if store.active_model() is None:
            raise HTTPException(
                status_code=409, detail="no active model; build & promote a gallery first"
            )
        _validate_bounds(req.since_id, req.until_id)

        # Same synchronous embed-deps check as gallery/build — 503 with the install
        # hint rather than a delayed status().error.
        from compute.identification.embed import Embedder

        try:
            Embedder().ensure_available()
        except ImportError as exc:
            raise HTTPException(status_code=503, detail=str(exc))

        return {
            **training_manager.enqueue_identify(store, req.since_id, req.until_id),
            "enough": True,
        }

    return app
