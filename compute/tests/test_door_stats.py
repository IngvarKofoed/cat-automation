"""Tests for the user dashboard's Visits page: ``Store.door_stats`` and
``GET /api/door-stats`` (compute/collection/store.py, compute/api/app.py).

``door_stats`` is a read-time projection over ``Store.events`` — the same clustering
and the same ``_aggregate_identity`` vote the Activity feed renders — so these tests
build ``StreamFrame`` objects directly and seed identities / detections with
``write_identifications_batch`` / ``write_analysis``, mirroring test_events.py and
test_event_subject_classification.py.

Covers: the trailing-window arithmetic (6 h nested in 24 h), the exclusive totals
ladder, per-cat counts + share + day/night, the honesty flags (``covered``,
``truncated``, uncalibrated model), and the route's day/night gate.
"""
from __future__ import annotations

import os

from fastapi.testclient import TestClient

from compute.collection.store import Store, _MAX_EVENTS, _MAX_STATS_PAGES, _VISIT_GAP_MS
from compute.ingest import StreamFrame
from shared.wire import StreamFrameMeta

_JPEG_BODY = b"\xff\xd8\xff\xe0" + b"fake-jpeg-body" + b"\xff\xd9"

_CAT = 15      # COCO cat
_PERSON = 0    # COCO person

# A fixed "now" so the windows are deterministic; every visit below is placed
# relative to it.
_NOW = 1_700_000_000_000
_HOUR = 3_600_000


def _frame(frame_id: int = 1, motion: bool = True, area: float = 0.1) -> StreamFrame:
    meta = StreamFrameMeta(frame_id=frame_id, ts=0, motion=motion, bbox=None, area=area)
    return StreamFrame(meta, _JPEG_BODY)


def _store(tmp_path, max_bytes: int = 10_000_000) -> Store:
    return Store(
        db_path=str(tmp_path / "index.db"),
        media_root=str(tmp_path / "media"),
        max_bytes=max_bytes,
    )


def _boxes_detail(boxes: "list[list[float]]") -> dict:
    return {"boxes": boxes}


def _add_version(store: Store, *, threshold: "float | None" = 0.5, gallery_dir: str = "g") -> int:
    """An ACTIVE gallery version (its gallery.npz materialised so active_model sees it)."""
    d = os.path.join(store.models_root, gallery_dir)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "gallery.npz"), "wb") as fh:
        fh.write(b"\x00")
    vid = store.add_model_version(
        status="draft", kind="gallery", backbone="dinov2_vits14", imgsz=224,
        n_cats=2, n_vectors=10, threshold=threshold, quality="gallery",
        metrics=None, gallery_dir=gallery_dir,
    )
    store.promote_model(vid)
    return vid


_next_id = [0]


def _visit(store: Store, at_ts: int, *, n: int = 2) -> "tuple[int, int]":
    """Add one motion cluster of ``n`` frames at ``at_ts`` → its (start_id, end_id).

    Frames are spaced well inside ``_VISIT_GAP_MS`` so they cluster, and successive
    calls must be placed further apart than the gap to stay separate visits.

    Call these in CHRONOLOGICAL order. The collector appends frames in receive order, so
    every windowed read (``resolve_ts_range`` above all) relies on ``recv_ts`` being
    non-decreasing with ``id``; seeding a test backwards puts the window's resolved
    ``since_id`` past the newest visit and silently drops it.
    """
    ids = []
    for i in range(n):
        _next_id[0] += 1
        ids.append(store.add(_frame(frame_id=_next_id[0]), recv_ts_ms=at_ts + i * 200))
    return ids[0], ids[-1]


def _cat_box(store: Store, span: "tuple[int, int]", conf: float = 0.9, cls: int = _CAT) -> None:
    """Give every frame in a span a yolo-serial detection of ``cls``."""
    for fid in range(span[0], span[1] + 1):
        store.write_analysis(
            fid, "yolo-serial", cls == _CAT, conf, _boxes_detail([[0, 0, 5, 5, conf, cls]])
        )


def _name(store: Store, span: "tuple[int, int]", vid: int, cat_id: int, dist: float = 0.1) -> None:
    """Identify every frame in a span as ``cat_id`` at ``dist``."""
    store.write_identifications_batch(
        [(fid, vid, cat_id, dist, [0, 0, 1, 1]) for fid in range(span[0], span[1] + 1)]
    )


def _stats(store: Store, **kw) -> dict:
    return store.door_stats(now_ms=_NOW, **kw)


# --- window arithmetic ----------------------------------------------------


def test_six_hour_window_is_nested_in_twenty_four(tmp_path):
    store = _store(tmp_path)
    vid = _add_version(store)
    cat = store.create_cat("Mittens", is_resident=True)["id"]

    ancient = _visit(store, _NOW - 30 * _HOUR)   # in neither window
    older = _visit(store, _NOW - 10 * _HOUR)     # in 24 h only
    recent = _visit(store, _NOW - 1 * _HOUR)     # in both
    for span in (ancient, older, recent):
        _cat_box(store, span)
        _name(store, span, vid, cat)

    result = _stats(store)

    assert result["totals"]["6h"]["resident"] == 1
    assert result["totals"]["24h"]["resident"] == 2  # recent + older, never ancient
    record = result["cats"][0]
    assert record["cat_id"] == cat and record["name"] == "Mittens"
    assert record["6h"]["visits"] == 1
    assert record["24h"]["visits"] == 2


def test_windows_report_since_ts_and_hours(tmp_path):
    store = _store(tmp_path)
    result = _stats(store)
    assert result["generated_ts"] == _NOW
    assert result["windows"]["6h"] == {
        "hours": 6, "since_ts": _NOW - 6 * _HOUR, "covered": False,
    }
    assert result["windows"]["24h"]["since_ts"] == _NOW - 24 * _HOUR


def test_covered_is_false_when_the_store_does_not_reach_back(tmp_path):
    store = _store(tmp_path)
    _visit(store, _NOW - 9 * _HOUR)  # oldest retained frame is 9 h old

    result = _stats(store)

    # The store reaches back past the 6 h bound but not the 24 h one, so a "last 24 h"
    # count here is a partial and must say so.
    assert result["windows"]["6h"]["covered"] is True
    assert result["windows"]["24h"]["covered"] is False
    assert result["store_oldest_ts"] == _NOW - 9 * _HOUR


def test_empty_store_returns_the_zeroed_shape(tmp_path):
    store = _store(tmp_path)
    store.create_cat("Mittens", is_resident=True)

    result = _stats(store)

    assert result["truncated"] is False
    assert result["store_oldest_ts"] is None
    assert result["totals"]["24h"]["door_events"] == 0
    assert result["cats"][0]["24h"] == {"visits": 0, "day": None, "night": None, "share": None}


def test_window_older_than_every_frame_reads_no_events_at_all(tmp_path, monkeypatch):
    """A store whose newest frame predates the window must not read ``events`` unbounded.

    ``resolve_ts_range`` returns ``since_id = None`` here, and ``events(None, until_id)``
    is UNBOUNDED on the since side — it would return the newest events in the whole
    store, which the per-window filter then has to throw away.

    Asserted on the CALL, not on the counts: the per-event ``start_ts < since_ts`` filter
    zeroes those totals whether or not the guard exists (verified by deleting the guard —
    the count-based version of this test still passed), so counts cannot tell the two
    apart. What the guard actually buys is not reading at all.
    """
    store = _store(tmp_path)
    vid = _add_version(store)
    cat = store.create_cat("Mittens", is_resident=True)["id"]
    span = _visit(store, _NOW - 40 * _HOUR)  # older than the widest window
    _cat_box(store, span)
    _name(store, span, vid, cat)

    calls = []
    real_events = store.events
    monkeypatch.setattr(
        store, "events",
        lambda *a, **kw: (calls.append(a), real_events(*a, **kw))[1],
    )
    result = _stats(store)

    assert calls == []  # the early return fired; no unbounded read was issued
    assert result["totals"]["24h"]["door_events"] == 0
    assert result["cats"][0]["24h"]["visits"] == 0


# --- the totals ladder ----------------------------------------------------


def test_totals_ladder_is_exclusive_and_sums_to_door_events(tmp_path):
    store = _store(tmp_path)
    vid = _add_version(store)
    resident = store.create_cat("Mittens", is_resident=True)["id"]
    neighbour = store.create_cat("Sultan", is_resident=False)["id"]

    # One visit per rung, each its own cluster (spaced past the gap), CHRONOLOGICAL.
    noise = _visit(store, _NOW - 5 * _HOUR - 120_000)
    for fid in range(noise[0], noise[1] + 1):  # swept, nothing found → below the floor
        store.write_analysis(fid, "yolo-serial", False, 0.0, _boxes_detail([]))

    _visit(store, _NOW - 5 * _HOUR - 60_000)  # never swept → unanalyzed

    person = _visit(store, _NOW - 5 * _HOUR)
    _cat_box(store, person, cls=_PERSON)

    unidentified = _visit(store, _NOW - 4 * _HOUR)
    _cat_box(store, unidentified)  # YOLO says cat, nothing identified it

    unknown = _visit(store, _NOW - 3 * _HOUR)
    _cat_box(store, unknown)
    _name(store, unknown, vid, resident, dist=0.9)  # beyond the 0.5 threshold

    n = _visit(store, _NOW - 2 * _HOUR)
    _cat_box(store, n)
    _name(store, n, vid, neighbour)

    r = _visit(store, _NOW - 1 * _HOUR)
    _cat_box(store, r)
    _name(store, r, vid, resident)

    totals = _stats(store)["totals"]["6h"]

    assert totals["resident"] == 1
    assert totals["neighbour"] == 1
    assert totals["unknown_cat"] == 1
    assert totals["unidentified"] == 1
    assert totals["other"] == 1        # the person
    assert totals["unanalyzed"] == 1
    assert totals["noise"] == 1
    assert totals["door_events"] == 7
    # Exclusive: the seven buckets sum to door_events, so the UI can derive any figure.
    assert sum(totals[b] for b in Store._STATS_BUCKETS) == totals["door_events"]
    # A person and a wind trigger are not cat visits.
    assert totals["cat_visits"] == 4


def test_person_has_no_figure_of_its_own(tmp_path):
    store = _store(tmp_path)
    span = _visit(store, _NOW - 1 * _HOUR)
    _cat_box(store, span, cls=_PERSON)

    totals = _stats(store)["totals"]["6h"]

    assert totals["other"] == 1
    assert "person" not in totals
    assert totals["cat_visits"] == 0


# --- per-cat records ------------------------------------------------------


def test_zero_visit_cat_still_gets_a_row_and_retired_cats_do_not(tmp_path):
    store = _store(tmp_path)
    store.create_cat("Mittens", is_resident=True)
    gone = store.create_cat("Ghost", is_resident=True)["id"]
    store.update_cat(gone, {"active": False})

    records = _stats(store)["cats"]

    assert [r["name"] for r in records] == ["Mittens"]
    assert records[0]["24h"]["visits"] == 0


def test_share_divides_by_named_visits_and_is_none_without_any(tmp_path):
    store = _store(tmp_path)
    vid = _add_version(store)
    a = store.create_cat("A", is_resident=True)["id"]
    b = store.create_cat("B", is_resident=True)["id"]

    # An unnamed cat visit: it inflates cat_visits but NOT the share denominator.
    _cat_box(store, _visit(store, _NOW - 5 * _HOUR))
    for i, cat in enumerate([a, a, a, b]):  # chronological: -4 h … -1 h
        span = _visit(store, _NOW - (4 - i) * _HOUR)
        _cat_box(store, span)
        _name(store, span, vid, cat)

    result = _stats(store)
    by_name = {r["name"]: r for r in result["cats"]}

    assert result["totals"]["6h"]["cat_visits"] == 5
    assert by_name["A"]["6h"]["share"] == 0.75  # 3 of 4 NAMED visits
    assert by_name["B"]["6h"]["share"] == 0.25


def test_share_is_none_not_zero_when_nothing_was_named(tmp_path):
    store = _store(tmp_path)
    store.create_cat("Mittens", is_resident=True)
    _cat_box(store, _visit(store, _NOW - 1 * _HOUR))  # a cat, but unnamed

    record = _stats(store)["cats"][0]

    # No named traffic means the share is UNMEASURED, not zero.
    assert record["6h"]["share"] is None
    assert record["6h"]["visits"] == 0


def test_day_night_split_buckets_a_visit_whole_by_its_first_frame(tmp_path):
    store = _store(tmp_path)
    vid = _add_version(store)
    cat = store.create_cat("Mittens", is_resident=True)["id"]
    night_span = _visit(store, _NOW - 2 * _HOUR)
    day_span = _visit(store, _NOW - 1 * _HOUR)
    for span in (night_span, day_span):
        _cat_box(store, span)
        _name(store, span, vid, cat)

    # Inject a classifier: everything at or before the night visit's start is night.
    result = _stats(store, is_night=lambda ts: ts <= _NOW - 2 * _HOUR)

    record = result["cats"][0]
    assert record["6h"] == {"visits": 2, "day": 1, "night": 1, "share": 1.0}


def test_no_classifier_leaves_day_night_none(tmp_path):
    store = _store(tmp_path)
    vid = _add_version(store)
    cat = store.create_cat("Mittens", is_resident=True)["id"]
    span = _visit(store, _NOW - 1 * _HOUR)
    _cat_box(store, span)
    _name(store, span, vid, cat)

    record = _stats(store)["cats"][0]

    # An absent split must never read as "all day".
    assert record["6h"]["visits"] == 1
    assert record["6h"]["day"] is None and record["6h"]["night"] is None


# --- model state ----------------------------------------------------------


def test_no_active_model_reports_none_and_names_nobody(tmp_path):
    store = _store(tmp_path)
    cat = store.create_cat("Mittens", is_resident=True)["id"]
    _cat_box(store, _visit(store, _NOW - 1 * _HOUR))

    result = _stats(store)

    assert result["model"] is None
    assert result["totals"]["6h"]["unidentified"] == 1
    assert result["totals"]["6h"]["resident"] == 0
    assert result["cats"][0]["cat_id"] == cat and result["cats"][0]["6h"]["visits"] == 0


def test_uncalibrated_model_resolves_every_visit_to_unknown(tmp_path):
    store = _store(tmp_path)
    vid = _add_version(store, threshold=None)  # uncomputable threshold
    cat = store.create_cat("Mittens", is_resident=True)["id"]
    span = _visit(store, _NOW - 1 * _HOUR)
    _cat_box(store, span)
    _name(store, span, vid, cat, dist=0.01)  # would be a confident match if calibrated

    result = _stats(store)

    assert result["model"] == {"id": vid, "calibrated": False}
    # The fail-safe carries over from events(): an uncalibrated gallery names nobody.
    assert result["totals"]["6h"]["unknown_cat"] == 1
    assert result["totals"]["6h"]["resident"] == 0
    assert result["cats"][0]["6h"]["visits"] == 0


# --- paging ---------------------------------------------------------------


def test_pages_past_one_events_call(tmp_path):
    """More visits than ``_MAX_EVENTS`` in the window are all counted, not just a page."""
    store = _store(tmp_path)
    n = _MAX_EVENTS + 25
    # One single-frame cluster each, spaced past the gap, all inside the 6 h window.
    for i in range(n):
        _visit(store, _NOW - 5 * _HOUR + i * (_VISIT_GAP_MS + 500), n=1)

    result = _stats(store)

    assert result["totals"]["6h"]["door_events"] == n
    assert result["truncated"] is False


def _fake_page(start_id: int) -> dict:
    """One synthetic events() record — enough keys for the classifier to bucket it."""
    return {
        "start_id": start_id, "end_id": start_id, "start_ts": _NOW - _HOUR,
        "n_frames": 1, "rep_frame_id": start_id,
        "identity": None, "subject": {"kind": "cat"},
    }


def test_truncated_when_the_page_budget_is_spent(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _visit(store, _NOW - _HOUR, n=1)  # so the window resolves to a since_id at all

    # Every page hands back one more event and claims more exists, with ids kept well
    # above the window floor — so the BUDGET is the only thing that can stop the walk.
    calls = [0]

    def fake_events(*a, **kw):
        calls[0] += 1
        return {"events": [_fake_page(10_000 - calls[0])], "truncated": True}

    monkeypatch.setattr(store, "events", fake_events)
    result = _stats(store)

    assert result["truncated"] is True
    assert calls[0] == _MAX_STATS_PAGES  # stopped at the budget, not before or after
    assert result["totals"]["6h"]["door_events"] == _MAX_STATS_PAGES


def test_a_page_reaching_the_window_floor_is_not_truncated(tmp_path, monkeypatch):
    """Paging down to the window's own floor counted everything — so no warning.

    The keyset bound is (oldest start_id − 1), which falls BELOW ``since_id`` when the
    oldest cluster starts at the window's first frame. Continuing would ask for an
    inverted range, get nothing back, and report ``truncated`` — putting a "figures are
    incomplete" banner on a complete reading.
    """
    store = _store(tmp_path)
    # One cluster only, and it starts at the store's very first frame — which is what
    # `resolve_ts_range` resolves the window's `since_id` to.
    _visit(store, _NOW - 1 * _HOUR, n=1)

    real_events = store.events
    monkeypatch.setattr(
        store, "events",
        lambda *a, **kw: {**real_events(*a, **kw), "truncated": True},
    )
    result = _stats(store)

    assert result["totals"]["6h"]["door_events"] == 1
    assert result["truncated"] is False


def test_truncated_with_an_empty_page_stops_rather_than_looping(tmp_path, monkeypatch):
    """A page that is empty yet claims truncation cannot be keyed off (CHANGELOG 271)."""
    store = _store(tmp_path)
    _visit(store, _NOW - 1 * _HOUR, n=1)

    calls = [0]

    def fake_events(*a, **kw):
        calls[0] += 1
        if calls[0] == 1:
            return {"events": [_fake_page(10_000)], "truncated": True}
        return {"events": [], "truncated": True}

    monkeypatch.setattr(store, "events", fake_events)
    result = _stats(store)

    assert result["truncated"] is True
    assert calls[0] == 2 < _MAX_STATS_PAGES  # stopped at the empty page, no spin


# --- GET /api/door-stats --------------------------------------------------


def _client(tmp_path, monkeypatch) -> TestClient:
    from compute.api.app import create_app

    monkeypatch.setenv("CAT_COLLECT_DIR", str(tmp_path))
    return TestClient(create_app(start_collector=False))


def test_route_reports_split_unavailable_without_a_location(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as client:
        body = client.get("/api/door-stats").json()

    assert body["split"] == {"available": False, "reason": "location_unset"}
    assert body["windows"]["6h"]["hours"] == 6 and body["windows"]["24h"]["hours"] == 24
    # Unavailable split → no day/night guesses anywhere.
    assert all(r["24h"]["day"] is None for r in body["cats"])


def test_route_reports_the_split_available_with_a_location(tmp_path, monkeypatch):
    from compute.analysis.suntimes import astral_available

    if not astral_available():
        import pytest

        pytest.skip("astral not installed")

    with _client(tmp_path, monkeypatch) as client:
        assert client.post(
            "/api/location", json={"latitude": 55.68, "longitude": 12.57}
        ).status_code == 200
        body = client.get("/api/door-stats").json()

    assert body["split"]["available"] is True
    assert body["split"]["location"] == {"latitude": 55.68, "longitude": 12.57}
