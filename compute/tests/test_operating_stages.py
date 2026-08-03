"""Tests for the operating-stage model — ``stage`` on ``compute/collection/store.py``,
the stage-aware eviction policy, and ``POST /api/stage``.

See docs/specs/2026-08-02-operating-stages.md. What the spec calls out as worth its
own test, all of it in the eviction/accounting path the strategy flagged:

1. STAGE-AWARE VICTIM ORDER — outside ``tuning`` non-motion frames are reclaimed
   first (keeping annotatable motion history longer for the same disk), while
   ``tuning`` stays byte-for-byte the historical plain oldest-first.
2. The FRONTIER MARKER — preferential eviction strips a window PARTIALLY, so it
   must advance ``nonmotion_evicted_through`` and ``motion_only_spans`` must fold
   it in; otherwise a scorecard reads near-perfect gate recall over frames that
   were deleted (the entries 97/126/167 trap).
3. ``clear()`` RESETS the marker — it is a frame id living in the settings KV,
   which ``clear`` deliberately preserves, so a stale value would banner a fresh
   store as unmeasurable (the entries 141/143/144 hazard).
4. COUNTER LOCKSTEP — every preferential delete goes through
   ``_delete_frame_locked``, so ``_count`` / ``_motion_count`` / ``_total_bytes``
   cannot drift from the DB.
5. MIGRATION — the stage is derived ONCE from the pre-stage ``motion_only`` flag,
   and ``running`` is never inferred.

Pure sqlite: no torch, no cv2, no model.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from compute.collection.store import (
    STAGE_COLLECTING,
    STAGE_RUNNING,
    STAGE_TUNING,
    Store,
    stage_keeps_all_frames,
)

_JPEG_BODY = b"\xff\xd8\xff\xe0" + b"x" * 400 + b"\xff\xd9"


def _frame(frame_id: int = 1, ts: int = 1_000, motion: bool = True):
    from compute.ingest import StreamFrame
    from shared.wire import StreamFrameMeta

    meta = StreamFrameMeta(frame_id=frame_id, ts=ts, motion=motion, bbox=None, area=0.5)
    return StreamFrame(meta, _JPEG_BODY)


def _store(tmp_path, max_bytes=10_000_000) -> Store:
    return Store(
        db_path=str(tmp_path / "index.db"),
        media_root=str(tmp_path / "media"),
        max_bytes=max_bytes,
    )


def _add(store: Store, recv_ts: int, *, motion: bool) -> int:
    return store.add(_frame(frame_id=recv_ts, ts=recv_ts, motion=motion), recv_ts_ms=recv_ts)


def _live_rows(store: Store):
    """``[(id, motion)]`` straight from the DB, so a test never trusts the counters."""
    return store._conn.execute("SELECT id, motion FROM frames ORDER BY id ASC").fetchall()


def _assert_counters_match_db(store: Store) -> None:
    row = store._conn.execute(
        "SELECT COALESCE(SUM(bytes), 0), COUNT(*), COALESCE(SUM(motion), 0) FROM frames"
    ).fetchone()
    assert store._total_bytes == int(row[0])
    assert store._count == int(row[1])
    assert store._motion_count == int(row[2])


# --- the stage setting itself -------------------------------------------------


def test_stage_defaults_to_tuning_and_round_trips(tmp_path):
    store = _store(tmp_path)
    # A store that has never been told is `tuning`: the conservative stage, since it is
    # the only one keeping every frame and you cannot recover frames you did not store.
    assert store.get_stage() == STAGE_TUNING

    store.set_stage(STAGE_RUNNING)
    assert store.get_stage() == STAGE_RUNNING
    store.close()

    # Survives a reopen — it is the persisted intent, not process state.
    assert _store(tmp_path).get_stage() == STAGE_RUNNING


def test_unknown_stage_is_refused_not_coerced(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        store.set_stage("collect")  # near-miss of a real value
    assert store.get_stage() == STAGE_TUNING


def test_stage_keeps_all_frames_maps_only_tuning(tmp_path):
    # The ONE place the stage→capture mapping lives, so the API route and the eviction
    # policy cannot disagree about a stage.
    assert stage_keeps_all_frames(STAGE_TUNING) is True
    assert stage_keeps_all_frames(STAGE_COLLECTING) is False
    assert stage_keeps_all_frames(STAGE_RUNNING) is False


def test_stage_is_derived_once_from_the_pre_stage_capture_flag(tmp_path):
    # An install predating the stage model has only `motion_only`. Deriving from it is
    # what puts such a box straight into the stage it was already effectively in.
    store = _store(tmp_path)
    store.set_setting("motion_only", "1")
    assert store.derive_stage_if_unset() == STAGE_COLLECTING

    # Idempotent: a second call is a pure read, so a later `motion_only` flip (which the
    # stage itself now writes) can never silently re-derive and override the operator.
    store.set_setting("motion_only", "0")
    assert store.derive_stage_if_unset() == STAGE_COLLECTING


def test_keep_all_install_derives_tuning(tmp_path):
    store = _store(tmp_path)
    store.set_setting("motion_only", "0")
    assert store.derive_stage_if_unset() == STAGE_TUNING


def test_running_is_never_derived_even_with_a_promoted_model(tmp_path):
    # `running` and `collecting` are the SAME configuration, so nothing in the store
    # distinguishes them — it is a claim the household makes. Guessing it from "a gallery
    # is promoted" would be wrong exactly when it matters: a model is promoted DURING
    # active learning too, so it would flip the console into "nobody is watching" mid-work.
    store = _store(tmp_path)
    store.set_setting("motion_only", "1")
    # promote_model refuses a version whose artifact is missing, so lay a stub down.
    gallery = tmp_path / "models" / "g"
    gallery.mkdir(parents=True)
    (gallery / "gallery.npz").write_bytes(b"stub")
    mv = store.add_model_version(
        status="draft",
        kind="gallery",
        backbone="dinov2_vits14",
        imgsz=224,
        n_cats=2,
        n_vectors=4,
        threshold=0.4,
        quality="gallery",
        metrics=None,
        gallery_dir="g",
    )
    store.promote_model(mv)
    assert store.active_model() is not None
    assert store.derive_stage_if_unset() == STAGE_COLLECTING


# --- eviction: victim order per stage ----------------------------------------


def test_tuning_evicts_plain_oldest_first(tmp_path):
    # Byte-for-byte the historical behaviour. Note what this does NOT assert: that
    # non-motion frames are spared. They age out at the same rate as everything else in
    # `tuning`; they are merely never PREFERENTIALLY targeted, because there they are the
    # data the stage exists to collect (a gate miss lives in one).
    store = _store(tmp_path)
    store.set_stage(STAGE_TUNING)
    # Alternating motion/non-motion, so a preferential policy would be visible here.
    ids = [_add(store, t, motion=(t % 2 == 0)) for t in range(1, 7)]

    per = store._total_bytes // 6  # every frame carries the same body
    store._max_bytes = per * 2
    newest = _add(store, 7, motion=True)  # triggers eviction down to two frames

    surviving = [r[0] for r in _live_rows(store)]
    # Oldest-first regardless of motion → the two NEWEST survive, and the non-motion
    # frames among the evicted got no protection from the stage.
    assert surviving == [ids[-1], newest]
    # And no frontier was recorded: plain oldest-first removes WHOLE windows, so there is
    # nothing partially-stripped to misread, and leaving the marker untouched is what
    # keeps `tuning` identical to before.
    assert store.nonmotion_evicted_through() == 0
    _assert_counters_match_db(store)


def test_collecting_evicts_non_motion_first(tmp_path):
    # The stage is trusted, so non-motion frames earn nothing and go first — which keeps
    # the annotatable motion history for the same disk.
    store = _store(tmp_path)
    store.set_stage(STAGE_COLLECTING)
    motion_ids = [_add(store, t, motion=True) for t in (1, 2, 3)]
    still_ids = [_add(store, t, motion=False) for t in (4, 5, 6)]

    # Shrink the cap so exactly three frames have to go, then add one to trigger it.
    per = store._total_bytes // 6
    store._max_bytes = per * 4
    newest = _add(store, 9, motion=True)

    surviving = [r[0] for r in _live_rows(store)]
    # Every non-motion frame gone, and every motion frame kept — INCLUDING the oldest
    # frames in the store, which a plain oldest-first policy would have taken first.
    assert surviving == motion_ids + [newest]
    assert all(i not in surviving for i in still_ids)
    _assert_counters_match_db(store)


def test_preferential_eviction_falls_back_to_oldest_first(tmp_path):
    # Once the non-motion frames are exhausted and it is still over cap, it must keep
    # reclaiming rather than spin — the fallback is the historical path.
    store = _store(tmp_path)
    store.set_stage(STAGE_RUNNING)
    still_ids = [_add(store, t, motion=False) for t in range(1, 5)]
    motion_ids = [_add(store, t, motion=True) for t in range(5, 9)]

    per = store._total_bytes // 8
    store._max_bytes = per * 2  # far below what the non-motion frames alone can free
    newest = _add(store, 99, motion=True)

    surviving = [r[0] for r in _live_rows(store)]
    # All four non-motion frames went first, then it kept going oldest-first through the
    # motion frames rather than stalling above the cap.
    assert all(i not in surviving for i in still_ids)
    assert surviving == [motion_ids[-1], newest]
    # The frontier covers only what the PREFERENTIAL pass stripped, not the fallback's
    # whole-window deletes (which leave nothing behind to misread).
    assert store.nonmotion_evicted_through() == still_ids[-1]
    _assert_counters_match_db(store)


# --- eviction: the frontier marker -------------------------------------------


def test_preferential_eviction_advances_and_persists_the_frontier(tmp_path):
    store = _store(tmp_path)
    store.set_stage(STAGE_COLLECTING)
    _add(store, 1, motion=True)
    still_id = _add(store, 2, motion=False)
    _add(store, 3, motion=True)

    store._max_bytes = store._total_bytes - 1
    store.add(_frame(frame_id=4, ts=4, motion=True), recv_ts_ms=4)

    assert store.nonmotion_evicted_through() == still_id
    store.close()
    # Persisted, not just in memory — a restart must keep warning over that window.
    assert _store(tmp_path).nonmotion_evicted_through() == still_id


def test_frontier_makes_the_window_read_unmeasurable(tmp_path):
    # THE point of the marker. Without it a scorecard over a partially-stripped window
    # reads near-perfect gate recall over frames that were deleted.
    store = _store(tmp_path)
    store.set_stage(STAGE_COLLECTING)
    assert store.motion_only_spans() == []  # nothing stripped yet

    _add(store, 1, motion=False)
    _add(store, 2, motion=True)
    store._max_bytes = store._total_bytes - 1
    store.add(_frame(frame_id=3, ts=3, motion=True), recv_ts_ms=3)

    frontier = store.nonmotion_evicted_through()
    assert frontier > 0
    # Folded in as the id PREFIX it really is (eviction works from the oldest end).
    assert store.motion_only_spans() == [{"start_id": 1, "end_id": frontier}]
    # ...and it clips to a queried window like any other span.
    assert store.motion_only_spans(since_id=1, until_id=frontier) == [
        {"start_id": 1, "end_id": frontier}
    ]


def test_clear_resets_the_frontier(tmp_path):
    # `clear()` KEEPS the settings KV (it is config, so `stage` survives), but this one key
    # is frame-id state living in that table — and a clear restarts ids at 1, so a stale
    # value would mark every brand-new frame as stripped and banner a fresh store as
    # unmeasurable. Entries 141/143/144 hit this hazard from three other directions.
    store = _store(tmp_path)
    store.set_stage(STAGE_COLLECTING)
    _add(store, 1, motion=False)
    _add(store, 2, motion=True)
    store._max_bytes = store._total_bytes - 1
    store.add(_frame(frame_id=3, ts=3, motion=True), recv_ts_ms=3)
    assert store.nonmotion_evicted_through() > 0

    store._max_bytes = 10_000_000
    store.clear()

    assert store.nonmotion_evicted_through() == 0
    assert store.get_stage() == STAGE_COLLECTING  # config survives, as before
    _add(store, 10, motion=True)
    assert store.motion_only_spans() == []  # a fresh store reads measurable
    store.close()
    assert _store(tmp_path).nonmotion_evicted_through() == 0


def test_frontier_never_regresses_after_a_failed_add(tmp_path):
    # `add`'s rollback resyncs the counters from the DB; the frontier must be resynced with
    # them. Left advanced in memory it would sit ahead of the persisted value, and the
    # advance-only guard would then never re-persist it — so a restart would silently
    # REGRESS the marker and stop warning over a window whose frames really are gone.
    store = _store(tmp_path)
    store.set_stage(STAGE_COLLECTING)
    _add(store, 1, motion=False)
    _add(store, 2, motion=True)
    store._max_bytes = store._total_bytes - 1
    store.add(_frame(frame_id=3, ts=3, motion=True), recv_ts_ms=3)
    committed = store.nonmotion_evicted_through()
    assert committed > 0

    # Force the write path to blow up after the insert, so eviction's in-memory advance is
    # rolled back along with everything else.
    original = store._delete_frame_locked

    def _boom(*a, **kw):
        raise RuntimeError("disk on fire")

    store._delete_frame_locked = _boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        _add(store, 4, motion=False)
    store._delete_frame_locked = original  # type: ignore[method-assign]

    assert store.nonmotion_evicted_through() == committed
    store.close()
    assert _store(tmp_path).nonmotion_evicted_through() == committed


# --- the API route -----------------------------------------------------------


def _app(store: Store):
    from compute.api.app import create_app

    class _FakeClient:
        def iter_stream_reconnecting(self):
            return iter(())

    return create_app(store=store, client=_FakeClient(), start_collector=False)


def test_post_stage_sets_capture_mode_through_the_collector(tmp_path):
    # ORDER IS LOAD-BEARING: capture mode must go through the collector manager, which is
    # what records the `mode_changes` boundary row that `motion_only_spans` reconstructs
    # the unmeasurable windows from. A direct `set_setting` would lose that boundary and
    # make a motion-only window read as measurable.
    store = _store(tmp_path)
    with TestClient(_app(store)) as client:
        resp = client.post("/api/stage", json={"stage": STAGE_COLLECTING})
        assert resp.status_code == 200
        assert resp.json() == {"stage": STAGE_COLLECTING, "motion_only": True}
        assert store.get_setting("motion_only") == "1"

        boundaries = store._conn.execute(
            "SELECT motion_only FROM mode_changes ORDER BY rowid"
        ).fetchall()
        assert boundaries == [(1,)]

        # ...and back, which must record the reverse boundary.
        resp = client.post("/api/stage", json={"stage": STAGE_TUNING})
        assert resp.json() == {"stage": STAGE_TUNING, "motion_only": False}
        boundaries = store._conn.execute(
            "SELECT motion_only FROM mode_changes ORDER BY rowid"
        ).fetchall()
        assert boundaries == [(1,), (0,)]


def test_running_stage_keeps_motion_only_capture(tmp_path):
    # `running` and `collecting` are the same configuration — the assertion is the whole
    # difference, so capture must not quietly change between them.
    store = _store(tmp_path)
    with TestClient(_app(store)) as client:
        assert client.post("/api/stage", json={"stage": STAGE_RUNNING}).json() == {
            "stage": STAGE_RUNNING,
            "motion_only": True,
        }


def test_post_stage_rejects_an_unknown_stage(tmp_path):
    store = _store(tmp_path)
    with TestClient(_app(store)) as client:
        resp = client.post("/api/stage", json={"stage": "annotating"})
        assert resp.status_code == 400
        # Nothing applied — capture mode must not move on a rejected stage.
        assert store.get_stage() == STAGE_TUNING
        assert store.get_setting("motion_only") is None


def test_orphan_probe_uses_the_recv_ts_index(tmp_path):
    # The launch sweep made this primitive AUTOMATIC, so its plan matters. `frames.path` is
    # unindexed, so the old `WHERE path = ?` was a full table scan PER FILE — O(files x rows),
    # measured at 11 s for 20k files against 20k rows and quadratic in store size, i.e.
    # effectively unbounded on the real store. Probing the indexed `recv_ts` (recoverable from
    # the filename `add` composes) makes it a seek; the `path` equality still runs, so the
    # ANSWER is identical and only the plan changed.
    from compute.collection.store import _recv_ts_from_relpath

    store = _store(tmp_path)
    live_id = _add(store, 1_700_000_000_123, motion=True)
    rel = store._conn.execute("SELECT path FROM frames WHERE id = ?", (live_id,)).fetchone()[0]
    assert _recv_ts_from_relpath(rel) == 1_700_000_000_123

    plan = store._conn.execute(
        "EXPLAIN QUERY PLAN SELECT 1 FROM frames WHERE recv_ts = ? AND path = ?",
        (1_700_000_000_123, rel),
    ).fetchall()
    assert any("idx_frames_recv_ts" in str(r) for r in plan), plan

    # A name that does not parse must fall back, NOT be assumed orphaned — correctness must
    # not depend on the naming convention holding.
    assert _recv_ts_from_relpath("2026-08-02/12/legacy-name.jpg") is None


def test_orphan_sweep_still_deletes_only_unreferenced_files(tmp_path):
    # The indexed probe must not change WHAT is swept: a live frame's file is never touched,
    # and a file with no row always is.
    store = _store(tmp_path)
    _add(store, 1_700_000_000_500, motion=True)
    live_rel = store._conn.execute("SELECT path FROM frames").fetchone()[0]

    orphan_dir = tmp_path / "media" / "2026-08-02" / "12"
    orphan_dir.mkdir(parents=True, exist_ok=True)
    (orphan_dir / "1754130000000_f9.jpg").write_bytes(b"orphan")
    (orphan_dir / "not-our-scheme.jpg").write_bytes(b"orphan-too")

    res = store.delete_orphan_batch(
        ["2026-08-02/12/1754130000000_f9.jpg", "2026-08-02/12/not-our-scheme.jpg", live_rel]
    )
    assert res["deleted"] == 2  # both orphans, including the unparseable one
    import os as _os

    assert _os.path.isfile(_os.path.join(str(tmp_path / "media"), live_rel))  # live file kept


def test_a_misparsed_filename_deletes_nothing(monkeypatch, tmp_path):
    # The fast probe's correctness rests on the filename parse, and this call REMOVES FILES,
    # so a negative is CONFIRMED with the unindexed equality before anything is deleted.
    # Without that, a parse returning a wrong-but-plausible millisecond would find no row and
    # destroy every live frame it looked at. Asserted with a deliberately broken parser
    # because the failure is silent and total: the safe behaviour must be structural, not a
    # consequence of the parse happening to be right.
    import compute.collection.store as store_mod

    store = _store(tmp_path)
    for t in (1_700_000_000_100, 1_700_000_000_200):
        _add(store, t, motion=True)
    live = [r[0] for r in store._conn.execute("SELECT path FROM frames").fetchall()]

    monkeypatch.setattr(store_mod, "_recv_ts_from_relpath", lambda p: 1)  # always wrong
    res = store.delete_orphan_batch(live)

    assert res["deleted"] == 0
    assert store._conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0] == 2
    import os as _os

    for rel in live:
        assert _os.path.isfile(_os.path.join(str(tmp_path / "media"), rel))


def test_stats_reports_the_stage(tmp_path):
    store = _store(tmp_path)
    store.set_stage(STAGE_RUNNING)
    with TestClient(_app(store)) as client:
        body = client.get("/api/stats").json()
        assert body["stage"] == STAGE_RUNNING
        # The old switches stay in the payload as READOUTS, so the UI can still show what
        # each worker is doing (and surface a last_error) without offering a toggle.
        assert "motion_only" in body
        assert "yolo_oracle" in body
        assert "live_identify" in body
