"""The foreign-cat alarm: when to sound, when to stop, and the record of each episode.

The decision half of the system's first actuator (docs/specs/2026-11-01-foreign-cat-alarm.md).
The live-naming worker (``live_identify.py``) owns the GPU work — it detects and
identifies the newest frames once a second while the switch is On — and drives the
``Alarm`` here once per tick. This module holds the rules and the episode state:

- **Start** — at least ``DETER_MIN_FRAMES`` *counted* frames received in the last
  ``DETER_WINDOW_S``, and ``Store._aggregate_identity`` (the feed's own voter) calls them
  foreign: a named non-resident won the vote, or nothing came within the threshold. Visit
  boundaries are ignored. An uncalibrated gallery (NULL threshold) never starts one —
  there every cat reads *unknown*, so every resident would trip it.
- **Gone** — no *foreign* counted frame for ``ALARM_GONE_S``. A frame that matches a
  resident within the threshold never prolongs the alarm, so a resident following a
  stranger in stops the noise.
- **The loop** — the Pi plays one clip per call; this asks for the next (``alarm1``,
  ``alarm2``, alternating) once the previous one is due to end. If the compute dies, the
  clip in progress finishes and the Pi goes quiet: fail-safe by construction.

A *counted* frame is what ``Store.alarm_frames`` returns: a ``yolo-serial`` cat box at or
above the feed's 0.3 floor, with a gallery match for the active model.

Every episode is one ``actions`` row: written when it starts, its clip counters updated as
clips land, completed with how it ended. Test plays write a ``test`` row the same way, so
the log proves the compute → Pi path end to end.
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

# The start rule: this many counted frames, received within the last this-many seconds.
DETER_MIN_FRAMES = 10
DETER_WINDOW_S = 5.0
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
        # it: at "gone" the start window can still hold the departed cat's last frames
        # (3-5 s old), which would otherwise re-start the alarm in the same tick.
        self._last_end_ms: "int | None" = None
        self._last_ended: "str | None" = None
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

    def maybe_start(self, model: dict, now_ms: int, visit_start_ts: "int | None" = None) -> bool:
        """Step 3: start an episode if the recent counted frames vote foreign. True if it started."""
        if self.running or not is_calibrated(model):
            return False
        since = now_ms - int(DETER_WINDOW_S * 1000)
        with self._lock:
            last_end = self._last_end_ms
        if last_end is not None:
            since = max(since, last_end + 1)
        frames = self._store.alarm_frames(model["id"], since)
        if len(frames) < DETER_MIN_FRAMES:
            return False
        names, residents = self._roster()
        agg = Store._aggregate_identity(
            [(cid, dist) for _fid, _ts, cid, dist in frames], model["threshold"], names, residents
        )
        if agg is None or (agg["cat_id"] is not None and agg["is_resident"]):
            return False
        # The deciding frame: the newest counted frame, the one whose arrival tipped the vote.
        frame_id, frame_ts, _cid, _dist = frames[-1]
        verdict = "unknown" if agg["cat_id"] is None else "neighbour"
        action_id = self._store.add_action({
            "kind": "sound",
            "mode": MODE_ON,
            "start_ts": int(now_ms),
            "verdict": verdict,
            "cat_id": agg["cat_id"],
            "n_counted": len(frames),
            "n_votes": agg["n_frames_voted"],
            "distance": agg["distance"],
            "model_version_id": model["id"],
            "threshold": model["threshold"],
            "frame_id": frame_id,
            "frame_recv_ts": frame_ts,
            "visit_start_ts": visit_start_ts,
        })
        with self._lock:
            self._episode = {
                "action_id": action_id,
                "started_ms": int(now_ms),
                "verdict": verdict,
                "cat_id": agg["cat_id"],
                "cat_name": agg["cat_name"],
                "next_clip": 0,
                "next_due_ms": int(now_ms),
                "clips_played": 0,
                "clips_failed": 0,
                "pi_disabled": False,
                "last_error": None,
            }
        logger.info(
            "foreign-cat alarm STARTED (action %d): %s%s, %d counted frames, %d votes, distance %.3f",
            action_id, verdict, f" {agg['cat_name']}" if agg["cat_name"] else "",
            len(frames), agg["n_frames_voted"], agg["distance"],
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
