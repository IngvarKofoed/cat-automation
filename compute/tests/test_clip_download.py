"""Tests for the visit-clip download — ``GET /api/clip``, ``compute.api.clip`` and
``Store.span_frame_paths``.

See docs/specs/2026-11-02-user-visit-clip-download.md. What earns a test here:

1. The TIMING: every kept frame is held until the next one's ``recv_ts``, so the clip
   lasts as long as the visit did, and a long visit is thinned rather than refused. A
   wrong duration produces a clip that plays at the wrong speed, with no error anywhere.
2. Every stored frame SURVIVES the encode. Measured while building this: with the image
   demuxer's default 25 fps time base every timestamp snaps to a 40 ms grid and frames
   sharing a slot are dropped — a clip that silently shows less than the visit held.
3. The lock is released on EVERY exit path, and an encode whose phone has gone is killed:
   a leak in either would answer "another clip is still being made" until a restart.
4. The refusals carry the household sentences the phone prints as-is.

The encode tests run the real ``imageio-ffmpeg`` binary over a handful of tiny JPEGs and
skip when the package is absent; the lifecycle tests drive a fake subprocess instead.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from compute.api import clip
from compute.collection.store import Store


def _jpeg(w: int = 65, h: int = 49, shade: int = 128) -> bytes:
    # Odd on both axes on purpose: 4:2:0 needs even, so the encode must round DOWN.
    img = np.full((h, w, 3), shade, dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", img)
    assert ok
    return buf.tobytes()


_next_edge_id = [0]


def _frame(ts: int, body: bytes):
    from compute.ingest import StreamFrame
    from shared.wire import StreamFrameMeta

    _next_edge_id[0] += 1
    meta = StreamFrameMeta(frame_id=_next_edge_id[0], ts=ts, motion=True, bbox=None, area=0.5)
    return StreamFrame(meta, body)


def _store(tmp_path, media="media") -> Store:
    return Store(
        db_path=str(tmp_path / "index.db"),
        media_root=str(tmp_path / media),
        max_bytes=50_000_000,
    )


def _visit(store: Store, stamps, body: "bytes | None" = None) -> "tuple[int, int]":
    """One frame per ``recv_ts`` in ``stamps`` → the span's (start_id, end_id)."""
    ids = []
    for i, ts in enumerate(stamps):
        b = body if body is not None else _jpeg(shade=40 + (i * 7) % 180)
        ids.append(store.add(_frame(ts, b), recv_ts_ms=ts))
    return ids[0], ids[-1]


def _client(store: Store) -> TestClient:
    from compute.api.app import create_app

    return TestClient(create_app(store=store, start_collector=False))


def _rows(stamps, root="/m") -> "list[tuple[int, int, str]]":
    return [(i + 1, ts, f"{root}/{i + 1}.jpg") for i, ts in enumerate(stamps)]


@pytest.fixture(autouse=True)
def _lock_is_free():
    """Every test starts AND ends with the encode lock free — a leaked hold is a bug."""
    assert not clip.ENCODE_LOCK.locked()
    yield
    assert not clip.ENCODE_LOCK.locked(), "the encode lock was not released"


# --- the store read --------------------------------------------------------


def test_span_read_is_id_ordered_bounded_and_absolute(tmp_path, monkeypatch):
    # A RELATIVE media root, like compute.sh's default ./data/collection: ffmpeg's concat
    # demuxer resolves relative list entries against the list file's dir, not the CWD.
    monkeypatch.chdir(tmp_path)
    s = Store(db_path=str(tmp_path / "rel.db"), media_root="rel-media", max_bytes=50_000_000)
    first = _visit(s, [1000, 1100, 1200, 1300, 1400])
    rows = s.span_frame_paths(first[0] + 1, first[1] - 1)
    assert [r[0] for r in rows] == [first[0] + 1, first[0] + 2, first[0] + 3]
    assert [r[1] for r in rows] == [1100, 1200, 1300]
    for _, _, path in rows:
        assert os.path.isabs(path)
        assert os.path.isfile(path)
    assert s.span_frame_paths(first[1] + 1, first[1] + 50) == []


# --- timing and thinning ---------------------------------------------------


def test_each_frame_is_held_until_the_next_and_the_last_gets_the_median():
    plan = clip.plan_clip(_rows([0, 100, 250, 350, 1350]))
    assert plan.step == 1
    assert plan.durations[:4] == pytest.approx([0.1, 0.15, 0.1, 1.0])
    # median of the four gaps above = 0.125
    assert plan.durations[4] == pytest.approx(0.125)
    # the clip lasts as long as the visit did, plus the last frame's own hold
    assert sum(plan.durations) == pytest.approx(1.35 + 0.125)


def test_a_single_frame_visit_is_held_a_tenth_of_a_second():
    plan = clip.plan_clip(_rows([5000]))
    assert plan.durations == [pytest.approx(0.1)]


def test_durations_are_clamped_against_a_backwards_step_and_an_absurd_gap():
    # recv_ts monotonic with id is ASSUMED, not enforced (changelog 278).
    plan = clip.plan_clip(_rows([1000, 900, 1000, 60_000]))
    assert plan.durations[0] == pytest.approx(0.010)       # -100 ms → floor
    assert plan.durations[1] == pytest.approx(0.1)
    assert plan.durations[2] == pytest.approx(2.0)         # 59 s → k × the 2 s visit gap


def test_a_long_visit_is_thinned_and_still_lasts_as_long(monkeypatch):
    monkeypatch.setattr(clip, "_CLIP_MAX_FRAMES", 10)
    stamps = [i * 100 for i in range(31)]                  # 31 frames over 3.0 s
    plan = clip.plan_clip(_rows(stamps))
    assert plan.step == 4                                  # ⌈31 / 10⌉
    assert len(plan.frames) <= 10
    # every 4th from the first, then the visit's own last frame closes it
    assert [f[1] for f in plan.frames] == stamps[::4] + [stamps[-1]]
    assert plan.durations[:-2] == [pytest.approx(0.4)] * (len(plan.frames) - 2)
    assert plan.durations[-2] == pytest.approx(0.2)       # frame 28 → 30
    # the ceiling scales with the stride: a 7 s hole between kept frames is held 8 s max
    plan = clip.plan_clip(_rows([0, 100, 200, 300, 7300, 7400, 7500, 7600, 7700, 7800, 7900]))
    assert plan.step == 2
    assert max(plan.durations) <= 2 * 2.0 + 1e-9


def test_a_thinned_clip_ends_on_the_visits_last_frame_within_the_cap(monkeypatch):
    """`frames[::k]` alone stops up to k-1 frames short of the visit's end."""
    monkeypatch.setattr(clip, "_CLIP_MAX_FRAMES", 10)
    stamps = [i * 100 for i in range(20)]                  # k = 2: the stride fills the cap
    plan = clip.plan_clip(_rows(stamps))
    assert len(plan.frames) == 10
    assert plan.frames[0][1] == 0 and plan.frames[-1][1] == stamps[-1]


def test_the_rate_hint_is_the_kept_frames_average_rate(monkeypatch):
    assert clip.plan_clip(_rows([i * 100 for i in range(50)])).rate_hint == 10
    assert clip.plan_clip(_rows([5000])).rate_hint == 10
    monkeypatch.setattr(clip, "_CLIP_MAX_FRAMES", 10)
    # thinned 4x: the clip really does run at ~2.5 fps, and says so
    assert clip.plan_clip(_rows([i * 100 for i in range(40)])).rate_hint == 2


def test_thinning_never_exceeds_the_cap_at_the_widest_span():
    plan = clip.plan_clip(_rows(list(range(0, 10_001 * 100, 100))))  # 10 001 ids, the cap
    assert plan.step == 4
    assert len(plan.frames) <= clip._CLIP_MAX_FRAMES


# --- the ffmpeg inputs -----------------------------------------------------


def test_concat_quoting_keeps_windows_backslashes_and_escapes_quotes():
    assert clip.concat_quote(r"C:\cat door\media\a.jpg") == r"'C:\cat door\media\a.jpg'"
    assert clip.concat_quote("/x/it's.jpg") == "'/x/it'\\''s.jpg'"


def test_the_list_repeats_the_last_file_and_pins_a_millisecond_time_base(tmp_path):
    plan = clip.plan_clip(_rows([0, 100, 230]))
    path = tmp_path / "l.txt"
    clip.write_concat_list(str(path), plan, modern=True)
    lines = path.read_text().splitlines()
    assert lines == [
        "file '/m/1.jpg'", "option framerate 1000", "duration 0.100",
        "file '/m/2.jpg'", "option framerate 1000", "duration 0.130",
        "file '/m/3.jpg'", "option framerate 1000", "duration 0.115",
        "file '/m/3.jpg'", "option framerate 1000",
    ]
    clip.write_concat_list(str(path), plan, modern=False)
    assert "option" not in path.read_text()


@pytest.mark.parametrize("version, modern", [
    ("7.1", True), ("n7.0.2", True), ("5.1.4", True), ("5.0", False),
    ("4.4.2-0ubuntu0.22.04.1", False), ("N-112345-gabcdef", True),
])
def test_ffmpeg_version_gate(version, modern):
    assert clip._is_modern(version) is modern


def test_threads_are_bounded_on_decoder_filter_and_encoder():
    cmd = clip.build_command("ff", "l.txt", "o.mp4", (64, 48), modern=True, rate_hint=10)
    i = cmd.index("-i")
    assert cmd[cmd.index("-threads") + 1] == "1" and cmd.index("-threads") < i
    assert cmd[cmd.index("-filter_threads") + 1] == "1"
    after = cmd[i:]
    assert after[after.index("-threads") + 1] == "2"
    assert "-fps_mode" in cmd and "-vsync" not in cmd
    assert cmd[cmd.index("-pix_fmt") + 1] == "yuv420p"
    assert "scale=64:48:out_range=tv" in cmd
    assert cmd[cmd.index("-x264-params") + 1] == "fps=10"
    old = clip.build_command("ff", "l.txt", "o.mp4", (64, 48), modern=False, rate_hint=10)
    assert "-vsync" in old and "-fps_mode" not in old


# --- route refusals --------------------------------------------------------


def test_too_wide_a_span_is_refused_in_household_words(tmp_path):
    r = _client(_store(tmp_path)).get("/api/clip", params={"start_id": 1, "end_id": 10_002})
    assert r.status_code == 400
    assert r.json()["detail"] == "This visit is too long to save as one clip."


def test_an_inverted_span_keeps_the_validators_wording(tmp_path):
    r = _client(_store(tmp_path)).get("/api/clip", params={"start_id": 9, "end_id": 3})
    assert r.status_code == 400
    assert "must be <=" in r.json()["detail"]


def test_both_bounds_are_required(tmp_path):
    r = _client(_store(tmp_path)).get("/api/clip", params={"start_id": 1})
    assert r.status_code == 422


def test_a_missing_encoder_answers_503_naming_the_install(tmp_path, monkeypatch):
    def broken():
        raise clip.EncoderUnavailable("No module named 'imageio_ffmpeg'")

    monkeypatch.setattr(clip, "ffmpeg_tool", broken)
    r = _client(_store(tmp_path)).get("/api/clip", params={"start_id": 1, "end_id": 5})
    assert r.status_code == 503
    assert "compute/requirements.txt" in r.json()["detail"]


def test_frames_that_aged_out_answer_404(tmp_path, monkeypatch):
    monkeypatch.setattr(clip, "ffmpeg_tool", lambda: ("ffmpeg", True))
    store = _store(tmp_path)
    span = _visit(store, [1000, 1100, 1200])
    for _, _, path in store.span_frame_paths(*span):
        os.remove(path)                       # rows kept, JPEGs gone
    client = _client(store)
    for params in ({"start_id": span[0], "end_id": span[1]},
                   {"start_id": span[1] + 10, "end_id": span[1] + 20}):   # no rows at all
        r = client.get("/api/clip", params=params)
        assert r.status_code == 404
        assert r.json()["detail"] == "This visit's frames have aged out."


def test_a_second_clip_while_one_encodes_is_409(tmp_path, monkeypatch):
    monkeypatch.setattr(clip, "ffmpeg_tool", lambda: ("ffmpeg", True))
    store = _store(tmp_path)
    span = _visit(store, [1000, 1100])
    assert clip.ENCODE_LOCK.acquire(blocking=False)
    try:
        r = _client(store).get("/api/clip", params={"start_id": span[0], "end_id": span[1]})
    finally:
        clip.ENCODE_LOCK.release()
    assert r.status_code == 409
    assert r.json()["detail"] == "Another clip is still being made — try again in a moment."


def test_a_lock_being_released_is_waited_for_not_refused(tmp_path, monkeypatch):
    """The hop case: the abandoned encode's route sees the disconnect only at its next
    poll, so a tap on the next visit can land while its lock is still held. Measured in
    the browser before the grace existed — an immediate tap read "another clip"."""
    import threading

    monkeypatch.setattr(clip, "ffmpeg_tool", lambda: ("ffmpeg", True))
    store = _store(tmp_path)
    assert clip.ENCODE_LOCK.acquire(blocking=False)
    timer = threading.Timer(clip._LOCK_GRACE_S / 3, clip.ENCODE_LOCK.release)
    timer.start()
    try:
        # No frames in this span: a 404 proves the request got PAST the lock.
        r = _client(store).get("/api/clip", params={"start_id": 1, "end_id": 5})
    finally:
        timer.join()
    assert r.status_code == 404


def test_an_unreadable_first_frame_is_a_502_and_frees_the_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(clip, "ffmpeg_tool", lambda: ("ffmpeg", True))
    store = _store(tmp_path)
    span = _visit(store, [1000, 1100], body=b"not a jpeg")
    r = _client(store).get("/api/clip", params={"start_id": span[0], "end_id": span[1]})
    assert r.status_code == 502
    assert r.json()["detail"].startswith("Couldn't make the clip")


# --- the encode lifecycle, with a fake ffmpeg -------------------------------


def _fake_encoder(monkeypatch, tmp_path, script: str, start_delay: float = 0.0):
    """Swap ffmpeg for a Python child running ``script``; returns (procs, workdirs).
    ``start_delay`` holds the worker thread before the Popen, so a cancel can land
    while the start is in flight."""
    procs, workdirs = [], []

    def make_workdir():
        d = tmp_path / f"work{len(workdirs)}"
        d.mkdir()
        workdirs.append(str(d))
        return str(d)

    def start(exe, modern, plan, workdir):
        time.sleep(start_delay)
        with open(os.path.join(workdir, clip._LOG_NAME), "wb") as log:
            p = subprocess.Popen([sys.executable, "-c", script], stderr=log)
        p.started_at = time.monotonic()
        procs.append(p)
        return p

    monkeypatch.setattr(clip, "ffmpeg_tool", lambda: ("ffmpeg", True))
    monkeypatch.setattr(clip, "make_workdir", make_workdir)
    monkeypatch.setattr(clip, "start_encode", start)
    monkeypatch.setattr(clip, "_POLL_S", 0.05)
    return procs, workdirs


def _phone_leaves_after(monkeypatch, checks: int) -> None:
    """``request.is_disconnected()`` answers False ``checks`` times, then True."""
    from starlette.requests import Request

    calls = [0]

    async def maybe_gone(self):
        calls[0] += 1
        return calls[0] > checks

    monkeypatch.setattr(Request, "is_disconnected", maybe_gone)


def test_a_phone_gone_before_the_encode_starts_gets_none_started(tmp_path, monkeypatch):
    procs, workdirs = _fake_encoder(monkeypatch, tmp_path, "import time; time.sleep(30)")
    _phone_leaves_after(monkeypatch, 0)
    store = _store(tmp_path)
    span = _visit(store, [1000, 1100])
    r = _client(store).get("/api/clip", params={"start_id": span[0], "end_id": span[1]})
    assert r.status_code == 499
    assert procs == [] and workdirs == []


def test_a_phone_that_leaves_kills_the_encode_and_frees_the_lock(tmp_path, monkeypatch):
    procs, workdirs = _fake_encoder(monkeypatch, tmp_path, "import time; time.sleep(30)")
    _phone_leaves_after(monkeypatch, 2)          # the pre-start check, then one poll
    store = _store(tmp_path)
    span = _visit(store, [1000, 1100])
    t0 = time.monotonic()
    r = _client(store).get("/api/clip", params={"start_id": span[0], "end_id": span[1]})
    assert time.monotonic() - t0 < 10
    assert r.status_code == 499
    assert procs[0].poll() is not None, "ffmpeg was left running for a phone that left"
    assert not os.path.exists(workdirs[0])


def test_an_encode_past_the_timeout_is_killed_and_answers_502(tmp_path, monkeypatch):
    procs, workdirs = _fake_encoder(monkeypatch, tmp_path, "import time; time.sleep(30)")
    monkeypatch.setattr(clip, "_CLIP_TIMEOUT_S", 0.3)
    store = _store(tmp_path)
    span = _visit(store, [1000, 1100])
    r = _client(store).get("/api/clip", params={"start_id": span[0], "end_id": span[1]})
    assert r.status_code == 502
    assert "took longer than" in r.json()["detail"]
    assert procs[0].poll() is not None
    assert not os.path.exists(workdirs[0])


def test_a_failed_encode_reports_the_tail_of_its_stderr(tmp_path, monkeypatch):
    procs, workdirs = _fake_encoder(
        monkeypatch, tmp_path,
        "import sys; sys.stderr.write('noise\\nError opening output: disk full\\n'); sys.exit(1)",
    )
    store = _store(tmp_path)
    span = _visit(store, [1000, 1100])
    r = _client(store).get("/api/clip", params={"start_id": span[0], "end_id": span[1]})
    assert r.status_code == 502
    assert r.json()["detail"].startswith("Couldn't make the clip: ")
    assert r.json()["detail"].endswith("disk full")
    assert not os.path.exists(workdirs[0])


def test_the_timeout_counts_from_the_request_not_from_ffmpeg(tmp_path, monkeypatch):
    """It exists to answer before the PHONE gives up, and the phone has been waiting since
    it asked — so time spent before ffmpeg starts is part of the budget."""
    procs, workdirs = _fake_encoder(
        monkeypatch, tmp_path, "import time; time.sleep(30)", start_delay=1.2
    )
    monkeypatch.setattr(clip, "_CLIP_TIMEOUT_S", 1.0)
    store = _store(tmp_path)
    span = _visit(store, [1000, 1100])
    client = _client(store)
    r = client.get("/api/clip", params={"start_id": span[0], "end_id": span[1]})
    lived = time.monotonic() - procs[0].started_at
    assert r.status_code == 502
    # The 1.2 s spent before the start already exceeds the 1.0 s budget, so ffmpeg is
    # killed at its first poll. Timed from ffmpeg's own start it would live a full 1.0 s.
    assert lived < 0.6, f"ffmpeg lived {lived:.2f} s past a budget already spent"


def test_an_encoder_that_cannot_start_is_a_household_502(tmp_path, monkeypatch):
    def blocked(cmd, stderr):
        raise PermissionError(13, "Access is denied")      # e.g. antivirus on Windows

    monkeypatch.setattr(clip, "ffmpeg_tool", lambda: ("ffmpeg", True))
    monkeypatch.setattr(clip, "_popen_low_priority", blocked)
    store = _store(tmp_path)
    span = _visit(store, [1000, 1100])
    r = _client(store).get("/api/clip", params={"start_id": span[0], "end_id": span[1]})
    assert r.status_code == 502
    assert r.json()["detail"].startswith("Couldn't make the clip: the encoder could not be started")


def test_an_encode_that_exits_cleanly_but_writes_nothing_is_a_502(tmp_path, monkeypatch):
    procs, workdirs = _fake_encoder(monkeypatch, tmp_path, "pass")
    store = _store(tmp_path)
    span = _visit(store, [1000, 1100])
    r = _client(store).get("/api/clip", params={"start_id": span[0], "end_id": span[1]})
    assert r.status_code == 502
    assert r.json()["detail"] == "Couldn't make the clip (ffmpeg wrote no video)."
    assert not os.path.exists(workdirs[0])


def _clip_endpoint(store):
    from compute.api.app import create_app

    app = create_app(store=store, start_collector=False)
    return next(r.endpoint for r in app.routes if getattr(r, "path", None) == "/api/clip")


class _StillHere:
    async def is_disconnected(self):
        return False


@pytest.mark.parametrize("start_delay, cancel_after", [
    (0.5, 0.15),   # cancelled while the worker thread is still inside the start
    (0.0, 0.4),    # cancelled while ffmpeg runs
])
def test_a_cancelled_request_never_orphans_ffmpeg(tmp_path, monkeypatch, start_delay, cancel_after):
    """A server shutdown cancels the request coroutine. Measured before the fix: cancelled
    mid-start, the coroutine never received the process, so its `finally` had nothing to
    kill, deleted the workdir from under a running ffmpeg and released the lock."""
    import asyncio

    procs, workdirs = _fake_encoder(
        monkeypatch, tmp_path, "import time; time.sleep(30)", start_delay=start_delay
    )
    store = _store(tmp_path)
    span = _visit(store, [1000, 1100])
    endpoint = _clip_endpoint(store)

    async def run():
        task = asyncio.ensure_future(
            endpoint(request=_StillHere(), start_id=span[0], end_id=span[1])
        )
        await asyncio.sleep(cancel_after)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(start_delay + 0.3)   # let an in-flight start land

    asyncio.run(run())
    assert len(procs) == 1
    procs[0].wait(timeout=5)
    assert procs[0].returncode != 0, "ffmpeg ran on after its request was cancelled"
    assert not os.path.exists(workdirs[0])


# --- the real encode -------------------------------------------------------


def _encoder_or_skip():
    pytest.importorskip("imageio_ffmpeg")
    try:
        return clip.ffmpeg_tool()[0]
    except clip.EncoderUnavailable as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"no usable ffmpeg: {exc}")


def _probe(exe: str, path: str) -> "tuple[list[float], str]":
    """(every decoded frame's pts in seconds, the video stream line) of an MP4."""
    out = subprocess.run(
        [exe, "-hide_banner", "-i", path, "-vf", "showinfo", "-f", "null", "-"],
        capture_output=True, text=True, timeout=60,
    ).stderr
    pts = [float(m) for m in re.findall(r"pts_time:\s*([0-9.]+)", out)]
    stream = next((ln for ln in out.splitlines() if "Video:" in ln), "")
    return pts, stream


def test_a_clip_holds_every_frame_at_real_speed(tmp_path, monkeypatch):
    exe = _encoder_or_skip()
    workdirs = []
    real_make = clip.make_workdir

    def tracked():
        d = real_make()
        workdirs.append(d)
        return d

    monkeypatch.setattr(clip, "make_workdir", tracked)
    store = _store(tmp_path)
    # ~10 fps with the gaps a real store has: a 30 ms and a 12 ms jitter (both inside one
    # 40 ms slot of a 25 fps grid — the drop the millisecond time base prevents) and a
    # 1.5 s hole where motion-only storage kept nothing.
    t0 = 1_780_000_000_000
    gaps = [100] * 8 + [30, 100, 1500, 12, 100] + [100] * 6
    stamps = [t0]
    for g in gaps:
        stamps.append(stamps[-1] + g)
    span = _visit(store, stamps)

    r = _client(store).get("/api/clip", params={"start_id": span[0], "end_id": span[1]})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "video/mp4"
    assert r.headers["cache-control"] == "no-store"
    expected_name = time.strftime("catdoor-%Y-%m-%d-%H%M%S.mp4", time.localtime(t0 / 1000))
    assert r.headers["content-disposition"] == f'attachment; filename="{expected_name}"'

    out = tmp_path / "got.mp4"
    out.write_bytes(r.content)
    pts, stream = _probe(exe, str(out))
    # Every stored frame, plus the repeated last one that carries its hold.
    assert len(pts) == len(stamps) + 1, pts
    # Each frame lands at its own recv_ts offset, to the millisecond.
    want = [(s - t0) / 1000 for s in stamps]
    assert pts[: len(stamps)] == pytest.approx(want, abs=0.002)
    # ... so the clip runs first frame → last frame, plus the last frame's hold.
    assert pts[-1] == pytest.approx(want[-1] + 0.1, abs=0.002)
    assert "h264" in stream and "yuv420p(tv" in stream, stream
    assert "64x48" in stream, stream      # 65x49 rounded down to even
    # Sent, then deleted — nothing is kept on the compute PC.
    assert workdirs and not os.path.exists(workdirs[0])


def _level_idc(exe: str, path: str) -> int:
    out = subprocess.run(
        [exe, "-hide_banner", "-i", path, "-c", "copy", "-bsf:v", "trace_headers",
         "-f", "null", "-"],
        capture_output=True, text=True, timeout=60,
    ).stderr
    return int(re.search(r"level_idc\s+\d+\s*=\s*(\d+)", out).group(1))


def test_the_clip_signals_an_h264_level_for_its_real_rate(tmp_path):
    """The millisecond time base reads to x264 as 1000 fps, and it signals a level to
    match: measured L4.2 for this 320x240 clip without the rate hint, L6.0 at 1280x720 —
    levels a phone's hardware decoder may refuse. At the real ~10 fps it is L1.2."""
    exe = _encoder_or_skip()
    store = _store(tmp_path)
    t0 = 1_780_000_000_000
    span = _visit(store, [t0 + i * 100 for i in range(10)], body=_jpeg(320, 240))
    r = _client(store).get("/api/clip", params={"start_id": span[0], "end_id": span[1]})
    assert r.status_code == 200, r.text
    out = tmp_path / "got.mp4"
    out.write_bytes(r.content)
    assert _level_idc(exe, str(out)) <= 31     # ≤ L3.1, where 720p at ~10 fps also lands


def test_a_visit_with_evicted_frames_holds_the_previous_frame(tmp_path):
    exe = _encoder_or_skip()
    store = _store(tmp_path)
    t0 = 1_780_000_000_000
    stamps = [t0 + i * 100 for i in range(10)]
    span = _visit(store, stamps)
    rows = store.span_frame_paths(*span)
    for _, _, path in rows[3:6]:
        os.remove(path)                       # three frames aged out mid-visit
    r = _client(store).get("/api/clip", params={"start_id": span[0], "end_id": span[1]})
    assert r.status_code == 200, r.text
    out = tmp_path / "got.mp4"
    out.write_bytes(r.content)
    pts, _ = _probe(exe, str(out))
    assert len(pts) == 7 + 1
    # frame 2 is held across the hole until frame 6: still real speed, not sped up
    assert pts[3] == pytest.approx(0.6, abs=0.002)
    assert pts[-1] == pytest.approx(0.9 + 0.1, abs=0.002)
