"""End-to-end run of the visit-held-out probe against a REAL store.

Covers everything the feature added except DINOv2 itself (untouched by it): real
`labeled_crops` SQL carrying `src_recv_ts`/`labeled_ts`, real per-cat visit grouping, the
real day/night classifier gate, `run_feasibility`'s visit block, the real matplotlib
charts including the threshold-sweep curve, the real HTML render, and persistence of the
`metrics` JSON plus its read-back.

The Embedder is stubbed with synthetic vectors so the pipeline runs with no 85 MB
backbone download and no GPU — the crops here are 4-byte fake JPEGs that no real model
could embed anyway. matplotlib is an opt-in analysis extra, so the chart/HTML half skips
on a lean box rather than making it a hard test dependency.
"""
from __future__ import annotations

import json
import os
import time

import numpy as np
import pytest

from compute.collection.store import Store
from compute.ingest import StreamFrame
from shared.wire import StreamFrameMeta

_JPEG = b"\xff\xd8\xff\xe0" + b"fake" + b"\xff\xd9"

# One visit per (cat, slot), 6 crops each, spaced 200 ms — well inside the 60 s grouping
# gap — with visits themselves hours apart. Two cats, three visits each.
_CROPS_PER_VISIT = 6
_HOUR_MS = 3_600_000


def _store(tmp_path) -> Store:
    return Store(db_path=str(tmp_path / "index.db"), media_root=str(tmp_path / "media"),
                 max_bytes=50_000_000)


def _populate(store: Store) -> "list[tuple[int, int]]":
    """Label 2 cats x 3 visits x 6 crops. Returns the (cat_id, recv_ts) of each crop."""
    store.create_cat("Sultan", is_resident=True)
    store.create_cat("Store Sultan", is_resident=False)
    made = []
    fid = 0
    for visit in range(3):
        for cat_id in (1, 2):
            base = (visit * 8 + cat_id) * _HOUR_MS  # visits hours apart
            for c in range(_CROPS_PER_VISIT):
                fid += 1
                ts = base + c * 200
                row_id = store.add(
                    StreamFrame(StreamFrameMeta(frame_id=fid, ts=ts, motion=True,
                                                bbox=None, area=1.0), _JPEG),
                    recv_ts_ms=ts,
                )
                store.add_dataset_items([{
                    "frame_id": row_id, "label_kind": "identified", "cat_id": cat_id,
                    "quality": "gallery", "bbox": [0, 0, 10, 10],
                    "crop_path": f"c{fid}.jpg",
                }])
                made.append((cat_id, ts))
    return made


class _StubEmbedder:
    """Returns separable synthetic vectors: one cluster per cat, tight within a visit."""

    def __init__(self, labels):
        self._labels = labels

    def prepare(self):
        return None

    def embed_paths(self, paths, progress=None):
        rng = np.random.default_rng(7)
        vecs = []
        for row in self._labels:
            centre = np.zeros(8)
            centre[int(row["cat_id"])] = 3.0
            # per-visit jitter keyed on the visit's hour, then tiny per-crop noise
            visit_key = int(row["src_recv_ts"]) // _HOUR_MS
            centre = centre + np.random.default_rng(visit_key).normal(0, 0.15, size=8)
            vecs.append(centre + rng.normal(0, 0.004, size=8))
        if progress is not None:
            progress(len(vecs), len(vecs))
        return np.array(vecs), list(range(len(vecs)))


@pytest.fixture()
def probe_env(tmp_path, monkeypatch):
    store = _store(tmp_path)
    _populate(store)
    labels = store.labeled_crops(("identified",), ("gallery",), active_only=True)
    import compute.identification.probe as probe_mod

    monkeypatch.setattr(probe_mod, "Embedder", lambda: _StubEmbedder(labels))
    yield store, probe_mod, labels
    store.close()


def test_labeled_crops_carries_the_grouping_keys(probe_env):
    _store_, _probe, labels = probe_env
    assert len(labels) == 36
    for row in labels:
        assert isinstance(row["src_recv_ts"], int)
        assert isinstance(row["labeled_ts"], int)


def test_probe_end_to_end_produces_visit_metrics(probe_env, tmp_path):
    pytest.importorskip("matplotlib")
    store, probe_mod, _labels = probe_env
    out = str(tmp_path / "report")
    result = probe_mod.run_feasibility_probe(store, out, qualities=("gallery",))

    assert result["enough"] is True
    v = result["visits"]
    assert v["available"] is True
    assert v["n_groups"] == 6, "2 cats x 3 visits, grouped per cat at the 60s gap"
    assert v["n_scored"] == 6, "each cat has other visits, so all are scoreable"
    assert v["accuracy"] == 1.0, "separable synthetic clusters"
    assert v["correct"] + v["wrong"] + v["unknown"] == 6
    # The crop-level number is still produced, and is the inflated one.
    assert result["knn_accuracy"] == 1.0
    # No location configured in this store → the split is unavailable, not guessed.
    assert v["regimes"] is None and v["cross"] is None

    # The report + raw metrics landed on disk, charts inlined.
    with open(os.path.join(out, "feasibility.json"), encoding="utf-8") as fh:
        raw = json.load(fh)
    assert raw["visits"]["accuracy"] == 1.0
    with open(os.path.join(out, "feasibility.html"), encoding="utf-8") as fh:
        page = fh.read()
    assert "visit accuracy" in page
    assert "Crop-level scoring (for comparison)" in page
    # 3 old charts + the sweep curve + the TL;DR's per-cat bars. The day/night dumbbell
    # is NOT here: this fixture sets no location, so `regimes` is None and `_regime_png`
    # returns "" rather than drawing one regime as if it were both.
    assert page.count("data:image/png;base64,") == 5
    # The lead block and its limits, which a reader takes numbers from without reading
    # the section they came from.
    assert "In short" in page
    assert "What this cannot tell you" in page
    assert "No strangers were tested" in page


def test_day_night_split_runs_when_a_location_is_set(probe_env, tmp_path):
    pytest.importorskip("matplotlib")
    pytest.importorskip("astral")
    store, probe_mod, _labels = probe_env
    store.set_location(55.676, 12.568)  # Copenhagen, as the real store uses
    result = probe_mod.run_feasibility_probe(store, str(tmp_path / "r2"),
                                            qualities=("gallery",))
    v = result["visits"]
    assert v["regimes"] is not None, "a location enables the split"
    # Day + night must exactly partition the scored visits — neither can drift from All.
    scored = sum(r["n_scored"] for r in v["regimes"].values() if r is not None)
    assert scored == v["n_scored"]
    assert v["cross"] is not None


def test_metrics_persist_through_the_training_manager(probe_env, tmp_path):
    """The runner writes the visit block as the run's metrics JSON, and it reads back."""
    pytest.importorskip("matplotlib")
    store, probe_mod, _labels = probe_env
    from compute.learning.runner import TrainingManager

    mgr = TrainingManager(probe_runner=probe_mod.run_feasibility_probe)
    mgr.enqueue_feasibility(store, ["gallery"])
    deadline = time.monotonic() + 60
    while mgr.running and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not mgr.running, "feasibility job did not finish"

    runs = store.feasibility_runs()
    assert len(runs) == 1
    assert runs[0]["metrics"] is not None, "the honest number is persisted, not just shown"
    assert runs[0]["metrics"]["visits"]["accuracy"] == 1.0
    assert runs[0]["knn_accuracy"] == 1.0
