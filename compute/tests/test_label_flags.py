"""Tests for the user dashboard's "mark for labelling" flags — ``label_flags`` on
``compute/collection/store.py`` plus the ``/api/label/flags*`` routes.

See docs/specs/2026-07-30-user-flag-for-labelling.md. The three things the spec
calls out as worth their own tests:

1. OVERLAP dedup — a re-tap after the event's motion cluster has GROWN returns the
   existing flag rather than minting a second one, and un-mark clears by span.
2. The five-way coverage-derived STATE — a partly-swept span must never read as
   ``no_detection`` (unmeasured presenting as measured), and a flagged span is
   UNFLOORED, so a faint 0.2 cat box is labellable here though the queue hides it.
3. ``clear()`` DROPS the table — flags are frame-id-keyed work items, so a wipe that
   restarts rowids at 1 must not leave a flag pointing at brand-new frames.

Pure sqlite: no torch, no cv2, no model. Mirrors the suite's conventions
(test_annotation_p4.py) — ``_frame()`` builds StreamFrames directly.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from compute.collection.store import Store, _ANNOTATE_MIN_CONF

_JPEG_BODY = b"\xff\xd8\xff\xe0" + b"fake-jpeg-body" + b"\xff\xd9"


def _frame(frame_id: int = 1, ts: int = 1_000, motion: bool = True, area: float = 0.5):
    from compute.ingest import StreamFrame
    from shared.wire import StreamFrameMeta

    meta = StreamFrameMeta(frame_id=frame_id, ts=ts, motion=motion, bbox=None, area=area)
    return StreamFrame(meta, _JPEG_BODY)


def _store(tmp_path) -> Store:
    return Store(
        db_path=str(tmp_path / "index.db"),
        media_root=str(tmp_path / "media"),
        max_bytes=10_000_000,
    )


def _add(store: Store, recv_ts: int, *, edge_id: int = 1) -> int:
    return store.add(_frame(frame_id=edge_id, ts=recv_ts), recv_ts_ms=recv_ts)


def _sweep(store: Store, fid: int, score: "float | None") -> None:
    """Write a yolo-serial verdict: ``score=None`` = swept-but-empty, else a cat box."""
    if score is None:
        store.write_analysis(fid, "yolo-serial", False, None, {"boxes": []})
    else:
        store.write_analysis(
            fid, "yolo-serial", True, score, {"boxes": [[0, 0, 10, 10, score, 15]]}
        )


# --- 1. overlap dedup ------------------------------------------------------


def test_reflag_after_span_grows_returns_the_same_flag(tmp_path):
    """A re-tap on a GROWN event span is a no-op, not a second row.

    The whole reason identity is overlap and not an exact (start_id, end_id) pair:
    an event's motion cluster extends as later frames land within the visit gap, so
    the phone posts a wider span the second time.
    """
    store = _store(tmp_path)
    ids = [_add(store, 1_000 + i * 100) for i in range(6)]

    first = store.add_label_flag(ids[0], ids[2])
    assert first["created"] is True

    again = store.add_label_flag(ids[0], ids[5])  # same visit, grown span
    assert again["created"] is False
    assert again["id"] == first["id"]
    assert again["created_ts"] == first["created_ts"]  # not re-dated: not new work
    assert len(store.list_label_flags()) == 1


def test_flag_captures_start_ts_and_refuses_a_dead_span(tmp_path):
    store = _store(tmp_path)
    fid = _add(store, 5_000)

    flag = store.add_label_flag(fid, fid)
    assert flag["start_ts"] == 5_000  # captured, so a `gone` row can still say when

    # No live frame in the span at all → None (the route maps this to a 409).
    assert store.add_label_flag(fid + 500, fid + 900) is None
    with pytest.raises(ValueError):
        store.add_label_flag(9, 3)  # end before start


def test_flag_refuses_an_absurdly_wide_span(tmp_path):
    """A flag covers ONE visit, so an unbounded span is refused at the store.

    Nothing else bounds the client-supplied ids, and `_resolve_flag` materialises every
    live frame in the span — an absurd width would load the whole frames table into one
    response, and such a flag would overlap (so ⚑-mark) every event in the feed.
    """
    from compute.collection.store import _MAX_FLAG_SPAN

    store = _store(tmp_path)
    fid = _add(store, 1_000)
    with pytest.raises(ValueError, match="too wide"):
        store.add_label_flag(fid, fid + _MAX_FLAG_SPAN)
    assert store.add_label_flag(fid, fid + _MAX_FLAG_SPAN - 1) is not None  # at the bound


def test_unmark_clears_every_overlapping_flag(tmp_path):
    """Un-mark takes the span, so a visit with two overlapping flags fully clears.

    Two separately-flagged events can merge into one cluster later; deleting only
    the id the client holds would leave the ⚑ lit with no way to turn it off.
    """
    store = _store(tmp_path)
    ids = [_add(store, 1_000 + i * 100) for i in range(8)]
    store.add_label_flag(ids[0], ids[1])
    store.add_label_flag(ids[5], ids[6])
    assert len(store.list_label_flags()) == 2

    assert store.delete_label_flags_overlapping(ids[0], ids[7]) == 2
    assert store.list_label_flags() == []
    assert store.delete_label_flags_overlapping(ids[0], ids[7]) == 0  # idempotent

    # Width-bounded like add_label_flag: an id past SQLite's 64-bit INTEGER raises
    # OverflowError, which is NOT a ValueError and would escape the route as a 500.
    with pytest.raises(ValueError, match="too wide"):
        store.delete_label_flags_overlapping(1, 10 ** 19)


def test_delete_one_flag_by_id(tmp_path):
    store = _store(tmp_path)
    fid = _add(store, 1_000)
    flag = store.add_label_flag(fid, fid)
    assert store.delete_label_flag(flag["id"]) is True
    assert store.delete_label_flag(flag["id"]) is False


# --- 2. coverage-derived state ---------------------------------------------


def _state(store: Store) -> str:
    visits = store.flagged_visits()
    assert len(visits) == 1
    return visits[0]["state"]


def test_partly_swept_span_is_partial_not_no_detection(tmp_path):
    """The load-bearing honesty case: one swept frame among many unswept.

    Partial coverage is normal here (the live worker sweeps only inside visit spans,
    entry 76; the oracle worker is forward-only, entries 142/149). Reading it as
    `no_detection` would claim the detector rejected frames it never looked at.
    """
    store = _store(tmp_path)
    ids = [_add(store, 1_000 + i * 100) for i in range(5)]
    store.add_label_flag(ids[0], ids[-1])
    assert _state(store) == "unswept"

    _sweep(store, ids[0], None)  # 1 of 5 swept, nothing found
    assert _state(store) == "partial"

    for fid in ids[1:]:
        _sweep(store, fid, None)
    assert _state(store) == "no_detection"  # only now is it measured


def test_state_labellable_and_gone(tmp_path):
    store = _store(tmp_path)
    ids = [_add(store, 1_000 + i * 100) for i in range(3)]
    store.add_label_flag(ids[0], ids[-1])
    for fid in ids:
        _sweep(store, fid, 0.8)
    v = store.flagged_visits()[0]
    assert v["state"] == "labellable"
    assert [fr["id"] for fr in v["frames"]] == ids
    assert v["rep_frame_id"] in ids
    assert v["coverage"] == {"n_live": 3, "n_swept": 3, "n_boxed": 3}

    store.clear()
    # Re-flag against ids that no longer exist: resolve must not crash, and the span
    # falls back to the flag's captured start_ts so the row can still be read.
    store._conn.execute(
        "INSERT INTO label_flags (start_id, end_id, start_ts, created_ts) VALUES (?,?,?,?)",
        (900, 999, 4_242, 1),
    )
    store._conn.commit()
    v = store.flagged_visits()[0]
    assert v["state"] == "gone"
    assert v["frames"] == [] and v["rep_frame_id"] is None
    assert v["span"] == [4_242, 4_242]


def test_flagged_span_is_unfloored_where_the_queue_is_not(tmp_path):
    """A faint cat box is labellable when flagged, though the bulk queue hides it."""
    faint = _ANNOTATE_MIN_CONF - 0.1
    store = _store(tmp_path)
    fid = _add(store, 1_000)
    _sweep(store, fid, faint)

    # The queue floors it away entirely...
    assert store.annotation_queue_page("yolo-serial")["visits"] == []
    # ...while a flag on the same frame offers it for labelling, with its own score
    # visible so the operator can see how faint the box is.
    store.add_label_flag(fid, fid)
    v = store.flagged_visits()[0]
    assert v["state"] == "labellable"
    assert v["frames"][0]["score"] == pytest.approx(faint)


def test_current_label_reports_majority_and_flags_mixed(tmp_path):
    """A flagged motion span can hold two prior labelling gestures.

    One keypress rewrites both as one identity, so `mixed` has to say so — the same
    signal `labeled_visits` computes and the Labelled stage already renders.
    """
    store = _store(tmp_path)
    cat_a = store.create_cat("Alfa", is_resident=True)["id"]
    ids = [_add(store, 1_000 + i * 100) for i in range(3)]
    for fid in ids:
        _sweep(store, fid, 0.9)
    store.add_label_flag(ids[0], ids[-1])
    assert store.flagged_visits()[0]["current_label"] is None

    store.add_dataset_items([
        {"frame_id": ids[0], "label_kind": "identified", "cat_id": cat_a,
         "quality": "ok", "bbox": [0, 0, 10, 10], "crop_path": "x.jpg"},
        {"frame_id": ids[1], "label_kind": "identified", "cat_id": cat_a,
         "quality": "ok", "bbox": [0, 0, 10, 10], "crop_path": "y.jpg"},
        {"frame_id": ids[2], "label_kind": "not_cat"},
    ])
    label = store.flagged_visits()[0]["current_label"]
    assert label["label_kind"] == "identified" and label["cat_id"] == cat_a
    assert label["cat_name"] == "Alfa"
    assert label["n_frames"] == 3
    assert label["mixed"] is True


def test_flagged_visits_are_newest_flagged_first(tmp_path):
    store = _store(tmp_path)
    a, b = _add(store, 1_000), _add(store, 9_000)
    first = store.add_label_flag(a, a)
    second = store.add_label_flag(b, b)
    assert [v["flag_id"] for v in store.flagged_visits()] == [second["id"], first["id"]]


# --- 3. clear() drops the table -------------------------------------------


def test_clear_drops_flags(tmp_path):
    """Flags are frame-id-keyed, and clear() restarts rowids at 1.

    A surviving flag would point its span at brand-new unrelated frames — the reason
    groups/mode_changes/purge_spans are dropped too. Labels are NOT: they key on
    (src_frame_id, src_recv_ts), which stays collision-free.
    """
    store = _store(tmp_path)
    fid = _add(store, 1_000)
    store.add_label_flag(fid, fid)
    store.clear()
    assert store.list_label_flags() == []
    assert store.flagged_visits() == []


# --- the routes -----------------------------------------------------------


def _client(tmp_path) -> "tuple[TestClient, Store]":
    from compute.api.app import create_app

    store = _store(tmp_path)
    app = create_app(store=store, start_collector=False)
    return TestClient(app), store


def test_routes_round_trip_a_flag(tmp_path):
    client, store = _client(tmp_path)
    fid = _add(store, 1_000)

    r = client.post("/api/label/flags", json={"start_id": fid, "end_id": fid})
    assert r.status_code == 200, r.text
    assert r.json()["created"] is True

    # Idempotent: a double-tap returns the same flag, not a second.
    again = client.post("/api/label/flags", json={"start_id": fid, "end_id": fid})
    assert again.json()["id"] == r.json()["id"] and again.json()["created"] is False

    listed = client.get("/api/label/flags").json()
    assert listed["total"] == 1 and listed["flags"][0]["start_id"] == fid

    flagged = client.get("/api/label/flagged").json()
    assert flagged["total"] == 1 and flagged["visits"][0]["state"] == "unswept"

    assert client.post(
        "/api/label/flags/unmark", json={"start_id": fid, "end_id": fid}
    ).json() == {"deleted": 1}
    assert client.get("/api/label/flags").json()["total"] == 0


def test_route_rejects_a_dead_span_and_a_bad_one(tmp_path):
    client, store = _client(tmp_path)
    fid = _add(store, 1_000)

    # Ids that hold no live frame: 409 (legitimate ids, frames aged out), not a 404.
    dead = client.post("/api/label/flags", json={"start_id": fid + 50, "end_id": fid + 60})
    assert dead.status_code == 409
    assert "aged out" in dead.json()["detail"]

    assert client.post("/api/label/flags", json={"start_id": 9, "end_id": 3}).status_code == 400
    assert client.get("/api/label/flagged?oracle=nope").status_code == 400


def test_route_rejects_booleans_and_non_ids(tmp_path):
    """`true` must not coerce to frame id 1 (pydantic treats bool as an int subtype).

    The same guard `LocationRequest` carries: without it a malformed client would
    silently flag whatever visit sits at the start of the store.
    """
    client, store = _client(tmp_path)
    _add(store, 1_000)
    for body in ({"start_id": True, "end_id": True}, {"start_id": 0, "end_id": 3},
                 {"start_id": 1.7, "end_id": 6}, {}):
        assert client.post("/api/label/flags", json=body).status_code == 422, body
        assert client.post("/api/label/flags/unmark", json=body).status_code == 422, body
    assert store.list_label_flags() == []


def test_dismiss_one_flag_by_id(tmp_path):
    client, store = _client(tmp_path)
    fid = _add(store, 1_000)
    flag_id = client.post("/api/label/flags", json={"start_id": fid, "end_id": fid}).json()["id"]
    assert client.delete(f"/api/label/flags/{flag_id}").json() == {"deleted": True}
    assert client.delete(f"/api/label/flags/{flag_id}").json() == {"deleted": False}
