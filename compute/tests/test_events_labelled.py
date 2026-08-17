"""``Store.events(with_labels=True)`` — the already-decided flag the phone's skip reads.

Covers the opt-in itself (an absent key is what tells the page a backend is older),
the ``(src_frame_id, src_recv_ts)`` pair keying that stops a pre-``clear()`` row
marking a brand-new visit as decided, and the QUERY PLAN — which nothing else can
catch, since the flag is identical whichever table the planner drives from.
See docs/specs/2026-08-17-user-skip-handled-visits.md.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from compute.collection.store import Store
from compute.ingest import StreamFrame
from shared.wire import StreamFrameMeta

_JPEG_BODY = b"\xff\xd8\xff\xe0" + b"fake-jpeg-body" + b"\xff\xd9"
_BASE = 1_700_000_000_000


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
    """n motion frames within the visit gap -> one event; returns their row ids."""
    return [store.add(_frame(i, base + i * 100, area=0.1 + i / 100),
                      recv_ts_ms=base + i * 100) for i in range(n)]


def _label(store: Store, frame_id: int, cat_id: int = 1) -> int:
    return store.add_dataset_items([{
        "frame_id": frame_id,
        "label_kind": "identified",
        "cat_id": cat_id,
        "quality": "gallery",
        "bbox": [0, 0, 8, 8],
        "crop_path": f"cat_{cat_id}/{frame_id}.jpg",
    }])


# --- the opt-in ---------------------------------------------------------------


def test_without_with_labels_the_key_is_absent(tmp_path):
    # Load-bearing: `cats_overview` and `door_stats` reuse this feed, and the page reads
    # an absent key as "this backend predates the feature" — so the default must not
    # merely be False, it must not be there at all.
    store = _store(tmp_path)
    ids = _visit(store, _BASE)
    _label(store, ids[1])

    ev = store.events(None, None)["events"][0]
    assert "labelled" not in ev


def test_the_two_internal_feed_callers_stay_opted_out(tmp_path):
    # `with_labels`' whole premise: the activity route pays for the flag, the two internal
    # reusers of this feed do not — `door_stats` pages it up to _MAX_STATS_PAGES times for
    # counts alone. Flipping either to the default would be silent, so it is pinned here by
    # counting the read itself rather than by inspecting the returned dicts (both callers
    # project the feed into their own shapes, so the key would not survive to be asserted).
    store = _store(tmp_path)
    ids = _visit(store, _BASE)
    _label(store, ids[1])

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
    assert not [s for s in seen if "FROM dataset_items d" in s], (
        "cats_overview/door_stats now pay for the labelled read they never look at"
    )


def test_with_labels_reports_false_then_true(tmp_path):
    store = _store(tmp_path)
    ids = _visit(store, _BASE)

    ev = store.events(None, None, with_labels=True)["events"][0]
    assert ev["labelled"] is False, "nothing labelled yet"

    assert _label(store, ids[1]) == 1
    ev = store.events(None, None, with_labels=True)["events"][0]
    assert ev["labelled"] is True


def test_a_label_in_another_visit_does_not_mark_this_one(tmp_path):
    # Per-span, not one envelope read over the page: a row between two visits, or in the
    # other visit, must not bleed across.
    store = _store(tmp_path)
    older = _visit(store, _BASE)
    newer = _visit(store, _BASE + 10 * 60 * 1000)   # well past _VISIT_GAP_MS
    _label(store, newer[0])

    events = store.events(None, None, with_labels=True)["events"]
    assert len(events) == 2
    by_start = {e["start_id"]: e["labelled"] for e in events}
    assert by_start[newer[0]] is True
    assert by_start[older[0]] is False


# --- the clear()-safe pair ----------------------------------------------------


def test_a_pre_clear_row_does_not_mark_a_reused_frame_id(tmp_path):
    # `clear()` wipes `frames` (ids restart at 1) but deliberately SPARES
    # `dataset_items`. Keyed on src_frame_id alone, the stale row would report the new
    # visit as already decided and the nav would skip a visit nobody has looked at
    # (changelog 490). The pair catches it: recv_ts cannot match.
    store = _store(tmp_path)
    ids = _visit(store, _BASE)
    _label(store, ids[1])
    assert store.events(None, None, with_labels=True)["events"][0]["labelled"] is True

    store.clear()
    fresh = _visit(store, _BASE + 24 * 3600 * 1000)
    assert fresh == ids, "ids did not restart — the test no longer reaches the condition"
    assert store.count_identified_crops(None)[0] > 0, "clear() must spare the labels this guards"

    ev = store.events(None, None, with_labels=True)["events"][0]
    assert ev["labelled"] is False


# --- the route ----------------------------------------------------------------


def test_api_events_carries_the_flag_on_every_event(tmp_path):
    from compute.api.app import create_app

    store = _store(tmp_path)
    ids = _visit(store, _BASE)
    _visit(store, _BASE + 10 * 60 * 1000)
    _label(store, ids[0])

    client = TestClient(create_app(store=store, start_collector=False))
    events = client.get("/api/events").json()["events"]
    assert len(events) == 2
    # Present on BOTH, not only the decided one: the page latches on key presence, so a
    # flag attached per-decided-event would read as an older build on a fresh store.
    assert all("labelled" in e for e in events)
    assert sum(1 for e in events if e["labelled"]) == 1


# --- the query plan (nothing else catches a regression here) -------------------


def test_the_labelled_read_never_drives_from_frames(tmp_path):
    """``dataset_items`` is the OUTER loop, seeking its id range; nothing is scanned.

    Measured plan on this schema (no ``ANALYZE``, matching the real store — changelog
    266)::

        SEARCH d USING COVERING INDEX idx_dataset_src (src_frame_id>? AND src_frame_id<?)
        SEARCH f USING COVERING INDEX idx_frames_recv_ts (recv_ts=? AND rowid=?)

    i.e. the work is proportional to the span's LABELLED rows, and neither table is
    touched outside an index. Driving from ``frames`` instead is the regression: SQLite
    transfers the range through the ``f.id = d.src_frame_id`` equality, so it is a seek
    rather than a full scan (measured — do not overstate it), but it then walks every
    FRAME in the span probing ``dataset_items`` per row, which is the visit's whole
    length instead of its handful of labels, once per span, under the shared write lock.
    Changelog 229/265/276/307/385/391's class: the returned flag is identical either
    way, which is how that class has recurred five times, so the plan is asserted rather
    than described. The default plan is already right on a 3-frame store, so this needs
    no big fixture.
    """
    store = _store(tmp_path)
    ids = _visit(store, _BASE)
    _label(store, ids[1])

    real = store._conn
    captured: "list[tuple[str, list]]" = []

    class Spy:
        def execute(self, sql, params=()):
            captured.append((sql, list(params)))
            return real.execute(sql, params)

        def __getattr__(self, n):
            return getattr(real, n)

    store._conn = Spy()
    try:
        store.events(None, None, with_labels=True)
    finally:
        store._conn = real

    reads = [(sql, params) for sql, params in captured if "FROM dataset_items d" in sql]
    assert len(reads) == 1, f"expected one per-span labelled read, got {len(reads)}"
    sql, params = reads[0]
    plan = [row[-1] for row in real.execute("EXPLAIN QUERY PLAN " + sql, params)]
    assert plan[0].startswith("SEARCH d USING COVERING INDEX idx_dataset_src"), (
        f"dataset_items is no longer the outer loop seeking its id range: {plan}"
    )
    assert any(p.startswith("SEARCH f ") for p in plan), plan
    assert not any(p.lstrip().startswith("SCAN") for p in plan), f"a table is scanned: {plan}"
