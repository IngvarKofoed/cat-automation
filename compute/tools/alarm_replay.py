"""Replay the foreign-cat alarm's start rule over stored frames — read-only.

Run on the compute PC. A missed stranger writes no ``actions`` row, so "the new rule adds
no misses" is invisible in the alarm log: this replay is the only way to check it, now and
whenever the start rule's numbers are retuned (docs/specs/2026-11-05-alarm-presence-vote.md,
docs/specs/2026-11-10-one-visit-for-feed-and-alarm.md).

It replays two rules over one continuous timeline of COUNTED frames (``Store.alarm_frames``
under the active model) at 1 s ticks, at two tick phases, with no processing delay:

- **today** — the shipped presence rule, frozen here as the baseline: the counted frames
  after the last gap over 5 s, reaching back 2 minutes, voted foreign by
  ``Store._aggregate_identity`` and held 1 s of camera time, plus the takeover net;
- **new** — ``deterrent.start_decision``, the very function the live worker calls: the
  same vote over the household VISIT so far (bridged, as the feed's), held
  ``ALARM_HOLD_S``.

Visits are the feed's, bridged across the WHOLE range regardless of the saved bridge start
date (``bridge_since_ms=0``): the replay simulates and stores nothing, so there is no past
to protect. Both rules run through the same episode mechanics: the gone rule (no foreign
counted frame, by ``deterrent.is_foreign``, for ``ALARM_GONE_S``) and the floor at each
episode's end. Each start goes to the feed visit whose ``[start_ts, end_ts + _VISIT_GAP_MS]``
holds its deciding frame — the feed's own attribution rule — and counts as alarming that visit
only if it was decided at or before the visit's ``end_ts``: a start after the cat left is no
catch, and is listed as late. "Ours" is the visit's CARD (its whole-visit vote), not a label.
A visit under way at the range's first midnight is reported whole, from its true start.

It reports what the rules decide, not when the pipeline would have heard it: processing
delay is out of scope by design, so neither the frontier nor the gone check's measuring to
it can show here.

It calls no ``Store`` method that writes (the reads are ``active_model``, ``list_cats``,
``resolve_ts_range``, ``_walk_events``, ``alarm_frames`` and ``alarm_actions_between``), as
``diagnose_misses`` does. Like every tool that builds a ``Store``, though, CONSTRUCTING it
runs the additive schema migration: on a store this build has not opened yet (run before the
server restarts onto it) that adds ``actions.trigger``, which older code ignores. The
guarantee is "touches no row", not a DB-enforced read-only.

Usage (from the repo root, in the compute venv):
    python -m compute.tools.alarm_replay --since 2026-10-25 --until 2026-11-05
    CAT_COLLECT_DIR=D:/cat/data python -m compute.tools.alarm_replay --since 2026-11-01 --until 2026-11-01

Both dates are inclusive LOCAL days (the compute PC's own calendar): the range runs from
00:00 on ``--since`` to the end of ``--until``. It refuses, with a non-zero exit, when the
range holds no visits, or visits but no counted frames (a promotion after the range leaves
it with no identifications under the active model; eviction removes old frames), or when
the feed walk comes back truncated — never an empty "no added misses".
"""
from __future__ import annotations

import argparse
import bisect
import math
import os
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from compute.collection.store import Store, _VISIT_GAP_MS
from compute.learning import deterrent
from compute.learning.deterrent import (
    TRIGGER_PRESENCE,
    TRIGGER_TAKEOVER,
    StartDecision,
    is_foreign,
    prefix_winners,
    start_decision,
)
from compute.tools.diagnose_misses import _build_store, _resolve_root

RULES = ("today", "new")
PHASES_MS = (0, 500)
_TICK_MS = 1_000
# Feed pages per day of walk. `_walk_events` walks from the range's first frame to NOW (it
# has no upper bound), at `_MAX_EVENTS` visits a page; 20 a day allows 10,000 door events a
# day, far beyond any day seen. A budget that is still spent is a refusal, not a partial.
_PAGES_PER_DAY = 20
_DAY_MS = 86_400_000


class ReplayRefused(Exception):
    """The range cannot be replayed honestly; the message says why."""


@dataclass
class Visit:
    start_id: int
    end_id: int
    start_ts: int
    end_ts: int
    card: str                     # 'ours' | 'neighbour' | 'unknown' | 'unidentified'
    name: "str | None"
    votes: "tuple[int, int] | None"   # (winner's below-threshold votes, identified frames)
    has_counted: bool = False


@dataclass
class Start:
    rule: str
    phase_ms: int
    at_ms: int                    # the tick that decided it
    decision: StartDecision
    visit: "int | None" = None    # index into the visit list, or None (unclaimed)
    in_view: bool = False


@dataclass
class Report:
    model_id: int
    threshold: float
    since: date
    until: date
    range_ms: "tuple[int, int]"
    visits: "list[Visit]" = field(default_factory=list)
    n_counted: int = 0
    starts: "list[Start]" = field(default_factory=list)
    visit_longest_ms: int = 0
    visits_over_cap: int = 0
    real_actions: "list[dict]" = field(default_factory=list)
    days: "list[dict]" = field(default_factory=list)
    names: dict = field(default_factory=dict)
    # `alarmed` per (rule, phase), built on first use: the printer asks for each many times,
    # and `starts` is complete before anything reads it.
    _alarmed: dict = field(default_factory=dict, repr=False)

    def starts_for(self, rule: str, phase_ms: int) -> "list[Start]":
        return [s for s in self.starts if s.rule == rule and s.phase_ms == phase_ms]

    def alarmed(self, rule: str, phase_ms: int) -> "dict[int, Start]":
        """Visit index -> the FIRST start that alarmed it while in view."""
        key = (rule, phase_ms)
        if key not in self._alarmed:
            out: "dict[int, Start]" = {}
            for s in self.starts_for(rule, phase_ms):
                if s.visit is not None and s.in_view and s.visit not in out:
                    out[s.visit] = s
            self._alarmed[key] = out
        return self._alarmed[key]

    def added_misses(self, phase_ms: int) -> "list[int]":
        """Visits not named as ours that today's rule alarms in view and the new one does not."""
        base = self.alarmed("today", phase_ms)
        new = self.alarmed("new", phase_ms)
        return sorted(
            i for i in base if self.visits[i].card != "ours" and i not in new
        )


# --- the frozen baseline ------------------------------------------------------------


# The shipped presence rule's numbers (alarm-presence-vote spec), frozen HERE rather than
# read from `deterrent`, so retuning the live rule can never move its own baseline.
_BASE_WINDOW_MS = 5_000           # takeover window and staleness
_BASE_PRESENCE_GAP_MS = 5_000     # a presence splits on a counted-frame gap over this
_BASE_MAX_MS = 120_000            # how far back a presence reaches
_BASE_HOLD_MS = 1_000             # the hold, camera time
_BASE_MIN_FRAMES = 10
_BASE_TAKEOVER_MIN_FRAMES = 20
_BASE_TAKEOVER_SHARE = 0.9


def baseline_decision(
    frames: "list[tuple[int, int, int, float]]",
    now_ms: int,
    floor_ms: "int | None",
    frontier_ms: "int | None",
    threshold: "float | None",
    names: dict,
    residents: dict,
    *,
    visit_start_ts: "int | None" = None,
) -> "StartDecision | None":
    """The shipped presence rule (1 s hold, 5 s presence), frozen as the baseline.

    ``deterrent.start_decision`` as it stood before the one-visit-for-feed-and-alarm spec,
    copied with its constants: the presence is the counted frames after the last gap over
    5 s, reaching back 2 minutes and floored at the previous alarm's end; its vote must
    have stayed foreign 1 s of camera time to the frontier; the takeover net starts at once
    on ≥ 20 frames in the last 5 s, ≥ 90% foreign. ``visit_start_ts`` is accepted for a
    common signature and unused — this rule knew nothing of visits.
    """
    del visit_start_ts
    if threshold is None or not frames:
        return None
    lo = int(now_ms) - _BASE_MAX_MS
    if floor_ms is not None:
        lo = max(lo, int(floor_ms))
    frames = [f for f in frames if lo <= f[1] <= now_ms]
    if not frames:
        return None
    newest = frames[-1][1]
    eff = newest if frontier_ms is None else max(int(frontier_ms), newest)
    if eff - newest > _BASE_WINDOW_MS:
        return None

    i = len(frames)
    while i > 0 and frames[i - 1][1] >= newest - _BASE_WINDOW_MS:
        i -= 1
    window = frames[i:]
    foreign: "list[tuple[int, int, int, float]]" = []
    takeover_met = False
    if len(window) >= _BASE_TAKEOVER_MIN_FRAMES:
        foreign = [f for f in window if is_foreign(f[2], f[3], threshold, residents)]
        takeover_met = len(foreign) / len(window) >= _BASE_TAKEOVER_SHARE

    p = len(frames) - 1
    while p > 0 and frames[p][1] - frames[p - 1][1] <= _BASE_PRESENCE_GAP_MS:
        p -= 1
    presence = frames[p:]
    idents = [(cid, dist) for _f, _t, cid, dist in presence]
    run_start: "int | None" = None
    if len(presence) >= _BASE_MIN_FRAMES:
        for k, w in enumerate(prefix_winners(idents, threshold)):
            if k + 1 >= _BASE_MIN_FRAMES and (w is None or not residents.get(w, False)):
                if run_start is None:
                    run_start = k
            else:
                run_start = None
    vote_foreign = run_start is not None
    hold_met = vote_foreign and eff - presence[run_start][1] >= _BASE_HOLD_MS

    if not (takeover_met or hold_met):
        return None
    frame_id, frame_ts, _c, _d = frames[-1]
    if vote_foreign:
        agg = Store._aggregate_identity(idents, threshold, names, residents)
        if agg is not None and not (agg["cat_id"] is not None and agg["is_resident"]):
            return StartDecision(
                TRIGGER_PRESENCE,
                "unknown" if agg["cat_id"] is None else "neighbour",
                agg["cat_id"], agg["cat_name"],
                len(presence), agg["n_frames_voted"], agg["distance"], frame_id, frame_ts,
            )
    if not takeover_met:
        return None
    agg = Store._aggregate_identity(
        [(cid, dist) for _f, _t, cid, dist in window], threshold, names, residents
    )
    if agg is not None and agg["cat_id"] is not None and not agg["is_resident"]:
        verdict, cat_id, distance = "neighbour", agg["cat_id"], agg["distance"]
    else:
        verdict, cat_id = "unknown", None
        distance = min(f[3] for f in foreign)
    return StartDecision(
        TRIGGER_TAKEOVER, verdict, cat_id, names.get(cat_id) if cat_id is not None else None,
        len(window), len(foreign), distance, frame_id, frame_ts,
    )


_DECIDERS = {"today": baseline_decision, "new": start_decision}
_LOOKBACK_MS = {"today": _BASE_MAX_MS, "new": int(deterrent.ALARM_PRESENCE_MAX_S * 1000)}


def _vote_floor(rule: str, t: int, floor: "int | None") -> int:
    """The oldest ``recv_ts`` ``rule`` may vote on at tick ``t`` — each rule's own bound."""
    if rule == "new":
        return deterrent.vote_floor(t, floor)
    lo = t - _LOOKBACK_MS[rule]
    return lo if floor is None else max(lo, floor)


def visit_start_at(visit_starts: "list[int]", ts: int) -> "int | None":
    """The start of the visit holding a frame at ``ts``: the latest visit starting at or
    before it (``Store.visit_start``'s rule, over the replay's bridged visits), or ``None``
    when no visit starts that early."""
    k = bisect.bisect_right(visit_starts, ts) - 1
    return visit_starts[k] if k >= 0 else None


# --- the replay ---------------------------------------------------------------------


def _local_midnight_ms(d: date) -> int:
    """Local midnight of ``d`` on the compute PC's own clock (``Store._local_days``' rule)."""
    return int(datetime(d.year, d.month, d.day).timestamp() * 1000)


def _card(identity: "dict | None") -> "tuple[str, str | None, tuple[int, int] | None]":
    if identity is None:
        return "unidentified", None, None
    votes = (int(identity.get("n_frames_voted") or 0), int(identity.get("n_identified") or 0))
    if identity.get("cat_id") is None:
        return "unknown", None, votes
    return ("ours" if identity.get("is_resident") else "neighbour"), identity.get("cat_name"), votes


def simulate(
    frames: "list[tuple[int, int, int, float]]",
    rule: str,
    phase_ms: int,
    t_first: int,
    t_last: int,
    threshold: float,
    names: dict,
    residents: dict,
    visit_starts: "list[int]",
) -> "list[tuple[int, StartDecision]]":
    """Drive one rule at one phase over the timeline → ``[(decided_at_ms, decision)]``.

    1 s ticks from ``t_first + phase_ms`` to ``t_last``, no processing delay (every frame
    received by a tick is known at it, so the frontier is always the tick). A running episode
    ends at the first tick with no foreign counted frame in the last ``ALARM_GONE_S``, and
    the next vote is floored at that tick + 1 ms, as ``Alarm._end`` and ``maybe_start`` do.
    ``visit_starts`` are the bridged visits' ``start_ts``, ascending: the new rule votes over
    the visit holding the newest counted frame (``visit_start_at``). Idle stretches are
    skipped: neither rule can start at a tick with no counted frame in the
    ``DETER_WINDOW_S`` before it (both are stale there).
    """
    decide = _DECIDERS[rule]
    gone_ms = int(deterrent.ALARM_GONE_S * 1000)
    window_ms = int(deterrent.DETER_WINDOW_S * 1000)
    ts = [f[1] for f in frames]
    out: "list[tuple[int, StartDecision]]" = []
    running = False
    last_end: "int | None" = None
    t = t_first + phase_ms
    while t <= t_last:
        hi = bisect.bisect_right(ts, t)
        if running:
            lo = bisect.bisect_left(ts, t - gone_ms)
            if not any(is_foreign(f[2], f[3], threshold, residents) for f in frames[lo:hi]):
                running = False
                last_end = t
        if not running:
            floor = None if last_end is None else last_end + 1
            lo = bisect.bisect_left(ts, _vote_floor(rule, t, floor))
            if hi - lo >= deterrent.DETER_MIN_FRAMES:
                d = decide(
                    frames[lo:hi], t, floor, t, threshold, names, residents,
                    visit_start_ts=visit_start_at(visit_starts, ts[hi - 1]),
                )
                if d is not None:
                    out.append((t, d))
                    running = True
        if not running and (hi == 0 or ts[hi - 1] < t - window_ms):
            if hi >= len(ts):
                break
            # Jump to the first tick at or after the next frame.
            k = max(1, math.ceil((ts[hi] - t) / _TICK_MS))
            t += k * _TICK_MS
        else:
            t += _TICK_MS
    return out


def _visit_lengths(visits: "list[Visit]") -> "tuple[int, int]":
    """``(longest visit ms, visits longer than ALARM_PRESENCE_MAX_S)`` — the bridged visits
    the new rule votes over, and how many outlast its two-minute lookback (the vote slides)."""
    cap = int(deterrent.ALARM_PRESENCE_MAX_S * 1000)
    lengths = [v.end_ts - v.start_ts for v in visits]
    return max(lengths, default=0), sum(1 for n in lengths if n > cap)


def replay(store: Store, since: date, until: date, *, now_ms: "int | None" = None) -> Report:
    """The replay over the local days ``since``..``until`` inclusive. Raises ``ReplayRefused``."""
    if until < since:
        raise ReplayRefused(f"--until {until} is before --since {since}")
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    model = store.active_model()
    if model is None:
        raise ReplayRefused("no gallery is promoted — there is nothing to identify frames against")
    if model.get("threshold") is None:
        raise ReplayRefused(
            f"model {model['id']} is uncalibrated (no threshold) — neither rule can start an alarm on it"
        )
    threshold = float(model["threshold"])
    start_ms = _local_midnight_ms(since)
    end_ms = _local_midnight_ms(until + timedelta(days=1))  # exclusive

    since_id, until_id = store.resolve_ts_range(start_ms, end_ms - 1)
    if since_id is None or until_id is None or since_id > until_id:
        raise ReplayRefused(f"the store holds no frames from {since} to {until}")

    # The frames read reach past both edges of the range: back by the vote's two-minute
    # bound, so a visit under way at the first midnight is voted whole (and the episode state
    # it leaves carries in), and on past the last midnight to the end of the last visit that
    # STARTED in the range, so its tail is not cut off. Starts decided before the range
    # still shape that state but are not reported. The feed walk starts at the same point,
    # so the new rule knows where such a visit began.
    lookback_ms = max(_LOOKBACK_MS.values())
    read_from = start_ms - lookback_ms
    walk_from, _ = store.resolve_ts_range(read_from, end_ms - 1)
    days_walked = max(1, math.ceil((max(now_ms, end_ms) - read_from) / _DAY_MS))
    events, truncated = store._walk_events(
        walk_from, max_pages=_PAGES_PER_DAY * days_walked, with_subject=False, bridge_since_ms=0
    )
    if truncated:
        raise ReplayRefused(
            "the feed walk came back truncated — a report on part of the range would read as the whole"
        )
    visit_starts = sorted(e["start_ts"] for e in events)
    visits: "list[Visit]" = []
    for e in events:
        # Reported: every visit in the range, including one under way at its first midnight
        # (whole, from its true start). The walk runs on to now; later visits are dropped.
        if not (e["start_ts"] < end_ms and e["end_ts"] >= start_ms):
            continue
        card, name, votes = _card(e.get("identity"))
        visits.append(Visit(e["start_id"], e["end_id"], e["start_ts"], e["end_ts"], card, name, votes))
    visits.sort(key=lambda v: (v.start_ts, v.start_id))
    if not visits:
        raise ReplayRefused(f"the store holds frames but no visits from {since} to {until}")
    read_until = max(end_ms - 1, max(v.end_ts for v in visits) + _VISIT_GAP_MS)
    frames = store.alarm_frames(model["id"], read_from, until_ts=read_until)
    n_in_range = sum(1 for f in frames if start_ms <= f[1] < end_ms)
    if not n_in_range:
        raise ReplayRefused(
            f"{len(visits)} visits from {since} to {until}, but no counted frames under model "
            f"{model['id']} — it was promoted after the range (re-run Identify all) or the "
            "frames have aged out"
        )

    report = Report(model["id"], threshold, since, until, (start_ms, end_ms - 1))
    report.visits = visits
    report.n_counted = n_in_range

    counted_ids = sorted(f[0] for f in frames)
    for v in visits:
        k = bisect.bisect_left(counted_ids, v.start_id)
        v.has_counted = k < len(counted_ids) and counted_ids[k] <= v.end_id

    cats = store.list_cats()
    names = {c["id"]: c["name"] for c in cats}
    residents = {c["id"]: c["is_resident"] for c in cats}
    report.names = names
    reported_starts = [v.start_ts for v in visits]
    # Run past the last frame read long enough for it to decide and episodes to end.
    t_last = read_until + int(max(deterrent.DETER_WINDOW_S, deterrent.ALARM_GONE_S) * 1000) + _TICK_MS
    for rule in RULES:
        for phase in PHASES_MS:
            # Ticks keep the range's phase alignment while starting a lookback early. The
            # simulation sees EVERY walked visit's start (those before the range too).
            for at, d in simulate(
                frames, rule, phase, read_from, t_last, threshold, names, residents, visit_starts
            ):
                if at < start_ms:
                    continue
                s = Start(rule, phase, at, d)
                i = bisect.bisect_right(reported_starts, d.frame_recv_ts) - 1
                if i >= 0 and d.frame_recv_ts <= visits[i].end_ts + _VISIT_GAP_MS:
                    s.visit = i
                    s.in_view = at <= visits[i].end_ts
                report.starts.append(s)

    report.visit_longest_ms, report.visits_over_cap = _visit_lengths(visits)
    report.real_actions = store.alarm_actions_between(start_ms, end_ms - 1)

    counted_by_day = Counter(Store.local_date(f[1]) for f in frames)
    day = since
    while day <= until:
        key = day.isoformat()
        # A visit under way at the first midnight counts on the first day.
        day_visits = [v for v in visits if Store.local_date(max(v.start_ts, start_ms)) == key]
        unclaimed = {}
        for rule in RULES:
            for phase in PHASES_MS:
                unclaimed[(rule, phase)] = sum(
                    1 for s in report.starts_for(rule, phase)
                    if s.visit is None and Store.local_date(s.decision.frame_recv_ts) == key
                )
        report.days.append({
            "date": key,
            "visits": len(day_visits),
            "with_counted": sum(1 for v in day_visits if v.has_counted),
            "counted_frames": counted_by_day.get(key, 0),
            "unclaimed": unclaimed,
        })
        day += timedelta(days=1)
    return report


# --- printing -----------------------------------------------------------------------


def _clock(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000.0).strftime("%Y-%m-%d %H:%M:%S")


def _pct(values: "list[float]", q: float) -> "float | None":
    """The ``q`` quantile by linear interpolation (``statistics.quantiles``' inclusive rule)."""
    if not values:
        return None
    v = sorted(values)
    pos = (len(v) - 1) * q
    lo = math.floor(pos)
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (pos - lo)


def _fmt_s(x: "float | None") -> str:
    return "—" if x is None else f"{x:.1f} s"


def _phase(p: int) -> str:
    return f"phase {p / 1000:.1f} s"


def _alarm_desc(s: Start) -> str:
    d = s.decision
    who = "unknown cat" if d.verdict == "unknown" else (d.cat_name or f"cat #{d.cat_id}")
    trig = f" [{d.trigger}]"
    return f"{who}, {d.n_votes}/{d.n_counted} votes{trig}"


def delays(report: Report, rule: str, phase: int, *, foreign_only: bool) -> "list[float]":
    out = []
    for i, s in report.alarmed(rule, phase).items():
        if foreign_only and report.visits[i].card not in ("neighbour", "unknown"):
            continue
        out.append((s.at_ms - report.visits[i].start_ts) / 1000.0)
    return out


def print_report(report: Report) -> None:
    v = report.visits
    print(f"Model {report.model_id}, threshold {report.threshold:.3f}")
    print(f"Range {report.since} 00:00 to the end of {report.until} (local days), "
          f"{len(v)} visits, {report.n_counted} counted frames")
    print()
    print("Per day                visits  with counted  counted frames   unclaimed starts "
          "(today p0/p0.5, new p0/p0.5)")
    for d in report.days:
        u = d["unclaimed"]
        print(f"  {d['date']}         {d['visits']:6d}  {d['with_counted']:12d}  {d['counted_frames']:14d}"
              f"   {u[('today', 0)]}/{u[('today', 500)]}, {u[('new', 0)]}/{u[('new', 500)]}")
    print()

    ours = [i for i, x in enumerate(v) if x.card == "ours"]
    foreign = [i for i, x in enumerate(v) if x.card in ("neighbour", "unknown")]
    print(f"Visits named as ours: {len(ours)}; neighbour / unknown-cat visits: {len(foreign)}")
    print("                          alarms on ours   neighbour/unknown alarmed while in view")
    for rule in RULES:
        for phase in PHASES_MS:
            a = report.alarmed(rule, phase)
            print(f"  {rule:5s} {_phase(phase):14s} {sum(1 for i in ours if i in a):10d}"
                  f"   {sum(1 for i in foreign if i in a):10d} of {len(foreign)}")
    print()

    # What the new rule's rows would read: `takeover` only where the net overrode a vote that
    # did not call the cat foreign, so a lone neighbour should read `presence`.
    print("New rule's recorded trigger, by the card of the visit it went to (presence / takeover):")
    for phase in PHASES_MS:
        tally: "dict[str, list[int]]" = {}
        for st in report.starts_for("new", phase):
            card = "unclaimed" if st.visit is None else v[st.visit].card
            row = tally.setdefault(card, [0, 0])
            row[0 if st.decision.trigger == deterrent.TRIGGER_PRESENCE else 1] += 1
        parts = [f"{card} {p_}/{t_}" for card, (p_, t_) in sorted(tally.items())]
        print(f"  {_phase(phase)}: " + ("; ".join(parts) if parts else "no starts"))
    print()

    print("Added misses (not named as ours: today's rule alarms in view, the new one does not):")
    for phase in PHASES_MS:
        misses = report.added_misses(phase)
        if not misses:
            print(f"  {_phase(phase)}: none")
            continue
        for i in misses:
            x = v[i]
            late = [s for s in report.starts_for("new", phase) if s.visit == i]
            print(f"  {_phase(phase)}: {_clock(x.start_ts)}  {x.card} {x.name or ''}".rstrip()
                  + ("  (new rule started late)" if late else "")
                  + "  — check it in the player: its vote may have begun on an earlier card")
    print()

    print("Late starts (decided after the visit's last frame):")
    any_late = False
    for rule in RULES:
        for phase in PHASES_MS:
            for s in report.starts_for(rule, phase):
                if s.visit is not None and not s.in_view:
                    any_late = True
                    x = v[s.visit]
                    print(f"  {rule:5s} {_phase(phase)}: visit {_clock(x.start_ts)} ({x.card}"
                          f"{' ' + x.name if x.name else ''}), decided {(s.at_ms - x.end_ts) / 1000:.1f} s "
                          f"after its end — {_alarm_desc(s)}")
    if not any_late:
        print("  none")
    print()

    print("Decision delay (start minus visit start), median / 90th percentile:")
    for rule in RULES:
        for phase in PHASES_MS:
            f = delays(report, rule, phase, foreign_only=True)
            a = delays(report, rule, phase, foreign_only=False)
            print(f"  {rule:5s} {_phase(phase):14s} neighbour/unknown visits: {_fmt_s(_pct(f, 0.5))} / "
                  f"{_fmt_s(_pct(f, 0.9))} (n={len(f)});  all alarmed: {_fmt_s(_pct(a, 0.5))} / "
                  f"{_fmt_s(_pct(a, 0.9))} (n={len(a)})")
    print()

    cap_s = deterrent.ALARM_PRESENCE_MAX_S
    print(f"Longest visit: {report.visit_longest_ms / 1000:.1f} s; visits longer than the new rule's "
          f"{cap_s:.0f} s lookback: {report.visits_over_cap}")
    print()

    print("Visits named as ours that set an alarm off (check each in the player):")
    flagged = sorted({i for rule in RULES for p in PHASES_MS for i in report.alarmed(rule, p) if v[i].card == "ours"})
    if not flagged:
        print("  none")
    for i in flagged:
        x = v[i]
        votes = f"{x.votes[0]} of {x.votes[1]}" if x.votes else "—"
        print(f"  {_clock(x.start_ts)}  {x.name} ({votes} votes on the card)")
        for rule in RULES:
            parts = []
            for p in PHASES_MS:
                s = report.alarmed(rule, p).get(i)
                if s is not None:
                    parts.append(f"{p / 1000:.1f}: {_alarm_desc(s)}")
            print(f"      {rule:5s} " + ("; ".join(parts) if parts else "—"))
    print()

    print("Real alarm rows in the range beside today's rule's starts (phase 0.0 s). The replay")
    print("has no processing delay, so these will not match one for one; large differences mean")
    print("the replay does not reflect what the door did.")
    real = report.real_actions
    if not real:
        print("  no real alarm rows in the range")
        return
    first = real[0]["start_ts"]
    lines = [(r["start_ts"], "real ", r) for r in real]
    lines += [(s.at_ms, "today", s) for s in report.starts_for("today", 0) if s.at_ms >= first - 60_000]
    lines.sort(key=lambda x: x[0])
    for at, kind, item in lines:
        if kind == "real ":
            who = "unknown cat" if item["verdict"] == "unknown" else (
                report.names.get(item["cat_id"]) or f"cat #{item['cat_id']}"
            )
            trig = f" [{item['trigger']}]" if item.get("trigger") else ""
            print(f"  {_clock(at)}  real   {who}{trig}, {item['n_votes']}/{item['n_counted']} votes")
        else:
            print(f"  {_clock(at)}  today  {_alarm_desc(item)}")
    print(f"  {len(real)} real rows; {sum(1 for x in lines if x[1] == 'today')} starts by today's rule from "
          f"a minute before the first real row")


def _date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a date (YYYY-MM-DD): {text!r}") from exc


def main(argv: "list[str] | None" = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", type=_date, required=True, help="first local day, YYYY-MM-DD (inclusive)")
    ap.add_argument("--until", type=_date, required=True, help="last local day, YYYY-MM-DD (inclusive)")
    ap.add_argument("--dir", default=None, help="store root (default: $CAT_COLLECT_DIR or ./data/collection)")
    args = ap.parse_args(argv)
    root = _resolve_root(args.dir)
    store = _build_store(root)
    print(f"DB: {os.path.join(root, 'index.db')}")
    try:
        report = replay(store, args.since, args.until)
    except ReplayRefused as exc:
        raise SystemExit(f"refused: {exc}") from None
    print_report(report)


if __name__ == "__main__":
    main()
