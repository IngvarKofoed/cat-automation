"""The foreign-cat alarm: when to sound, when to stop, and the record of each episode.

The decision half of the system's first actuator (docs/specs/2026-11-01-foreign-cat-alarm.md;
its start rule replaced by docs/specs/2026-11-05-alarm-presence-vote.md). The live-naming
worker (``live_identify.py``) owns the GPU work — it detects and identifies the newest frames
once a second while the switch is On — and drives the ``Alarm`` here once per tick. This
module holds the rules and the episode state:

- **Start** — ``start_decision``, a pure function the worker and the read-only replay tool
  (``compute.tools.alarm_replay``) both call. Every window is measured in CAMERA time, from
  the newest counted frame's ``recv_ts``. Two paths; either one met starts it:

  - *Presence*: ``Store._aggregate_identity`` (the feed's own voter) over everything seen of
    the cat since it appeared — the run of counted frames with no gap over
    ``ALARM_PRESENCE_GAP_S``, reaching back at most ``ALARM_PRESENCE_MAX_S`` — has called
    them foreign (a named non-resident won, or nothing came within the threshold), with at
    least ``DETER_MIN_FRAMES`` frames, for ``ALARM_HOLD_S`` of camera time straight.
  - *Takeover*: in the ``DETER_WINDOW_S`` up to the newest counted frame there are at least
    ``ALARM_TAKEOVER_MIN_FRAMES`` counted frames and at least ``ALARM_TAKEOVER_SHARE`` of
    them are foreign by ``is_foreign``. Starts at once. It catches a stranger following one
    of our cats into the same presence, whose frames the presence vote hands to the resident.

  The row records the presence vote whenever it calls the cat foreign at that tick, and
  ``takeover`` only when the net started an alarm the vote would not have.

  Neither reaches back past the previous alarm's end (seeded from the log at start-up, so a
  restart cannot recount the last episode's stranger). An uncalibrated gallery (NULL
  threshold) never starts one — there every cat reads *unknown*, so every resident would
  trip it.
- **Gone** — no *foreign* counted frame for ``ALARM_GONE_S``. A frame that matches a
  resident within the threshold never prolongs the alarm, so a resident following a
  stranger in stops the noise.
- **The loop** — the Pi plays one clip per call; this asks for the next (``alarm1``,
  ``alarm2``, alternating) once the previous one is due to end. If the compute dies, the
  clip in progress finishes and the Pi goes quiet: fail-safe by construction.

A *counted* frame is what ``Store.alarm_frames`` returns: a ``yolo-serial`` cat box at or
above the feed's 0.3 floor, with a gallery match for the active model.

Every episode is one ``actions`` row: written when it starts (``trigger`` says whether the net
overrode the vote), its clip counters updated as clips land, completed with how it ended. Test
plays write a ``test`` row the same way, so the log proves the compute → Pi path end to end.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from compute.collection.store import Store

logger = logging.getLogger(__name__)

# The compute's switch, in the settings KV. Absent (a fresh install) reads Off: the alarm
# is an explicit opt-in. It is the ONLY compute-side gate — it applies in every stage.
DETERRENT_MODE_KEY = "deterrent_mode"
MODE_OFF = "off"
MODE_ON = "on"
MODES = (MODE_OFF, MODE_ON)

# The fewest counted frames a presence vote may start on.
DETER_MIN_FRAMES = 10
# The takeover window, the presence gap, and how long a caught-up step waits for a frame
# before it reads the cat as absent (the staleness check). Camera time.
DETER_WINDOW_S = 5.0
# A presence is the run of counted frames with no gap over this between consecutive ones —
# not the feed's 2 s visit, so it remembers at least as far back as the old 5 s window did.
ALARM_PRESENCE_GAP_S = DETER_WINDOW_S
# How far back a presence reaches. Bounds the read; past it the vote slides.
ALARM_PRESENCE_MAX_S = 120.0
# How long, in camera time, the presence vote must have stayed foreign before it starts.
ALARM_HOLD_S = 1.0
# The takeover net: at least this many counted frames in the last DETER_WINDOW_S, and at
# least this share of them foreign.
ALARM_TAKEOVER_MIN_FRAMES = 20
ALARM_TAKEOVER_SHARE = 0.9
# What an alarm's row records (`actions.trigger`): `takeover` only when the net started an
# alarm the presence vote did not call foreign; every other start reads `presence`.
TRIGGER_PRESENCE = "presence"
TRIGGER_TAKEOVER = "takeover"
# Most frames one alarm pass detects + identifies. Bounds a pass's GPU hold so the
# bookkeeping (the "gone" check, the next clip) runs about once a second.
DETER_MAX_FRAMES = 30
# The stop rule: no foreign counted frame for this long.
ALARM_GONE_S = 3.0
# The worker's tick period while the switch is On (it keeps its slower default while Off).
ALARM_TICK_S = 1.0
# Margin past a clip's reported duration before asking for the next one. `aplay` outlives
# `duration_s` slightly, so asking at exactly the end would mostly earn a 409.
CLIP_MARGIN_S = 0.3
# The playlist, alternated. alarm1 first.
CLIPS = ("alarm1", "alarm2")

# How an episode ended. `interrupted` is written only by the launch-time sweep
# (`Store.close_open_actions`), for a row a restart left open.
ENDED_GONE = "gone"
ENDED_SWITCHED_OFF = "switched_off"
ENDED_UNCALIBRATED = "uncalibrated"
ENDED_ERROR = "error"
ENDED_INTERRUPTED = "interrupted"


def get_mode(store) -> str:
    """The persisted switch; anything but an explicit ``on`` reads Off."""
    return MODE_ON if store.get_setting(DETERRENT_MODE_KEY) == MODE_ON else MODE_OFF


def set_mode(store, mode: str) -> None:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {list(MODES)}")
    store.set_setting(DETERRENT_MODE_KEY, mode)


def is_calibrated(model: "dict | None") -> bool:
    """An active gallery with a threshold — the only state in which an alarm can start."""
    return model is not None and model.get("threshold") is not None


@dataclass(frozen=True)
class PlayOutcome:
    """What one clip request came to.

    ``played`` (200, ``duration_s`` set), ``busy`` (409 — the previous clip is still
    sounding; routine, never a failure), ``disabled`` (423 — the Pi's own switch is off),
    or ``failed`` (unreachable, 5xx, anything else; ``error`` says what).
    """

    status: str
    duration_s: "float | None" = None
    error: "str | None" = None


def play_clip(sound, name: str) -> PlayOutcome:
    """Ask the Pi to play ``name`` through ``sound`` (an ``EdgeClient``) and classify the answer.

    Never raises: an unreachable Pi is a ``failed`` outcome the caller counts and retries.
    """
    if sound is None or not hasattr(sound, "play_sound"):
        return PlayOutcome("failed", error="no Pi connection configured (CAT_PI_URL)")
    try:
        status, body = sound.play_sound(name)
    except Exception as exc:  # noqa: BLE001 - EdgeUnavailable and anything transport-shaped
        return PlayOutcome("failed", error=str(exc) or exc.__class__.__name__)
    body = body if isinstance(body, dict) else {}
    if status == 200:
        try:
            duration = float(body["duration_s"])
        except (KeyError, TypeError, ValueError):
            return PlayOutcome("failed", error="Pi answered 200 without a duration")
        return PlayOutcome("played", duration_s=duration)
    if status == 409:
        return PlayOutcome("busy")
    if status == 423:
        return PlayOutcome("disabled", error="sound is disabled on the Pi")
    detail = body.get("error") or "no detail"
    return PlayOutcome("failed", error=f"Pi answered {status}: {detail}")


def is_foreign(cat_id: int, distance: float, threshold: "float | None", residents: dict) -> bool:
    """A counted frame keeps the alarm going unless it matches a RESIDENT within the threshold.

    Beyond the threshold it is an unknown cat (foreign); within it, the matched cat's
    roster flag decides. A cat id missing from the roster reads non-resident, as
    ``_aggregate_identity`` reads it.
    """
    if threshold is None or distance > threshold:
        return True
    return not residents.get(cat_id, False)


@dataclass(frozen=True)
class StartDecision:
    """A start, as ``start_decision`` returns it: the recorded trigger, verdict and evidence.

    ``trigger`` is ``presence`` whenever the presence vote called the cat foreign at the
    deciding tick, ``takeover`` only when the net overrode it. ``n_counted``, ``n_votes``
    and ``distance`` are read over the frames the recorded verdict came from — the
    presence, or the takeover window. ``n_votes`` is the winner's below-threshold count for
    a presence row and the foreign frames for a takeover. The deciding frame is the newest
    frame in that set.
    """

    trigger: str
    verdict: str            # 'neighbour' | 'unknown'
    cat_id: "int | None"
    cat_name: "str | None"
    n_counted: int
    n_votes: int
    distance: float
    frame_id: int
    frame_recv_ts: int


def prefix_winners(idents: "list[tuple[int, float]]", threshold: float) -> "list[int | None]":
    """``Store._aggregate_identity``'s winner over every prefix of ``idents``, in one pass.

    ``idents`` is ``(cat_id, distance)`` oldest first; entry ``k`` of the result is the cat
    the voter names over ``idents[:k + 1]`` — ``None`` where nothing is within ``threshold``
    (an unknown cat). The tallies reproduce the voter's rules exactly: a plurality among
    below-threshold frames, ties broken by the cat's minimum distance, and a tie on both
    going to the cat that FIRST came below the threshold (``min`` over the voter's
    insertion-ordered dict returns the first minimal key). Only the arriving frame's cat
    changes its key, and only downwards, so the new winner is the old one or that cat.
    """
    tallies: "dict[int, list]" = {}   # cat_id -> [count, min distance, insertion order]
    winner: "int | None" = None
    out: "list[int | None]" = []
    for cid, dist in idents:
        if dist <= threshold:
            t = tallies.get(cid)
            if t is None:
                t = tallies[cid] = [0, dist, len(tallies)]
            t[0] += 1
            if dist < t[1]:
                t[1] = dist
            if winner is None:
                winner = cid
            elif winner != cid:
                w = tallies[winner]
                if (-t[0], t[1], t[2]) < (-w[0], w[1], w[2]):
                    winner = cid
        out.append(winner)
    return out


def vote_floor(now_ms: int, floor_ms: "int | None") -> int:
    """The oldest ``recv_ts`` a start may vote on: ``ALARM_PRESENCE_MAX_S`` back from
    ``now_ms``, but never at or before the previous alarm's end (``floor_ms`` is that end
    + 1, or ``None``). The worker's read, ``start_decision``'s filter and the replay's slice
    all use this one bound, so the three cannot drift apart."""
    lo = int(now_ms) - int(ALARM_PRESENCE_MAX_S * 1000)
    return lo if floor_ms is None else max(lo, int(floor_ms))


def start_decision(
    frames: "list[tuple[int, int, int, float]]",
    now_ms: int,
    floor_ms: "int | None",
    frontier_ms: "int | None",
    threshold: "float | None",
    names: dict,
    residents: dict,
) -> "StartDecision | None":
    """The whole start rule, as a pure function: a start, or ``None``.

    ``frames`` are the counted frames ``(frame_id, recv_ts, cat_id, distance)``, oldest
    first; only those with ``recv_ts`` in ``[vote_floor(now_ms, floor_ms), now_ms]`` are
    considered (``floor_ms`` is the previous alarm's end + 1, or ``None``).

    ``frontier_ms`` is the ``recv_ts`` through which the step has processed every stored
    frame: ``now_ms`` when the last pass caught up, the last processed frame's when it is
    behind, ``None`` when unknown. Every frame still to come lies after it, so it — not a
    caught-up flag — is how far the absence of a counted frame may speak. ``eff`` below is
    ``max(frontier_ms, newest)`` (``newest`` alone when unknown), ``newest`` being the
    newest counted frame's ``recv_ts``. Every window is camera time, so a pass lagging the
    camera never reads its own delay as the cat's behaviour.

    - **Stale**: ``eff − newest > DETER_WINDOW_S`` — nothing still to come can continue the
      presence, so nobody is present. (A ``caught_up`` flag could not say this while behind,
      and a minute-old foreign presence then started an alarm at the next cat to arrive.)

    Then both paths are evaluated, and it starts when EITHER is met:

    - **Takeover**: ≥ ``ALARM_TAKEOVER_MIN_FRAMES`` frames in ``[newest − DETER_WINDOW_S,
      newest]``, ≥ ``ALARM_TAKEOVER_SHARE`` of them foreign by ``is_foreign``. No hold.
    - **Presence**: the frames after the last gap over ``ALARM_PRESENCE_GAP_S`` (from the
      oldest frame considered when no such gap is in range). Its last prefix must be foreign
      (a prefix is foreign at ≥ ``DETER_MIN_FRAMES`` frames with a foreign winner), and
      ``t0`` — the frame ending the SHORTEST prefix of the unbroken run of foreign prefixes
      ending at it, where the vote turned foreign and stayed so — must be ``ALARM_HOLD_S``
      old: ``eff − t0 ≥ hold``. The vote has stayed foreign through every frame processed
      since, so a short visit whose cat already left completes its hold, while a lagging
      pass cannot meet it on frames it has not seen.

    What is RECORDED follows the presence vote, never the order the paths were checked in —
    a lone neighbour at ~10 fps, or any lagging pass, meets both on one tick:

    - the presence's last prefix is foreign → ``presence``, with the verdict and evidence of
      ``_aggregate_identity`` over the presence, even when only the net was met because the
      hold had not yet run out;
    - otherwise the net overrode the vote → ``takeover``, read over the net's window: its
      vote when that names a neighbour, else ``unknown`` — a resident's within-threshold
      matches can still win a window whose foreign frames are mostly beyond it, and the
      record must never name a resident as the alarm's target.
    """
    if threshold is None or not frames:
        return None
    lo = vote_floor(now_ms, floor_ms)
    frames = [f for f in frames if lo <= f[1] <= now_ms]
    if not frames:
        return None
    newest = frames[-1][1]
    eff = newest if frontier_ms is None else max(int(frontier_ms), newest)
    window_ms = int(DETER_WINDOW_S * 1000)
    if eff - newest > window_ms:
        return None

    # The takeover path: the last DETER_WINDOW_S of camera time.
    i = len(frames)
    while i > 0 and frames[i - 1][1] >= newest - window_ms:
        i -= 1
    window = frames[i:]
    foreign: "list[tuple[int, int, int, float]]" = []
    takeover_met = False
    if len(window) >= ALARM_TAKEOVER_MIN_FRAMES:
        foreign = [f for f in window if is_foreign(f[2], f[3], threshold, residents)]
        takeover_met = len(foreign) / len(window) >= ALARM_TAKEOVER_SHARE

    # The presence path: the vote over every prefix of the presence, and its hold.
    gap_ms = int(ALARM_PRESENCE_GAP_S * 1000)
    p = len(frames) - 1
    while p > 0 and frames[p][1] - frames[p - 1][1] <= gap_ms:
        p -= 1
    presence = frames[p:]
    idents = [(cid, dist) for _f, _t, cid, dist in presence]
    run_start: "int | None" = None
    if len(presence) >= DETER_MIN_FRAMES:
        for k, w in enumerate(prefix_winners(idents, threshold)):
            if k + 1 >= DETER_MIN_FRAMES and (w is None or not residents.get(w, False)):
                if run_start is None:
                    run_start = k
            else:
                run_start = None
    vote_foreign = run_start is not None
    hold_met = False
    if vote_foreign:
        t0 = presence[run_start][1]
        hold_ms = int(ALARM_HOLD_S * 1000)
        hold_met = eff - t0 >= hold_ms

    if not (takeover_met or hold_met):
        return None
    frame_id, frame_ts, _c, _d = frames[-1]  # the newest frame closes both sets

    if vote_foreign:
        agg = Store._aggregate_identity(idents, threshold, names, residents)
        # Cannot fail while prefix_winners matches the voter (a test pins it); if it ever
        # did, the vote is not foreign and the start stands only on the net, below.
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


def play_test_clip(store, sound, name: str, now_ms: int) -> dict:
    """Play one clip on demand and write its ``test`` row; return the row's outcome.

    A test is deliberate, so it plays whatever the switch says. It still honours the Pi's
    refusals (a clip already sounding, or sound disabled there), and the row records which.
    """
    outcome = play_clip(sound, name)
    error = outcome.error
    if outcome.status == "busy":
        error = "Pi busy — a clip is still playing, so this one was not played"
    played = outcome.status == "played"
    row = {
        "kind": "sound",
        "mode": "test",
        "verdict": "test",
        "sound": name,
        "start_ts": int(now_ms),
        "end_ts": int(now_ms + (outcome.duration_s or 0.0) * 1000) if played else int(now_ms),
        "clips_played": 1 if played else 0,
        "clips_failed": 1 if outcome.status == "failed" else 0,
        "pi_disabled": 1 if outcome.status == "disabled" else 0,
        # A refusal for being disabled is carried by `pi_disabled` alone, as on an episode
        # row — it is the Pi's switch doing its job, not an error.
        "last_error": None if outcome.status == "disabled" else error,
    }
    action_id = store.add_action(row)
    return {"id": action_id, "outcome": outcome.status, "duration_s": outcome.duration_s, "error": error}


class Alarm:
    """One alarm's episode state and rules, driven once per tick by the live-naming worker.

    Worker-thread driven; ``status()`` is read from API threads, so the small episode
    dict is replaced/updated under ``_lock`` and snapshotted under it. Store and Pi calls
    happen outside the lock.
    """

    def __init__(self, store, sound) -> None:
        self._store = store
        self._sound = sound
        self._lock = threading.Lock()
        # The running episode, or None. Keys: action_id, started_ms, verdict, cat_id,
        # next_clip (index into CLIPS), next_due_ms, clips_played, clips_failed,
        # pi_disabled, last_error.
        self._episode: "dict | None" = None
        # When the previous episode ended. A new start counts only frames received after
        # it: at "gone" the vote can still hold the departed cat's last frames, which would
        # otherwise re-start the alarm in the same tick. SEEDED from the log, because the
        # presence vote reaches back up to ALARM_PRESENCE_MAX_S: memory-only, a restart
        # would let it recount the last episode's stranger. On a live app the launch sweep
        # has closed any row the previous process left open before this runs.
        seeded = store.last_alarm_end()
        self._last_end_ms: "int | None" = None if seeded is None else seeded[0]
        # How it ended, seeded with it so status() never reports an end with no reason.
        self._last_ended: "str | None" = None if seeded is None else seeded[1]
        # Step observability (set by the worker): the last pass, and its error if it raised.
        self._last_pass: "dict | None" = None
        self._pass_error: "str | None" = None

    @property
    def running(self) -> bool:
        with self._lock:
            return self._episode is not None

    # --- driven by the worker ----------------------------------------------------------

    def bookkeeping(
        self, *, mode_on: bool, model: "dict | None", pass_failed: bool, caught_up: bool, now_ms: int
    ) -> None:
        """Step 1 of a tick: end the episode if a reason applies, else play the next clip when due.

        Reasons, first that applies: the switch is Off; no calibrated gallery; the
        previous pass raised (an alarm must not keep sounding on a step that can't see);
        the cat is gone — checked only after a pass that caught up, or frames that simply
        haven't been identified yet would read as an absent cat.
        """
        with self._lock:
            episode = self._episode
        if episode is None:
            return
        reason = None
        if not mode_on:
            reason = ENDED_SWITCHED_OFF
        elif not is_calibrated(model):
            reason = ENDED_UNCALIBRATED
        elif pass_failed:
            reason = ENDED_ERROR
        elif caught_up and not self._foreign_since(model, now_ms - int(ALARM_GONE_S * 1000)):
            reason = ENDED_GONE
        if reason is not None:
            self._end(reason, now_ms)
            return
        if now_ms >= episode["next_due_ms"]:
            self._play_next(now_ms)

    def end(self, reason: str, now_ms: int) -> None:
        """End a running episode for ``reason`` (no-op when none is running)."""
        if self.running:
            self._end(reason, now_ms)

    def maybe_start(
        self,
        model: dict,
        now_ms: int,
        visit_start_ts: "int | None" = None,
        *,
        caught_up: bool = True,
        frontier_ms: "int | None" = None,
    ) -> bool:
        """Step 3: start an episode if ``start_decision`` says so. True if it started.

        ``caught_up`` is what the pass just reported: whether it left no frame unprocessed.
        When it did not, ``frontier_ms`` is the ``recv_ts`` of the last frame it processed
        (``None`` if that frame is gone); caught up, the frontier is ``now_ms``.
        """
        if self.running or not is_calibrated(model):
            return False
        with self._lock:
            last_end = self._last_end_ms
        floor = None if last_end is None else last_end + 1
        frontier = now_ms if caught_up else frontier_ms
        since = vote_floor(now_ms, floor)
        if frontier is not None:
            # Stale unless some counted frame lies within DETER_WINDOW_S of the frontier —
            # the usual answer, all day, with the door empty. A read of those few seconds
            # settles it without reading the whole two-minute presence and the roster.
            recent = max(since, int(frontier) - int(DETER_WINDOW_S * 1000))
            if not self._store.alarm_frames(model["id"], recent):
                return False
        frames = self._store.alarm_frames(model["id"], since)
        if len(frames) < DETER_MIN_FRAMES:
            return False  # neither path can fire on fewer; skips the roster read
        names, residents = self._roster()
        decision = start_decision(frames, now_ms, floor, frontier, model["threshold"], names, residents)
        if decision is None:
            return False
        action_id = self._store.add_action({
            "kind": "sound",
            "mode": MODE_ON,
            "start_ts": int(now_ms),
            "verdict": decision.verdict,
            "cat_id": decision.cat_id,
            "n_counted": decision.n_counted,
            "n_votes": decision.n_votes,
            "distance": decision.distance,
            "model_version_id": model["id"],
            "threshold": model["threshold"],
            "frame_id": decision.frame_id,
            "frame_recv_ts": decision.frame_recv_ts,
            "visit_start_ts": visit_start_ts,
            "trigger": decision.trigger,
        })
        with self._lock:
            self._episode = {
                "action_id": action_id,
                "started_ms": int(now_ms),
                "verdict": decision.verdict,
                "cat_id": decision.cat_id,
                "cat_name": decision.cat_name,
                "next_clip": 0,
                "next_due_ms": int(now_ms),
                "clips_played": 0,
                "clips_failed": 0,
                "pi_disabled": False,
                "last_error": None,
            }
        logger.info(
            "foreign-cat alarm STARTED (action %d, %s): %s%s, %d counted frames, %d votes, distance %.3f",
            action_id, decision.trigger, decision.verdict,
            f" {decision.cat_name}" if decision.cat_name else "",
            decision.n_counted, decision.n_votes, decision.distance,
        )
        self._play_next(now_ms)
        return True

    def record_pass(self, info: "dict | None", error: "str | None") -> None:
        """The worker's report of its latest pass, for ``status()``."""
        with self._lock:
            if info is not None:
                self._last_pass = info
            self._pass_error = error

    def status(self) -> dict:
        with self._lock:
            episode = dict(self._episode) if self._episode is not None else None
            return {
                "sounding": episode is not None,
                "episode": (
                    None if episode is None else {
                        "action_id": episode["action_id"],
                        "started_ms": episode["started_ms"],
                        "verdict": episode["verdict"],
                        "cat_id": episode["cat_id"],
                        "cat_name": episode["cat_name"],
                        "clips_played": episode["clips_played"],
                        "clips_failed": episode["clips_failed"],
                        "pi_disabled": episode["pi_disabled"],
                        "last_error": episode["last_error"],
                    }
                ),
                "last_end_ms": self._last_end_ms,
                "last_ended": self._last_ended,
                "last_pass": dict(self._last_pass) if self._last_pass is not None else None,
                "pass_error": self._pass_error,
            }

    # --- internals ---------------------------------------------------------------------

    def _roster(self) -> "tuple[dict, dict]":
        cats = self._store.list_cats()
        return {c["id"]: c["name"] for c in cats}, {c["id"]: c["is_resident"] for c in cats}

    def _foreign_since(self, model: dict, since_ts: int) -> bool:
        """Whether any counted frame received at or after ``since_ts`` is foreign."""
        frames = self._store.alarm_frames(model["id"], since_ts)
        if not frames:
            return False
        _names, residents = self._roster()
        return any(is_foreign(cid, dist, model["threshold"], residents) for _f, _t, cid, dist in frames)

    def _play_next(self, now_ms: int) -> None:
        """Request the next clip and fold the Pi's answer into the episode (and its row)."""
        with self._lock:
            episode = self._episode
            if episode is None:
                return
            name = CLIPS[episode["next_clip"] % len(CLIPS)]
        outcome = play_clip(self._sound, name)
        update: dict = {}
        with self._lock:
            if self._episode is not episode:
                return  # ended underneath us (cannot happen on the single worker thread)
            if outcome.status == "played":
                episode["clips_played"] += 1
                episode["next_clip"] += 1
                episode["next_due_ms"] = int(now_ms + (outcome.duration_s + CLIP_MARGIN_S) * 1000)
                update["clips_played"] = episode["clips_played"]
            elif outcome.status == "busy":
                pass  # "not yet": retried next pass, not a failure
            elif outcome.status == "disabled":
                if not episode["pi_disabled"]:
                    # Recorded once; keeps being checked so clips resume if re-enabled.
                    episode["pi_disabled"] = True
                    update["pi_disabled"] = 1
            else:
                episode["clips_failed"] += 1
                episode["last_error"] = outcome.error
                update["clips_failed"] = episode["clips_failed"]
                update["last_error"] = outcome.error
            action_id = episode["action_id"]
        if update:
            self._store.update_action(action_id, update)

    def _end(self, reason: str, now_ms: int) -> None:
        with self._lock:
            episode = self._episode
            if episode is None:
                return
            self._episode = None
            self._last_end_ms = int(now_ms)
            self._last_ended = reason
        self._store.update_action(episode["action_id"], {
            "end_ts": int(now_ms),
            "ended": reason,
            "clips_played": episode["clips_played"],
            "clips_failed": episode["clips_failed"],
            "pi_disabled": 1 if episode["pi_disabled"] else 0,
            "last_error": episode["last_error"],
        })
        logger.info(
            "foreign-cat alarm ENDED (action %d): %s after %.1f s, %d clips played, %d failed",
            episode["action_id"], reason, (now_ms - episode["started_ms"]) / 1000.0,
            episode["clips_played"], episode["clips_failed"],
        )
