"""Tests for the foreign-cat alarm (docs/specs/2026-11-01-foreign-cat-alarm.md).

Three layers:

- the store reads the alarm step adds — ``open_visit`` (the cluster ``closed_visits``
  leaves out, read backwards so it costs the open cluster, not the backlog),
  ``alarm_frames`` (the counted frames), the ``actions`` log — with their query plans
  pinned on a store with no ANALYZE stats, the state the real store is in;
- the worker, driven tick by tick over a simulated door: a REAL ``Store`` receives frames
  at 10 fps, fake detect/identify write each frame's scripted truth (a cat box score, the
  nearest cat, its distance) exactly where the real passes would, and a fake Pi plays
  clips with real durations, refusing overlap (409) and refusing everything while disabled
  (423);
- ``play_test_clip``, the admin card's test button.

No torch, no GPU, no network.
"""
from __future__ import annotations

import os
import threading

import pytest

from compute.collection.store import Store, _VISIT_GAP_MS
from compute.ingest import EdgeUnavailable, StreamFrame
from compute.learning import deterrent
from compute.learning.live_identify import LiveIdentifyManager
from shared.wire import StreamFrameMeta

_JPEG = b"\xff\xd8\xff\xe0" + b"fake-jpeg-body" + b"\xff\xd9"
BASE = 1_700_000_000_000
THRESHOLD = 0.4


def _store(tmp_path) -> Store:
    return Store(
        db_path=str(tmp_path / "index.db"),
        media_root=str(tmp_path / "media"),
        max_bytes=100_000_000,
    )


def _add(store: Store, ts: int, motion: bool, seq: "list[int]") -> int:
    seq[0] += 1
    meta = StreamFrameMeta(frame_id=seq[0], ts=ts, motion=motion, bbox=None, area=0.05 if motion else 0.0)
    return store.add(StreamFrame(meta, _JPEG), recv_ts_ms=ts)


def _plan(store: Store, sql: str, params) -> "list[str]":
    return [r[-1] for r in store._conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()]


# =====================================================================================
#  Store: open_visit
# =====================================================================================


def test_open_visit_is_exactly_the_cluster_closed_visits_leaves_out(tmp_path):
    store = _store(tmp_path)
    seq = [0]
    a = [_add(store, BASE + i * 100, True, seq) for i in range(5)]           # visit A
    _add(store, BASE + 1_000, False, seq)                                      # still scene
    b0 = _add(store, BASE + 5_000, True, seq)                                  # visit B starts
    b = [b0] + [_add(store, BASE + 5_000 + i * 300, True, seq) for i in range(1, 8)]
    tail = _add(store, BASE + 7_200, False, seq)                               # newest frame

    now = BASE + 7_300
    assert store.closed_visits(None, now) == [(a[0], a[-1])]
    assert store.open_visit(None, now) == (b[0], tail)
    # Once B settles, closed_visits takes it and open_visit has nothing.
    later = BASE + 5_000 + 7 * 300 + _VISIT_GAP_MS + 1
    assert store.closed_visits(None, later)[-1] == (b[0], b[-1])
    assert store.open_visit(None, later) is None
    # The watermark floors it as it floors closed_visits.
    assert store.open_visit(b[3], now) == (b[4], tail)


def test_open_visit_walks_past_a_page(tmp_path):
    # A cat sitting with continuous motion for minutes is thousands of frames: the walk
    # pages backwards and must still land on the cluster's first frame.
    store = _store(tmp_path)
    seq = [0]
    _add(store, BASE, True, seq)  # an earlier, separate visit
    first = None
    n = Store._OPEN_VISIT_PAGE * 2 + 37
    for i in range(n):
        fid = _add(store, BASE + 10_000 + i * 100, True, seq)
        first = first or fid
    now = BASE + 10_000 + (n - 1) * 100 + 50
    assert store.open_visit(None, now) == (first, fid)


def test_open_visit_plan_reads_the_motion_index_backwards(tmp_path):
    store = _store(tmp_path)
    for sql, params in (
        ("SELECT id, recv_ts FROM frames WHERE motion = 1 AND +id > ?"
         " ORDER BY recv_ts DESC, id DESC LIMIT 1", (0,)),
        ("SELECT id, recv_ts FROM frames WHERE motion = 1 AND +id > ?"
         " AND recv_ts <= ? AND (recv_ts < ? OR id < ?) ORDER BY recv_ts DESC, id DESC LIMIT ?",
         (0, 1, 1, 1, 512)),
    ):
        plan = " | ".join(_plan(store, sql, params))
        assert "idx_frames_motion_recv" in plan and "TEMP B-TREE" not in plan, plan


# =====================================================================================
#  Store: alarm_frames
# =====================================================================================


def _verdict(store, fid, score, present=True):
    detail = {"boxes": [[1, 1, 9, 9, score, 15]]} if present else None
    store.write_analysis_batch([(fid, "yolo-serial", present, score if present else None, detail)])


def _ident(store, fid, model_id, cat_id, dist):
    store.write_identifications_batch([(fid, model_id, cat_id, dist, [1, 1, 9, 9])])


def test_alarm_frames_counts_only_floored_identified_frames(tmp_path):
    store = _store(tmp_path)
    seq = [0]
    ids = [_add(store, BASE + i * 100, True, seq) for i in range(7)]
    _verdict(store, ids[0], 0.8); _ident(store, ids[0], 1, 5, 0.2)       # counted
    _verdict(store, ids[1], 0.25); _ident(store, ids[1], 1, 5, 0.2)      # below the 0.3 floor
    _verdict(store, ids[2], 0.8, present=False)                          # no cat
    _verdict(store, ids[3], 0.8); _ident(store, ids[3], 1, None, None)   # marker (un-embeddable)
    _verdict(store, ids[4], 0.8); _ident(store, ids[4], 2, 5, 0.2)       # another model
    _verdict(store, ids[5], 0.3); _ident(store, ids[5], 1, 6, 0.9)       # exactly the floor
    _ident(store, ids[6], 1, 5, 0.2)                                     # no verdict at all
    assert store.alarm_frames(1, BASE) == [
        (ids[0], BASE, 5, 0.2),
        (ids[5], BASE + 500, 6, 0.9),
    ]
    assert store.alarm_frames(1, BASE + 100) == [(ids[5], BASE + 500, 6, 0.9)]


def test_alarm_frames_until_ts_bounds_the_read_inclusively(tmp_path):
    store = _store(tmp_path)
    seq = [0]
    ids = [_add(store, BASE + i * 100, True, seq) for i in range(5)]
    for fid in ids:
        _verdict(store, fid, 0.8); _ident(store, fid, 1, 5, 0.2)
    got = store.alarm_frames(1, BASE + 100, until_ts=BASE + 300)
    assert [fid for fid, *_ in got] == ids[1:4]
    assert store.alarm_frames(1, BASE + 100) == store.alarm_frames(1, BASE + 100, until_ts=BASE + 400)


@pytest.mark.parametrize("until", [False, True])
def test_alarm_frames_plan_drives_from_frames(tmp_path, until):
    # Left to the planner (no ANALYZE stats), this join drives from the oracle's whole
    # analysis partition — the mis-plan entries 229/265/276/307/385/391 kept hitting.
    # Re-pinned with the presence vote's 2-minute bound and the replay's upper bound: the
    # read is now ~1,400 rows a tick during a long presence, so the seek must hold.
    store = _store(tmp_path)
    sql = (
        "SELECT f.id, f.recv_ts, i.cat_id, i.distance"
        " FROM frames f CROSS JOIN analysis a CROSS JOIN identifications i"
        " WHERE f.recv_ts >= ?" + (" AND f.recv_ts <= ?" if until else "") +
        " AND a.frame_id = f.id AND a.analyzer = ? AND a.verdict = 1 AND a.score >= ?"
        " AND i.frame_id = f.id AND i.model_version_id = ? AND i.cat_id IS NOT NULL"
        " ORDER BY f.recv_ts ASC, f.id ASC"
    )
    params = (0,) + ((10**13,) if until else ()) + ("yolo-serial", 0.3, 1)
    plan = _plan(store, sql, params)
    assert "idx_frames_recv_ts" in plan[0], plan
    assert all("idx_analysis_analyzer_verdict" not in p for p in plan), plan
    assert all("TEMP B-TREE" not in p for p in plan), plan


def test_alarm_frames_sql_is_the_pinned_text(tmp_path, monkeypatch):
    # The plan test above pins SQL text it writes out itself; this pins that alarm_frames
    # still issues exactly that text, so the two cannot drift apart.
    store = _store(tmp_path)
    seen = []
    real = store._conn

    class Spy:
        def execute(self, sql, params=()):
            seen.append(sql)
            return real.execute(sql, params)

    monkeypatch.setattr(store, "_conn", Spy())
    store.alarm_frames(1, 0)
    store.alarm_frames(1, 0, until_ts=5)
    assert seen[0].endswith("ORDER BY f.recv_ts ASC, f.id ASC") and "f.recv_ts <= ?" not in seen[0]
    assert " WHERE f.recv_ts >= ? AND f.recv_ts <= ? AND a.frame_id = f.id" in seen[1]


# =====================================================================================
#  Store: the actions log
# =====================================================================================


def test_actions_survive_clear_and_resolve_the_frame_on_the_pair(tmp_path):
    store = _store(tmp_path)
    seq = [0]
    fid = _add(store, BASE, True, seq)
    tom = store.create_cat("Tom", is_resident=False)["id"]
    aid = store.add_action({
        "kind": "sound", "mode": "on", "start_ts": BASE, "verdict": "neighbour", "cat_id": tom,
        "frame_id": fid, "frame_recv_ts": BASE,
    })
    row = store.list_actions()[0]
    assert row["id"] == aid and row["cat_name"] == "Tom" and row["frame_live"] is True
    assert row["end_ts"] is None and row["pi_disabled"] is False

    store.clear()
    # The id is reused by a different frame after the wipe: the pair no longer matches.
    seq2 = [0]
    reused = _add(store, BASE + 99_999, True, seq2)
    assert reused == fid
    rows = store.list_actions()
    assert [r["id"] for r in rows] == [aid]          # survived clear()
    assert rows[0]["frame_live"] is False            # reads gone, never someone else's frame


def test_close_open_actions_completes_only_open_rows(tmp_path):
    store = _store(tmp_path)
    open_id = store.add_action({"kind": "sound", "mode": "on", "start_ts": BASE, "verdict": "unknown"})
    done_id = store.add_action({
        "kind": "sound", "mode": "on", "start_ts": BASE, "verdict": "unknown",
        "end_ts": BASE + 5, "ended": "gone",
    })
    assert store.close_open_actions("interrupted", BASE + 10) == 1
    rows = {r["id"]: r for r in store.list_actions()}
    assert (rows[open_id]["ended"], rows[open_id]["end_ts"]) == ("interrupted", BASE + 10)
    assert (rows[done_id]["ended"], rows[done_id]["end_ts"]) == ("gone", BASE + 5)


def test_actions_carry_the_trigger(tmp_path):
    store = _store(tmp_path)
    store.add_action({"kind": "sound", "mode": "on", "start_ts": BASE, "verdict": "unknown",
                      "trigger": "takeover"})
    store.add_action({"kind": "sound", "mode": "on", "start_ts": BASE + 1, "verdict": "unknown"})
    rows = store.list_actions()
    assert [r["trigger"] for r in rows] == [None, "takeover"]     # newest first; older rows read NULL
    assert [r["trigger"] for r in store.alarm_actions_between(BASE, BASE + 1)] == ["takeover", None]
    assert store.alarm_actions_between(BASE + 2, BASE + 9) == []


def test_trigger_column_is_added_to_an_existing_actions_table(tmp_path):
    # A store from before the column: the probe-and-ALTER migration adds it, and the rows
    # already there read NULL — "older rows show none" in the admin log.
    import sqlite3

    db = tmp_path / "index.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE actions (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,"
        " mode TEXT NOT NULL, start_ts INTEGER NOT NULL, end_ts INTEGER, verdict TEXT NOT NULL,"
        " cat_id INTEGER, n_counted INTEGER, n_votes INTEGER, distance REAL,"
        " model_version_id INTEGER, threshold REAL, frame_id INTEGER, frame_recv_ts INTEGER,"
        " visit_start_ts INTEGER, sound TEXT, clips_played INTEGER NOT NULL DEFAULT 0,"
        " clips_failed INTEGER NOT NULL DEFAULT 0, ended TEXT,"
        " pi_disabled INTEGER NOT NULL DEFAULT 0, last_error TEXT)"
    )
    conn.execute("INSERT INTO actions (kind, mode, start_ts, verdict) VALUES ('sound', 'on', 1, 'unknown')")
    conn.commit()
    conn.close()
    store = _store(tmp_path)
    cols = {r[1] for r in store._conn.execute("PRAGMA table_info(actions)").fetchall()}
    assert "trigger" in cols
    assert store.list_actions()[0]["trigger"] is None


def test_action_columns_are_whitelisted(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        store.add_action({"kind": "sound", "mode": "on", "start_ts": 1, "verdict": "x", "bogus": 1})


# =====================================================================================
#  The worker, over a simulated door
# =====================================================================================


class FakePi:
    """The edge speaker: one clip at a time on the simulated clock, 423 while disabled."""

    DURATIONS = {"alarm1": 2.416, "alarm2": 6.472}

    def __init__(self, door: "Door") -> None:
        self._door = door
        self.enabled = True
        self.unreachable = False
        self.playing_until = 0
        self.plays: "list[tuple[int, str]]" = []     # (sim ms, clip) for every 200
        self.calls: "list[tuple[int, str, int]]" = []  # every request and its status

    def play_sound(self, name):
        now = self._door.now
        if self.unreachable:
            self.calls.append((now, name, 0))
            raise EdgeUnavailable("connection refused")
        if not self.enabled:
            code, body = 423, {"error": "sound is disabled on the Pi"}
        elif now < self.playing_until:
            code, body = 409, {"error": "still playing"}
        else:
            dur = self.DURATIONS[name]
            self.playing_until = now + int(dur * 1000) + 100  # aplay outlives the clip a little
            self.plays.append((now, name))
            code, body = 200, {"sound": name, "duration_s": dur}
        self.calls.append((now, name, code))
        return code, body


class _Embedder:
    def __init__(self, model, imgsz, **_):
        self.backbone, self.imgsz = model, imgsz

    def prepare(self):
        pass


# What a frame truly holds, written by the fake passes: (yolo score, nearest cat, distance).
EMPTY = None


class Door:
    """A door at 10 fps over a real Store, ticked once a simulated second."""

    def __init__(self, tmp_path, threshold=THRESHOLD, mode="on") -> None:
        self.store = _store(tmp_path)
        self.mittens = self.store.create_cat("Mittens", is_resident=True)["id"]
        self.tom = self.store.create_cat("Tom", is_resident=False)["id"]
        gdir = os.path.join(self.store.models_root, "g1")
        os.makedirs(gdir)
        open(os.path.join(gdir, "gallery.npz"), "wb").close()
        self.model_id = self.store.add_model_version(
            "draft", "gallery", "dinov2_vits14", 224, 2, 10, threshold, "all", None, "g1"
        )
        self.store.promote_model(self.model_id)
        deterrent.set_mode(self.store, mode)
        self.now = BASE
        self.seq = [0]
        self.truth: "dict[int, tuple | None]" = {}
        self.busy = False
        self.fail_identify = False
        self.detect_calls: "list[tuple[int, int]]" = []
        self.pi = FakePi(self)
        self.stop = threading.Event()
        self.mgr = LiveIdentifyManager(
            self.store,
            is_busy=lambda: self.busy,
            detect=self._detect,
            identify=self._identify,
            analyzer_factory=lambda: object(),
            embedder_factory=_Embedder,
            gallery_loader=lambda path: object(),
            now_ms=lambda: self.now,
            sound=self.pi,
        )

    # -- the fake GPU passes: write the scripted truth where the real ones would ------

    def _detect(self, store, analyzer, manager, since_id=None, until_id=None):
        self.detect_calls.append((since_id, until_id))
        rows = []
        for fid, _path in store.iter_unanalyzed("yolo-serial", since_id=since_id, until_id=until_id):
            t = self.truth.get(fid)
            if t is None:
                rows.append((fid, "yolo-serial", False, None, None))
            else:
                rows.append((fid, "yolo-serial", True, t[0], {"boxes": [[1, 1, 9, 9, t[0], 15]]}))
        store.write_analysis_batch(rows)

    def _identify(self, store, model, gallery_path, since_id=None, until_id=None,
                  embedder=None, gallery=None, progress=None):
        if self.fail_identify:
            raise RuntimeError("CUDA error: device-side assert")
        rows = []
        for fid, _path, bbox in store.iter_unidentified(model["id"], since_id, until_id):
            _score, cat, dist = self.truth[fid]
            rows.append((fid, model["id"], cat, dist, bbox))
        store.write_identifications_batch(rows)
        return {"n_identified": len(rows)}

    # -- driving ------------------------------------------------------------------------

    def second(self, what=EMPTY, motion=None, tick=True):
        """One simulated second: 10 frames holding ``what``, then (optionally) a tick."""
        motion = (what is not EMPTY) if motion is None else motion
        for i in range(10):
            fid = _add(self.store, self.now + i * 100, motion, self.seq)
            self.truth[fid] = what
        self.now += 1000
        if tick:
            self.mgr._tick(self.stop)

    def mixed_second(self, whats, motion=True, tick=True):
        """One simulated second holding ``whats`` frame by frame (10 entries)."""
        assert len(whats) == 10
        for i, what in enumerate(whats):
            fid = _add(self.store, self.now + i * 100, motion, self.seq)
            self.truth[fid] = what
        self.now += 1000
        if tick:
            self.mgr._tick(self.stop)

    def rows(self):
        return [r for r in self.store.list_actions() if r["mode"] == "on"]

    def tom_at(self, dist=0.2, score=0.8):
        return (score, self.tom, dist)

    def mittens_at(self, dist=0.2, score=0.8):
        return (score, self.mittens, dist)

    def stranger(self):
        return (0.8, self.mittens, 0.9)  # nearest is Mittens, far beyond the threshold


def test_neighbour_visit_loops_alternating_clips_and_stops_when_gone(tmp_path):
    door = Door(tmp_path)
    for _ in range(3):
        door.second()
    first_tom_ts = door.now
    for _ in range(7):
        door.second(door.tom_at())                       # Tom at the door for 7 s
    last_tom_ts = door.now - 100
    for _ in range(6):
        door.second()

    [row] = door.rows()
    assert (row["verdict"], row["cat_id"], row["cat_name"]) == ("neighbour", door.tom, "Tom")
    assert row["ended"] == "gone" and row["end_ts"] is not None
    # At 10 counted frames a second the takeover net's 20 frames, all foreign, land after
    # the second second of Tom — the same tick the presence vote's 1 s hold is met. Both
    # paths are met; the row follows the presence vote, which calls Tom foreign, so it reads
    # `presence` (20 counted: the whole presence, which is also the net's whole window).
    assert row["start_ts"] - first_tom_ts <= 2_000
    assert row["trigger"] == deterrent.TRIGGER_PRESENCE and row["n_counted"] == 20
    assert row["visit_start_ts"] == first_tom_ts
    # Stops within 3 s of the last foreign frame, plus at most one tick.
    assert 3_000 <= row["end_ts"] - last_tom_ts <= 4_000
    # The deciding frame is a real Tom frame, resolvable on its pair.
    assert row["frame_live"] is True and door.truth[row["frame_id"]] == door.tom_at()
    assert row["n_counted"] >= deterrent.DETER_MIN_FRAMES and row["n_votes"] == row["n_counted"]
    assert (row["model_version_id"], row["threshold"]) == (door.model_id, THRESHOLD)
    # alarm1, alarm2, alarm1 ... each requested only once the last was due to end.
    clips = [name for _t, name in door.pi.plays]
    assert clips[:2] == ["alarm1", "alarm2"] and row["clips_played"] == len(clips)
    assert all(code != 409 for _t, _n, code in door.pi.calls)
    assert row["clips_failed"] == 0 and row["pi_disabled"] is False
    assert door.mgr.alarm_status()["sounding"] is False


def test_does_not_restart_on_the_departed_cats_last_frames(tmp_path):
    # At "gone" (no foreign frame for 3 s) the 5 s start window still holds 2 s of the
    # departed cat's frames — far more than 10. They must not start a second episode.
    door = Door(tmp_path)
    door.second()
    for _ in range(6):
        door.second(door.tom_at())
    for _ in range(8):
        door.second()
    assert len(door.rows()) == 1


def test_a_returning_stranger_starts_a_new_episode(tmp_path):
    door = Door(tmp_path)
    for _ in range(4):
        door.second(door.tom_at())
    for _ in range(6):
        door.second()
    for _ in range(3):
        door.second(door.tom_at())
    assert [r["verdict"] for r in door.rows()] == ["neighbour", "neighbour"]


def test_unknown_cat_sets_it_off(tmp_path):
    door = Door(tmp_path)
    for _ in range(3):
        door.second(door.stranger())
    [row] = door.rows()
    assert (row["verdict"], row["cat_id"]) == ("unknown", None)
    # A lone stranger: the presence vote calls it unknown, so the row is the presence's —
    # nothing within the threshold, so no votes — even though the net was met too.
    assert row["trigger"] == deterrent.TRIGGER_PRESENCE
    assert row["distance"] == pytest.approx(0.9) and row["n_votes"] == 0 and row["n_counted"] == 20


def test_resident_visit_adds_no_row(tmp_path):
    door = Door(tmp_path)
    for _ in range(8):
        door.second(door.mittens_at())
    for _ in range(4):
        door.second()
    assert door.rows() == [] and door.pi.calls == []


def test_resident_following_a_stranger_stops_it_and_never_restarts_it(tmp_path):
    door = Door(tmp_path)
    for _ in range(4):
        door.second(door.tom_at())
    last_tom_ts = door.now - 100
    for _ in range(10):
        door.second(door.mittens_at())      # same continuous visit: Mittens follows Tom in
    for _ in range(4):
        door.second()
    [row] = door.rows()
    assert row["ended"] == "gone"
    # Mittens' frames did not prolong it: it ended on Tom's frames, not hers.
    assert row["end_ts"] - last_tom_ts <= 4_000


def test_boxes_the_feed_calls_noise_never_start_it(tmp_path):
    door = Door(tmp_path)
    for _ in range(6):
        door.second(door.tom_at(score=0.25))  # recall-first box, below the feed's 0.3 floor
    assert door.rows() == []


def test_uncalibrated_gallery_never_sounds(tmp_path):
    door = Door(tmp_path, threshold=None)
    for _ in range(6):
        door.second(door.stranger())
    assert door.rows() == [] and door.pi.calls == []
    # The alarm step processed nothing: no detect while the visit was open.
    assert door.detect_calls == []


def test_switch_off_sounds_nothing(tmp_path):
    door = Door(tmp_path, mode="off")
    for _ in range(6):
        door.second(door.tom_at())
    assert door.rows() == [] and door.pi.calls == []


def test_switching_off_mid_alarm_ends_it(tmp_path):
    door = Door(tmp_path)
    for _ in range(3):
        door.second(door.tom_at())
    deterrent.set_mode(door.store, "off")
    n_calls = len(door.pi.calls)
    for _ in range(5):
        door.second(door.tom_at())
    [row] = door.rows()
    assert row["ended"] == "switched_off"
    assert len(door.pi.calls) == n_calls


def test_demoting_to_uncalibrated_mid_alarm_ends_it(tmp_path):
    door = Door(tmp_path)
    for _ in range(3):
        door.second(door.tom_at())
    door.store.set_model_threshold(door.model_id, None)
    door.second(door.tom_at())
    assert door.rows()[0]["ended"] == "uncalibrated"


def test_a_failing_pass_ends_the_alarm_as_error(tmp_path):
    door = Door(tmp_path)
    for _ in range(3):
        door.second(door.tom_at())
    door.fail_identify = True
    door.second(door.tom_at())    # this pass raises ...
    assert door.rows()[0]["end_ts"] is None
    door.second(door.tom_at())    # ... and the next tick's bookkeeping ends the episode
    row = door.rows()[0]
    assert row["ended"] == "error"
    assert "device-side assert" in door.mgr.status()["last_error"]
    assert "device-side assert" in door.mgr.alarm_status()["pass_error"]


def test_pi_disabled_is_recorded_once_and_clips_resume_when_reenabled(tmp_path):
    door = Door(tmp_path)
    door.pi.enabled = False
    for _ in range(4):
        door.second(door.tom_at())
    row = door.rows()[0]
    assert row["pi_disabled"] is True and row["clips_played"] == 0 and row["clips_failed"] == 0
    assert door.mgr.alarm_status()["episode"]["pi_disabled"] is True
    door.pi.enabled = True
    door.second(door.tom_at())
    assert [n for _t, n in door.pi.plays] == ["alarm1"]  # the playlist did not advance while refused


def test_unreachable_pi_is_counted_and_retried_every_pass(tmp_path):
    door = Door(tmp_path)
    door.pi.unreachable = True
    for _ in range(4):
        door.second(door.tom_at())
    row = door.rows()[0]
    assert row["clips_failed"] >= 3 and "connection refused" in row["last_error"]
    door.pi.unreachable = False
    door.second(door.tom_at())
    assert [n for _t, n in door.pi.plays] == ["alarm1"]


def test_a_busy_pi_is_retried_not_counted(tmp_path):
    door = Door(tmp_path)
    door.pi.playing_until = door.now + 3_500  # a test clip is still sounding
    for _ in range(5):
        door.second(door.tom_at())
    row = door.rows()[0]
    assert row["clips_failed"] == 0 and row["clips_played"] >= 1
    assert any(code == 409 for _t, _n, code in door.pi.calls)


def test_the_alarm_step_does_not_yield_to_manual_jobs(tmp_path):
    door = Door(tmp_path)
    door.busy = True  # a motion-tuning re-run, "Identify all", ... holds the GPU for hours
    for _ in range(3):
        door.second(door.tom_at())
    assert len(door.rows()) == 1 and door.pi.plays


def test_closed_visits_wait_while_a_visit_is_open(tmp_path):
    door = Door(tmp_path)
    store, seq = door.store, door.seq
    a = [_add(store, BASE + i * 100, True, seq) for i in range(5)]         # visit A, empty scene
    for fid in a:
        door.truth[fid] = EMPTY
    b = [_add(store, BASE + 3_000 + i * 100, True, seq) for i in range(5)]  # visit B, open
    for fid in b:
        door.truth[fid] = EMPTY
    door.now = BASE + 3_500
    door.mgr._tick(door.stop)
    # A is closed and B is open: the alarm step took B, the closed pass waited.
    assert door.mgr.status()["watermark"] == 0
    assert all(lo >= b[0] for lo, _hi in door.detect_calls)
    # Off, the same tick names A as before.
    deterrent.set_mode(store, "off")
    door.mgr._tick(door.stop)
    assert door.mgr.status()["watermark"] == a[-1]


def test_closed_visits_also_wait_while_an_alarm_sounds(tmp_path):
    # A stranger that sits still in Tuning: its motion cluster closes after 2 s, but the
    # alarm keeps sounding on the stored non-motion frames. Naming that closed visit then
    # must not run between ticks and delay the next clip or the "gone" stop.
    door = Door(tmp_path)
    for _ in range(3):
        door.second(door.tom_at())                     # moving: motion frames
    for _ in range(5):
        door.second(door.tom_at(), motion=False)       # sitting still: no motion
    assert door.mgr.alarm_status()["sounding"] is True
    assert door.mgr.status()["watermark"] == 0         # the closed visit waited
    for _ in range(6):
        door.second()                                   # gone -> the pass runs again
    assert door.rows()[0]["ended"] == "gone"
    assert door.mgr.status()["watermark"] > 0


def test_switching_on_mid_visit_starts_from_the_window_not_the_visit_start(tmp_path):
    # Tom has been at the door 20 s when the switch goes On. Replaying the visit from its
    # first frame at 30 frames a pass would hold the start back for many ticks.
    door = Door(tmp_path, mode="off")
    first = door.store.latest_id() + 1
    for _ in range(20):
        door.second(door.tom_at())
    deterrent.set_mode(door.store, "on")
    door.second(door.tom_at())
    assert len(door.rows()) == 1                        # started on the first tick On
    lo = min(lo for lo, _hi in door.detect_calls)
    assert lo > first + 100                             # within the last ~5 s, not from frame 1


def test_stopping_the_worker_ends_a_sounding_alarm(tmp_path):
    from compute.learning.live_identify import run_live_identify

    door = Door(tmp_path)
    for _ in range(3):
        door.second(door.tom_at())
    assert door.mgr.alarm_status()["sounding"] is True
    stopped = threading.Event()
    stopped.set()
    run_live_identify(door.mgr, stopped, 0.01)          # the thread's exit path
    assert door.mgr.alarm_status()["sounding"] is False
    row = door.rows()[0]
    assert row["ended"] == "interrupted" and row["end_ts"] is not None


def test_hwm_reseeds_on_reset_and_clamps_to_regressed_ids(tmp_path):
    door = Door(tmp_path)
    door.mgr._alarm_hwm = 10**9            # e.g. left above every frame by a purge
    door.second(door.tom_at())             # clamped to the newest frame this tick ...
    assert door.mgr.alarm_status()["hwm"] == door.store.latest_id()
    door.second(door.tom_at())             # ... so the next second's frames ARE processed
    assert door.detect_calls, "a mark above every frame must clamp, not process nothing"
    door.mgr.reset_watermark(0)
    assert door.mgr.alarm_status()["hwm"] == 0


def test_tick_period_is_one_second_while_on_and_the_interval_while_off(tmp_path):
    door = Door(tmp_path)
    assert door.mgr._next_wait(0.25, 5.0) == pytest.approx(0.75)
    assert door.mgr._next_wait(1.7, 5.0) == 0.0
    assert door.mgr._next_wait(0.0, 0.01) == 0.01   # never longer than the plain interval
    deterrent.set_mode(door.store, "off")
    assert door.mgr._next_wait(0.25, 5.0) == 5.0


def test_pass_summary_reports_what_it_processed(tmp_path):
    door = Door(tmp_path)
    door.second(door.tom_at())
    last = door.mgr.alarm_status()["last_pass"]
    assert last["ids"] == 10 and last["caught_up"] is True and last["behind"] == 0


def test_a_sparse_foreign_presence_starts_on_the_presence_vote(tmp_path):
    # Three counted frames a second (the rest below the feed's 0.3 floor) never reach the
    # takeover net's 20 in 5 s, so the presence vote, held 1 s of camera time, fires.
    door = Door(tmp_path)
    noise = door.tom_at(score=0.2)
    pattern = [door.tom_at(), noise, noise, door.tom_at(), noise, noise, door.tom_at(), noise, noise, noise]
    first_ts = door.now
    for _ in range(6):
        door.mixed_second(pattern)
    [row] = door.rows()
    assert row["trigger"] == deterrent.TRIGGER_PRESENCE
    assert (row["verdict"], row["cat_id"]) == ("neighbour", door.tom)
    assert row["n_counted"] >= deterrent.DETER_MIN_FRAMES and row["n_votes"] == row["n_counted"]
    # 10 counted frames by the 4th second (3 a second), then a 1 s hold.
    assert row["start_ts"] - first_ts <= 5_000


def test_a_resident_presence_with_a_lookalike_window_does_not_start(tmp_path):
    # The failure the presence vote exists for: a long resident visit in which, for a few
    # seconds, a lookalike neighbour edges ahead of the last 5 s. The old rule fired; the
    # whole presence still names Mittens, so nothing starts.
    door = Door(tmp_path)
    for _ in range(10):
        door.second(door.mittens_at())                   # 100 Mittens frames
    lookalike = [door.tom_at()] * 6 + [door.mittens_at()] * 4
    for _ in range(6):
        door.mixed_second(lookalike)                     # 60% Tom over these 6 s
    for _ in range(4):
        door.second()
    # The old rule's last 5 s held 30 Tom to 20 Mittens from the 5th second and fired
    # (replayed in test_alarm_replay.py); the presence holds 100 + 24 Mittens to 36 Tom.
    assert door.rows() == []


def test_a_stranger_tailgating_a_resident_is_caught_by_the_takeover_net(tmp_path):
    # Mittens in first, a stranger straight behind her in the same presence: the presence
    # vote hands the stranger's frames to Mittens, the takeover net does not.
    door = Door(tmp_path)
    for _ in range(15):
        door.second(door.mittens_at())                   # 150 Mittens frames
    for _ in range(4):
        door.second(door.stranger())
    # 4 s in, the last 5 s still hold a second of Mittens: 40 of 50, under the 90% share.
    assert door.rows() == []
    door.second(door.stranger())                         # now 50 of 51 are foreign
    [row] = door.rows()
    assert row["trigger"] == deterrent.TRIGGER_TAKEOVER
    assert (row["verdict"], row["cat_id"]) == ("unknown", None)


def test_the_previous_alarm_floor_is_seeded_from_the_log(tmp_path):
    door = Door(tmp_path)
    store = door.store
    assert deterrent.Alarm(store, None)._last_end_ms is None          # no episode yet
    store.add_action({"kind": "sound", "mode": "on", "start_ts": BASE, "verdict": "unknown",
                      "end_ts": BASE + 9_000, "ended": "gone"})
    store.add_action({"kind": "sound", "mode": "test", "start_ts": BASE + 20_000,
                      "verdict": "test", "end_ts": BASE + 22_000})
    seeded = deterrent.Alarm(store, None)
    assert seeded._last_end_ms == BASE + 9_000                        # tests do not count
    assert seeded.status()["last_ended"] == "gone"                    # never an end with no reason
    # An episode still open reads None: on a live app the launch sweep closes it first.
    store.add_action({"kind": "sound", "mode": "on", "start_ts": BASE + 30_000, "verdict": "unknown"})
    assert deterrent.Alarm(store, None)._last_end_ms is None
    store.close_open_actions("interrupted", BASE + 31_000)
    assert deterrent.Alarm(store, None)._last_end_ms == BASE + 31_000


def _restart_after_gone(door, seed=True):
    """Tom 4 s -> alarm; 3 empty seconds -> gone; restart; Tom back within 5 s of his last
    frame, so his old frames are in the same presence. Returns the rows after one new second."""
    for _ in range(4):
        door.second(door.tom_at())
    for _ in range(3):
        door.second()
    [row] = door.rows()
    assert row["ended"] == "gone"
    store = door.store
    if not seed:
        store.last_alarm_end = lambda: None  # the memory-only floor of before
    door.mgr = LiveIdentifyManager(
        store, is_busy=lambda: False, detect=door._detect, identify=door._identify,
        analyzer_factory=lambda: object(), embedder_factory=_Embedder,
        gallery_loader=lambda path: object(), now_ms=lambda: door.now, sound=door.pi,
    )
    if seed:
        assert door.mgr._alarm._last_end_ms == row["end_ts"]
    door.second(door.tom_at())
    return door.rows()


def test_a_restart_does_not_recount_the_last_episodes_stranger(tmp_path):
    # The presence vote reaches back 2 minutes, so after a restart the floor has to come
    # from the log. Control first: with the floor unseeded, the same door DOES restart the
    # alarm on the departed episode's frames — so the seeded case below is not passing
    # merely because the scenario never reached that condition.
    assert len(_restart_after_gone(Door(tmp_path / "control"), seed=False)) == 2
    assert len(_restart_after_gone(Door(tmp_path / "seeded"))) == 1


def test_pass_summary_splits_detection_from_identification(tmp_path):
    door = Door(tmp_path)
    door.second(door.tom_at())
    last = door.mgr.alarm_status()["last_pass"]
    assert {"detect_ms", "identify_ms"} <= set(last)
    assert last["detect_ms"] + last["identify_ms"] <= last["ms"]
    door.second()  # nothing open: the idle summary carries the same keys
    for _ in range(3):
        door.second()
    idle = door.mgr.alarm_status()["last_pass"]
    assert idle["ids"] == 0 and idle["detect_ms"] == 0 and idle["identify_ms"] == 0


# =====================================================================================
#  The test button
# =====================================================================================


class _Client:
    def __init__(self, reply=None, exc=None):
        self.reply, self.exc, self.calls = reply, exc, []

    def play_sound(self, name):
        self.calls.append(name)
        if self.exc:
            raise self.exc
        return self.reply


@pytest.mark.parametrize("reply,exc,outcome,played,failed,disabled", [
    ((200, {"sound": "alarm2", "duration_s": 6.5}), None, "played", 1, 0, 0),
    ((409, {"error": "still playing"}), None, "busy", 0, 0, 0),
    ((423, {"error": "disabled"}), None, "disabled", 0, 0, 1),
    ((502, {"error": "audio open error"}), None, "failed", 0, 1, 0),
    (None, EdgeUnavailable("timed out"), "failed", 0, 1, 0),
])
def test_play_test_clip_writes_a_test_row(tmp_path, reply, exc, outcome, played, failed, disabled):
    store = _store(tmp_path)
    res = deterrent.play_test_clip(store, _Client(reply, exc), "alarm2", BASE)
    assert res["outcome"] == outcome
    [row] = store.list_actions()
    assert (row["mode"], row["verdict"], row["sound"]) == ("test", "test", "alarm2")
    assert (row["clips_played"], row["clips_failed"], int(row["pi_disabled"])) == (played, failed, disabled)
    assert row["end_ts"] is not None
    if outcome in ("busy", "failed"):
        assert row["last_error"]
    else:
        assert row["last_error"] is None  # played, or refused by the Pi's own switch (the flag says so)


def test_play_test_clip_without_a_pi_client(tmp_path):
    store = _store(tmp_path)
    res = deterrent.play_test_clip(store, None, "alarm1", BASE)
    assert res["outcome"] == "failed" and "CAT_PI_URL" in res["error"]


def test_an_old_foreign_presence_cannot_start_an_alarm_while_the_pass_is_behind(tmp_path):
    # Tom visited with the switch Off; the closed-visit pass named his frames. A minute
    # later the switch is On and a new visit opens with more frames than one pass takes,
    # none of the first 30 counted yet. The step is BEHIND, but its frontier is a minute
    # past Tom's newest frame — nothing still to come can continue that presence, so it is
    # stale. Without the frontier, "behind" meant "never stale" and Tom's minute-old
    # presence met its hold in camera time and sounded at whoever had just arrived.
    door = Door(tmp_path, mode="off")
    for _ in range(4):
        door.second(door.tom_at())
    for _ in range(60):
        door.second()                                    # a quiet minute; Tom is named
    assert len(door.store.alarm_frames(door.model_id, 0)) == 40
    deterrent.set_mode(door.store, "on")
    for _ in range(4):
        door.second(EMPTY, motion=True, tick=False)      # a new visit, nothing counted yet
    door.mgr._tick(door.stop)
    assert door.mgr.alarm_status()["last_pass"]["caught_up"] is False
    assert door.rows() == []


def test_an_empty_door_costs_only_the_short_probe(tmp_path):
    # With the switch On the step runs every second all day. Once nothing counted lies
    # within DETER_WINDOW_S of the frontier the vote is stale, so the two-minute read (and
    # the roster) is skipped: only the few-seconds probe is paid.
    door = Door(tmp_path)
    for _ in range(4):
        door.second(door.mittens_at())
    for _ in range(8):
        door.second()
    reads = []
    real = door.store.alarm_frames
    door.store.alarm_frames = lambda mid, since, **kw: (reads.append(since), real(mid, since, **kw))[1]
    door.second()
    window_ms = int(deterrent.DETER_WINDOW_S * 1000)
    assert reads and all(since >= door.now - window_ms - 1_000 for since in reads), reads
