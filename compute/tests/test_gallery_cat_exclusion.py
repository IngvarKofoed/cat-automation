"""Tests for excluding a cat from a gallery build / validation run
(docs/specs/2026-08-03-gallery-build-cat-exclusion.md).

Three layers, all torch-free:

- **The store filter** — ``labeled_crops`` / ``count_identified_crops`` must agree
  exactly (the pre-check guards the job, and an exclusion is the first build parameter
  that can drop whole CATS), and ``enrollable_cats`` is the list the panel decides from.
- **The manager** — the exclusion belongs to the artifact's identity: it lands in the
  dedup key (sorted, so tick order is not identity) and in the artifact dir slug.
  Exercised with the same FAKE builder/probe style as ``test_training_jobs.py``.
- **The endpoints** — the exclude-list's wire contract: unknown id 400, retired id
  no-op, boolean rejected, and the two-cat floor re-checked WITH the exclusion applied.

The real ``build_gallery`` / ``run_feasibility_probe`` embed paths need torch and run on
the compute PC; what is checked here is that the ids reach ``labeled_crops`` (via a stub
store) and that nothing else in those functions branches on them.
"""
from __future__ import annotations

import os
import time

import pytest
from fastapi.testclient import TestClient

from compute.collection.store import Store
from compute.identification.probe import run_feasibility_probe
from compute.ingest.client import StreamFrame, StreamFrameMeta
from compute.learning.runner import TrainingManager

_JPEG = b"\xff\xd8\xff\xd9"  # minimal SOI+EOI; store.add writes bytes verbatim (no decode)


def _store(tmp_path) -> Store:
    return Store(
        db_path=str(tmp_path / "index.db"),
        media_root=str(tmp_path / "media"),
        max_bytes=10_000_000,
    )


def _add_frame(store: Store, fid: int) -> int:
    meta = StreamFrameMeta(frame_id=fid, ts=fid, motion=False, bbox=None, area=0.0)
    return store.add(StreamFrame(meta, _JPEG), recv_ts_ms=1000 + fid)


def _label(store: Store, cat_id: "int | None", fid: int, quality: str = "gallery") -> None:
    """One ``identified`` (or catless ``unknown_cat``) crop row anchored to a fresh frame.

    Each call is its own ``add_dataset_items`` commit, so ``labeled_ts`` differs per call
    — which is what ``enrollable_cats``' ``label_commits`` counts.
    """
    row_id = _add_frame(store, fid)
    kind = "identified" if cat_id is not None else "unknown_cat"
    n = store.add_dataset_items(
        [
            {
                "frame_id": row_id,
                "label_kind": kind,
                "cat_id": cat_id,
                "quality": quality,
                "bbox": [0, 0, 10, 10],
                "crop_path": f"cat_{cat_id}/{fid}.jpg",
            }
        ]
    )
    assert n == 1


# --- The store filter -----------------------------------------------------------------


def test_labeled_crops_exclusion_drops_that_cat_only(tmp_path):
    store = _store(tmp_path)
    a, b = store.create_cat("A"), store.create_cat("B")
    _label(store, a["id"], 1)
    _label(store, b["id"], 2)
    _label(store, b["id"], 3)

    kept = store.labeled_crops(("identified",), exclude_cat_ids=(b["id"],))
    assert [r["cat_name"] for r in kept] == ["A"]
    # None and [] both mean "exclude nothing" — the field fails toward enrolling everyone.
    assert len(store.labeled_crops(("identified",), exclude_cat_ids=None)) == 3
    assert len(store.labeled_crops(("identified",), exclude_cat_ids=())) == 3
    # Duplicates are harmless, and an id naming no crop simply removes nothing.
    assert len(store.labeled_crops(("identified",), exclude_cat_ids=(b["id"], b["id"]))) == 1
    assert len(store.labeled_crops(("identified",), exclude_cat_ids=(999,))) == 3


def test_labeled_crops_exclusion_keeps_catless_crops(tmp_path):
    # NULL-safety, the same trap `active_only` has: the join is a LEFT JOIN, so an
    # `unknown_cat` crop has a NULL cat_id — and a bare NOT IN would drop every one of
    # them, which is not an excluded cat's crop.
    store = _store(tmp_path)
    a = store.create_cat("A")
    _label(store, a["id"], 1)
    _label(store, None, 2)

    rows = store.labeled_crops(("identified", "unknown_cat"), exclude_cat_ids=(a["id"],))
    assert [r["label_kind"] for r in rows] == ["unknown_cat"]


def test_count_identified_crops_exclusion_matches_labeled_crops(tmp_path):
    # The pre-check must count EXACTLY what the build embeds, or a guard that ignored the
    # exclusion would wave through a build whose surviving set falls under the two-cat
    # floor — the reason this parameter is pre-checked at all.
    store = _store(tmp_path)
    a, b, c = store.create_cat("A"), store.create_cat("B"), store.create_cat("C")
    _label(store, a["id"], 1)
    _label(store, b["id"], 2)
    _label(store, c["id"], 3)
    _label(store, c["id"], 4)

    for excluded in ((), (c["id"],), (b["id"], c["id"])):
        counted = store.count_identified_crops(
            None, active_only=True, exclude_cat_ids=excluded or None
        )
        embedded = store.labeled_crops(
            ("identified",), None, active_only=True, exclude_cat_ids=excluded or None
        )
        assert counted == (len(embedded), len({r["cat_id"] for r in embedded}))

    # Excluding two of three cats breaks the two-cat floor — which capping never could.
    assert store.count_identified_crops(
        None, active_only=True, exclude_cat_ids=(b["id"], c["id"])
    ) == (1, 1)


def test_count_identified_crops_exclusion_composes_with_grades(tmp_path):
    store = _store(tmp_path)
    a, b = store.create_cat("A"), store.create_cat("B")
    _label(store, a["id"], 1, quality="gallery")
    _label(store, b["id"], 2, quality="gallery")
    _label(store, b["id"], 3, quality="poor")

    assert store.count_identified_crops(("gallery",), exclude_cat_ids=(b["id"],)) == (1, 1)
    assert store.count_identified_crops(("poor",), exclude_cat_ids=(b["id"],)) == (0, 0)


# --- enrollable_cats ------------------------------------------------------------------


def test_enrollable_cats_counts_crops_and_commits_per_grade(tmp_path):
    store = _store(tmp_path)
    a, b = store.create_cat("A"), store.create_cat("B")
    store.update_cat(b["id"], {"is_resident": True})
    # `labeled_ts` is stamped in MILLISECONDS per commit, so the sleeps are what make two
    # commits distinguishable here; a human keypress is never this fast in production.
    for cid, fid, q in ((a["id"], 1, "gallery"), (a["id"], 2, "poor"),
                        (b["id"], 3, "gallery"), (None, 4, "gallery")):
        _label(store, cid, fid, quality=q)
        time.sleep(0.002)

    rows = store.enrollable_cats()
    assert [r["cat_id"] for r in rows] == [a["id"], b["id"]]  # creation order, like list_cats
    assert [r["crops"] for r in rows] == [2, 1]
    # One commit per label keypress, so distinct labeled_ts values stand in for visits.
    assert [r["label_commits"] for r in rows] == [2, 1]
    assert [r["is_resident"] for r in rows] == [False, True]

    # The counts follow the grade selection — the whole point, since a cat can have 50
    # labelled crops and 17 at gallery grade, and a build enrols the 17.
    at_gallery = store.enrollable_cats(("gallery",))
    assert [r["crops"] for r in at_gallery] == [1, 1]
    assert [r["crops"] for r in store.enrollable_cats(("poor",))] == [1, 0]


def test_enrollable_cats_matches_what_a_build_would_enrol(tmp_path):
    # The list's numbers and the build's crop set must be the same universe, or the
    # operator decides on one number while the build enrols another.
    store = _store(tmp_path)
    a, b = store.create_cat("A"), store.create_cat("B")
    _label(store, a["id"], 1, quality="gallery")
    _label(store, a["id"], 2, quality="ok")
    _label(store, b["id"], 3, quality="gallery")

    for quals in (None, ("gallery",), ("gallery", "ok")):
        listed = {r["cat_id"]: r["crops"] for r in store.enrollable_cats(quals)}
        enrolled: "dict[int, int]" = {}
        for row in store.labeled_crops(("identified",), quals, active_only=True):
            enrolled[row["cat_id"]] = enrolled.get(row["cat_id"], 0) + 1
        assert listed == {cid: enrolled.get(cid, 0) for cid in listed}


def test_enrollable_cats_omits_retired_and_keeps_zero_crop_cats(tmp_path):
    store = _store(tmp_path)
    a, b, c = store.create_cat("A"), store.create_cat("B"), store.create_cat("C")
    _label(store, a["id"], 1)
    _label(store, b["id"], 2)
    store.update_cat(b["id"], {"active": False})

    rows = store.enrollable_cats()
    # A retired cat is already excluded from every build, so listing it unchecked-but-
    # present would imply ticking it enrols it.
    assert [r["cat_id"] for r in rows] == [a["id"], c["id"]]
    # A cat with nothing at these grades still lists (at 0), so it surfaces rather than
    # silently missing.
    assert rows[1]["crops"] == 0 and rows[1]["label_commits"] == 0


def test_enrollable_cats_grade_semantics_match_labeled_crops(tmp_path):
    store = _store(tmp_path)
    store.create_cat("A")
    with pytest.raises(ValueError):
        store.enrollable_cats(("mint",))
    # An explicitly empty tuple selects nothing — every cat still lists, at zero.
    rows = store.enrollable_cats(())
    assert [r["crops"] for r in rows] == [0]


# --- The probe / build pass-through ---------------------------------------------------


class _RecordingStore:
    """Stub store capturing the ``labeled_crops`` kwargs the probe passes."""

    def __init__(self, rows: "list[dict]") -> None:
        self._rows = rows
        self.calls: "list[dict]" = []

    def labeled_crops(self, kinds, qualities, active_only=False, exclude_cat_ids=None):
        self.calls.append(
            {"qualities": qualities, "active_only": active_only, "exclude": exclude_cat_ids}
        )
        return list(self._rows)


def test_probe_forwards_the_exclusion_and_names_it_in_the_cold_start(tmp_path):
    # The guard path returns before any Embedder is built, so this runs torch-free. It is
    # the SECOND line (the endpoint pre-checks first), but its message must still not read
    # as "you have no labels" when the operator has plenty and deselected too much.
    store = _RecordingStore([{"cat_id": 1, "cat_name": "A", "crop_path": "/x/1.jpg"}])
    result = run_feasibility_probe(
        store, str(tmp_path / "out"), qualities=("gallery",), exclude_cat_ids=[7, 3, 3]
    )
    assert store.calls[0]["exclude"] == (3, 7)  # sorted + deduped
    assert store.calls[0]["active_only"] is True
    assert result["enough"] is False
    assert "2 excluded cat(s)" in result["message"]
    assert not (tmp_path / "out").exists()


def test_probe_without_an_exclusion_passes_none(tmp_path):
    store = _RecordingStore([])
    run_feasibility_probe(store, str(tmp_path / "out"))
    assert store.calls[0]["exclude"] is None


# --- The manager: dedup key + artifact identity ---------------------------------------


class _RecordingBuilder:
    """Fake gallery builder recording its kwargs; always reports not-enough (no artifact)."""

    def __init__(self) -> None:
        self.calls: "list[dict]" = []

    def __call__(
        self, store, out_dir, qualities=None, max_per_cat=None, exclude_cat_ids=None,
        geometry=None, progress=None,
    ):
        self.calls.append(
            {"out_dir": out_dir, "qualities": qualities, "cap": max_per_cat,
             "exclude": exclude_cat_ids, "geometry": geometry}
        )
        return {"enough": False, "message": "no", "n_crops": 0, "n_cats": 0, "quality": "all"}


def _wait(pred, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.005)
    return bool(pred())


def test_gallery_build_params_carry_the_sorted_exclusion(tmp_path):
    store = _store(tmp_path)
    builder = _RecordingBuilder()
    manager = TrainingManager(gallery_builder=builder)

    manager.enqueue_gallery_build(store, ["gallery"], None, [9, 2, 9])
    assert _wait(lambda: not manager.running)

    # The 4th member is the crop geometry (None = legacy) — part of the key so two arms
    # at different geometries are different work rather than one deduped press.
    assert manager.status()["params"] == [("gallery",), None, (2, 9), None]
    assert builder.calls[0]["exclude"] == [2, 9]
    # A count, not the ids: the dir name is a human handle, and the ids live on the row.
    assert os.path.basename(builder.calls[0]["out_dir"]).endswith("-gallery-ex2")


def test_gallery_build_slug_orders_cap_before_exclusion(tmp_path):
    store = _store(tmp_path)
    builder = _RecordingBuilder()
    manager = TrainingManager(gallery_builder=builder)

    manager.enqueue_gallery_build(store, ["gallery"], 40, [2])
    assert _wait(lambda: not manager.running)
    # `<ts>-<grades>[-max<cap>][-ex<n>]`, so two builds' dir names stay comparable.
    assert os.path.basename(builder.calls[0]["out_dir"]).endswith("-gallery-max40-ex1")


def test_exclusion_is_part_of_the_dedup_key(tmp_path):
    # Changing only which cats are ticked is genuinely different work; with the exclusion
    # outside the key the running-job double-click guard would silently drop the second.
    import threading

    entered, release, calls = threading.Event(), threading.Event(), []

    def gated(store, out_dir, qualities=None, max_per_cat=None, exclude_cat_ids=None,
              progress=None, **kwargs):
        calls.append(exclude_cat_ids)
        entered.set()
        release.wait(timeout=5)
        return {"enough": False, "message": "no", "n_crops": 0, "n_cats": 0, "quality": "all"}

    store = _store(tmp_path)
    manager = TrainingManager(gallery_builder=gated)
    first = manager.enqueue_gallery_build(store, ["gallery"], None, [2])
    assert first["deduped"] is False
    assert entered.wait(timeout=5)

    same = manager.enqueue_gallery_build(store, ["gallery"], None, [2])
    assert same["deduped"] is True  # a genuine double-click, in either tick order
    reordered = manager.enqueue_gallery_build(store, ["gallery"], None, [2, 2])
    assert reordered["deduped"] is True
    different = manager.enqueue_gallery_build(store, ["gallery"], None, [3])
    assert different["deduped"] is False and different["position"] == 1

    release.set()
    assert _wait(lambda: not manager.running)
    assert calls == [[2], [3]]


class _SuccessProbe:
    """Fake probe: writes a stub report and returns a successful summary with the echo."""

    def __call__(self, store, out_dir, qualities=None, exclude_cat_ids=None, progress=None, **kwargs):
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "feasibility.html"), "w", encoding="utf-8") as fh:
            fh.write("<html></html>")
        return {
            "enough": True, "n_crops": 4, "n_cats": 2, "knn_accuracy": 0.9, "auc": 0.8,
            "threshold": 0.3, "quality": "all" if qualities is None else "+".join(qualities),
            "report_dir": out_dir, "visits": None,
            "excluded_cat_ids": list(exclude_cat_ids) if exclude_cat_ids else None,
        }


def test_feasibility_run_records_which_cat_set_it_scored(tmp_path):
    # A run that left a cat out is not comparable with one over the whole roster, so the
    # runs row has to say so rather than only the request that made it.
    store = _store(tmp_path)
    manager = TrainingManager(probe_runner=_SuccessProbe())
    manager.enqueue_feasibility(store, ["gallery"], [5, 4])
    assert _wait(lambda: not manager.running)

    runs = store.feasibility_runs()
    assert len(runs) == 1
    assert runs[0]["metrics"] == {"excluded_cat_ids": [4, 5]}
    assert runs[0]["report_available"] is True
    # The report dir's slug carries the exclusion as a COUNT, after the grades.
    assert [d.endswith("-gallery-ex2") for d in os.listdir(store.training_root)] == [True]


def test_feasibility_run_without_an_exclusion_writes_no_metrics(tmp_path):
    # An older probe (no visit block, no echo) must still write NULL, which reads back as
    # "not measured" — never a dict of Nones that a reader could take for a measurement.
    store = _store(tmp_path)
    manager = TrainingManager(probe_runner=_SuccessProbe())
    manager.enqueue_feasibility(store, None)
    assert _wait(lambda: not manager.running)
    assert store.feasibility_runs()[0]["metrics"] is None


# --- The endpoints --------------------------------------------------------------------


class _FakeClient:
    def iter_stream_reconnecting(self):
        return iter(())


class _SpyManager:
    """Records the enqueue calls both training endpoints make."""

    def __init__(self) -> None:
        self.build_calls: "list[dict]" = []
        self.feasibility_calls: "list[dict]" = []

    def enqueue_gallery_build(self, store, qualities, max_per_cat=None, exclude_cat_ids=None,
                              geometry=None):
        self.build_calls.append({"qualities": qualities, "cap": max_per_cat,
                                 "exclude": exclude_cat_ids, "geometry": geometry})
        return {"position": 0, "deduped": False}

    def enqueue_feasibility(self, store, qualities, exclude_cat_ids=None, geometry=None):
        self.feasibility_calls.append({"qualities": qualities, "exclude": exclude_cat_ids,
                                       "geometry": geometry})
        return {"position": 0, "deduped": False}


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    """A ``TestClient`` over a real ``Store`` + spy manager, with the embed deps stubbed."""
    monkeypatch.setattr(
        "compute.identification.embed.Embedder.ensure_available", lambda self: None
    )
    from compute.api.app import create_app

    store = _store(tmp_path)
    manager = _SpyManager()
    app = create_app(
        store=store, client=_FakeClient(), start_collector=False, training_manager=manager
    )
    return TestClient(app), store, manager


def _two_cats(store: Store) -> "tuple[dict, dict]":
    a, b = store.create_cat("A"), store.create_cat("B")
    _label(store, a["id"], 1)
    _label(store, b["id"], 2)
    return a, b


@pytest.mark.parametrize("route", ["/api/training/gallery/build", "/api/training/feasibility/run"])
def test_endpoints_reject_an_id_naming_no_cat(app_env, route):
    # A stale UI holding a deleted cat's id is asking to exclude something that does not
    # exist; silently ignoring it would build/score a set the operator did not ask for.
    client, store, manager = app_env
    _two_cats(store)
    resp = client.post(route, json={"exclude_cat_ids": [999]})
    assert resp.status_code == 400
    assert "999" in resp.json()["detail"]
    assert manager.build_calls == [] and manager.feasibility_calls == []


def test_build_accepts_a_retired_cats_id_as_a_no_op(app_env):
    # `active_only` already excludes a retired cat, so rejecting its id would turn a
    # harmless stale tick into an error.
    client, store, manager = app_env
    a, b = _two_cats(store)
    c = store.create_cat("C")
    _label(store, c["id"], 3)
    store.update_cat(c["id"], {"active": False})

    resp = client.post("/api/training/gallery/build", json={"exclude_cat_ids": [c["id"]]})
    assert resp.status_code == 200 and resp.json()["enough"] is True
    assert manager.build_calls[0]["exclude"] == [c["id"]]


@pytest.mark.parametrize("route", ["/api/training/gallery/build", "/api/training/feasibility/run"])
def test_endpoints_reject_a_boolean_cat_id(app_env, route):
    # pydantic treats bool as an int subtype, so without the guard `[true]` would coerce
    # to cat id 1 and silently drop whichever cat is first on the roster.
    client, store, _manager = app_env
    _two_cats(store)
    assert client.post(route, json={"exclude_cat_ids": [True]}).status_code == 422


@pytest.mark.parametrize("route", ["/api/training/gallery/build", "/api/training/feasibility/run"])
def test_endpoints_forward_sorted_ids_and_collapse_empty(app_env, route):
    client, store, manager = app_env
    a, b = _two_cats(store)
    c, d = store.create_cat("C"), store.create_cat("D")
    _label(store, c["id"], 3)
    _label(store, d["id"], 4)  # A + D survive the exclusion, so the pre-check passes

    assert client.post(route, json={"exclude_cat_ids": [c["id"], b["id"], c["id"]]}).status_code == 200
    assert client.post(route, json={"exclude_cat_ids": []}).status_code == 200
    calls = manager.build_calls or manager.feasibility_calls
    assert [c_["exclude"] for c_ in calls] == [[b["id"], c["id"]], None]


@pytest.mark.parametrize("route", ["/api/training/gallery/build", "/api/training/feasibility/run"])
def test_the_pre_check_applies_the_exclusion_and_names_it(app_env, route):
    # The floor is the load-bearing case: an exclusion is the only build parameter that can
    # drop whole cats, so the guard has to count what the job will embed — and say WHY,
    # since "not enough labelled data" is misleading with plenty labelled but deselected.
    client, store, manager = app_env
    a, b = _two_cats(store)

    resp = client.post(route, json={"exclude_cat_ids": [b["id"]]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["enough"] is False
    assert (body["n_crops"], body["n_cats"]) == (1, 1)
    assert "1 cat(s) are excluded" in body["message"]
    assert manager.build_calls == [] and manager.feasibility_calls == []


# --- GET /api/label/enrollable --------------------------------------------------------


def test_enrollable_endpoint_shape_and_grade_filter(app_env):
    client, store, _manager = app_env
    a, b = _two_cats(store)
    time.sleep(0.002)  # a distinct labeled_ts, i.e. a distinct commit (see above)
    _label(store, a["id"], 3, quality="poor")

    body = client.get("/api/label/enrollable").json()
    assert body["qualities"] is None  # absent means ALL grades
    assert body["cats"] == [
        {"cat_id": a["id"], "cat_name": "A", "is_resident": False, "crops": 2,
         "label_commits": 2},
        {"cat_id": b["id"], "cat_name": "B", "is_resident": False, "crops": 1,
         "label_commits": 1},
    ]

    scoped = client.get("/api/label/enrollable?qualities=gallery").json()
    assert scoped["qualities"] == ["gallery"]
    assert [c["crops"] for c in scoped["cats"]] == [1, 1]
    both = client.get("/api/label/enrollable?qualities=gallery&qualities=poor").json()
    assert [c["crops"] for c in both["cats"]] == [2, 1]


def test_enrollable_endpoint_rejects_a_bad_grade(app_env):
    client, _store, _manager = app_env
    assert client.get("/api/label/enrollable?qualities=mint").status_code == 400
