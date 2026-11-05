"""The alarm replay tool (``compute.tools.alarm_replay``; alarm-presence-vote spec).

Driven over a REAL ``Store`` holding motion frames, ``yolo-serial`` verdicts and
identifications under a promoted, calibrated model — the rows the live worker writes — so
the feed walk, the counted-frame read, the attribution and both rules run as they would on
the compute PC. Scenarios are built so each answer is known in advance: a lookalike window
inside a resident visit (today's rule only), a neighbour (both), a stranger tailgating a
resident (both, the new rule by the takeover net), a plain resident visit (neither), and a
short neighbour visit the new rule decides only after it ended — an added miss the report
must surface rather than swallow.
"""
from __future__ import annotations

import os
import random
from datetime import date, datetime, timedelta

import pytest

from compute.collection.store import Store
from compute.ingest import StreamFrame
from compute.learning import deterrent
from compute.tools import alarm_replay
from compute.tools.alarm_replay import ReplayRefused, replay, simulate
from shared.wire import StreamFrameMeta

_JPEG = b"\xff\xd8\xff\xe0" + b"fake-jpeg-body" + b"\xff\xd9"
DAY = date(2026, 10, 26)
MIDNIGHT = int(datetime(DAY.year, DAY.month, DAY.day).timestamp() * 1000)
H10 = MIDNIGHT + 10 * 3_600_000
TH = 0.4


class Door:
    def __init__(self, tmp_path, threshold=TH, promote=True):
        self.root = tmp_path
        self.store = Store(
            db_path=str(tmp_path / "index.db"), media_root=str(tmp_path / "media"), max_bytes=10**9
        )
        self.mittens = self.store.create_cat("Mittens", is_resident=True)["id"]
        self.tom = self.store.create_cat("Tom", is_resident=False)["id"]
        self.model_id = None
        if promote:
            gdir = os.path.join(self.store.models_root, "g1")
            os.makedirs(gdir)
            open(os.path.join(gdir, "gallery.npz"), "wb").close()
            self.model_id = self.store.add_model_version(
                "draft", "gallery", "dinov2_vits14", 224, 2, 10, threshold, "all", None, "g1"
            )
            self.store.promote_model(self.model_id)
        self.seq = 0

    def frames(self, start_ms, truths, step=100, identify=True):
        """Motion frames ``step`` ms apart; each truth is (cat_id, distance) or None."""
        rows_a, rows_i = [], []
        for k, truth in enumerate(truths):
            self.seq += 1
            ts = start_ms + k * step
            meta = StreamFrameMeta(frame_id=self.seq, ts=ts, motion=True, bbox=None, area=0.05)
            fid = self.store.add(StreamFrame(meta, _JPEG), recv_ts_ms=ts)
            if truth is None:
                rows_a.append((fid, "yolo-serial", False, None, None))
                continue
            rows_a.append((fid, "yolo-serial", True, 0.8, {"boxes": [[1, 1, 9, 9, 0.8, 15]]}))
            if identify:
                rows_i.append((fid, self.model_id, truth[0], truth[1], [1, 1, 9, 9]))
        self.store.write_analysis_batch(rows_a)
        if rows_i:
            self.store.write_identifications_batch(rows_i)

    def mittens_at(self, d=0.2):
        return (self.mittens, d)

    def tom_at(self, d=0.2):
        return (self.tom, d)

    def stranger(self):
        return (self.mittens, 0.9)


def _scenario(door):
    """Four visits, each 5 minutes apart, whose answers are known."""
    m, t, x = door.mittens_at(), door.tom_at(), door.stranger()
    # A: Mittens 10 s, then 6 s in which Tom leads 6 to 4 a second. Card: Mittens.
    door.frames(H10, [m] * 100 + ([t] * 6 + [m] * 4) * 6)
    # B: Tom alone for 4 s. Card: Tom (neighbour).
    door.frames(H10 + 300_000, [t] * 40)
    # C: Mittens 15 s, a stranger straight behind her for 6 s. Card: Mittens (the
    # stranger's frames are beyond the threshold, so only hers are voted).
    door.frames(H10 + 600_000, [m] * 150 + [x] * 60)
    # D: Mittens alone for 8 s.
    door.frames(H10 + 900_000, [m] * 80)
    return {"A": H10, "B": H10 + 300_000, "C": H10 + 600_000, "D": H10 + 900_000}


def _visit(report, start_ts):
    [i] = [i for i, v in enumerate(report.visits) if v.start_ts == start_ts]
    return i


def test_the_scenario_reads_as_designed(tmp_path):
    door = Door(tmp_path)
    at = _scenario(door)
    report = replay(door.store, DAY, DAY, now_ms=MIDNIGHT + 86_400_000)
    assert (report.model_id, report.threshold) == (door.model_id, TH)
    assert [v.card for v in report.visits] == ["ours", "neighbour", "ours", "ours"]
    a, b, c, d = (_visit(report, at[k]) for k in "ABCD")
    for phase in alarm_replay.PHASES_MS:
        today, new = report.alarmed("today", phase), report.alarmed("new", phase)
        assert set(today) == {a, b, c}, phase       # the lookalike window fires today's rule
        assert set(new) == {b, c}, phase            # ... but not the presence vote
        # A lone neighbour meets both paths on one tick; the row follows the presence vote.
        assert new[b].decision.trigger == deterrent.TRIGGER_PRESENCE
        # The tailgate: the presence names Mittens, so the net overrode the vote.
        assert new[c].decision.trigger == deterrent.TRIGGER_TAKEOVER
        assert new[c].decision.verdict == "unknown"
        assert report.added_misses(phase) == []
        assert not [s for s in report.starts if s.visit is not None and not s.in_view]
        assert not [s for s in report.starts if s.visit is None]
    assert report.days[0]["visits"] == 4 and report.days[0]["with_counted"] == 4


def test_a_new_rule_start_after_the_visit_ended_is_an_added_miss_and_a_late_start(tmp_path):
    # A 1.5 s neighbour visit: 15 frames, under the net's 20. Today's rule decides at the
    # first tick holding 10 frames; the presence vote's 1 s hold is met only after the cat
    # has gone. At phase 0 the old rule's tick falls inside the visit — so that phase
    # reports an added miss, which the report must list, never smooth over.
    door = Door(tmp_path)
    door.frames(H10, [door.tom_at()] * 15)
    report = replay(door.store, DAY, DAY, now_ms=MIDNIGHT + 86_400_000)
    [v] = report.visits
    assert report.added_misses(0) == [0]
    [late] = [s for s in report.starts_for("new", 0) if s.visit == 0]
    assert not late.in_view and late.at_ms > v.end_ts
    assert late.decision.trigger == deterrent.TRIGGER_PRESENCE


def test_unclaimed_starts_are_counted_on_their_day(tmp_path):
    # Counted frames on NON-motion frames between visits belong to no feed visit.
    door = Door(tmp_path)
    door.frames(H10, [door.mittens_at()] * 5)                  # one visit, so the range has one
    door.seq += 1
    rows_a, rows_i = [], []
    for k in range(30):
        door.seq += 1
        ts = H10 + 60_000 + k * 100
        meta = StreamFrameMeta(frame_id=door.seq, ts=ts, motion=False, bbox=None, area=0.0)
        fid = door.store.add(StreamFrame(meta, _JPEG), recv_ts_ms=ts)
        rows_a.append((fid, "yolo-serial", True, 0.8, {"boxes": [[1, 1, 9, 9, 0.8, 15]]}))
        rows_i.append((fid, door.model_id, door.tom, 0.2, [1, 1, 9, 9]))
    door.store.write_analysis_batch(rows_a)
    door.store.write_identifications_batch(rows_i)
    report = replay(door.store, DAY, DAY, now_ms=MIDNIGHT + 86_400_000)
    u = report.days[0]["unclaimed"]
    assert all(u[(rule, p)] == 1 for rule in alarm_replay.RULES for p in alarm_replay.PHASES_MS)


def test_real_alarm_rows_in_the_range_are_read_beside_the_replay(tmp_path):
    door = Door(tmp_path)
    _scenario(door)
    door.store.add_action({"kind": "sound", "mode": "on", "start_ts": H10 + 302_500,
                           "verdict": "neighbour", "cat_id": door.tom, "trigger": "takeover"})
    door.store.add_action({"kind": "sound", "mode": "test", "start_ts": H10, "verdict": "test"})
    door.store.add_action({"kind": "sound", "mode": "on", "start_ts": MIDNIGHT - 1, "verdict": "unknown"})
    report = replay(door.store, DAY, DAY, now_ms=MIDNIGHT + 86_400_000)
    assert [r["start_ts"] for r in report.real_actions] == [H10 + 302_500]


# =====================================================================================
#  Refusals
# =====================================================================================


def test_refuses_a_range_the_store_holds_nothing_for(tmp_path):
    door = Door(tmp_path)
    _scenario(door)
    with pytest.raises(ReplayRefused, match="no frames"):
        replay(door.store, date(2025, 3, 3), date(2025, 3, 9))


def test_refuses_visits_with_no_counted_frames_under_the_active_model(tmp_path):
    # As after a promotion: the range's frames carry identifications only under an older
    # model, so both rules would alarm on nothing — a clean report would be a lie.
    door = Door(tmp_path)
    door.frames(H10, [door.tom_at()] * 40, identify=False)
    with pytest.raises(ReplayRefused, match="no counted frames"):
        replay(door.store, DAY, DAY, now_ms=MIDNIGHT + 86_400_000)


@pytest.mark.parametrize("promote,threshold,match", [(False, TH, "no gallery"), (True, None, "uncalibrated")])
def test_refuses_without_a_calibrated_gallery(tmp_path, promote, threshold, match):
    door = Door(tmp_path, threshold=threshold, promote=promote)
    with pytest.raises(ReplayRefused, match=match):
        replay(door.store, DAY, DAY)


def test_refuses_a_truncated_walk(tmp_path, monkeypatch):
    door = Door(tmp_path)
    _scenario(door)
    real = door.store._walk_events
    monkeypatch.setattr(door.store, "_walk_events", lambda *a, **k: (real(*a, **k)[0], True))
    with pytest.raises(ReplayRefused, match="truncated"):
        replay(door.store, DAY, DAY, now_ms=MIDNIGHT + 86_400_000)


def test_main_exits_non_zero_on_a_refusal_and_prints_on_success(tmp_path, capsys):
    door = Door(tmp_path)
    _scenario(door)
    with pytest.raises(SystemExit) as exc:
        alarm_replay.main(["--since", "2025-03-03", "--until", "2025-03-09", "--dir", str(tmp_path)])
    assert exc.value.code not in (0, None)
    nxt = (DAY + timedelta(days=1)).isoformat()
    alarm_replay.main(["--since", DAY.isoformat(), "--until", nxt, "--dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert f"Model {door.model_id}" in out and "Added misses" in out
    assert "phase 0.0 s: none" in out and "phase 0.5 s: none" in out
    assert "takeover" in out
    # The trigger tally: the lone neighbour's start reads presence, the tailgate takeover.
    assert "phase 0.0 s: neighbour 1/0; ours 0/1" in out


# =====================================================================================
#  The simulation itself
# =====================================================================================


def _naive(frames, rule, phase, t_first, t_last, threshold, names, residents):
    """``simulate`` without the idle-stretch skip: every tick, always."""
    decide = alarm_replay._DECIDERS[rule]
    lookback = alarm_replay._LOOKBACK_MS[rule]
    out, running, last_end = [], False, None
    t = t_first + phase
    while t <= t_last:
        known = [f for f in frames if f[1] <= t]
        if running and not any(
            deterrent.is_foreign(f[2], f[3], threshold, residents) for f in known if f[1] >= t - 3_000
        ):
            running, last_end = False, t
        if not running:
            floor = None if last_end is None else last_end + 1
            lo = t - lookback if floor is None else max(t - lookback, floor)
            window = [f for f in known if f[1] >= lo]
            if rule == "new":
                assert lo == deterrent.vote_floor(t, floor)
            if len(window) >= deterrent.DETER_MIN_FRAMES:
                d = decide(window, t, floor, t, threshold, names, residents)
                if d is not None:
                    out.append((t, d))
                    running = True
        t += 1_000
    return out


@pytest.mark.parametrize("seed", range(6))
@pytest.mark.parametrize("rule", alarm_replay.RULES)
def test_skipping_idle_ticks_changes_no_decision(seed, rule):
    rng = random.Random(seed)
    names = {1: "M", 2: "T"}
    residents = {1: True, 2: False}
    frames, t, fid = [], 0, 0
    for _burst in range(8):
        t += rng.randint(1_000, 40_000)
        for _ in range(rng.randint(5, 80)):
            t += rng.choice([100, 100, 200, 400, 1_500])
            fid += 1
            frames.append((fid, t, rng.choice([1, 2]), rng.choice([0.2, 0.3, 0.9])))
    end = t + 10_000
    for phase in alarm_replay.PHASES_MS:
        fast = simulate(frames, rule, phase, 0, end, TH, names, residents)
        slow = _naive(frames, rule, phase, 0, end, TH, names, residents)
        assert [(a, d.frame_id, d.trigger) for a, d in fast] == [(a, d.frame_id, d.trigger) for a, d in slow]


def test_baseline_is_the_old_five_second_rule():
    names, residents = {1: "M", 2: "T"}, {1: True, 2: False}
    fr = [(i, i * 100, 2, 0.2) for i in range(10)]
    d = alarm_replay.baseline_decision(fr, 900, None, True, TH, names, residents)
    assert d is not None and (d.verdict, d.cat_id, d.n_counted) == ("neighbour", 2, 10)
    assert alarm_replay.baseline_decision(fr, 5_100, None, True, TH, names, residents) is None  # 9 left
    assert alarm_replay.baseline_decision(fr, 900, 1, True, TH, names, residents) is None       # floored
    assert alarm_replay.baseline_decision(fr, 900, None, True, None, names, residents) is None  # uncalibrated


def test_a_presence_under_way_at_the_first_midnight_is_voted_whole(tmp_path):
    # Mittens for 10 s up to midnight, then 6 s in which Tom leads 6 to 4. Read from
    # midnight only, the presence would be the Tom-led remainder and the new rule would
    # start on a resident's visit that never alarmed live; read a lookback early, her
    # earlier frames are in the vote.
    door = Door(tmp_path)
    m, t = door.mittens_at(), door.tom_at()
    door.frames(MIDNIGHT - 10_000, [m] * 100 + ([t] * 6 + [m] * 4) * 6)
    report = replay(door.store, DAY, DAY, now_ms=MIDNIGHT + 86_400_000)
    assert report.starts_for("new", 0) == [] and report.starts_for("new", 500) == []
    assert all(s.at_ms >= MIDNIGHT for s in report.starts)       # none reported before the range


def test_a_visit_starting_just_before_the_last_midnight_keeps_its_tail(tmp_path):
    # A neighbour arriving half a second before the range ends: only 5 of its frames fall
    # inside the range. Cut there, neither rule could start; read to the visit's end, both do.
    door = Door(tmp_path)
    end = MIDNIGHT + 86_400_000
    door.frames(end - 500, [door.tom_at()] * 40)
    report = replay(door.store, DAY, DAY, now_ms=end + 3_600_000)
    [v] = report.visits
    assert report.n_counted == 5
    for rule in alarm_replay.RULES:
        assert 0 in report.alarmed(rule, 0), rule
