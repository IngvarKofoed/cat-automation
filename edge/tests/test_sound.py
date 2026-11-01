"""Tests for the edge sound player and its routes (docs/specs/2026-11-01-foreign-cat-alarm.md).

No process is started: a fake runner hands back fake ``aplay`` processes whose exit the
test controls, and the clips are tiny WAVs written into a tmp dir. The shipped clips are
checked separately, because their FORMAT is a decision (16-bit PCM plays on any device
ALSA can open; the float/24-bit originals did not).
"""
from __future__ import annotations

import json
import subprocess
import wave
from pathlib import Path

import pytest

from edge.actuators.sound import (
    SOUNDS_DIR,
    SoundBusy,
    SoundDisabled,
    SoundFailed,
    SoundPlayer,
    SoundUnavailable,
    UnknownSound,
)
from edge.capture.fake_source import FakeCaptureSource
from edge.server.app import create_app


class FakeProc:
    """A fake ``aplay``: alive until the test ends it, or exits during settle if told to."""

    def __init__(self, exit_during_settle: "int | None" = None) -> None:
        self.returncode: "int | None" = None
        self._exit_during_settle = exit_during_settle
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None and self._exit_during_settle is not None:
            self.returncode = self._exit_during_settle
        if self.returncode is None:
            raise subprocess.TimeoutExpired("aplay", timeout)
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9

    def finish(self, rc: int = 0) -> None:
        self.returncode = rc


class FakeRunner:
    """Records every spawn; ``next_exit`` makes the next process die in the settle window."""

    def __init__(self, aplay: "str | None" = "/usr/bin/aplay") -> None:
        self.aplay = aplay
        self.argvs: "list[list[str]]" = []
        self.procs: "list[FakeProc]" = []
        self.next_exit: "int | None" = None
        self.next_stderr = b""

    def which(self, program):
        return self.aplay if program == "aplay" else None

    def spawn(self, argv, stderr):
        self.argvs.append(list(argv))
        stderr.write(self.next_stderr)
        proc = FakeProc(exit_during_settle=self.next_exit)
        self.next_exit = None
        self.next_stderr = b""
        self.procs.append(proc)
        return proc


def _write_wav(path: Path, seconds: float, rate: int = 8000) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate))


@pytest.fixture
def sounds_dir(tmp_path):
    d = tmp_path / "sounds"
    d.mkdir()
    _write_wav(d / "alarm1.wav", 2.0)
    _write_wav(d / "alarm2.wav", 0.5)
    return d


@pytest.fixture
def runner():
    return FakeRunner()


@pytest.fixture
def player(sounds_dir, runner):
    return SoundPlayer(sounds_dir, runner=runner, alsa_device="", settle_s=0.0)


# --- the shipped clips ---------------------------------------------------------------


def test_shipped_clips_are_16bit_pcm_and_listed():
    # The decision: 16-bit PCM 44.1 kHz stereo, so any device ALSA can open plays them.
    # A float WAV is not even readable by `wave`, so it would silently drop off the list.
    for name in ("alarm1", "alarm2"):
        with wave.open(str(SOUNDS_DIR / f"{name}.wav"), "rb") as w:
            assert (w.getsampwidth(), w.getframerate(), w.getnchannels()) == (2, 44100, 2)
    p = SoundPlayer(runner=FakeRunner())
    durations = {s["name"]: s["duration_s"] for s in p.status()["sounds"]}
    assert set(durations) == {"alarm1", "alarm2"}
    # alarm2 is the longest: "at most 6.5 s" is what bounds a crashed compute's tail.
    assert 2.0 < durations["alarm1"] < 3.0 and 6.0 < durations["alarm2"] <= 6.5


# --- the player ----------------------------------------------------------------------


def test_status_lists_clips_by_stem_with_durations(player):
    s = player.status()
    assert s == {
        "available": True,
        "enabled": True,
        "sounds": [{"name": "alarm1", "duration_s": 2.0}, {"name": "alarm2", "duration_s": 0.5}],
        "playing": None,
        "last_error": None,
    }


def test_unreadable_wav_is_left_out(tmp_path, runner):
    d = tmp_path / "s"
    d.mkdir()
    _write_wav(d / "ok.wav", 1.0)
    (d / "broken.wav").write_bytes(b"RIFF\x00\x00\x00\x00WAVEjunk")
    p = SoundPlayer(d, runner=runner, settle_s=0.0)
    assert p.names() == ["ok"]


def test_play_spawns_aplay_quietly_and_reports_duration(player, runner, sounds_dir):
    assert player.play("alarm1") == {"sound": "alarm1", "duration_s": 2.0}
    assert runner.argvs == [["/usr/bin/aplay", "-q", str(sounds_dir / "alarm1.wav")]]
    assert player.status()["playing"] == "alarm1"


def test_alsa_device_is_passed_with_dash_d(sounds_dir, runner):
    p = SoundPlayer(sounds_dir, runner=runner, alsa_device="hw:1,0", settle_s=0.0)
    p.play("alarm2")
    assert runner.argvs[0][:4] == ["/usr/bin/aplay", "-q", "-D", "hw:1,0"]


def test_alsa_device_read_from_env(sounds_dir, runner, monkeypatch):
    monkeypatch.setenv("CAT_EDGE_ALSA_DEVICE", "plughw:2")
    SoundPlayer(sounds_dir, runner=runner, settle_s=0.0).play("alarm1")
    assert "-D" in runner.argvs[0] and "plughw:2" in runner.argvs[0]


def test_second_play_while_sounding_is_refused_not_queued(player, runner):
    player.play("alarm1")
    with pytest.raises(SoundBusy):
        player.play("alarm2")
    assert len(runner.argvs) == 1
    runner.procs[0].finish(0)
    assert player.play("alarm2")["sound"] == "alarm2"


def test_refusals(player, runner):
    with pytest.raises(UnknownSound):
        player.play("../etc/passwd")
    player.set_enabled(False)
    with pytest.raises(SoundDisabled):
        player.play("alarm1")
    assert runner.argvs == []


def test_no_aplay_is_unavailable(sounds_dir):
    p = SoundPlayer(sounds_dir, runner=FakeRunner(aplay=None), settle_s=0.0)
    assert p.available is False and p.status()["available"] is False
    with pytest.raises(SoundUnavailable):
        p.play("alarm1")


def test_aplay_dying_in_settle_window_is_a_failure_with_its_stderr(player, runner):
    runner.next_exit = 1
    runner.next_stderr = b"aplay: main:850: audio open error: Device or resource busy\n"
    with pytest.raises(SoundFailed) as exc:
        player.play("alarm1")
    assert "Device or resource busy" in str(exc.value)
    s = player.status()
    assert s["playing"] is None and "resource busy" in s["last_error"]
    # A later good play clears the error: last_error describes the speaker now.
    player.play("alarm1")
    assert player.status()["last_error"] is None


def test_disable_during_the_settle_window_is_not_reported_as_played(sounds_dir):
    # The settle wait runs outside the lock; a disable landing inside it cuts the clip.
    # The play must then answer "disabled", not 200 with a duration for a clip that died.
    holder = {}

    class DisablingProc(FakeProc):
        def wait(self, timeout=None):
            if self.returncode is None and timeout is not None:
                holder["player"].set_enabled(False)   # lands while play() settles
                raise subprocess.TimeoutExpired("aplay", timeout)
            return super().wait(timeout)

    class Runner(FakeRunner):
        def spawn(self, argv, stderr):
            proc = DisablingProc()
            self.procs.append(proc)
            return proc

    runner = Runner()
    holder["player"] = p = SoundPlayer(sounds_dir, runner=runner, settle_s=0.3)
    with pytest.raises(SoundDisabled):
        p.play("alarm2")
    assert runner.procs[0].terminated is True and p.status()["playing"] is None


def test_failure_after_settle_is_recorded_on_next_status(player, runner):
    player.play("alarm1")
    runner.procs[0].finish(1)
    s = player.status()
    assert s["playing"] is None and "code 1" in s["last_error"]


def test_disabling_mid_clip_stops_it(player, runner):
    player.play("alarm2")
    player.set_enabled(False)
    proc = runner.procs[0]
    assert proc.terminated is True
    s = player.status()
    # Our own stop is not reported as an aplay failure.
    assert s["playing"] is None and s["enabled"] is False and s["last_error"] is None


# --- the routes ----------------------------------------------------------------------


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    monkeypatch.setenv("CAT_EDGE_CONFIG", str(path))
    return path


def _app(player):
    return create_app(
        source_factory=FakeCaptureSource, start_grabber=False, start_scheduler=False, sound=player
    )


def test_get_sound(config_path, player):
    c = _app(player).test_client()
    r = c.get("/api/sound")
    assert r.status_code == 200
    assert [s["name"] for s in r.get_json()["sounds"]] == ["alarm1", "alarm2"]


def test_play_route_codes(config_path, player, runner):
    c = _app(player).test_client()
    assert c.post("/api/sound/play", json={}).status_code == 400
    assert c.post("/api/sound/play", json={"sound": "nope"}).status_code == 404
    r = c.post("/api/sound/play", json={"sound": "alarm1"})
    assert r.status_code == 200 and r.get_json() == {"sound": "alarm1", "duration_s": 2.0}
    assert c.post("/api/sound/play", json={"sound": "alarm2"}).status_code == 409
    runner.procs[0].finish(0)
    runner.next_exit = 1
    runner.next_stderr = b"format not supported"
    r = c.post("/api/sound/play", json={"sound": "alarm2"})
    assert r.status_code == 502 and "format not supported" in r.get_json()["error"]


def test_play_route_503_without_aplay(config_path, sounds_dir):
    p = SoundPlayer(sounds_dir, runner=FakeRunner(aplay=None), settle_s=0.0)
    r = _app(p).test_client().post("/api/sound/play", json={"sound": "alarm1"})
    assert r.status_code == 503


def test_disable_persists_survives_restart_and_refuses_with_423(config_path, sounds_dir, runner):
    c = _app(SoundPlayer(sounds_dir, runner=runner, settle_s=0.0)).test_client()
    r = c.post("/api/sound", json={"enabled": False})
    assert r.status_code == 200 and r.get_json()["enabled"] is False
    assert json.loads(config_path.read_text())["sound_enabled"] is False
    assert c.post("/api/sound/play", json={"sound": "alarm1"}).status_code == 423
    # A camera POST rewrites the file wholesale — it must carry the switch through.
    assert c.post("/api/config", json={"fps": 10}).status_code == 200
    assert json.loads(config_path.read_text())["sound_enabled"] is False
    # A restart re-reads it: the new player comes up disabled.
    c2 = _app(SoundPlayer(sounds_dir, runner=runner, settle_s=0.0)).test_client()
    assert c2.get("/api/sound").get_json()["enabled"] is False
    assert c2.post("/api/sound/play", json={"sound": "alarm1"}).status_code == 423
    assert runner.argvs == []


def test_disable_route_cuts_the_clip_in_progress(config_path, player, runner):
    c = _app(player).test_client()
    c.post("/api/sound/play", json={"sound": "alarm2"})
    c.post("/api/sound", json={"enabled": False})
    assert runner.procs[0].terminated is True
    assert c.get("/api/sound").get_json()["playing"] is None


def test_enable_route_rejects_non_bool(config_path, player):
    c = _app(player).test_client()
    assert c.post("/api/sound", json={"enabled": "no"}).status_code == 400
    assert c.post("/api/sound", data="x").status_code == 400
