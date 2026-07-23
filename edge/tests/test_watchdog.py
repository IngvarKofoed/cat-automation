"""Tests for the grab-stall watchdog, Grabber.is_running(), and the /stream bound.

The watchdog's real action is process-exit, so we exercise the PURE decision
method (``check``) against a stub grabber — never the real ``os._exit``.
``is_running()`` is tested on a real Grabber via start/stop and grab_once, and the
bounded ``/stream`` generator is driven to termination with the timers shrunk.
"""
from __future__ import annotations

import logging
import threading
import time

import edge.server.app as appmod
from edge.capture.fake_source import FakeCaptureSource
from edge.server.app import create_app
from edge.server.grabber import FrameSnapshot, GrabConfig, Grabber, MotionConfig
from edge.server.watchdog import (
    EXIT_NO_FIRST_FRAME,
    EXIT_WEDGE,
    Watchdog,
    env_positive_float,
)
from shared import wire


# -- helpers --------------------------------------------------------------

class _StubGrabber:
    """Minimal grabber stand-in with a fixed is_running() and snapshot()."""

    def __init__(self, running: bool, snap: FrameSnapshot) -> None:
        self._running = running
        self._snap = snap

    def is_running(self) -> bool:
        return self._running

    def snapshot(self) -> FrameSnapshot:
        return self._snap


class _FreshStub:
    """Running grabber whose snapshot is always fresh (mono == now)."""

    def is_running(self) -> bool:
        return True

    def snapshot(self) -> FrameSnapshot:
        return _snap(frame_id=5, mono=time.monotonic())


def _snap(frame_id: int, mono: float) -> FrameSnapshot:
    return FrameSnapshot(
        frame=(object() if frame_id else None),
        frame_id=frame_id,
        ts=0,
        last_error=None,
        mono=mono,
        motion=False,
        bbox=None,
        area=0.0,
    )


def _wd(grabber, **kw) -> Watchdog:
    kw.setdefault("watchdog_s", 20.0)
    kw.setdefault("boot_grace_s", 60.0)
    wd = Watchdog(grabber, **kw)
    wd._start_mono = 0.0
    return wd


def _fake_read_config() -> GrabConfig:
    return GrabConfig(
        source=FakeCaptureSource(),
        fps=100.0,  # fast pacing so start/stop tests don't dawdle
        rotation=0,
        clip=None,
        motion=MotionConfig(
            var_threshold=16.0,
            learning_rate=0.001,
            min_area=0.01,
            max_area_fraction=0.6,
            persistence=2,
            downscale=320,
        ),
    )


# -- watchdog decision ----------------------------------------------------

def test_check_none_when_grabber_stopped():
    # A frozen slot (old mono) but a stopped grabber is never a stall — stop()
    # freezes mono by design; this is what protects a shutdown / test teardown.
    wd = _wd(_StubGrabber(False, _snap(frame_id=5, mono=0.0)))
    assert wd.check(now=1_000.0) is None


def test_check_wedge_when_armed_and_stale():
    wd = _wd(_StubGrabber(True, _snap(frame_id=5, mono=100.0)))
    assert wd.check(now=100.0 + 21.0) == "wedge"


def test_check_ok_when_armed_and_fresh():
    wd = _wd(_StubGrabber(True, _snap(frame_id=5, mono=100.0)))
    assert wd.check(now=100.0 + 5.0) is None


def test_check_no_first_frame_after_boot_grace():
    wd = _wd(_StubGrabber(True, _snap(frame_id=0, mono=0.0)))
    wd._start_mono = 10.0
    assert wd.check(now=10.0 + 61.0) == "no_first_frame"


def test_check_ok_within_boot_grace():
    wd = _wd(_StubGrabber(True, _snap(frame_id=0, mono=0.0)))
    wd._start_mono = 10.0
    assert wd.check(now=10.0 + 30.0) is None


def test_exit_codes_distinct():
    assert EXIT_WEDGE != EXIT_NO_FIRST_FRAME


# -- watchdog run loop (dispatch + heartbeat) -----------------------------

def test_run_fires_on_stall_with_injected_recorder():
    # A running grabber with a stale slot → the loop calls on_stall with the reason,
    # the deciding snapshot, and since. The recorder stands in for the real os._exit.
    fired = []
    done = threading.Event()

    def recorder(reason, snap, since):
        fired.append((reason, snap.frame_id, since))
        done.set()

    stub = _StubGrabber(True, _snap(frame_id=5, mono=0.0))  # mono=0 → always stale
    wd = Watchdog(
        stub, watchdog_s=0.01, boot_grace_s=0.01, poll_s=0.01, heartbeat_s=999.0,
        on_stall=recorder,
    )
    wd.start()
    try:
        assert done.wait(2.0), "watchdog did not fire on a stalled grabber"
    finally:
        wd.stop()
    reason, frame_id, since = fired[0]
    assert reason == "wedge"
    assert frame_id == 5
    assert since is not None  # seconds-since-last-success is passed through


def test_run_does_not_fire_when_fresh():
    fired = []
    wd = Watchdog(
        _FreshStub(), watchdog_s=5.0, boot_grace_s=5.0, poll_s=0.01, heartbeat_s=999.0,
        on_stall=lambda *a: fired.append(a),
    )
    wd.start()
    try:
        time.sleep(0.15)  # several poll cycles, all fresh
    finally:
        wd.stop()
    assert fired == []


def test_run_emits_heartbeat(caplog):
    wd = Watchdog(
        _FreshStub(), watchdog_s=999.0, boot_grace_s=999.0, poll_s=0.01,
        heartbeat_s=0.01, on_stall=lambda *a: None,
    )
    with caplog.at_level(logging.INFO, logger="edge.watchdog"):
        wd.start()
        try:
            time.sleep(0.15)
        finally:
            wd.stop()
    assert any("edge alive" in r.getMessage() for r in caplog.records)


def test_env_positive_float_rejects_bad_values(monkeypatch):
    monkeypatch.setenv("CAT_TEST_KNOB", "-3")
    assert env_positive_float("CAT_TEST_KNOB", 20.0) == 20.0
    monkeypatch.setenv("CAT_TEST_KNOB", "nope")
    assert env_positive_float("CAT_TEST_KNOB", 20.0) == 20.0
    monkeypatch.delenv("CAT_TEST_KNOB", raising=False)
    assert env_positive_float("CAT_TEST_KNOB", 20.0) == 20.0
    monkeypatch.setenv("CAT_TEST_KNOB", "7.5")
    assert env_positive_float("CAT_TEST_KNOB", 20.0) == 7.5


# -- Grabber.is_running ---------------------------------------------------

def test_is_running_false_before_start_and_under_grab_once():
    g = Grabber(_fake_read_config)
    assert g.is_running() is False
    g.grab_once()  # populates the slot with NO thread
    assert g.snapshot().frame_id == 1
    assert g.is_running() is False  # so grab_once-driven tests never arm a watchdog


def test_is_running_true_while_started_then_false_after_stop():
    g = Grabber(_fake_read_config)
    g.start()
    try:
        assert g.is_running() is True
    finally:
        g.stop()
    assert g.is_running() is False


# -- /stream bound --------------------------------------------------------

def test_stream_terminates_on_stall(tmp_path, monkeypatch):
    # With no grab thread, frame_id freezes at 1 after one grab_once. The bounded
    # generator must RETURN (not loop forever) once nothing has been sent for the
    # stall window — proving the fix that stops the per-reconnect handler leak.
    monkeypatch.setenv("CAT_EDGE_CONFIG", str(tmp_path / "settings.json"))
    monkeypatch.setattr(appmod, "_STREAM_WAIT_S", 0.02)
    monkeypatch.setattr(appmod, "_STREAM_STALL_EXIT_S", 0.1)
    app = create_app(source_factory=FakeCaptureSource, start_grabber=False)
    app.grabber.grab_once()
    # Read in a thread with a join deadline: a regressed bound would loop forever,
    # so assert the read RETURNS rather than relying on it (which would hang the
    # suite — there is no pytest-timeout here).
    result = {}

    def read():
        result["data"] = app.test_client().get("/stream").get_data()

    t = threading.Thread(target=read, daemon=True)
    t.start()
    t.join(timeout=5.0)
    assert not t.is_alive(), "/stream did not terminate on stall (the bound regressed)"
    assert result["data"].count(b"--" + wire.BOUNDARY.encode()) >= 1  # first frame sent
