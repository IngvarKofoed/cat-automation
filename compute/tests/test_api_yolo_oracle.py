"""Tests for the YOLO-oracle worker's API wiring on ``compute/api/app.py``.

Mirrors ``test_api_live_identify.py``: a real temp ``Store`` (these routes are thin toggle +
status wiring, not the real GPU worker) plus a hand-rolled ``FakeYoloOracleManager`` injected
via ``yolo_oracle_manager=``. The fake spins NO thread and imports NO torch — the injection
short-circuits ``create_app``'s lazy ``from compute.learning.yolo_oracle import
YoloOracleManager`` build. Only the wiring is under test here (routing, the ``/api/stats``
fold-in, and the ``/api/clear`` watermark re-seed); the real manager's tick/threading
lifecycle lives in ``test_yolo_oracle.py``.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from compute.collection.store import Store


class FakeYoloOracleManager:
    """A minimal stand-in for ``YoloOracleManager``: a ``running`` flag flipped by
    ``start()``/``stop()``, a canned ``status()``, and a recording ``reset_watermark``.

    Implements exactly the surface ``create_app`` touches — ``running`` (a property, as on
    the real manager), ``start``/``stop`` (the two endpoints), ``status`` (folded into
    ``/api/stats``), ``reset_watermark`` (called by ``/api/clear``), plus ``restore``/``join``
    (launch-restore and shutdown teardown). No thread, no torch.
    """

    def __init__(self) -> None:
        self._running = False
        self.restore_calls: "list[bool]" = []
        self.reset_calls: "list[int]" = []
        self.stop_calls = 0
        self.stop_persist: "list[bool]" = []
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
        self.reset_calls.append(value)

    def status(self) -> dict:
        return {
            "running": self._running,
            "watermark": 7,
            "last_tick_ts": 1_700_000_000_000,
            "last_error": None,
        }

    @property
    def running(self) -> bool:
        return self._running


class _FakeClient:
    """A stand-in edge connection: no network, no real Pi."""

    def iter_stream_reconnecting(self):
        return iter(())


def _make_app(tmp_path):
    """A ``TestClient`` over a fresh ``Store`` + injected ``FakeYoloOracleManager``.

    ``start_collector=False`` so no real edge/thread is created (and so the launch
    ``restore`` — gated on ``start_collector`` — is skipped). Returns ``(client, manager)``.
    """
    from compute.api.app import create_app

    store = Store(
        db_path=str(tmp_path / "index.db"),
        media_root=str(tmp_path / "media"),
        max_bytes=10_000_000,
    )
    manager = FakeYoloOracleManager()
    app = create_app(
        store=store,
        client=_FakeClient(),
        start_collector=False,
        yolo_oracle_manager=manager,
    )
    return TestClient(app), manager


def test_start_flips_running_true(tmp_path):
    client, manager = _make_app(tmp_path)
    assert manager.running is False

    resp = client.post("/api/yolo-oracle/start")
    assert resp.status_code == 200
    assert resp.json() == {"running": True}
    assert manager.running is True


def test_stop_flips_running_false(tmp_path):
    client, manager = _make_app(tmp_path)
    client.post("/api/yolo-oracle/start")
    assert manager.running is True

    resp = client.post("/api/yolo-oracle/stop")
    assert resp.status_code == 200
    assert resp.json() == {"running": False}
    assert manager.running is False


def test_stats_carries_yolo_oracle_object(tmp_path):
    client, manager = _make_app(tmp_path)

    body = client.get("/api/stats").json()
    assert body["yolo_oracle"] == {
        "running": False,
        "watermark": 7,
        "last_tick_ts": 1_700_000_000_000,
        "last_error": None,
    }
    # The two always-on workers are reported separately — the Start page renders one toggle
    # per worker, and they are independent (neither gates the other).
    assert "live_identify" in body

    client.post("/api/yolo-oracle/start")
    assert client.get("/api/stats").json()["yolo_oracle"]["running"] is True


def test_start_allowed_while_motion_only(tmp_path):
    # Enabling the oracle under motion-only capture is a legitimate intent (the tick just
    # idles until capture returns to keep-all), so the route must NOT reject it — otherwise
    # the operator's intent would be lost across a temporary mode flip.
    client, manager = _make_app(tmp_path)
    client.post("/api/collector/motion-only", json={"motion_only": True})

    resp = client.post("/api/yolo-oracle/start")
    assert resp.status_code == 200
    assert resp.json() == {"running": True}


def test_promoted_model_does_not_auto_start_the_oracle(tmp_path):
    # A DELIBERATE divergence from live-identify, which also restores when a gallery exists
    # (changelog 100). A promoted model is a run-mode NAMING signal; the oracle is a
    # motion-gate TUNING tool, so it starts only when the operator asked. Without this test a
    # regression that copied live-identify's `or active_model() is not None` clause into the
    # oracle's restore would pass the whole suite silently.
    from compute.api.app import create_app

    store = Store(
        db_path=str(tmp_path / "index.db"),
        media_root=str(tmp_path / "media"),
        max_bytes=10_000_000,
    )
    manager = FakeYoloOracleManager()
    # A live app (start_collector=True) is what runs the restore at all; the intent is OFF.
    app = create_app(
        store=store,
        client=_FakeClient(),
        start_collector=True,
        yolo_oracle_manager=manager,
        live_identify_manager=_InertWorker(),
    )
    with TestClient(app):
        pass
    # restore consulted the persisted intent only — and it was absent, so no GPU thread.
    assert manager.restore_calls == [False]
    assert manager.running is False


class _InertWorker:
    """A do-nothing live-identify stand-in, so the test above can build a LIVE app without
    the real worker's lazy torch import or a spawned GPU thread."""

    def __init__(self) -> None:
        self._running = False

    def start(self) -> None:
        self._running = True

    def stop(self, persist: bool = True) -> None:
        self._running = False

    def join(self, timeout: "float | None" = None) -> None:
        pass

    def restore(self, flag: bool) -> None:
        pass

    def reset_watermark(self, value: int) -> None:
        pass

    def status(self) -> dict:
        return {"running": self._running, "watermark": 0, "last_tick_ts": None, "last_error": None}

    @property
    def running(self) -> bool:
        return self._running


def test_clear_reseeds_watermark_to_post_wipe_horizon(tmp_path):
    # /api/clear keeps the settings KV while frame rowids restart, so both always-on workers'
    # watermarks must be re-pointed at the post-wipe horizon (0 for an emptied store) or they
    # would sit ahead of every new frame and silently cover nothing.
    client, manager = _make_app(tmp_path)

    resp = client.post("/api/clear")
    assert resp.status_code == 200
    assert manager.reset_calls == [0]


def test_test_app_does_not_restore_intent(tmp_path):
    # start_collector=False must never auto-start a GPU worker.
    _client, manager = _make_app(tmp_path)
    assert manager.restore_calls == []
    assert manager.running is False


def test_route_stop_persists_intent_but_shutdown_does_not(tmp_path):
    # The restore contract hinges on this split. An OPERATOR stop is a remembered "off"
    # (persist=True). A PROCESS exit must NOT persist, or a clean restart would clear the
    # operator's on-intent and the launch-time restore could never bring the worker back.
    client, manager = _make_app(tmp_path)

    client.post("/api/yolo-oracle/stop")
    assert manager.stop_persist == [True]  # route: remembered

    with client:  # entering/exiting the TestClient context fires startup + shutdown
        pass
    assert manager.stop_persist[-1] is False  # shutdown: intent preserved
