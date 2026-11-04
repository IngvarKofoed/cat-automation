"""Tests for the user app's per-cat daily chart: ``Store.daily_visits`` (and its two
halves, ``daily_visit_counts`` / ``daily_recording``) and ``GET /api/door-stats/daily``
(compute/collection/store.py, compute/api/app.py; user-cat-daily-visits-chart spec).

Days are the compute PC's LOCAL calendar days, so every store-level test pins the
process zone to Europe/Copenhagen (``TZ`` + ``time.tzset``) — the window then spans the
2026-10-25 DST switch the spec names. Frames are seeded in chronological order, as the
collector writes them (``recv_ts`` non-decreasing with ``id``), with non-motion "filler"
frames every few hours standing in for continuous capture, so the recording probes see a
day as recorded.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from compute.collection.store import (
    _DAILY_GAP_HOURS,
    _MAX_DAILY_PAGES,
    _VISIT_GAP_MS,
    Store,
)
from compute.ingest import StreamFrame
from shared.wire import StreamFrameMeta

_JPEG_BODY = b"\xff\xd8\xff\xe0" + b"fake-jpeg-body" + b"\xff\xd9"
_CAT = 15
_HOUR = 3_600_000


@pytest.fixture
def cph():
    """Pin the process's local zone to Copenhagen for the test, then restore it."""
    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset unavailable (Windows)")
    old = os.environ.get("TZ")
    os.environ["TZ"] = "Europe/Copenhagen"
    time.tzset()
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()


def _ts(y, mo, d, h=0, mi=0) -> int:
    """A LOCAL wall-clock instant → epoch ms (under whatever zone is pinned)."""
    return int(datetime(y, mo, d, h, mi).timestamp() * 1000)


def _store(tmp_path) -> Store:
    return Store(
        db_path=str(tmp_path / "index.db"),
        media_root=str(tmp_path / "media"),
        max_bytes=200_000_000,
    )


def _add_version(store: Store, *, threshold=0.5, metrics=None, gallery_dir="g") -> int:
    d = os.path.join(store.models_root, gallery_dir)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "gallery.npz"), "wb") as fh:
        fh.write(b"\x00")
    vid = store.add_model_version(
        status="draft", kind="gallery", backbone="dinov2_vits14", imgsz=224,
        n_cats=2, n_vectors=10, threshold=threshold, quality="gallery",
        metrics=metrics, gallery_dir=gallery_dir,
    )
    store.promote_model(vid)
    return vid


class _Seed:
    """Collects frames, then writes them in recv_ts order (the collector's order).

    ``visit(ts, tag)`` adds a 2-frame motion cluster; ``fill(lo, hi, every)`` adds
    non-motion frames — continuous capture — so recording probes see those hours.
    ``build(store)`` returns ``{tag: (start_id, end_id)}``.
    """

    def __init__(self):
        self.items: "list[tuple[int, bool, str | None]]" = []

    def visit(self, ts: int, tag: str, n: int = 2) -> None:
        for i in range(n):
            self.items.append((ts + i * 200, True, tag))

    def fill(self, lo: int, hi: int, every: int = 3 * _HOUR) -> None:
        t = lo
        while t < hi:
            self.items.append((t, False, None))
            t += every

    def build(self, store: Store) -> "dict[str, tuple[int, int]]":
        spans: "dict[str, list[int]]" = {}
        for n, (ts, motion, tag) in enumerate(sorted(self.items, key=lambda x: x[0])):
            meta = StreamFrameMeta(frame_id=n + 1, ts=0, motion=motion, bbox=None, area=0.1)
            fid = store.add(StreamFrame(meta, _JPEG_BODY), recv_ts_ms=ts)
            if tag is not None:
                spans.setdefault(tag, []).append(fid)
        return {t: (min(ids), max(ids)) for t, ids in spans.items()}


def _name(store: Store, span, vid: int, cat_id, dist: float = 0.1) -> None:
    store.write_identifications_batch(
        [(fid, vid, cat_id, dist, [0, 0, 1, 1]) for fid in range(span[0], span[1] + 1)]
    )


def _day(result: dict, iso: str) -> dict:
    return next(d for d in result["days"] if d["date"] == iso)


# Now = Wed 4 Nov 2026 15:00 Copenhagen; the 90-day window is 7 Aug .. 4 Nov.
def _now() -> int:
    return _ts(2026, 11, 4, 15, 0)


# --- the day list ----------------------------------------------------------


def test_ninety_local_days_oldest_first_today_last(tmp_path, cph):
    store = _store(tmp_path)
    now = _now()
    result = store.daily_visits(now_ms=now)

    days = result["days"]
    assert len(days) == 90 == result["window_days"]
    assert days[0]["date"] == "2026-08-07"
    assert days[-1]["date"] == "2026-11-04"
    assert days[-1]["today"] is True and not any(d["today"] for d in days[:-1])
    assert days[-1]["end_ts"] == now
    # Contiguous: each day starts where the previous one ended.
    for a, b in zip(days, days[1:]):
        assert a["end_ts"] == b["start_ts"]


def test_dst_day_is_25_hours_and_every_day_starts_at_local_midnight(tmp_path, cph):
    store = _store(tmp_path)
    result = store.daily_visits(now_ms=_now())

    switch = _day(result, "2026-10-25")
    assert switch["end_ts"] - switch["start_ts"] == 25 * _HOUR
    for d in result["days"]:
        # One fixed offset would shift every pre-switch day by an hour; local midnight
        # must hold on both sides.
        assert datetime.fromtimestamp(d["start_ts"] / 1000).strftime("%H:%M") == "00:00"


# --- counting ---------------------------------------------------------------


def test_counts_a_named_visit_on_its_day_sparse_and_split(tmp_path, cph):
    store = _store(tmp_path)
    vid = _add_version(store)
    mittens = store.create_cat("Mittens", is_resident=True)["id"]
    other = store.create_cat("Other", is_resident=True)["id"]
    seed = _Seed()
    seed.fill(_ts(2026, 11, 2), _now())
    seed.visit(_ts(2026, 11, 3, 9), "a")       # by day
    seed.visit(_ts(2026, 11, 3, 22), "b")      # after dark
    seed.visit(_ts(2026, 11, 4, 8), "c")
    spans = seed.build(store)
    for tag in "abc":
        _name(store, spans[tag], vid, mittens)

    is_night = lambda ts: datetime.fromtimestamp(ts / 1000).hour >= 18  # noqa: E731
    result = store.daily_visits(now_ms=_now(), is_night=is_night)

    nov3 = _day(result, "2026-11-03")
    assert nov3["state"] == "full"
    assert nov3["cats"] == {str(mittens): {"visits": 2, "day": 1, "night": 1}}
    assert str(other) not in nov3["cats"]  # sparse: a cat with no visit is absent
    assert _day(result, "2026-11-04")["cats"][str(mittens)]["visits"] == 1


def test_without_a_classifier_day_and_night_are_none(tmp_path, cph):
    store = _store(tmp_path)
    vid = _add_version(store)
    cat = store.create_cat("Mittens", is_resident=True)["id"]
    seed = _Seed()
    seed.fill(_ts(2026, 11, 3), _now())
    seed.visit(_ts(2026, 11, 3, 9), "a")
    spans = seed.build(store)
    _name(store, spans["a"], vid, cat)

    result = store.daily_visits(now_ms=_now())
    assert _day(result, "2026-11-03")["cats"][str(cat)] == {"visits": 1, "day": None, "night": None}


def test_a_visit_straddling_midnight_counts_once_on_its_start_day(tmp_path, cph):
    store = _store(tmp_path)
    vid = _add_version(store)
    cat = store.create_cat("Mittens", is_resident=True)["id"]
    seed = _Seed()
    seed.fill(_ts(2026, 11, 2), _now())
    # One cluster from 23:59:59.4 to 00:00:00.6 — frames 200 ms apart, one visit.
    seed.visit(_ts(2026, 11, 3, 23, 59) + 59_400, "x", n=7)
    spans = seed.build(store)
    _name(store, spans["x"], vid, cat)

    result = store.daily_visits(now_ms=_now())
    assert _day(result, "2026-11-03")["cats"] == {str(cat): {"visits": 1, "day": None, "night": None}}
    assert _day(result, "2026-11-04")["cats"] == {}


def test_a_visit_under_way_at_the_window_start_is_not_put_on_the_first_day(tmp_path, cph):
    """The floor sits an hour before the window, so a visit already running at midnight
    is read whole — and discarded, because it STARTED before the window."""
    store = _store(tmp_path)
    vid = _add_version(store)
    cat = store.create_cat("Mittens", is_resident=True)["id"]
    window_start = _ts(2026, 8, 7)
    seed = _Seed()
    seed.fill(window_start - 6 * _HOUR, _now())
    # A cluster from 23:59:58 the night before to 00:00:02 — frames well inside the gap.
    for i in range(21):
        seed.items.append((window_start - 2000 + i * 200, True, "x"))
    spans = seed.build(store)
    _name(store, spans["x"], vid, cat)

    result = store.daily_visits(now_ms=_now())
    assert _day(result, "2026-08-07")["cats"] == {}


def test_retired_cats_are_counted(tmp_path, cph):
    store = _store(tmp_path)
    vid = _add_version(store)
    cat = store.create_cat("Gone", is_resident=True)["id"]
    store.update_cat(cat, {"active": False})
    seed = _Seed()
    seed.fill(_ts(2026, 11, 3), _now())
    seed.visit(_ts(2026, 11, 3, 9), "a")
    spans = seed.build(store)
    _name(store, spans["a"], vid, cat)

    result = store.daily_visits(now_ms=_now())
    assert _day(result, "2026-11-03")["cats"][str(cat)]["visits"] == 1


def test_enrolled_cat_ids_come_from_the_models_per_cat(tmp_path, cph):
    store = _store(tmp_path)
    _add_version(store, metrics={"per_cat": [{"cat_id": 3, "n": 5}, {"cat_id": 1, "n": 2}]})
    assert store.daily_visits(now_ms=_now())["model"]["enrolled_cat_ids"] == [1, 3]


def test_enrolled_cat_ids_none_for_a_model_without_per_cat(tmp_path, cph):
    store = _store(tmp_path)
    vid = _add_version(store, metrics=None)
    model = store.daily_visits(now_ms=_now())["model"]
    assert model == {"id": vid, "calibrated": True, "enrolled_cat_ids": None}


# --- recording states -------------------------------------------------------


def test_days_with_no_frames_read_none_and_days_before_the_store_read_expired(tmp_path, cph):
    store = _store(tmp_path)
    _add_version(store)
    seed = _Seed()
    # An outage from 26 Oct 20:00 to 1 Nov 07:00. Hourly fillers: 3-hourly ones stepped
    # in epoch ms drift an hour against local time across the 25 Oct DST switch.
    seed.fill(_ts(2026, 10, 20), _ts(2026, 10, 26, 20), every=_HOUR)
    seed.fill(_ts(2026, 11, 1, 7), _now(), every=_HOUR)
    seed.build(store)

    result = store.daily_visits(now_ms=_now())
    assert result["store_oldest_ts"] == _ts(2026, 10, 20)
    assert _day(result, "2026-10-19")["state"] == "expired"
    assert _day(result, "2026-08-07")["state"] == "expired"
    for iso in ("2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30", "2026-10-31"):
        assert _day(result, iso)["state"] == "none", iso
    # The days the outage only TOUCHES are partly recorded: each holds the end of a
    # > 7 h empty run on its own side of the gap.
    assert _day(result, "2026-10-26")["state"] == "partial"
    assert _day(result, "2026-11-01")["state"] == "partial"
    assert _day(result, "2026-10-22")["state"] == "full"


def test_a_gap_crossing_midnight_marks_both_days_partial(tmp_path, cph):
    """The 24 Sep shape: 19:00 for 11 h. Neither day holds 7 empty hours on its own
    side of midnight (5 before, 6 after) — only a run measured ACROSS midnight sees it."""
    store = _store(tmp_path)
    _add_version(store)
    seed = _Seed()
    seed.fill(_ts(2026, 9, 20), _ts(2026, 9, 24, 19), every=_HOUR)
    seed.fill(_ts(2026, 9, 25, 6), _now(), every=_HOUR)
    seed.build(store)

    result = store.daily_visits(now_ms=_now())
    assert _day(result, "2026-09-24")["state"] == "partial"
    assert _day(result, "2026-09-25")["state"] == "partial"
    assert _day(result, "2026-09-23")["state"] == "full"
    assert _day(result, "2026-09-26")["state"] == "full"


def test_a_gap_one_hour_short_of_the_threshold_reads_full(tmp_path, cph):
    store = _store(tmp_path)
    _add_version(store)
    seed = _Seed()
    gap = _DAILY_GAP_HOURS - 1
    seed.fill(_ts(2026, 9, 20), _ts(2026, 9, 24, 12) + _HOUR, every=_HOUR)
    seed.fill(_ts(2026, 9, 24, 13) + gap * _HOUR, _now(), every=_HOUR)
    seed.build(store)

    result = store.daily_visits(now_ms=_now())
    assert _day(result, "2026-09-24")["state"] == "full"


def test_an_outage_still_open_at_now_marks_today(tmp_path, cph):
    store = _store(tmp_path)
    _add_version(store)
    seed = _Seed()
    seed.fill(_ts(2026, 10, 1), _ts(2026, 11, 4, 6), every=_HOUR)  # nothing since 06:00
    seed.build(store)

    result = store.daily_visits(now_ms=_now())  # 15:00 — 9 empty hours and counting
    assert _day(result, "2026-11-04")["state"] == "partial"
    assert _day(result, "2026-11-03")["state"] == "full"


def test_a_partial_day_keeps_its_counts(tmp_path, cph):
    store = _store(tmp_path)
    vid = _add_version(store)
    cat = store.create_cat("Mittens", is_resident=True)["id"]
    seed = _Seed()
    seed.fill(_ts(2026, 10, 1), _ts(2026, 11, 3, 6), every=_HOUR)
    seed.visit(_ts(2026, 11, 3, 5), "a")
    seed.fill(_ts(2026, 11, 3, 20), _now(), every=_HOUR)
    spans = seed.build(store)
    _name(store, spans["a"], vid, cat)

    nov3 = _day(store.daily_visits(now_ms=_now()), "2026-11-03")
    assert nov3["state"] == "partial"
    assert nov3["cats"][str(cat)]["visits"] == 1  # a floor, not an estimate


def test_the_hour_probe_seeks_the_recv_ts_index(tmp_path):
    """~2,200 of these run per request, uncached, on a store with no ANALYZE stats, and
    their results are identical whatever the plan — so only a plan assertion can catch a
    regression to a scan (entries 229/265/276/307/385/391's class)."""
    import sqlite3

    from compute.collection.store import _HOUR_PROBE_SQL

    store = _store(tmp_path)
    store.add(
        StreamFrame(StreamFrameMeta(frame_id=1, ts=0, motion=False, bbox=None, area=0.0), _JPEG_BODY),
        recv_ts_ms=1_000,
    )
    conn = sqlite3.connect(str(tmp_path / "index.db"))
    try:
        plan = " ".join(
            row[-1] for row in conn.execute("EXPLAIN QUERY PLAN " + _HOUR_PROBE_SQL, (0, 3_600_000))
        )
    finally:
        conn.close()
    assert "idx_frames_recv_ts" in plan and "recv_ts>? AND recv_ts<?" in plan, plan
    assert "SCAN" not in plan, plan


# --- naming states ----------------------------------------------------------


def test_a_day_whose_visits_carry_no_identity_reads_unnamed(tmp_path, cph):
    """After a promotion, past days the Identify-all pass hasn't reached carry no
    identity under the new model — they must not draw as zero."""
    store = _store(tmp_path)
    vid = _add_version(store)
    cat = store.create_cat("Mittens", is_resident=True)["id"]
    seed = _Seed()
    seed.fill(_ts(2026, 11, 1), _now())
    seed.visit(_ts(2026, 11, 2, 9), "old")   # never identified under this model
    seed.visit(_ts(2026, 11, 3, 9), "new")
    spans = seed.build(store)
    _name(store, spans["new"], vid, cat)

    result = store.daily_visits(now_ms=_now())
    assert _day(result, "2026-11-02")["state"] == "unnamed"
    assert _day(result, "2026-11-02")["cats"] == {}
    assert _day(result, "2026-11-03")["state"] == "full"
    # A recorded day with no visits at all is a real zero, not unnamed.
    assert _day(result, "2026-11-01")["state"] == "full"


def test_an_unknown_identity_counts_as_named_for_the_day(tmp_path, cph):
    store = _store(tmp_path)
    vid = _add_version(store, threshold=0.2)
    cat = store.create_cat("Mittens", is_resident=True)["id"]
    seed = _Seed()
    seed.fill(_ts(2026, 11, 2), _now())
    seed.visit(_ts(2026, 11, 3, 9), "far")
    spans = seed.build(store)
    _name(store, spans["far"], vid, cat, dist=0.9)  # beyond the threshold → unknown cat

    nov3 = _day(store.daily_visits(now_ms=_now()), "2026-11-03")
    assert nov3["state"] == "full"   # the model DID look at it
    assert nov3["cats"] == {}


def test_no_active_model_walks_nothing_and_names_no_day(tmp_path, cph, monkeypatch):
    store = _store(tmp_path)
    seed = _Seed()
    seed.fill(_ts(2026, 11, 1), _now())
    seed.visit(_ts(2026, 11, 3, 9), "a")
    seed.build(store)
    calls = []
    monkeypatch.setattr(store, "events", lambda *a, **kw: calls.append(a))

    result = store.daily_visits(now_ms=_now())
    assert calls == []
    assert result["model"] is None
    assert _day(result, "2026-11-03")["state"] == "unnamed"
    assert _day(result, "2026-10-01")["state"] == "expired"  # before the store's first frame
    assert all(d["cats"] == {} for d in result["days"])


def test_an_uncalibrated_model_walks_nothing(tmp_path, cph, monkeypatch):
    store = _store(tmp_path)
    _add_version(store, threshold=None)
    seed = _Seed()
    seed.fill(_ts(2026, 11, 1), _now())
    seed.build(store)
    calls = []
    monkeypatch.setattr(store, "events", lambda *a, **kw: calls.append(a))

    result = store.daily_visits(now_ms=_now())
    assert calls == []
    assert result["model"]["calibrated"] is False
    assert _day(result, "2026-11-03")["state"] == "unnamed"


# --- truncation -------------------------------------------------------------


def _fake_event(start_id: int, start_ts: int) -> dict:
    return {
        "start_id": start_id, "end_id": start_id, "start_ts": start_ts, "end_ts": start_ts,
        "n_frames": 1, "rep_frame_id": start_id, "identity": {"cat_id": 1, "is_resident": True},
    }


def test_truncation_marks_the_stop_day_and_everything_older_uncounted(tmp_path, cph, monkeypatch):
    store = _store(tmp_path)
    _add_version(store)
    seed = _Seed()
    seed.fill(_ts(2026, 8, 1), _now())
    seed.build(store)

    # Each page hands back one visit a day older than the last, and claims more exist,
    # so the BUDGET is the only thing that stops the walk — inside 1 Nov after 40 pages
    # would be far too deep, so space them 2 h apart: the 40th lands on 1 Nov.
    calls = [0]
    base = _ts(2026, 11, 4, 14)

    def fake_events(*a, **kw):
        calls[0] += 1
        assert kw.get("with_subject") is False
        ts = base - (calls[0] - 1) * 2 * _HOUR
        return {"events": [_fake_event(100_000 - calls[0], ts)], "truncated": True}

    monkeypatch.setattr(store, "events", fake_events)
    result = store.daily_visits(now_ms=_now())

    assert calls[0] == _MAX_DAILY_PAGES
    assert result["truncated"] is True
    stop_ts = base - (_MAX_DAILY_PAGES - 1) * 2 * _HOUR
    stop_day = datetime.fromtimestamp(stop_ts / 1000).date().isoformat()
    for d in result["days"]:
        if d["date"] <= stop_day:
            assert d["state"] == "uncounted", d["date"]
            assert d["cats"] == {}
        else:
            assert d["state"] == "full", d["date"]
    # The newest days carry their counts.
    assert _day(result, "2026-11-04")["cats"]["1"]["visits"] >= 1


def test_a_count_that_got_past_the_window_start_leaves_no_day_uncounted(tmp_path, cph, monkeypatch):
    """The budget ran out only after reading into the floor's spare hour, so every
    visit IN the window was counted — the walk was cut short, yet no day may read
    uncounted (the page banners truncation only on those)."""
    store = _store(tmp_path)
    _add_version(store)
    seed = _Seed()
    seed.fill(_ts(2026, 8, 1), _now())
    seed.build(store)
    window_start = _ts(2026, 8, 7)

    calls = [0]

    def fake_events(*a, **kw):
        calls[0] += 1
        ts = _now() - _HOUR if calls[0] < _MAX_DAILY_PAGES else window_start - 1000
        return {"events": [_fake_event(100_000 - calls[0], ts)], "truncated": True}

    monkeypatch.setattr(store, "events", fake_events)
    result = store.daily_visits(now_ms=_now())
    assert calls[0] == _MAX_DAILY_PAGES
    assert result["truncated"] is True
    assert not any(d["state"] == "uncounted" for d in result["days"])


def test_pages_past_one_events_call(tmp_path, cph):
    """More visits than one events() page are all counted (the shared keyset walk)."""
    from compute.collection.store import _MAX_EVENTS

    store = _store(tmp_path)
    vid = _add_version(store)
    cat = store.create_cat("Mittens", is_resident=True)["id"]
    seed = _Seed()
    seed.fill(_ts(2026, 11, 2), _now())
    n = _MAX_EVENTS + 30
    t0 = _ts(2026, 11, 3, 8)
    for i in range(n):
        seed.visit(t0 + i * (_VISIT_GAP_MS + 500), f"v{i}", n=1)
    spans = seed.build(store)
    for i in range(n):
        _name(store, spans[f"v{i}"], vid, cat)

    result = store.daily_visits(now_ms=_now())
    assert result["truncated"] is False
    assert _day(result, "2026-11-03")["cats"][str(cat)]["visits"] == n


# --- GET /api/door-stats/daily ------------------------------------------------


def _client(tmp_path, monkeypatch) -> TestClient:
    from compute.api.app import create_app

    monkeypatch.setenv("CAT_COLLECT_DIR", str(tmp_path))
    return TestClient(create_app(start_collector=False))


def _count_walks(monkeypatch, delay: float = 0.0) -> list:
    calls: list = []
    real = Store.daily_visit_counts

    def wrapped(self, **kw):
        calls.append(kw)
        if delay:
            time.sleep(delay)
        return real(self, **kw)

    monkeypatch.setattr(Store, "daily_visit_counts", wrapped)
    return calls


def test_route_answers_ninety_days_with_split(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        body = client.get("/api/door-stats/daily").json()
    assert len(body["days"]) == 90 and body["window_days"] == 90
    assert body["days"][-1]["today"] is True
    assert body["split"] == {"available": False, "reason": "location_unset"}
    assert body["model"] is None
    assert "threshold" not in body  # internal cache key, never on the wire


def test_route_serves_a_second_request_from_the_cache(tmp_path, monkeypatch):
    calls = _count_walks(monkeypatch)
    with _client(tmp_path, monkeypatch) as client:
        a = client.get("/api/door-stats/daily").json()
        b = client.get("/api/door-stats/daily").json()
    assert len(calls) == 1
    assert a["generated_ts"] == b["generated_ts"]


def test_route_recounts_once_the_entry_expires(tmp_path, monkeypatch):
    import compute.api.app as app_module

    calls = _count_walks(monkeypatch)
    monkeypatch.setattr(app_module, "_DAILY_CACHE_TTL_S", 0)
    with _client(tmp_path, monkeypatch) as client:
        client.get("/api/door-stats/daily")
        client.get("/api/door-stats/daily")
    assert len(calls) == 2


def test_route_recounts_at_once_when_the_threshold_moves(tmp_path, monkeypatch):
    calls = _count_walks(monkeypatch)
    with _client(tmp_path, monkeypatch) as client:
        store = client.app.state.store
        client.get("/api/door-stats/daily")
        assert len(calls) == 1
        # Promote a gallery: the key's model id changes.
        vid = _add_version(store)
        client.get("/api/door-stats/daily")
        assert len(calls) == 2
        # Move the threshold: the key's threshold changes.
        store.set_model_threshold(vid, 0.4)
        client.get("/api/door-stats/daily")
        assert len(calls) == 3
        client.get("/api/door-stats/daily")
        assert len(calls) == 3


def test_route_keeps_the_split_it_counted_with(tmp_path, monkeypatch):
    """A location set inside the ten minutes must not pair `available: true` with
    day/night columns counted without a classifier."""
    from compute.analysis.suntimes import astral_available

    if not astral_available():
        pytest.skip("astral not installed")
    with _client(tmp_path, monkeypatch) as client:
        first = client.get("/api/door-stats/daily").json()
        assert client.post(
            "/api/location", json={"latitude": 55.68, "longitude": 12.57}
        ).status_code == 200
        second = client.get("/api/door-stats/daily").json()
    assert first["split"]["available"] is False
    assert second["split"] == first["split"]


def test_route_recomputes_recording_on_every_request(tmp_path, monkeypatch):
    """A Clear must show at once, cache or not: recording is never cached."""
    with _client(tmp_path, monkeypatch) as client:
        store = client.app.state.store
        now = int(time.time() * 1000)
        meta = StreamFrameMeta(frame_id=1, ts=0, motion=False, bbox=None, area=0.0)
        store.add(StreamFrame(meta, _JPEG_BODY), recv_ts_ms=now - 1000)
        before = client.get("/api/door-stats/daily").json()
        assert before["days"][-1]["state"] != "none"
        store.clear()
        after = client.get("/api/door-stats/daily").json()
    assert after["store_oldest_ts"] is None
    assert after["days"][-1]["state"] == "none"
    assert after["generated_ts"] == before["generated_ts"]  # the counts were cached


def test_route_is_single_flight(tmp_path, monkeypatch):
    """A request arriving while a count runs waits for it rather than starting a second."""
    calls = _count_walks(monkeypatch, delay=0.5)
    results: list = []
    with _client(tmp_path, monkeypatch) as client:
        def hit():
            results.append(client.get("/api/door-stats/daily").json()["generated_ts"])

        threads = [threading.Thread(target=hit) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert len(calls) == 1
    assert len(results) == 3 and len(set(results)) == 1
