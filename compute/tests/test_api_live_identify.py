"""Tests for the live-identify worker's API wiring on ``compute/api/app.py``
(the live-identify-worker spec's API layer, contract #4).

Mirrors ``test_api_identification.py``'s ``create_app(store=...,
start_collector=False)`` ``TestClient`` pattern: a real temp ``Store`` (these
routes are thin toggle + status wiring, not the real GPU worker) plus a
hand-rolled ``FakeLiveIdentifyManager`` injected via ``live_identify_manager=``.
The fake spins NO thread and imports NO torch — the injection short-circuits
``create_app``'s lazy ``from compute.learning.live_identify import
LiveIdentifyManager`` build, so this module is loadable and runnable even before
that sibling file exists, matching ``compute/CLAUDE.md``'s "GPU-/model-dependent
tests should run against small fixtures or be skippable" rule. Only the routing —
that ``POST /api/live-identify/{start,stop}`` flip the reported ``running`` state
and that ``GET /api/stats`` folds in the worker's ``status()`` — is under test
here; the real manager's tick/threading/lifecycle lives in its own test file.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from compute.collection.store import Store


class FakeLiveIdentifyManager:
    """A minimal stand-in for ``LiveIdentifyManager``: an authoritative ``running``
    flag flipped by ``start()``/``stop()`` and a canned ``status()`` snapshot.

    Implements exactly the surface ``create_app`` touches — ``running`` (a property,
    as on the real manager), ``start``/``stop`` (the two endpoints), ``status`` (folded
    into ``/api/stats``), plus ``restore``/``join`` (the launch-restore and the shutdown
    teardown) as no-ops/records so an app teardown never errors. No thread, no torch.
    """

    def __init__(self) -> None:
        self._running = False
        self.restore_calls: "list[bool]" = []
        self.stop_calls = 0
        self.stop_persist: "list[bool]" = []
        self.reset_calls: "list[int]" = []
        self.join_calls = 0

    def start(self) -> None:
        self._running = True

    def stop(self, persist: bool = True) -> None:
        self._running = False
        self.stop_calls += 1
        self.stop_persist.append(persist)

    def join(self, timeout: "float | None" = None) -> None:
        self.join_calls += 1

    def restore(self, flag: bool) -> None:
        self.restore_calls.append(flag)
        if flag:
            self.start()

    def reset_watermark(self, value: int) -> None:
        # /api/clear re-seeds this worker's watermark too (a pre-wipe value would sit above
        # every new frame and it would silently stop naming). Present here so the fake keeps
        # matching the surface create_app touches — a drifted fake turns a real app bug into
        # a confusing AttributeError, or hides it entirely.
        self.reset_calls.append(value)

    def status(self) -> dict:
        return {
            "running": self._running,
            "watermark": 42,
            "last_tick_ts": 1_700_000_000_000,
            "last_error": None,
        }

    @property
    def running(self) -> bool:
        return self._running


class _FakeClient:
    """A stand-in edge connection: no network, no real Pi (mirrors test_api_identification.py)."""

    def iter_stream_reconnecting(self):
        return iter(())


def _store(tmp_path) -> Store:
    return Store(
        db_path=str(tmp_path / "index.db"),
        media_root=str(tmp_path / "media"),
        max_bytes=10_000_000,
    )


def _make_app(tmp_path):
    """A ``TestClient`` over a fresh ``Store`` + injected ``FakeLiveIdentifyManager``.

    ``start_collector=False`` so no real edge/thread is created (and so the launch
    ``restore`` — gated on ``start_collector`` — is skipped, as a test app must never
    auto-start the GPU worker). Returns ``(client, manager)``.
    """
    from compute.api.app import create_app

    store = _store(tmp_path)
    manager = FakeLiveIdentifyManager()
    app = create_app(
        store=store,
        client=_FakeClient(),
        start_collector=False,
        live_identify_manager=manager,
    )
    return TestClient(app), manager


def test_start_flips_running_true(tmp_path):
    client, manager = _make_app(tmp_path)
    assert manager.running is False

    resp = client.post("/api/live-identify/start")
    assert resp.status_code == 200
    assert resp.json() == {"running": True}
    assert manager.running is True


def test_stop_flips_running_false(tmp_path):
    client, manager = _make_app(tmp_path)
    client.post("/api/live-identify/start")
    assert manager.running is True

    resp = client.post("/api/live-identify/stop")
    assert resp.status_code == 200
    assert resp.json() == {"running": False}
    assert manager.running is False


def test_stats_carries_live_identify_object(tmp_path):
    client, manager = _make_app(tmp_path)

    body = client.get("/api/stats").json()
    assert "live_identify" in body
    # The worker's status() snapshot is folded in verbatim (running/watermark/
    # last_tick_ts/last_error), and it tracks the toggle it reports.
    assert body["live_identify"] == {
        "running": False,
        "watermark": 42,
        "last_tick_ts": 1_700_000_000_000,
        "last_error": None,
    }

    client.post("/api/live-identify/start")
    body = client.get("/api/stats").json()
    assert body["live_identify"]["running"] is True


def test_clear_reseeds_watermark(tmp_path):
    # Mirrors the oracle's test: /api/clear must re-point this worker's watermark at the
    # post-wipe horizon, or a pre-wipe value sits above every new frame and naming stops.
    client, manager = _make_app(tmp_path)
    assert client.post("/api/clear").status_code == 200
    assert manager.reset_calls == [0]


def test_test_app_does_not_restore_intent(tmp_path):
    # start_collector=False must never auto-start the GPU worker: the launch-time
    # ``restore`` is gated on start_collector, so the fake's restore is never called.
    _client, manager = _make_app(tmp_path)
    assert manager.restore_calls == []
    assert manager.running is False
