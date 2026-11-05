"""The alarm's start rule as a pure function (docs/specs/2026-11-05-alarm-presence-vote.md).

``deterrent.start_decision`` is the whole rule — the worker and the replay tool both call
it — so its edges are pinned here directly on synthetic counted frames, with no store and
no worker: the prefix tallies against the feed's own voter, the camera-time hold and its
``caught_up`` branch, the staleness check, the presence gap and 2-minute bound, the floor,
and the takeover net's thresholds and verdict.
"""
from __future__ import annotations

import random

import pytest

from compute.collection.store import Store
from compute.learning import deterrent
from compute.learning.deterrent import (
    TRIGGER_PRESENCE,
    TRIGGER_TAKEOVER,
    prefix_winners,
    start_decision,
)

TH = 0.4
MITTENS, KALI, TOM, REX = 1, 2, 3, 4
NAMES = {MITTENS: "Mittens", KALI: "Kali", TOM: "Tom", REX: "Rex"}
RESIDENTS = {MITTENS: True, KALI: True, TOM: False, REX: False}
T = 1_700_000_000_000

NEAR, FAR = 0.2, 0.9


def frames_at(spec, start=T, step=100, first_id=1):
    """``spec`` is a list of (cat_id, distance); frames ``step`` ms apart from ``start``."""
    return [(first_id + i, start + i * step, cid, d) for i, (cid, d) in enumerate(spec)]


def decide(frames, now=None, floor=None, caught_up=True, threshold=TH, frontier=None):
    """Caught up, the frontier is ``now``; behind, it is ``frontier`` (``None`` = unknown,
    so only camera time — the newest counted frame — may speak)."""
    now = frames[-1][1] if now is None else now
    return start_decision(
        frames, now, floor, now if caught_up else frontier, threshold, NAMES, RESIDENTS
    )


# =====================================================================================
#  The prefix tallies reproduce the feed's voter exactly
# =====================================================================================


@pytest.mark.parametrize("seed", range(40))
def test_prefix_winners_match_the_voter_on_every_prefix(seed):
    # Distances from a tiny set so ties on count AND min distance are common; the voter
    # breaks those by dict insertion order, which the tallies must reproduce.
    rng = random.Random(seed)
    idents = [
        (rng.choice([MITTENS, KALI, TOM, REX]), rng.choice([0.1, 0.2, 0.2, 0.3, 0.4, 0.5, 0.9]))
        for _ in range(rng.randint(1, 60))
    ]
    winners = prefix_winners(idents, TH)
    for k in range(len(idents)):
        agg = Store._aggregate_identity(idents[: k + 1], TH, NAMES, RESIDENTS)
        assert winners[k] == agg["cat_id"], (k, idents[: k + 1])


def test_prefix_winner_tie_on_count_and_distance_goes_to_the_first_below():
    idents = [(TOM, 0.2), (MITTENS, 0.2)]
    assert prefix_winners(idents, TH) == [TOM, TOM]
    assert Store._aggregate_identity(idents, TH, NAMES, RESIDENTS)["cat_id"] == TOM


def test_presence_verdict_is_the_voter_over_the_whole_presence():
    spec = [(MITTENS, NEAR)] * 4 + [(TOM, 0.3)] * 6 + [(REX, FAR)] * 5 + [(TOM, 0.25)] * 3
    fr = frames_at(spec, step=400)          # 2.5 fps: never the takeover net's 20 in 5 s
    d = decide(fr, now=fr[-1][1] + 100)
    agg = Store._aggregate_identity([(c, x) for _f, _t, c, x in fr], TH, NAMES, RESIDENTS)
    assert d is not None and d.trigger == TRIGGER_PRESENCE
    assert (d.cat_id, d.n_votes, d.distance, d.n_counted) == (
        agg["cat_id"], agg["n_frames_voted"], agg["distance"], len(fr)
    )
    assert (d.frame_id, d.frame_recv_ts) == fr[-1][:2]


# =====================================================================================
#  Presence vote + 1 s hold, in camera time
# =====================================================================================


def test_ten_foreign_frames_need_a_further_second_of_camera_time():
    # 2 fps of Tom: the 10th frame turns the vote foreign at t0 = 4.5 s; the hold is met
    # once the newest frame is a second past it.
    fr = frames_at([(TOM, NEAR)] * 12, step=500)
    t0 = fr[9][1]
    assert decide(fr[:10], caught_up=False) is None            # just turned foreign
    assert decide(fr[:11], caught_up=False) is None            # 0.5 s held
    d = decide(fr[:12], caught_up=False)                       # 1.0 s held
    assert d is not None and d.trigger == TRIGGER_PRESENCE and d.verdict == "neighbour"
    assert fr[11][1] - t0 == 1_000


def test_a_lagging_pass_cannot_meet_the_hold_on_frames_it_has_not_seen():
    fr = frames_at([(TOM, NEAR)] * 10, step=500)
    late = fr[-1][1] + 4_000                                   # wall clock far ahead
    assert decide(fr, now=late, caught_up=False) is None       # behind: camera time only
    d = decide(fr, now=late, caught_up=True)                   # caught up: the cat left
    assert d is not None and d.trigger == TRIGGER_PRESENCE     # a short visit completes


def test_caught_up_wall_clock_branch_needs_the_full_second():
    fr = frames_at([(TOM, NEAR)] * 10, step=500)
    t0 = fr[-1][1]
    assert decide(fr, now=t0 + 999, caught_up=True) is None
    assert decide(fr, now=t0 + 1_000, caught_up=True) is not None


def test_t0_is_where_the_vote_turned_foreign_not_the_presence_start():
    # Mittens leads for 8 frames, then Tom catches up: the vote turns foreign on the frame
    # where Tom's count passes hers, and the hold runs from THERE.
    spec = [(MITTENS, NEAR)] * 8 + [(TOM, NEAR)] * 12
    fr = frames_at(spec, step=400)
    turned = 8 + 9 - 1                         # Tom's 9th frame: 9 to 8
    assert prefix_winners([(c, x) for _f, _t, c, x in fr], TH)[turned] == TOM
    assert prefix_winners([(c, x) for _f, _t, c, x in fr], TH)[turned - 1] == MITTENS
    t0 = fr[turned][1]
    upto = [f for f in fr if f[1] < t0 + 1_000]
    assert decide(upto, caught_up=False) is None
    upto = [f for f in fr if f[1] <= t0 + 1_200]
    d = decide(upto, caught_up=False)
    assert d is not None and d.trigger == TRIGGER_PRESENCE and d.cat_id == TOM


def test_a_broken_foreign_run_restarts_the_hold():
    # Foreign, then a resident retakes the lead for one prefix, then foreign again: t0
    # is the LATEST turn, so the earlier foreign run does not count towards the hold.
    spec = (
        [(TOM, NEAR)] * 10 + [(MITTENS, NEAR)] * 11      # Mittens retakes at 11 to 10
        + [(TOM, NEAR)] * 2                              # Tom back ahead: 12 to 11
    )
    fr = frames_at(spec, step=400)
    assert decide(fr, caught_up=False) is None           # 0.4 s held since the retake
    more = fr + frames_at([(TOM, NEAR)] * 2, start=fr[-1][1] + 400, step=400, first_id=100)
    d = decide(more, caught_up=False)
    assert d is not None and d.trigger == TRIGGER_PRESENCE


def test_a_resident_presence_never_starts_however_long():
    fr = frames_at([(MITTENS, NEAR)] * 200, step=400)
    assert decide(fr, now=fr[-1][1] + 500) is None


def test_a_lookalike_window_inside_a_resident_presence_does_not_start():
    # 5 Nov 04:15's shape: a long resident visit in which a neighbour edges ahead of the
    # last 5 s. The presence still names the resident.
    spec = [(KALI, NEAR)] * 40 + [(TOM, 0.3)] * 8 + [(KALI, NEAR)] * 2 + [(TOM, 0.3)] * 8
    fr = frames_at(spec, step=300)
    assert decide(fr, now=fr[-1][1] + 100) is None


def test_fewer_than_ten_frames_never_start():
    fr = frames_at([(TOM, NEAR)] * 9, step=500)
    assert decide(fr, now=fr[-1][1] + 3_000) is None


def test_an_uncalibrated_gallery_never_starts():
    fr = frames_at([(TOM, NEAR)] * 40, step=100)
    assert decide(fr, threshold=None) is None


# =====================================================================================
#  Staleness, the presence gap, the 2-minute bound, the floor
# =====================================================================================


def test_stale_when_the_frontier_is_a_window_past_the_newest_frame():
    fr = frames_at([(TOM, NEAR)] * 12, step=500)
    newest = fr[-1][1]
    assert decide(fr, now=newest + 5_000, caught_up=True) is not None
    assert decide(fr, now=newest + 5_001, caught_up=True) is None
    # Behind, the frontier — not the wall clock — decides: frames still to come lie after
    # it, so they can continue the presence only while it is within the window.
    late = newest + 60_000
    assert decide(fr, now=late, caught_up=False, frontier=newest + 3_000) is not None
    assert decide(fr, now=late, caught_up=False, frontier=newest + 5_001) is None
    # Unknown frontier (its frame is gone): camera time only, never stale.
    assert decide(fr, now=late, caught_up=False, frontier=None) is not None


def test_a_minute_old_foreign_presence_is_stale_to_a_pass_that_is_behind():
    # The hole a caught-up flag left: a stranger's presence a minute ago, never alarmed
    # (switch Off, frames named later). A new visit's first pass is behind with nothing
    # counted yet. Its frontier is a minute past that presence, so nothing to come can
    # continue it — no start, where "behind = never stale" sounded at the new arrival.
    fr = frames_at([(TOM, NEAR)] * 40, step=100)
    newest = fr[-1][1]
    assert decide(fr, now=newest + 64_000, caught_up=False, frontier=newest + 63_000) is None


def test_behind_the_hold_runs_to_the_frontier():
    # 2 fps of Tom: the vote turns foreign on the 10th frame, the newest. Behind with the
    # frontier 1 s past it — every frame up to there processed, none counted — the vote
    # has stayed foreign a second: met. Short of that, not.
    fr = frames_at([(TOM, NEAR)] * 10, step=500)
    t0 = fr[-1][1]
    assert decide(fr, now=t0 + 4_000, caught_up=False, frontier=t0 + 999) is None
    d = decide(fr, now=t0 + 4_000, caught_up=False, frontier=t0 + 1_000)
    assert d is not None and d.trigger == TRIGGER_PRESENCE


def test_a_gap_over_five_seconds_starts_a_new_presence():
    # Mittens, a 5.1 s gap, then Tom: the earlier frames are another presence and cannot
    # outvote him. Exactly 5 s is no gap.
    mittens = frames_at([(MITTENS, NEAR)] * 30, step=400)
    tom_start = mittens[-1][1] + 5_100
    tom = frames_at([(TOM, NEAR)] * 13, start=tom_start, step=400, first_id=100)
    d = decide(mittens + tom, caught_up=False)
    assert d is not None and d.n_counted == 13
    joined = frames_at([(TOM, NEAR)] * 13, start=mittens[-1][1] + 5_000, step=400, first_id=100)
    assert decide(mittens + joined, caught_up=False) is None   # same presence: Mittens 30 to 13


def test_the_presence_reaches_back_two_minutes_and_then_slides():
    # 150 s of Mittens at 1 fps then 20 s of Tom at 1 fps: within the bound Mittens still
    # leads; once her older frames fall out of the 2-minute read, Tom takes the vote.
    spec = [(MITTENS, NEAR)] * 150 + [(TOM, NEAR)] * 140
    fr = frames_at(spec, step=1_000)
    at = lambda i: decide(fr[: i + 1], caught_up=False)  # noqa: E731
    assert at(169) is None                                 # 120 s read: 100 M to 20 T
    d = at(289)
    assert d is not None and d.trigger == TRIGGER_PRESENCE and d.cat_id == TOM
    assert d.n_counted == 121                              # [now-120 s, now] inclusive at 1 fps


def test_frames_before_the_floor_are_not_voted():
    fr = frames_at([(TOM, NEAR)] * 30, step=400)
    floor = fr[20][1] + 1                                  # the previous alarm ended just after #21
    assert decide(fr, floor=floor, caught_up=False) is None  # 9 frames left
    assert decide(fr, floor=None, caught_up=False) is not None


# =====================================================================================
#  The takeover net
# =====================================================================================


def _tailgate(n_resident_window, n_foreign, foreign=(REX, FAR)):
    """A long resident presence, then a tail spread over the whole last 5 s — so the net's
    window holds exactly the tail: ``n_resident_window`` Mittens frames, then foreign."""
    n = n_resident_window + n_foreign
    lead = frames_at([(MITTENS, NEAR)] * 100, step=200)
    step = 4_990 // (n - 1)
    tail = frames_at(
        [(MITTENS, NEAR)] * n_resident_window + [foreign] * n_foreign,
        start=lead[-1][1] + 200, step=step, first_id=1_000,
    )
    assert tail[-1][1] - tail[0][1] <= 5_000 < tail[-1][1] - lead[-1][1]
    return lead + tail


def test_takeover_fires_at_ninety_percent_of_twenty():
    fr = _tailgate(2, 18)                                  # exactly 0.9, 20 frames
    d = decide(fr)
    assert d is not None and d.trigger == TRIGGER_TAKEOVER
    assert (d.verdict, d.cat_id, d.n_counted, d.n_votes) == ("unknown", None, 20, 18)
    assert d.distance == FAR and (d.frame_id, d.frame_recv_ts) == fr[-1][:2]


def test_takeover_below_the_share_or_the_count_does_not_fire():
    assert decide(_tailgate(3, 17)) is None                # 0.85
    # 19 foreign frames in a fresh presence: never the net, only the held presence vote.
    lead = frames_at([(MITTENS, NEAR)] * 100, step=200)
    few = frames_at([(REX, FAR)] * 19, start=lead[-1][1] + 5_100, step=100, first_id=1_000)
    d = decide(lead + few, now=few[-1][1] + 200)
    assert d is not None and d.trigger == TRIGGER_PRESENCE


def test_takeover_window_is_the_five_seconds_up_to_the_newest_frame():
    # 19 foreign frames in the last 5 s, a 20th just outside it: no takeover. The resident
    # presence before them keeps the presence vote on Mittens.
    lead = frames_at([(MITTENS, NEAR)] * 100, step=200)
    start = lead[-1][1] + 200
    tail = frames_at([(REX, FAR)] * 20, start=start, step=264, first_id=1_000)
    assert tail[-1][1] - tail[0][1] > 5_000
    assert decide(lead + tail) is None


def test_takeover_names_a_neighbour_when_the_window_votes_one():
    d = decide(_tailgate(0, 25, foreign=(TOM, NEAR)))
    assert d is not None and d.trigger == TRIGGER_TAKEOVER
    assert (d.verdict, d.cat_id, d.cat_name, d.n_votes) == ("neighbour", TOM, "Tom", 25)


def test_takeover_never_names_a_resident_as_its_target():
    # 18 strangers beyond the threshold, 2 Mittens within it: the window's vote names
    # Mittens (the only below-threshold cat), yet 90% of it is foreign. The record says
    # unknown, never Mittens.
    fr = _tailgate(2, 18)
    window = fr[-20:]
    agg = Store._aggregate_identity([(c, x) for _f, _t, c, x in window], TH, NAMES, RESIDENTS)
    assert agg["cat_id"] == MITTENS
    d = decide(fr)
    assert d.trigger == TRIGGER_TAKEOVER and (d.verdict, d.cat_id, d.cat_name) == ("unknown", None, None)


def test_a_lone_neighbour_meeting_both_paths_is_recorded_as_presence():
    # 10 fps of Tom: on the tick his 20th frame lands, the net (20, all foreign) and the
    # presence hold (turned foreign at his 10th, a second ago) are BOTH met. The row
    # follows the vote, not the order the paths are checked in.
    fr = frames_at([(TOM, NEAR)] * 20, step=100)
    window_all_foreign = all(deterrent.is_foreign(c, x, TH, RESIDENTS) for _f, _t, c, x in fr)
    assert len(fr) >= deterrent.ALARM_TAKEOVER_MIN_FRAMES and window_all_foreign
    d = decide(fr, caught_up=False)
    assert d is not None and d.trigger == TRIGGER_PRESENCE
    assert (d.verdict, d.cat_id, d.n_counted, d.n_votes) == ("neighbour", TOM, 20, 20)


def test_net_met_before_the_hold_starts_now_but_records_the_presence_vote():
    # Mittens 30 frames over 12 s, then Tom 31 frames spread over the last 4.8 s: the net's
    # window is all Tom (met), and the presence vote turns foreign on Tom's 31st frame — the
    # newest — so its hold has not run. It starts on this tick (when is unchanged), but the
    # vote already calls Tom foreign, so the row is the presence's, not the net's.
    mittens = frames_at([(MITTENS, NEAR)] * 30, step=400)
    tom = frames_at([(TOM, NEAR)] * 31, start=mittens[-1][1] + 400, step=160, first_id=100)
    fr = mittens + tom
    assert prefix_winners([(c, x) for _f, _t, c, x in fr], TH)[-2] == MITTENS  # 30 to 30, hers first
    d = decide(fr, caught_up=False)
    assert d is not None and d.trigger == TRIGGER_PRESENCE
    assert (d.verdict, d.cat_id, d.n_counted, d.n_votes) == ("neighbour", TOM, 61, 31)
    # One frame earlier the vote is 30 to 30 and Mittens wins the tie (first below the
    # threshold), while the net's window is already all Tom: there the net overrides the
    # vote, and only there does the row read `takeover`, over the window.
    d = decide(fr[:-1], caught_up=False)
    assert d is not None and d.trigger == TRIGGER_TAKEOVER
    assert (d.verdict, d.cat_id, d.n_counted, d.n_votes) == ("neighbour", TOM, 30, 30)


def test_takeover_is_also_stale_checked():
    fr = _tailgate(0, 25)
    assert decide(fr, now=fr[-1][1] + 5_001, caught_up=True) is None


def test_constants_are_the_spec_values():
    assert deterrent.ALARM_PRESENCE_GAP_S == deterrent.DETER_WINDOW_S == 5.0
    assert deterrent.ALARM_PRESENCE_MAX_S == 120.0
    assert deterrent.ALARM_HOLD_S == 1.0
    assert (deterrent.ALARM_TAKEOVER_MIN_FRAMES, deterrent.ALARM_TAKEOVER_SHARE) == (20, 0.9)
    assert deterrent.DETER_MIN_FRAMES == 10
