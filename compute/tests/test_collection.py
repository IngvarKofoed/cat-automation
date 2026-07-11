"""Tests for the frame-collection store and browse API (compute/collection/,
compute/api/app.py).

No real edge and no network: ``StreamFrame`` objects are built directly (a
Store.add caller only ever needs ``.meta``/``.jpeg``), and the FastAPI app is
built via ``create_app(store=..., start_collector=False)`` — the same
injection-seam pattern as the edge's ``create_app(source_factory,
start_grabber)`` — so no collector thread and no ``EdgeClient`` are created.
See docs/specs/2026-07-09-frame-collection-browser.md.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from compute.collection.store import Store
from compute.ingest import StreamFrame
from shared.wire import StreamFrameMeta

# A minimal but genuinely valid JPEG (SOI ... EOI) — Store.add writes it
# verbatim and never decodes it, so the body's realism doesn't matter, only
# that tests can tell distinct frames apart by length/content.
_JPEG_BODY = b"\xff\xd8\xff\xe0" + b"fake-jpeg-body" + b"\xff\xd9"


def _frame(
    frame_id: int = 1,
    ts: int = 1_000,
    motion: bool = False,
    bbox=None,
    area: float = 0.0,
    body: bytes = _JPEG_BODY,
) -> StreamFrame:
    """Build a ``StreamFrame`` directly — the shape ``Store.add`` consumes."""
    meta = StreamFrameMeta(frame_id=frame_id, ts=ts, motion=motion, bbox=bbox, area=area)
    return StreamFrame(meta, body)


# --- Store: add() ------------------------------------------------------------


def test_add_writes_file_and_row_with_meta_fields(tmp_path):
    store = Store(
        db_path=str(tmp_path / "index.db"),
        media_root=str(tmp_path / "media"),
        max_bytes=10_000_000,
    )
    frame = _frame(frame_id=42, ts=1234, motion=True, bbox=(0.1, 0.2, 0.3, 0.4), area=0.05)
    row_id = store.add(frame, recv_ts_ms=1_700_000_000_000)

    path = store.path_for(row_id)
    assert path is not None
    assert os.path.isfile(path)
    with open(path, "rb") as fh:
        assert fh.read() == _JPEG_BODY

    rows, _ = store.query(cursor=None, limit=10, motion="all", order="time")
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == row_id
    assert row["recv_ts"] == 1_700_000_000_000
    assert row["edge_ts"] == 1234
    assert row["frame_id"] == 42
    assert row["motion"] is True
    assert row["area"] == pytest.approx(0.05)
    assert row["bbox"] == [0.1, 0.2, 0.3, 0.4]
    assert row["url"] == f"/media/{row_id}"


def test_add_still_frame_has_no_bbox(tmp_path):
    store = Store(db_path=str(tmp_path / "index.db"), media_root=str(tmp_path / "media"), max_bytes=10_000_000)
    frame = _frame(motion=False, bbox=None, area=0.0)
    row_id = store.add(frame, recv_ts_ms=1_700_000_000_000)

    rows, _ = store.query(cursor=None, limit=10, motion="all", order="time")
    assert rows[0]["motion"] is False
    assert rows[0]["bbox"] is None
    assert rows[0]["id"] == row_id


# --- Store: retention ---------------------------------------------------------


def test_retention_evicts_oldest_rows_and_files_and_keeps_stats_correct(tmp_path):
    # Cap smaller than 2 frames' worth so every add past the first evicts.
    body_len = len(_JPEG_BODY)
    store = Store(
        db_path=str(tmp_path / "index.db"),
        media_root=str(tmp_path / "media"),
        max_bytes=int(body_len * 2.5),
    )

    ids = []
    paths = []
    for i in range(5):
        row_id = store.add(_frame(frame_id=i, ts=i), recv_ts_ms=1_700_000_000_000 + i)
        ids.append(row_id)
        paths.append(store.path_for(row_id))

    stats = store.stats()
    # Cap fits 2 frames; eviction runs after each add, so at most 2 remain.
    assert stats["count"] == 2
    assert stats["bytes"] == body_len * 2
    assert stats["cap_bytes"] == int(body_len * 2.5)

    # The two oldest ids (0, 1) were evicted; their rows AND files are gone.
    rows, _ = store.query(cursor=None, limit=10, motion="all", order="time")
    remaining_ids = {r["id"] for r in rows}
    assert remaining_ids == set(ids[-2:])
    for evicted_id, evicted_path in zip(ids[:-2], paths[:-2]):
        assert store.path_for(evicted_id) is None
        assert not os.path.isfile(evicted_path)

    # Surviving files are still on disk.
    for surviving_path in paths[-2:]:
        assert os.path.isfile(surviving_path)


def test_retention_running_total_survives_restart(tmp_path):
    db_path = str(tmp_path / "index.db")
    media_root = str(tmp_path / "media")
    store = Store(db_path=db_path, media_root=media_root, max_bytes=10_000_000)
    for i in range(3):
        store.add(_frame(frame_id=i), recv_ts_ms=1_700_000_000_000 + i)
    expected_bytes = store.stats()["bytes"]

    # A fresh Store over the same db/media recomputes the total from SUM(bytes).
    reopened = Store(db_path=db_path, media_root=media_root, max_bytes=10_000_000)
    assert reopened.stats()["bytes"] == expected_bytes
    assert reopened.stats()["count"] == 3


def test_eviction_survives_an_undeletable_media_file(tmp_path, monkeypatch):
    # If a media file can't be removed during eviction (a transient OSError, not
    # just FileNotFoundError), eviction must NOT roll back and resurrect the row —
    # the DB and byte total stay consistent and add() still succeeds. Guards the
    # best-effort _unlink (swallows OSError) against a regression to a narrow catch.
    body_len = len(_JPEG_BODY)
    store = Store(
        db_path=str(tmp_path / "index.db"),
        media_root=str(tmp_path / "media"),
        max_bytes=int(body_len * 1.5),  # room for exactly one frame
    )
    store.add(_frame(frame_id=0), recv_ts_ms=1_700_000_000_000)

    calls = {"n": 0}

    def flaky_remove(path):
        calls["n"] += 1
        raise PermissionError("cannot remove")

    monkeypatch.setattr("compute.collection.store.os.remove", flaky_remove)

    # Pushes over the cap → eviction tries to remove the oldest file, which now
    # raises. add() must still succeed and leave a consistent, single-row store.
    new_id = store.add(_frame(frame_id=1), recv_ts_ms=1_700_000_000_001)
    assert new_id is not None
    assert calls["n"] >= 1  # eviction did attempt a remove
    stats = store.stats()
    assert stats["count"] == 1
    assert stats["bytes"] == body_len


def test_eviction_does_not_drop_group_but_count_reflects_thinning(tmp_path):
    # Cap fits exactly 4 frames' worth; each add past that evicts exactly the
    # single oldest row (see the retention test above for the same arithmetic).
    # A group's bounds are id ranges, not a stored membership set, so eviction
    # (per the frame-range-groups spec) must NEVER cascade-delete the group
    # itself — only its live COUNT(*) should shrink as endpoint frames age out.
    body_len = len(_JPEG_BODY)
    store = Store(
        db_path=str(tmp_path / "index.db"),
        media_root=str(tmp_path / "media"),
        max_bytes=int(body_len * 4.5),
    )
    ids = [store.add(_frame(frame_id=i), recv_ts_ms=1_700_000_000_000 + i) for i in range(4)]
    group = store.create_group("thinning", ids[0], ids[3])
    assert group["count"] == 4

    # One more add pushes past the cap and evicts the oldest row (ids[0]), which
    # is the group's own start endpoint — the bookmark must still survive.
    store.add(_frame(frame_id=4), recv_ts_ms=1_700_000_000_004)
    groups = store.list_groups()
    assert len(groups) == 1  # NOT dropped by eviction
    assert groups[0]["id"] == group["id"]
    assert groups[0]["start_id"] == ids[0]  # bounds are untouched...
    assert groups[0]["count"] == 3          # ...but the live count already shrank

    # Evict the rest of the group's span (ids[1], ids[2], ids[3]) one at a time.
    store.add(_frame(frame_id=5), recv_ts_ms=1_700_000_000_005)
    store.add(_frame(frame_id=6), recv_ts_ms=1_700_000_000_006)
    store.add(_frame(frame_id=7), recv_ts_ms=1_700_000_000_007)

    groups = store.list_groups()
    assert len(groups) == 1  # a wholly-evicted group is reported empty, not deleted
    assert groups[0]["count"] == 0


# --- Store: clear() -----------------------------------------------------------


def test_clear_removes_all_rows_and_files_and_returns_count(tmp_path):
    store = Store(db_path=str(tmp_path / "index.db"), media_root=str(tmp_path / "media"), max_bytes=10_000_000)
    paths = [store.path_for(store.add(_frame(frame_id=i), recv_ts_ms=1_700_000_000_000 + i)) for i in range(4)]

    deleted = store.clear()
    assert deleted == 4

    for path in paths:
        assert not os.path.isfile(path)

    rows, _ = store.query(cursor=None, limit=10, motion="all", order="time")
    assert rows == []
    stats = store.stats()
    assert stats["count"] == 0
    assert stats["bytes"] == 0
    assert stats["motion_count"] == 0
    assert stats["oldest_ts"] is None
    assert stats["newest_ts"] is None


def test_clear_also_drops_groups(tmp_path):
    # A full clear() must wipe saved groups too — unlike eviction (see the
    # retention test above). After a clear, SQLite reuses rowids from 1, so a
    # stale group's old [start_id, end_id] would otherwise spuriously match
    # brand-new, unrelated frames.
    store = Store(db_path=str(tmp_path / "index.db"), media_root=str(tmp_path / "media"), max_bytes=10_000_000)
    ids = [store.add(_frame(frame_id=i), recv_ts_ms=1_700_000_000_000 + i) for i in range(3)]
    store.create_group("wiped on clear", ids[0], ids[2])
    assert len(store.list_groups()) == 1

    store.clear()
    assert store.list_groups() == []


# --- Store: query() -----------------------------------------------------------


@pytest.fixture
def populated_store(tmp_path):
    """A store with a mix of motion/still frames at varied areas, oldest→newest."""
    store = Store(db_path=str(tmp_path / "index.db"), media_root=str(tmp_path / "media"), max_bytes=10_000_000)
    # (motion, area) tuples in insertion order — id 1..6.
    specs = [
        (False, 0.10),
        (True, 0.02),
        (False, 0.30),
        (True, 0.50),
        (False, 0.01),
        (True, 0.20),
    ]
    ids = []
    for i, (motion, area) in enumerate(specs):
        bbox = (0.0, 0.0, 0.1, 0.1) if motion else None
        row_id = store.add(
            _frame(frame_id=i, ts=1000 + i, motion=motion, bbox=bbox, area=area),
            recv_ts_ms=1_700_000_000_000 + i,
        )
        ids.append(row_id)
    return store, ids, specs


def test_query_motion_filter_all_motion_still(populated_store):
    store, ids, specs = populated_store

    rows, _ = store.query(cursor=None, limit=100, motion="all", order="time")
    assert len(rows) == 6

    rows, _ = store.query(cursor=None, limit=100, motion="motion", order="time")
    assert {r["id"] for r in rows} == {ids[i] for i, (m, _) in enumerate(specs) if m}
    assert all(r["motion"] is True for r in rows)

    rows, _ = store.query(cursor=None, limit=100, motion="still", order="time")
    assert {r["id"] for r in rows} == {ids[i] for i, (m, _) in enumerate(specs) if not m}
    assert all(r["motion"] is False for r in rows)


def test_query_order_time_is_newest_first(populated_store):
    store, ids, _ = populated_store
    rows, next_cursor = store.query(cursor=None, limit=100, motion="all", order="time")
    assert [r["id"] for r in rows] == list(reversed(ids))
    # Fewer rows than the limit → no more pages.
    assert next_cursor is None


def test_query_order_area_desc_and_asc(populated_store):
    store, ids, specs = populated_store

    rows, next_cursor = store.query(cursor=None, limit=100, motion="all", order="area_desc")
    areas = [r["area"] for r in rows]
    assert areas == sorted(areas, reverse=True)
    assert next_cursor is None  # all 6 rows fit one page (< limit) → no more

    rows, next_cursor = store.query(cursor=None, limit=100, motion="all", order="area_asc")
    areas = [r["area"] for r in rows]
    assert areas == sorted(areas)
    assert next_cursor is None


def test_query_area_order_respects_limit_and_motion_filter(populated_store):
    store, ids, specs = populated_store
    # Top 2 by area among motion-only frames: areas 0.02, 0.20, 0.50 -> top 2 desc.
    rows, _ = store.query(cursor=None, limit=2, motion="motion", order="area_desc")
    assert len(rows) == 2
    assert [r["area"] for r in rows] == [0.50, 0.20]


def test_query_area_order_keyset_pagination(populated_store):
    # area_desc over all: 0.50(id4), 0.30(id3), 0.20(id6), 0.10(id1), 0.02(id2), 0.01(id5).
    # Walking with limit 2 must cover every frame once, in strict area-desc order,
    # across a compound (area, id) keyset — no gaps or dupes at page edges.
    store, ids, _ = populated_store

    page1, c1 = store.query(cursor=None, limit=2, motion="all", order="area_desc")
    assert [r["id"] for r in page1] == [ids[3], ids[2]]
    assert c1 is not None

    page2, c2 = store.query(cursor=c1, limit=2, motion="all", order="area_desc")
    assert [r["id"] for r in page2] == [ids[5], ids[0]]

    page3, c3 = store.query(cursor=c2, limit=2, motion="all", order="area_desc")
    assert [r["id"] for r in page3] == [ids[1], ids[4]]

    page4, c4 = store.query(cursor=c3, limit=2, motion="all", order="area_desc")
    assert page4 == []
    assert c4 is None

    walked = [r["area"] for r in page1 + page2 + page3]
    assert walked == sorted(walked, reverse=True)
    assert len(walked) == len(ids)


def test_query_time_order_keyset_pagination(populated_store):
    store, ids, _ = populated_store
    # ids are 1..6 (newest = 6). Page 1: limit 2 -> ids [6, 5], next_cursor = "5"
    # (opaque string token now, not a bare int).
    page1, cursor1 = store.query(cursor=None, limit=2, motion="all", order="time")
    assert [r["id"] for r in page1] == [ids[5], ids[4]]
    assert cursor1 == str(ids[4])

    # Page 2 passes the token back: ids < cursor1 -> [4, 3].
    page2, cursor2 = store.query(cursor=cursor1, limit=2, motion="all", order="time")
    assert [r["id"] for r in page2] == [ids[3], ids[2]]
    assert cursor2 == str(ids[2])

    # Page 3: [2, 1], exactly limit-sized so there's a token, but the next page
    # after is short (empty).
    page3, cursor3 = store.query(cursor=cursor2, limit=2, motion="all", order="time")
    assert [r["id"] for r in page3] == [ids[1], ids[0]]
    assert cursor3 == str(ids[0])

    page4, cursor4 = store.query(cursor=cursor3, limit=2, motion="all", order="time")
    assert page4 == []
    assert cursor4 is None


def test_query_rejects_invalid_motion_and_order(tmp_path):
    store = Store(db_path=str(tmp_path / "index.db"), media_root=str(tmp_path / "media"), max_bytes=10_000_000)
    with pytest.raises(ValueError):
        store.query(cursor=None, limit=10, motion="bogus", order="time")
    with pytest.raises(ValueError):
        store.query(cursor=None, limit=10, motion="all", order="bogus")


# --- Store: query() range scoping (since_id/until_id) -------------------------
#
# The frame-range-groups spec's scoping half: an optional inclusive id range
# that a group (or a pending, unsaved selection) expands into. None on both
# sides must remain the whole-store feed, unchanged (see the plain query()
# tests above); these cover the new floor/ceiling and their interaction with
# keyset paging.


def test_query_scoped_by_since_and_until_id_returns_only_in_range_rows(populated_store):
    store, ids, _specs = populated_store

    # Bounded on both sides: only the middle four survive (ids[1]..ids[4]).
    rows, _ = store.query(
        cursor=None, limit=100, motion="all", order="time", since_id=ids[1], until_id=ids[4]
    )
    assert [r["id"] for r in rows] == list(reversed(ids[1:5]))

    # since_id only: unbounded above.
    rows, _ = store.query(cursor=None, limit=100, motion="all", order="time", since_id=ids[1])
    assert [r["id"] for r in rows] == list(reversed(ids[1:]))

    # until_id only: unbounded below.
    rows, _ = store.query(cursor=None, limit=100, motion="all", order="time", until_id=ids[4])
    assert [r["id"] for r in rows] == list(reversed(ids[:5]))

    # Both None (today's default) is still the whole store.
    rows, _ = store.query(cursor=None, limit=100, motion="all", order="time")
    assert [r["id"] for r in rows] == list(reversed(ids))


def test_query_time_order_keyset_pagination_stays_within_scope(populated_store):
    # Same walk as test_query_time_order_keyset_pagination above, but bounded to
    # the middle four ids (ids[1]..ids[4]) — paging must never surface ids[0] or
    # ids[5], and must terminate once the WINDOW (not the whole store) is
    # exhausted.
    store, ids, _ = populated_store
    since_id, until_id = ids[1], ids[4]

    page1, cursor1 = store.query(
        cursor=None, limit=2, motion="all", order="time", since_id=since_id, until_id=until_id
    )
    assert [r["id"] for r in page1] == [ids[4], ids[3]]
    assert cursor1 == str(ids[3])

    page2, cursor2 = store.query(
        cursor=cursor1, limit=2, motion="all", order="time", since_id=since_id, until_id=until_id
    )
    assert [r["id"] for r in page2] == [ids[2], ids[1]]
    assert cursor2 == str(ids[1])  # exactly limit-sized -> still a token

    page3, cursor3 = store.query(
        cursor=cursor2, limit=2, motion="all", order="time", since_id=since_id, until_id=until_id
    )
    assert page3 == []
    assert cursor3 is None


# --- Store: query_disagreements() range scoping --------------------------------


def test_query_disagreements_scoped_by_since_and_until_id(tmp_path):
    store = Store(db_path=str(tmp_path / "index.db"), media_root=str(tmp_path / "media"), max_bytes=10_000_000)
    # Six still frames, every one an oracle-present "missed" disagreement
    # (motion=0, verdict=1) — scoping is the only thing narrowing the result.
    ids = []
    for i in range(6):
        row_id = store.add(_frame(frame_id=i, ts=1000 + i, motion=False), recv_ts_ms=1_700_000_000_000 + i)
        store.write_analysis(row_id, "yolo", True, 0.9, None)
        ids.append(row_id)

    since_id, until_id = ids[1], ids[4]
    rows, _ = store.query_disagreements(
        "yolo", "missed", cursor=None, limit=100, since_id=since_id, until_id=until_id
    )
    assert {r["id"] for r in rows} == set(ids[1:5])

    # Keyset paging stays within the scope across a full walk to exhaustion.
    page1, c1 = store.query_disagreements(
        "yolo", "missed", cursor=None, limit=2, since_id=since_id, until_id=until_id
    )
    assert [r["id"] for r in page1] == [ids[4], ids[3]]
    assert c1 == str(ids[3])

    page2, c2 = store.query_disagreements(
        "yolo", "missed", cursor=c1, limit=2, since_id=since_id, until_id=until_id
    )
    assert [r["id"] for r in page2] == [ids[2], ids[1]]
    assert c2 == str(ids[1])

    page3, c3 = store.query_disagreements(
        "yolo", "missed", cursor=c2, limit=2, since_id=since_id, until_id=until_id
    )
    assert page3 == []
    assert c3 is None


# --- Store: path_for() ---------------------------------------------------------


def test_path_for_unknown_id_returns_none(tmp_path):
    store = Store(db_path=str(tmp_path / "index.db"), media_root=str(tmp_path / "media"), max_bytes=10_000_000)
    assert store.path_for(999) is None


# --- Store: frame-range groups (create/list/delete) ----------------------------
#
# A group is a named, contiguous [start_id, end_id] bookmark (see the
# frame-range-groups spec) — no membership set, just bounds resolved against
# the live frames table.


def test_create_group_resolves_start_end_ts_from_endpoint_frames(tmp_path):
    store = Store(db_path=str(tmp_path / "index.db"), media_root=str(tmp_path / "media"), max_bytes=10_000_000)
    ids = [store.add(_frame(frame_id=i), recv_ts_ms=1_700_000_000_000 + i * 100) for i in range(5)]

    group = store.create_group("dusk visit", ids[1], ids[3])
    assert group["start_id"] == ids[1]
    assert group["end_id"] == ids[3]
    assert group["start_ts"] == 1_700_000_000_000 + 100  # ids[1]'s recv_ts
    assert group["end_ts"] == 1_700_000_000_000 + 300    # ids[3]'s recv_ts
    assert group["name"] == "dusk visit"
    assert group["count"] == 3  # ids[1], ids[2], ids[3]
    assert "created_ts" in group


def test_create_group_normalizes_endpoints_regardless_of_arg_order(tmp_path):
    store = Store(db_path=str(tmp_path / "index.db"), media_root=str(tmp_path / "media"), max_bytes=10_000_000)
    ids = [store.add(_frame(frame_id=i), recv_ts_ms=1_700_000_000_000 + i * 100) for i in range(5)]

    # The two endpoint clicks can arrive in either order — the later frame
    # passed as start_id must still resolve to start_id=min/end_id=max.
    forward = store.create_group("forward", ids[1], ids[3])
    backward = store.create_group("backward", ids[3], ids[1])
    for group in (forward, backward):
        assert group["start_id"] == ids[1]
        assert group["end_id"] == ids[3]
        assert group["start_ts"] == 1_700_000_000_000 + 100
        assert group["end_ts"] == 1_700_000_000_000 + 300


def test_create_group_raises_on_unknown_endpoint(tmp_path):
    store = Store(db_path=str(tmp_path / "index.db"), media_root=str(tmp_path / "media"), max_bytes=10_000_000)
    valid_id = store.add(_frame(frame_id=0), recv_ts_ms=1_700_000_000_000)

    with pytest.raises(ValueError):
        store.create_group("bad end", valid_id, 999_999)
    with pytest.raises(ValueError):
        store.create_group("bad start", 999_999, valid_id)
    with pytest.raises(ValueError):
        store.create_group("both bad", 999_998, 999_999)


def test_list_groups_returns_newest_first_with_live_count(tmp_path):
    store = Store(db_path=str(tmp_path / "index.db"), media_root=str(tmp_path / "media"), max_bytes=10_000_000)
    ids = [store.add(_frame(frame_id=i), recv_ts_ms=1_700_000_000_000 + i * 100) for i in range(6)]

    first = store.create_group("first", ids[0], ids[2])
    second = store.create_group("second", ids[2], ids[4])
    third = store.create_group("third", ids[3], ids[5])

    groups = store.list_groups()
    assert [g["id"] for g in groups] == [third["id"], second["id"], first["id"]]
    assert [g["name"] for g in groups] == ["third", "second", "first"]
    assert groups[0]["count"] == 3  # third: ids[3], ids[4], ids[5]
    assert groups[1]["count"] == 3  # second: ids[2], ids[3], ids[4]
    assert groups[2]["count"] == 3  # first: ids[0], ids[1], ids[2]


def test_delete_group_removes_it(tmp_path):
    store = Store(db_path=str(tmp_path / "index.db"), media_root=str(tmp_path / "media"), max_bytes=10_000_000)
    ids = [store.add(_frame(frame_id=i), recv_ts_ms=1_700_000_000_000 + i * 100) for i in range(3)]
    group = store.create_group("temp", ids[0], ids[2])

    deleted = store.delete_group(group["id"])
    assert deleted == 1
    assert store.list_groups() == []

    # Idempotent: deleting an already-gone (or never-existing) id is a 0
    # rowcount, not an error.
    assert store.delete_group(group["id"]) == 0
    assert store.delete_group(999_999) == 0


# --- Store: count_in_range() ----------------------------------------------------


def test_count_in_range_with_and_without_each_bound(tmp_path):
    store = Store(db_path=str(tmp_path / "index.db"), media_root=str(tmp_path / "media"), max_bytes=10_000_000)
    ids = [store.add(_frame(frame_id=i), recv_ts_ms=1_700_000_000_000 + i) for i in range(5)]

    assert store.count_in_range() == 5                              # both None -> whole store
    assert store.count_in_range(since_id=ids[2]) == 3                # ids[2], ids[3], ids[4]
    assert store.count_in_range(until_id=ids[2]) == 3                # ids[0], ids[1], ids[2]
    assert store.count_in_range(since_id=ids[1], until_id=ids[3]) == 3  # ids[1..3]
    assert store.count_in_range(since_id=ids[4] + 1) == 0            # past the newest id
    assert store.count_in_range(until_id=ids[0] - 1) == 0            # before the oldest id


# --- API ------------------------------------------------------------------


@pytest.fixture
def api_client(tmp_path):
    """A TestClient over a fresh Store, with no collector thread and no edge."""
    from compute.api.app import create_app

    store = Store(db_path=str(tmp_path / "index.db"), media_root=str(tmp_path / "media"), max_bytes=10_000_000)
    app = create_app(store=store, start_collector=False)
    return TestClient(app), store


def test_api_stats_shape(api_client):
    client, store = api_client
    store.add(_frame(frame_id=1, motion=True, area=0.1, bbox=(0, 0, 1, 1)), recv_ts_ms=1_700_000_000_000)
    store.add(_frame(frame_id=2, motion=False, area=0.0), recv_ts_ms=1_700_000_000_100)

    resp = client.get("/api/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 2
    assert body["motion_count"] == 1
    assert body["cap_bytes"] == 10_000_000
    assert body["bytes"] > 0
    assert body["oldest_ts"] == 1_700_000_000_000
    assert body["newest_ts"] == 1_700_000_000_100


def test_api_frames_filtering_and_shape(api_client):
    client, store = api_client
    id_motion = store.add(
        _frame(frame_id=1, motion=True, area=0.2, bbox=(0.1, 0.1, 0.2, 0.2)),
        recv_ts_ms=1_700_000_000_000,
    )
    id_still = store.add(_frame(frame_id=2, motion=False, area=0.0), recv_ts_ms=1_700_000_000_100)

    resp = client.get("/api/frames", params={"motion": "all", "order": "time"})
    assert resp.status_code == 200
    body = resp.json()
    assert "next_cursor" in body
    ids = [f["id"] for f in body["frames"]]
    assert ids == [id_still, id_motion]  # newest first
    frame = body["frames"][0]
    assert set(frame.keys()) == {"id", "recv_ts", "edge_ts", "frame_id", "motion", "area", "bbox", "url"}

    resp = client.get("/api/frames", params={"motion": "motion", "order": "time"})
    body = resp.json()
    assert [f["id"] for f in body["frames"]] == [id_motion]

    resp = client.get("/api/frames", params={"motion": "still", "order": "time"})
    body = resp.json()
    assert [f["id"] for f in body["frames"]] == [id_still]


def test_api_frames_invalid_filter_is_400(api_client):
    client, _store = api_client
    resp = client.get("/api/frames", params={"motion": "bogus"})
    assert resp.status_code == 400


def test_api_media_returns_bytes_and_404s_on_unknown(api_client):
    client, store = api_client
    row_id = store.add(_frame(), recv_ts_ms=1_700_000_000_000)

    resp = client.get(f"/media/{row_id}")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/jpeg"
    assert resp.content == _JPEG_BODY

    resp = client.get("/media/999999")
    assert resp.status_code == 404


def test_api_clear_empties_the_store(api_client):
    client, store = api_client
    store.add(_frame(frame_id=1), recv_ts_ms=1_700_000_000_000)
    store.add(_frame(frame_id=2), recv_ts_ms=1_700_000_000_100)
    assert store.stats()["count"] == 2

    resp = client.post("/api/clear")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"ok": True, "deleted": 2}
    assert store.stats()["count"] == 0
