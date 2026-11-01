"""Tests for the foreign-cat alarm's compute API and its contract with the edge speaker.

The ``/api/deterrent`` routes are driven with a fake edge client; then the HTTP contract
the alarm depends on — ``GET /api/sound`` and ``POST /api/sound/play`` and their codes —
is exercised end to end against the REAL edge app served in-process (its ``aplay``
replaced by a fake runner), through the real ``EdgeClient``. The two tiers must agree
exactly on that contract, and only a test that runs both can say they do.
"""
from __future__ import annotations

import os
import subprocess
import threading
import wave

import pytest
from fastapi.testclient import TestClient

from compute.api.app import create_app
from compute.collection.store import Store
from compute.ingest import EdgeClient, EdgeUnavailable
from compute.learning import deterrent

BASE = 1_700_000_000_000


def _store(tmp_path) -> Store:
    return Store(
        db_path=str(tmp_path / "index.db"),
        media_root=str(tmp_path / "media"),
        max_bytes=10_000_000,
    )


class FakeEdge:
    """The two speaker calls the compute makes, scripted."""

    def __init__(self, sound=None, play=(200, {"sound": "alarm1", "duration_s": 2.4}), exc=None):
        self.sound = sound if sound is not None else {
            "available": True, "enabled": True,
            "sounds": [{"name": "alarm1", "duration_s": 2.4}, {"name": "alarm2", "duration_s": 6.5}],
            "playing": None, "last_error": None,
        }
        self.play = play
        self.exc = exc
        self.played: "list[str]" = []

    def get_sound(self):
        if self.exc:
            raise self.exc
        return self.sound

    def play_sound(self, name):
        if self.exc:
            raise self.exc
        self.played.append(name)
        return self.play


def _app(store, client=None):
    return TestClient(create_app(store=store, start_collector=False, client=client))


def _calibrated_model(store, threshold=0.4):
    gdir = os.path.join(store.models_root, "g1")
    os.makedirs(gdir, exist_ok=True)
    open(os.path.join(gdir, "gallery.npz"), "wb").close()
    mid = store.add_model_version("draft", "gallery", "dinov2_vits14", 224, 2, 10, threshold, "all", None, "g1")
    store.promote_model(mid)
    return mid


def test_readiness_defaults_off_and_reports_the_pi(tmp_path):
    store = _store(tmp_path)
    r = _app(store, FakeEdge()).get("/api/deterrent").json()
    assert r["mode"] == "off"                          # a fresh install sounds nothing
    assert r["calibrated"] is False and r["model"] is None
    assert r["pi"]["reachable"] is True and r["pi"]["enabled"] is True
    assert [s["name"] for s in r["pi"]["sounds"]] == ["alarm1", "alarm2"]
    assert r["alarm"]["sounding"] is False


def test_readiness_says_calibrated_only_with_a_threshold(tmp_path):
    store = _store(tmp_path)
    mid = _calibrated_model(store, threshold=None)
    c = _app(store, FakeEdge())
    assert c.get("/api/deterrent").json()["calibrated"] is False
    store.set_model_threshold(mid, 0.42)
    r = c.get("/api/deterrent").json()
    assert r["calibrated"] is True and r["model"] == {"id": mid, "threshold": 0.42}


def test_readiness_with_the_pi_disabled_unreachable_or_absent(tmp_path):
    store = _store(tmp_path)
    off = FakeEdge(sound={"available": True, "enabled": False, "sounds": [], "playing": None, "last_error": None})
    assert _app(store, off).get("/api/deterrent").json()["pi"]["enabled"] is False
    down = _app(store, FakeEdge(exc=EdgeUnavailable("connect timeout"))).get("/api/deterrent").json()
    assert down["pi"] == {"reachable": False, "error": "connect timeout"}
    none = _app(store, None).get("/api/deterrent").json()
    assert none["pi"]["reachable"] is False and "CAT_PI_URL" in none["pi"]["error"]


def test_switch_persists_and_rejects_anything_else(tmp_path):
    store = _store(tmp_path)
    c = _app(store, FakeEdge())
    assert c.post("/api/deterrent", json={"mode": "on"}).json() == {"mode": "on"}
    assert store.get_setting("deterrent_mode") == "on"
    assert c.get("/api/deterrent").json()["mode"] == "on"
    assert c.post("/api/deterrent", json={"mode": "log"}).status_code == 400
    assert c.post("/api/deterrent", json={"mode": "off"}).json() == {"mode": "off"}


def test_test_button_plays_in_either_mode_and_logs_a_test_row(tmp_path):
    store = _store(tmp_path)
    edge = FakeEdge(play=(200, {"sound": "alarm2", "duration_s": 6.5}))
    c = _app(store, edge)
    r = c.post("/api/deterrent/test", json={"sound": "alarm2"}).json()   # switch is Off
    assert r["outcome"] == "played" and edge.played == ["alarm2"]
    [row] = c.get("/api/deterrent/log").json()["actions"]
    assert (row["mode"], row["verdict"], row["sound"], row["clips_played"]) == ("test", "test", "alarm2", 1)
    assert row["end_ts"] - row["start_ts"] == 6500
    assert c.post("/api/deterrent/test", json={"sound": "../x"}).status_code == 400


def test_test_button_with_the_pi_disabled_says_so_in_the_row(tmp_path):
    store = _store(tmp_path)
    c = _app(store, FakeEdge(play=(423, {"error": "sound is disabled on the Pi"})))
    assert c.post("/api/deterrent/test", json={"sound": "alarm1"}).json()["outcome"] == "disabled"
    [row] = c.get("/api/deterrent/log").json()["actions"]
    assert row["pi_disabled"] is True and row["clips_played"] == 0


def test_log_is_newest_first_and_capped(tmp_path):
    store = _store(tmp_path)
    for i in range(25):
        store.add_action({"kind": "sound", "mode": "test", "start_ts": BASE + i, "verdict": "test"})
    c = _app(store, FakeEdge())
    rows = c.get("/api/deterrent/log").json()["actions"]
    assert len(rows) == 20 and rows[0]["start_ts"] == BASE + 24
    assert c.get("/api/deterrent/log?limit=500").status_code == 422


# =====================================================================================
#  The cross-tier contract: the real edge app, the real EdgeClient
# =====================================================================================


class _Proc:
    def __init__(self, exit_rc=None):
        self.returncode = None
        self._exit_rc = exit_rc

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None and self._exit_rc is not None:
            self.returncode = self._exit_rc
        if self.returncode is None:
            raise subprocess.TimeoutExpired("aplay", timeout)
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9


class _Runner:
    def __init__(self):
        self.procs = []

    def which(self, program):
        return "/usr/bin/aplay"

    def spawn(self, argv, stderr):
        p = _Proc()
        self.procs.append(p)
        return p


@pytest.fixture
def edge(tmp_path, monkeypatch):
    """The real edge app with a fake aplay, served on an ephemeral port."""
    from werkzeug.serving import make_server

    from edge.actuators.sound import SoundPlayer
    from edge.capture.fake_source import FakeCaptureSource
    from edge.server.app import create_app as create_edge_app

    monkeypatch.setenv("CAT_EDGE_CONFIG", str(tmp_path / "settings.json"))
    sounds = tmp_path / "sounds"
    sounds.mkdir()
    for name, secs in (("alarm1", 2.0), ("alarm2", 6.0)):
        with wave.open(str(sounds / f"{name}.wav"), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000)
            w.writeframes(b"\x00\x00" * int(secs * 8000))
    runner = _Runner()
    app = create_edge_app(
        source_factory=FakeCaptureSource, start_grabber=False, start_scheduler=False,
        sound=SoundPlayer(sounds, runner=runner, settle_s=0.0),
    )
    server = make_server("127.0.0.1", 0, app, threaded=True)
    host, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://{host}:{port}", runner
    finally:
        server.shutdown()
        thread.join(timeout=5.0)


def test_contract_codes_through_the_real_client(edge):
    url, runner = edge
    client = EdgeClient(url)
    snap = client.get_sound()
    assert snap["available"] is True and snap["enabled"] is True
    assert [s["name"] for s in snap["sounds"]] == ["alarm1", "alarm2"]

    assert client.play_sound("alarm1") == (200, {"sound": "alarm1", "duration_s": 2.0})
    assert deterrent.play_clip(client, "alarm2").status == "busy"          # 409
    runner.procs[0].returncode = 0
    played = deterrent.play_clip(client, "alarm2")
    assert (played.status, played.duration_s) == ("played", 6.0)
    assert deterrent.play_clip(client, "nope").status == "failed"          # 404
    runner.procs[-1].returncode = 0

    import requests
    requests.post(f"{url}/api/sound", json={"enabled": False}, timeout=2)
    assert client.get_sound()["enabled"] is False
    assert deterrent.play_clip(client, "alarm1").status == "disabled"      # 423


def test_contract_admin_test_button_end_to_end(edge, tmp_path):
    url, _runner = edge
    store = _store(tmp_path / "compute")
    c = _app(store, EdgeClient(url))
    assert c.get("/api/deterrent").json()["pi"]["reachable"] is True
    r = c.post("/api/deterrent/test", json={"sound": "alarm1"}).json()
    assert r["outcome"] == "played" and r["duration_s"] == 2.0
    assert c.get("/api/deterrent/log").json()["actions"][0]["clips_played"] == 1


def test_contract_unreachable_pi_is_a_failure_not_a_hang():
    # Nothing listens on this port: the short sound timeout turns it into a fast failure.
    client = EdgeClient("http://127.0.0.1:9")
    with pytest.raises(EdgeUnavailable):
        client.get_sound()
    assert deterrent.play_clip(client, "alarm1").status == "failed"
