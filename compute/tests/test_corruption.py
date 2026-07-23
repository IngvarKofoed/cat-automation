"""Tests for the corruption-review backend (docs/specs/2026-07-23-corruption-review-page.md).

Three layers, mirroring test_analysis.py / test_mog2.py:

- **CorruptionAnalyzer** — the stateless offline wrapper of the shared corrupt-frame
  guard. Its ``analyze`` takes a decoded BGR frame directly (numpy only, no cv2), so
  the verdict/detail tests need no JPEG round-trip.
- **Store feed** — ``corruption_feed`` (join + 3 filters + scope + paging),
  ``corruption_staleness`` (stamped-thresholds drift count), and ``cat_coverage``.
  These write verdicts DIRECTLY (``write_analysis``), so they need no cv2 either.
- **API** — ``POST /api/corruption/run`` (a real sweep; cv2-gated) and
  ``GET /api/corruption`` (feed + readout), plus the isolation guarantees: corruption
  is NOT in ``ANALYZER_NAMES`` / the scorecard oracles / the disagreement path.
"""
from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from compute.analysis import ANALYZER_NAMES, get_analyzer
from compute.analysis.corruption import CorruptionAnalyzer
from compute.analysis.runner import AnalysisManager
from compute.collection.store import Store
from compute.ingest import StreamFrame
from shared.motion import corruption_thresholds
from shared.wire import StreamFrameMeta

try:
    import cv2
    import numpy as np

    _HAVE_CV = True
except Exception:  # pragma: no cover - only where the CV stack is absent
    _HAVE_CV = False

_requires_cv = pytest.mark.skipif(not _HAVE_CV, reason="cv2/numpy required for these tests")

# A tiny valid JPEG for feed tests that never reach a decode (the feed reads stored
# flags, never opens the file); sweep tests use real images (_jpeg) instead.
_JPEG_BODY = b"\xff\xd8\xff\xe0" + b"fake-jpeg-body" + b"\xff\xd9"

_W, _H = 160, 120
_BG_LEVEL = 60


def _frame(frame_id: int, *, motion: bool = False, body: bytes = _JPEG_BODY) -> StreamFrame:
    meta = StreamFrameMeta(frame_id=frame_id, ts=frame_id, motion=motion, bbox=None, area=0.0)
    return StreamFrame(meta, body)


def _store(tmp_path) -> Store:
    return Store(
        db_path=str(tmp_path / "index.db"),
        media_root=str(tmp_path / "media"),
        max_bytes=10_000_000,
    )


# --- Synthetic frames (mirror shared/tests/test_motion.py) --------------------


def _background():
    return np.full((_H, _W, 3), _BG_LEVEL, dtype=np.uint8)


def _cast():
    frame = np.zeros((_H, _W, 3), dtype=np.uint8)
    frame[:, :, 0] = 200  # B
    frame[:, :, 1] = 2  # G collapsed
    frame[:, :, 2] = 200  # R
    return frame


def _line(rows=(50, 52), color=(20, 20, 200)):
    frame = _background()
    frame[rows[0] : rows[1], :] = color
    return frame


def _jpeg(img) -> bytes:
    ok, buf = cv2.imencode(".jpg", img)
    assert ok
    return buf.tobytes()


# --- CorruptionAnalyzer -------------------------------------------------------


def test_analyzer_identity_is_stateless_and_named():
    a = CorruptionAnalyzer()
    assert a.name == "corruption"
    assert a.windowed is False
    # ensure_available/prepare are no-ops (numpy is a base dep, nothing to load).
    assert a.ensure_available() is None
    assert a.prepare(store=None) is None


@_requires_cv
def test_analyze_cast_frame_is_corrupt_with_reason_and_thresholds():
    result = CorruptionAnalyzer().analyze(_cast())
    assert result.verdict is True
    assert result.score is None  # the guard has no continuous confidence
    assert result.detail["reason"] == "cast"
    assert result.detail["thresholds"] == corruption_thresholds()
    json.dumps(result.detail)  # stamped detail must be JSON-serializable


@_requires_cv
def test_analyze_line_frame_reports_line_reason():
    result = CorruptionAnalyzer().analyze(_line())
    assert result.verdict is True
    assert result.detail["reason"] == "line"


@_requires_cv
def test_analyze_clean_frame_is_not_corrupt():
    result = CorruptionAnalyzer().analyze(_background())
    assert result.verdict is False
    assert result.detail["reason"] is None
    assert result.detail["thresholds"] == corruption_thresholds()


# --- Registry / scorecard / disagreement isolation ----------------------------


def test_corruption_is_not_a_registered_oracle():
    # The load-bearing "non-registered analyzer" decision: absent from ANALYZER_NAMES,
    # so it never wires into the scorecard, disagreement, or oracle-coverage loops.
    assert "corruption" not in ANALYZER_NAMES
    with pytest.raises(ValueError):
        get_analyzer("corruption")


def test_corruption_is_not_in_scorecard_oracles(tmp_path):
    from compute.collection.store import _SCORECARD_ORACLES

    assert "corruption" not in _SCORECARD_ORACLES
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        store.gate_scorecard(
            "live", "corruption", min_area=0.01, max_area=0.6, persistence=2
        )


# --- Store.corruption_feed: join + filters + scope + paging -------------------


def _seed_feed(store: Store):
    """Four frames; write corruption + cat verdicts to make each filter distinguishable.

    Returns the four ids. Layout:
      ids[0]: corrupt=1, cat=1   -> the danger frame
      ids[1]: corrupt=1, cat=0   -> corrupt, no cat
      ids[2]: corrupt=0, cat=1   -> a cat, not corrupt
      ids[3]: (un-swept)         -> corrupt/cat both None
    """
    ids = [store.add(_frame(i), recv_ts_ms=1_700_000_000_000 + i) for i in range(4)]
    thr = corruption_thresholds()
    store.write_analysis(ids[0], "corruption", True, None, {"reason": "cast", "thresholds": thr})
    store.write_analysis(ids[0], "yolo-serial", True, 0.9, None)
    store.write_analysis(ids[1], "corruption", True, None, {"reason": "line", "thresholds": thr})
    store.write_analysis(ids[1], "yolo", False, 0.1, None)
    store.write_analysis(ids[2], "corruption", False, None, {"reason": None, "thresholds": thr})
    store.write_analysis(ids[2], "yolo", True, 0.8, None)
    return ids


def test_feed_all_filter_joins_corruption_and_cat_flags(tmp_path):
    store = _store(tmp_path)
    ids = _seed_feed(store)
    rows, nxt = store.corruption_feed("all", None, 100, None, None)
    assert nxt is None
    by_id = {r["id"]: r for r in rows}
    assert {r["id"] for r in rows} == set(ids)
    assert (by_id[ids[0]]["corrupt"], by_id[ids[0]]["reason"], by_id[ids[0]]["cat"]) == (True, "cast", True)
    assert (by_id[ids[1]]["corrupt"], by_id[ids[1]]["reason"], by_id[ids[1]]["cat"]) == (True, "line", False)
    assert (by_id[ids[2]]["corrupt"], by_id[ids[2]]["reason"], by_id[ids[2]]["cat"]) == (False, None, True)
    # Un-swept frame: both flags None (distinguishes "not corrupt" from "not checked").
    assert (by_id[ids[3]]["corrupt"], by_id[ids[3]]["reason"], by_id[ids[3]]["cat"]) == (None, None, None)


def test_feed_corrupt_filter_returns_only_flagged(tmp_path):
    store = _store(tmp_path)
    ids = _seed_feed(store)
    rows, _ = store.corruption_feed("corrupt", None, 100, None, None)
    assert {r["id"] for r in rows} == {ids[0], ids[1]}


def test_feed_corrupt_and_cat_filter_is_the_danger_set(tmp_path):
    store = _store(tmp_path)
    ids = _seed_feed(store)
    rows, _ = store.corruption_feed("corrupt-and-cat", None, 100, None, None)
    assert {r["id"] for r in rows} == {ids[0]}  # only corrupt AND cat


def test_feed_bad_filter_raises(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        store.corruption_feed("bogus", None, 100, None, None)


def test_feed_scopes_by_since_until(tmp_path):
    store = _store(tmp_path)
    ids = _seed_feed(store)
    rows, _ = store.corruption_feed("all", None, 100, ids[1], ids[2])
    assert {r["id"] for r in rows} == {ids[1], ids[2]}


def test_feed_keyset_paging(tmp_path):
    store = _store(tmp_path)
    ids = _seed_feed(store)
    page1, cur = store.corruption_feed("all", None, 2, None, None)
    assert [r["id"] for r in page1] == [ids[3], ids[2]]  # newest-first
    assert cur is not None
    page2, cur2 = store.corruption_feed("all", cur, 2, None, None)
    assert [r["id"] for r in page2] == [ids[1], ids[0]]
    # A FULL page always hands back a cursor (same keyset contract as
    # query_disagreements); the emptiness is discovered on the next fetch.
    assert cur2 is not None
    page3, cur3 = store.corruption_feed("all", cur2, 2, None, None)
    assert page3 == [] and cur3 is None


# --- Store.corruption_staleness -----------------------------------------------


def test_staleness_counts_verdicts_predating_a_constant_change(tmp_path):
    store = _store(tmp_path)
    ids = [store.add(_frame(i), recv_ts_ms=1_700_000_000_000 + i) for i in range(3)]
    thr = corruption_thresholds()
    # Two current-threshold verdicts, one stamped with an OLD threshold set.
    store.write_analysis(ids[0], "corruption", True, None, {"reason": "cast", "thresholds": thr})
    store.write_analysis(ids[1], "corruption", False, None, {"reason": None, "thresholds": thr})
    stale_thr = {**thr, "cast_chroma": thr["cast_chroma"] + 999}
    store.write_analysis(ids[2], "corruption", True, None, {"reason": "cast", "thresholds": stale_thr})

    result = store.corruption_staleness()
    assert result == {"analyzed": 3, "stale": 1}

    # Scoped to the two current ones -> no stale.
    assert store.corruption_staleness(ids[0], ids[1]) == {"analyzed": 2, "stale": 0}


def test_staleness_absent_stamp_counts_stale(tmp_path):
    # A verdict with NULL detail (no stamped thresholds at all) must count stale:
    # it can't be proven current, so the page should warn it needs a re-sweep.
    store = _store(tmp_path)
    fid = store.add(_frame(1), recv_ts_ms=1_700_000_000_000)
    store.write_analysis(fid, "corruption", True, None, None)
    assert store.corruption_staleness() == {"analyzed": 1, "stale": 1}


def test_staleness_underscore_key_is_matched_literally(tmp_path):
    # The thresholds JSON has underscore keys (cast_chroma, ...). If the LIKE didn't
    # escape "_", a mutated value that keeps the same length could still match. Prove
    # a genuinely-changed stamp reads as stale.
    store = _store(tmp_path)
    fid = store.add(_frame(1), recv_ts_ms=1_700_000_000_000)
    thr = corruption_thresholds()
    store.write_analysis(fid, "corruption", True, None, {"reason": "cast", "thresholds": {**thr, "line_max_rows": 21}})
    assert store.corruption_staleness() == {"analyzed": 1, "stale": 1}


# --- Store.cat_coverage -------------------------------------------------------


def test_cat_coverage_counts_either_oracle(tmp_path):
    store = _store(tmp_path)
    ids = _seed_feed(store)  # ids[0] cat via yolo-serial, ids[1] yolo=0, ids[2] yolo=1, ids[3] none
    cov = store.cat_coverage()
    assert cov == {"total": 4, "analyzed": 3, "present": 2}


def test_cat_coverage_scopes_by_since_until(tmp_path):
    store = _store(tmp_path)
    ids = _seed_feed(store)  # ids[1] yolo=0 (analyzed, absent), ids[2] yolo=1 (present)
    cov = store.cat_coverage(ids[1], ids[2])
    assert cov == {"total": 2, "analyzed": 2, "present": 1}


# --- API ----------------------------------------------------------------------


class _FakeClient:
    def iter_stream_reconnecting(self):
        return iter(())


def _make_app(tmp_path):
    from compute.api.app import create_app

    store = Store(
        db_path=str(tmp_path / "index.db"),
        media_root=str(tmp_path / "media"),
        max_bytes=10_000_000,
    )
    # A real AnalysisManager: enqueue_analyzer runs the instance directly, never the
    # resolver, so the default get_analyzer resolver is fine (never consulted here).
    manager = AnalysisManager()
    app = create_app(store=store, client=_FakeClient(), start_collector=False, analysis_manager=manager)
    return TestClient(app), store


def _poll_idle(client, timeout=5.0):
    deadline = time.monotonic() + timeout
    st = client.get("/api/analysis/status").json()
    while st["running"] and time.monotonic() < deadline:
        time.sleep(0.01)
        st = client.get("/api/analysis/status").json()
    assert not st["running"], f"sweep did not finish: {st}"
    return st


@_requires_cv
def test_corruption_run_sweeps_and_persists_verdicts(tmp_path):
    client, store = _make_app(tmp_path)
    # One real corrupt (cast) JPEG + one clean, both decodable by the sweep.
    store.add(_frame(1, body=_jpeg(_cast())), recv_ts_ms=1_700_000_000_000)
    store.add(_frame(2, body=_jpeg(_background())), recv_ts_ms=1_700_000_000_001)

    resp = client.post("/api/corruption/run", json={})
    assert resp.status_code == 200
    assert resp.json()["analyzer"] == "corruption"
    _poll_idle(client)

    cov = store.analysis_coverage("corruption")
    assert cov["analyzed"] == 2
    assert cov["present"] == 1  # the cast frame only


@_requires_cv
def test_corruption_get_returns_feed_and_readout(tmp_path):
    client, store = _make_app(tmp_path)
    ids = _seed_feed(store)

    resp = client.get("/api/corruption", params={"filter": "all"})
    assert resp.status_code == 200
    body = resp.json()
    assert {f["id"] for f in body["frames"]} == set(ids)
    assert body["coverage"] == {"total": 4, "analyzed": 3, "present": 2}
    assert body["stale"] == {"analyzed": 3, "stale": 0}
    assert body["cat_coverage"] == {"total": 4, "analyzed": 3, "present": 2}

    danger = client.get("/api/corruption", params={"filter": "corrupt-and-cat"}).json()
    assert {f["id"] for f in danger["frames"]} == {ids[0]}


def test_corruption_get_bad_filter_is_400(tmp_path):
    client, _store = _make_app(tmp_path)
    assert client.get("/api/corruption", params={"filter": "bogus"}).status_code == 400


def test_corruption_run_forwards_scope_and_motion_only(tmp_path):
    # motion_only + scope thread through enqueue_analyzer into the job (dedup key), so a
    # tight run stays distinct from a full one. Verify the enqueue is accepted and the
    # status reports the scope; the sweep body is covered by the cv2 test above.
    client, store = _make_app(tmp_path)
    ids = [store.add(_frame(i, motion=(i == 0), body=_JPEG_BODY), recv_ts_ms=1_700_000_000_000 + i) for i in range(2)]
    resp = client.post(
        "/api/corruption/run",
        json={"since_id": ids[0], "until_id": ids[0], "motion_only": True, "reanalyze": True},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["since_id"] == ids[0]
    assert body["until_id"] == ids[0]
    _poll_idle(client)


@_requires_cv
def test_corruption_run_motion_only_skips_non_motion_frames(tmp_path):
    # A decodable motion frame and a decodable non-motion frame in one window; a
    # motion_only sweep must analyze ONLY the motion frame (the flag threads through
    # enqueue_analyzer -> run_analysis -> iter/count_unanalyzed's frames.motion=1).
    client, store = _make_app(tmp_path)
    motion_id = store.add(_frame(1, motion=True, body=_jpeg(_cast())), recv_ts_ms=1_700_000_000_000)
    store.add(_frame(2, motion=False, body=_jpeg(_cast())), recv_ts_ms=1_700_000_000_001)

    resp = client.post("/api/corruption/run", json={"motion_only": True})
    assert resp.status_code == 200
    _poll_idle(client)

    cov = store.analysis_coverage("corruption")
    assert cov["analyzed"] == 1  # only the motion frame
    rows, _ = store.corruption_feed("corrupt", None, 100, None, None)
    assert {r["id"] for r in rows} == {motion_id}


def test_corruption_run_motion_only_is_a_distinct_job(tmp_path):
    # motion_only rides the dedup key, so a tight (motion-only) and a full sweep of the
    # SAME window are two jobs, not a collision that drops the second enqueue.
    client, store = _make_app(tmp_path)
    ids = [store.add(_frame(i, motion=(i == 0), body=_JPEG_BODY), recv_ts_ms=1_700_000_000_000 + i) for i in range(2)]
    body = {"since_id": ids[0], "until_id": ids[1], "reanalyze": True}
    assert client.post("/api/corruption/run", json={**body, "motion_only": True}).status_code == 200
    assert client.post("/api/corruption/run", json={**body, "motion_only": False}).status_code == 200
    st = _poll_idle(client)
    # Both enqueues survived as separate jobs (neither dropped as a duplicate).
    assert len(st["history"]) == 2


def test_corruption_run_inverted_range_is_400(tmp_path):
    client, _store = _make_app(tmp_path)
    resp = client.post("/api/corruption/run", json={"since_id": 90, "until_id": 10})
    assert resp.status_code == 400


def test_corruption_rejected_by_analyzer_gated_routes(tmp_path):
    # corruption is a non-registered analyzer: the ANALYZER_NAMES-gated routes must
    # reject it (400) — it is not a gate oracle, only its own routes serve it.
    client, _store = _make_app(tmp_path)
    assert client.post("/api/analysis/run", json={"analyzer": "corruption"}).status_code == 400
    assert client.get(
        "/api/frames", params={"analyzer": "corruption", "disagree": "missed"}
    ).status_code == 400
    assert client.get("/api/timeline", params={"oracle": "corruption"}).status_code == 400
    assert client.get("/api/visits", params={"oracle": "corruption"}).status_code == 400
