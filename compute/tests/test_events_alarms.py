"""``Store.events(with_alarms=True)`` — the alarm mark on the visit that set it off.

Covers the opt-in (and that the feed's two internal reusers stay out of ``actions``),
attribution by the DECIDING frame's time to exactly one visit — inside the cluster, in
its ``_VISIT_GAP_MS`` tail, never past it, never by a reused ``frame_id`` — the payload
shape, and the ``alarm_rev`` nudge, both in ``activity_signal`` and in the SSE key that
actually decides whether a client is told to refetch.
See docs/specs/2026-11-04-user-visit-alarm-mark.md.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from compute.collection.store import _VISIT_GAP_MS, Store
from compute.ingest import StreamFrame
from shared.wire import StreamFrameMeta

_JPEG_BODY = b"\xff\xd8\xff\xe0" + b"fake-jpeg-body" + b"\xff\xd9"
_BASE = 1_700_000_000_000
_FAR = 10 * 60 * 1000   # well past _VISIT_GAP_MS: a separate visit


def _frame(frame_id: int, ts: int, *, motion: bool = True, area: float = 0.1) -> StreamFrame:
    meta = StreamFrameMeta(frame_id=frame_id, ts=ts, motion=motion, bbox=None, area=area)
    return StreamFrame(meta, _JPEG_BODY)


def _store(tmp_path) -> Store:
    return Store(
        db_path=str(tmp_path / "index.db"),
        media_root=str(tmp_path / "media"),
        max_bytes=10_000_000,
    )


def _visit(store: Store, base: int, n: int = 3) -> "list[int]":
    """n motion frames 100 ms apart -> one event spanning [base, base + (n-1)*100]."""
    return [store.add(_frame(i, base + i * 100, area=0.1 + i / 100),
                      recv_ts_ms=base + i * 100) for i in range(n)]


def _alarm(store: Store, frame_ts: int, *, frame_id: int = 1, mode: str = "on",
           start_ts: "int | None" = None, **over) -> int:
    row = {
        "kind": "sound", "mode": mode,
        "start_ts": frame_ts + 50 if start_ts is None else start_ts,
        "verdict": "test" if mode == "test" else "unknown",
        "frame_id": frame_id, "frame_recv_ts": frame_ts,
        "clips_played": 0, "clips_failed": 0, "pi_disabled": 0,
    }
    row.update(over)
    return store.add_action(row)


def _alarms_by_start(store: Store) -> "dict[int, list]":
    events = store.events(None, None, with_alarms=True)["events"]
    return {e["start_ts"]: e["alarms"] for e in events}


# --- the opt-in ---------------------------------------------------------------


def test_without_with_alarms_the_key_is_absent(tmp_path):
    store = _store(tmp_path)
    _visit(store, _BASE)
    _alarm(store, _BASE + 100)
    assert "alarms" not in store.events(None, None)["events"][0]


def test_with_alarms_the_key_is_present_and_empty_when_none(tmp_path):
    store = _store(tmp_path)
    _visit(store, _BASE)
    _visit(store, _BASE + _FAR)
    events = store.events(None, None, with_alarms=True)["events"]
    assert len(events) == 2
    assert all(e["alarms"] == [] for e in events)


def test_the_two_internal_feed_callers_never_read_actions(tmp_path):
    # Same premise as `with_labels`: the activity route pays for the read, the two
    # internal reusers of this feed (door_stats pages it up to 8x) do not.
    store = _store(tmp_path)
    _visit(store, _BASE)
    _alarm(store, _BASE + 100)

    real = store._conn
    seen: "list[str]" = []

    class Spy:
        def execute(self, sql, params=()):
            seen.append(sql)
            return real.execute(sql, params)

        def __getattr__(self, n):
            return getattr(real, n)

    store._conn = Spy()
    try:
        store.cats_overview()
        store.door_stats()
    finally:
        store._conn = real
    assert seen, "neither caller read anything — the spy is not wired up"
    assert not [s for s in seen if "FROM actions" in s], (
        "cats_overview/door_stats now pay for the alarm read they never look at"
    )


# --- attribution ----------------------------------------------------------------


def test_an_alarm_lands_on_the_visit_holding_its_deciding_frame_only(tmp_path):
    store = _store(tmp_path)
    _visit(store, _BASE)
    _visit(store, _BASE + _FAR)
    _alarm(store, _BASE + _FAR + 100)

    by_start = _alarms_by_start(store)
    assert len(by_start[_BASE + _FAR]) == 1
    assert by_start[_BASE] == [], "an alarm must land on ONE visit, never bleed to another"


def test_the_tail_window_holds_and_ends_exactly_at_the_gap(tmp_path):
    # Tuning: the deciding frame can be a NON-motion frame just after the last motion
    # frame. [start_ts, end_ts + gap] is inclusive at both ends; one ms past is nowhere.
    store = _store(tmp_path)
    _visit(store, _BASE)              # end_ts = _BASE + 200
    end_ts = _BASE + 200
    _alarm(store, end_ts + _VISIT_GAP_MS, start_ts=_BASE + 10)
    _alarm(store, end_ts + _VISIT_GAP_MS + 1, start_ts=_BASE + 20)
    _alarm(store, _BASE - 1, start_ts=_BASE + 30)          # before the visit began

    alarms = _alarms_by_start(store)[_BASE]
    assert [a["start_ts"] for a in alarms] == [_BASE + 10]


def test_a_tail_alarm_does_not_attach_to_the_next_visit(tmp_path):
    # Two visits just over the gap apart: the windows are disjoint, so a frame in the
    # older visit's tail belongs to the older visit and nothing else.
    store = _store(tmp_path)
    _visit(store, _BASE)                                  # end_ts = _BASE + 200
    newer = _BASE + 200 + _VISIT_GAP_MS + 1
    _visit(store, newer)
    _alarm(store, _BASE + 200 + _VISIT_GAP_MS)

    by_start = _alarms_by_start(store)
    assert len(by_start) == 2, "the fixture no longer makes two visits"
    assert len(by_start[_BASE]) == 1
    assert by_start[newer] == []


def test_test_plays_are_never_attached(tmp_path):
    store = _store(tmp_path)
    _visit(store, _BASE)
    _alarm(store, _BASE + 100, mode="test")
    assert _alarms_by_start(store)[_BASE] == []


def test_several_alarms_on_one_visit_come_in_start_order(tmp_path):
    # Switched off and on again mid-visit: two episodes, one visit.
    store = _store(tmp_path)
    _visit(store, _BASE, n=10)
    _alarm(store, _BASE + 800, start_ts=_BASE + 850)
    _alarm(store, _BASE + 100, start_ts=_BASE + 150)
    alarms = _alarms_by_start(store)[_BASE]
    assert [a["start_ts"] for a in alarms] == [_BASE + 150, _BASE + 850]


def test_matching_is_by_time_not_by_a_reused_frame_id(tmp_path):
    # `clear()` restarts frame ids at 1 but the actions log survives it. A pre-clear
    # alarm whose frame_id now names a frame of a brand-new visit must not mark it.
    store = _store(tmp_path)
    ids = _visit(store, _BASE)
    _alarm(store, _BASE + 100, frame_id=ids[1])
    store.clear()
    fresh = _visit(store, _BASE + 24 * 3600 * 1000)
    assert fresh == ids, "ids did not restart — the test no longer reaches the condition"
    assert _alarms_by_start(store)[_BASE + 24 * 3600 * 1000] == []


# --- payload ---------------------------------------------------------------------


def test_payload_shape_and_types(tmp_path):
    store = _store(tmp_path)
    _visit(store, _BASE)
    cat_id = store.create_cat("Tom", is_resident=False)["id"]
    _alarm(store, _BASE + 100, start_ts=_BASE + 150, end_ts=_BASE + 65_150, ended="gone",
           verdict="neighbour", cat_id=cat_id, clips_played=3, clips_failed=1, pi_disabled=1)
    _alarm(store, _BASE + 200, start_ts=_BASE + 70_000)   # still open, unknown cat

    done, still = _alarms_by_start(store)[_BASE]
    assert done == {
        "start_ts": _BASE + 150, "end_ts": _BASE + 65_150, "ended": "gone",
        "verdict": "neighbour", "cat_id": cat_id, "cat_name": "Tom",
        "clips_played": 3, "clips_failed": 1, "pi_disabled": True,
    }
    assert still["end_ts"] is None and still["ended"] is None
    assert still["verdict"] == "unknown" and still["cat_id"] is None and still["cat_name"] is None
    assert still["pi_disabled"] is False


def test_alarms_ride_the_feed_with_no_active_model(tmp_path):
    # The identity join returns early without a model; the alarm read must come first,
    # since an alarm's record outlives the gallery that decided it.
    store = _store(tmp_path)
    _visit(store, _BASE)
    _alarm(store, _BASE + 100)
    assert store.active_model() is None
    assert len(_alarms_by_start(store)[_BASE]) == 1


# --- the route ----------------------------------------------------------------------


def test_api_events_carries_alarms_on_every_event(tmp_path):
    from compute.api.app import create_app

    store = _store(tmp_path)
    _visit(store, _BASE)
    _visit(store, _BASE + _FAR)
    _alarm(store, _BASE + 100)

    client = TestClient(create_app(store=store, start_collector=False))
    events = client.get("/api/events").json()["events"]
    assert len(events) == 2
    assert all("alarms" in e for e in events)
    assert sum(len(e["alarms"]) for e in events) == 1


# --- alarm_rev ----------------------------------------------------------------------


def test_alarm_rev_moves_on_open_and_on_close_only(tmp_path):
    store = _store(tmp_path)
    assert store.activity_signal()["alarm_rev"] == 0
    aid = _alarm(store, _BASE)
    assert store.activity_signal()["alarm_rev"] == 1
    store.update_action(aid, {"clips_played": 2})          # a clip landing: no nudge
    assert store.activity_signal()["alarm_rev"] == 1
    store.update_action(aid, {"end_ts": _BASE + 5000, "ended": "gone"})
    assert store.activity_signal()["alarm_rev"] == 2
    _alarm(store, _BASE + 10, mode="test", end_ts=_BASE + 20)   # tests never move it
    assert store.activity_signal()["alarm_rev"] == 2


def test_events_stream_pushes_when_an_alarm_opens(tmp_path, monkeypatch):
    """`alarm_rev` must be in the SSE change KEY, not merely in the dict.

    The endpoint builds its key as an explicit tuple, so a field the signal returns but
    the key omits nudges nothing. Only an alarm moves here — no frame is stored — so a
    push can come from `alarm_rev` alone.
    """
    import compute.api.app as appmod

    monkeypatch.setattr(appmod, "_SSE_POLL_SECONDS", 0.01)
    store = _store(tmp_path)
    app = appmod.create_app(store=store, start_collector=False)

    async def drive() -> bool:
        scope = {
            "type": "http", "http_version": "1.1", "method": "GET",
            "path": "/api/events/stream", "raw_path": b"/api/events/stream",
            "query_string": b"", "headers": [], "scheme": "http",
            "client": ("test", 1), "server": ("test", 80),
        }
        disconnect = asyncio.Event()
        first = True
        got_activity = asyncio.Event()

        async def receive():
            nonlocal first
            if first:
                first = False
                return {"type": "http.request", "body": b"", "more_body": False}
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body" and b"event: activity" in message.get("body", b""):
                got_activity.set()

        task = asyncio.ensure_future(app(scope, receive, send))
        try:
            await asyncio.sleep(0.1)   # baseline sample first
            _alarm(store, _BASE)
            try:
                await asyncio.wait_for(got_activity.wait(), timeout=2.0)
                return True
            except asyncio.TimeoutError:
                return False
        finally:
            disconnect.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert asyncio.run(drive()), "an alarm opening did not nudge the feed"
