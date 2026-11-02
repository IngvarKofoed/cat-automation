"""On-demand visit clips: a span's stored JPEGs → one iPhone-playable H.264 MP4.

The machinery behind ``GET /api/clip`` (docs/specs/2026-11-02-user-visit-clip-download.md).
A clip is a VIEW of frames the store already holds: it is encoded on request into a
temporary directory, sent, and deleted. Nothing here keeps a clip, because a second media
store outside the frame store's byte cap would have nothing to evict it.

The route owns the request lifecycle (validation, the lock, watching for the phone to
leave); this module owns everything ffmpeg-shaped, so each piece is testable on its own:

- ``ffmpeg_tool`` finds the encoder ``imageio-ffmpeg`` ships, or raises
  ``EncoderUnavailable`` — the "pip install not run yet" state the route answers 503 for.
- ``plan_clip`` thins a long visit and times every kept frame off its own ``recv_ts``.
- ``start_encode`` writes the concat-demuxer list and starts ffmpeg at low priority with
  its threads bounded, because the collector, the detection worker and the alarm's 1 s
  tick share this machine.
"""
from __future__ import annotations

import asyncio
import math
import os
import re
import shutil
import statistics
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass

from starlette.concurrency import run_in_threadpool

from compute.collection.store import _VISIT_GAP_MS

# Above this many frames a visit is THINNED (every k-th frame kept) rather than refused.
# It bounds the encode, which must finish inside the phone's request: with +faststart
# nothing is sent until the encode is done, and iOS Safari gives up on a response that
# hasn't started after roughly a minute. ~5 min of visit at the ~10 fps this door runs.
_CLIP_MAX_FRAMES = 3000

# The encode's own deadline, deliberately below the phone's give-up so the 502 still
# reaches the phone and the lock is not held for a clip nobody will receive.
_CLIP_TIMEOUT_S = 45.0

# How often the route checks whether the phone has gone while ffmpeg runs.
_POLL_S = 0.5

# Per-frame duration bounds. The floor keeps a backwards `recv_ts` step (monotonic with id
# is assumed, never enforced — changelog 278) from producing a zero or negative duration;
# the ceiling is k × the visit gap, since no gap between consecutive frames of one visit
# exceeds `_VISIT_GAP_MS`, so none between kept frames exceeds k times that.
_MIN_DURATION_S = 0.010
_SINGLE_FRAME_S = 0.1

# One clip at a time, process-wide: a household tapping Download on two phones must not
# put two encodes on the CPU the door's workers share. Never queued — a second request
# is refused with 409 (see ``acquire_encode_lock`` for the one short wait it gets).
ENCODE_LOCK = threading.Lock()

# How long a request waits for a lock that is being RELEASED before it answers 409. An
# encode whose phone left is noticed at the next disconnect poll and torn down in
# milliseconds after it, so two polls covers it with margin; a genuinely running encode
# holds the lock for seconds, so it is still refused.
_LOCK_GRACE_S = 2 * _POLL_S

_LIST_NAME = "frames.txt"
_LOG_NAME = "ffmpeg.log"
_OUTPUT_NAME = "clip.mp4"


async def acquire_encode_lock() -> bool:
    """Take ``ENCODE_LOCK`` without blocking the event loop; False if still held after
    ``_LOCK_GRACE_S``.

    The grace is what makes "tap ⬇, hop, tap ⬇ again" start the new clip at once. The
    abandoned encode's fetch is aborted the instant the reader hops, but the route only
    sees the disconnect at its next poll, up to ``_POLL_S`` later — measured in the
    browser, a tap on the next visit inside that window was refused "another clip is
    still being made" about a clip nobody wanted.
    """
    deadline = time.monotonic() + _LOCK_GRACE_S
    while not ENCODE_LOCK.acquire(blocking=False):
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.05)
    return True


class EncoderUnavailable(Exception):
    """``imageio_ffmpeg`` won't import, or it can't produce a usable ffmpeg binary."""


class ClipError(Exception):
    """A clip could not be started for a reason worth one sentence to the phone."""


@dataclass(frozen=True)
class ClipPlan:
    """The frames a clip will show and how long each is held.

    ``frames`` are the kept ``(id, recv_ts, absolute_path)`` rows, id ASC; ``durations``
    holds one value in seconds per kept frame; ``step`` is the thinning stride k (1 when
    nothing was thinned).
    """

    frames: "list[tuple[int, int, str]]"
    durations: "list[float]"
    step: int

    @property
    def rate_hint(self) -> int:
        """The clip's average frame rate, whole, in [1, 60] — what x264 is told the rate
        is (see ``build_command``). Never a constraint on the timestamps."""
        total = sum(self.durations)
        return max(1, min(60, round(len(self.frames) / total))) if total > 0 else 10


_tool: "tuple[str, bool] | None" = None


def ffmpeg_tool() -> "tuple[str, bool]":
    """``(ffmpeg_exe, modern)`` — the binary ``imageio-ffmpeg`` ships, and whether it is
    ffmpeg ≥ 5.1.

    ``modern`` picks two spellings: ``-fps_mode vfr`` (older builds only know
    ``-vsync vfr``) and the concat list's per-file ``option framerate`` line (see
    ``write_concat_list``). Only an ``IMAGEIO_FFMPEG_EXE`` override can make it false; the
    bundled binary is 7.x.

    Cached on SUCCESS only, so a compute PC that gains the package needs no code path of
    its own — just a restart. Probing the version runs the binary once, so this belongs
    off the event loop on its first call.
    """
    global _tool
    if _tool is not None:
        return _tool
    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        version = imageio_ffmpeg.get_ffmpeg_version()
    except Exception as exc:  # ImportError, RuntimeError, a missing/broken override …
        raise EncoderUnavailable(str(exc)) from exc
    _tool = (exe, _is_modern(version))
    return _tool


def _is_modern(version: str) -> bool:
    """Whether a version string is ffmpeg ≥ 5.1. A string with no ``major.minor`` in it
    (a git snapshot such as ``N-112345-gabc``) is a recent build, so it counts as modern."""
    m = re.search(r"(\d+)\.(\d+)", version or "")
    if not m:
        return True
    return (int(m.group(1)), int(m.group(2))) >= (5, 1)


def present_frames(rows: "list[tuple[int, int, str]]") -> "list[tuple[int, int, str]]":
    """The rows whose JPEG is still on disk. Called OFF the store lock, after the span read
    has returned — see ``Store.span_frame_paths``."""
    return [r for r in rows if os.path.isfile(r[2])]


def plan_clip(frames: "list[tuple[int, int, str]]") -> ClipPlan:
    """Thin ``frames`` (non-empty, id ASC) to at most ``_CLIP_MAX_FRAMES`` and time each kept
    frame from ``recv_ts``, so the clip lasts as long as the visit did.

    Above the cap every k-th frame is kept, k = ⌈n / cap⌉, starting with the first — and
    the visit's LAST frame always closes it. ``frames[::k]`` alone stops up to k-1 frames
    short, so a thinned clip would end before the visit did and lose the cat's final
    position; the last frame is appended, or takes the final slot when the stride already
    filled the cap, so the cap still holds. Each kept frame is held until the next kept
    frame's ``recv_ts`` — so a gap (motion-only storage, an evicted frame) holds the
    previous frame for the gap instead of speeding the clip up. The last frame gets the
    median of the others (0.1 s on its own), then every duration is clamped to
    [10 ms, k × the visit gap].
    """
    if not frames:
        raise ValueError("plan_clip needs at least one frame")
    step = max(1, math.ceil(len(frames) / _CLIP_MAX_FRAMES))
    kept = frames[::step]
    if kept[-1] is not frames[-1]:
        if len(kept) < _CLIP_MAX_FRAMES:
            kept.append(frames[-1])
        else:
            kept[-1] = frames[-1]
    ts = [f[1] for f in kept]
    gaps = [(ts[i + 1] - ts[i]) / 1000.0 for i in range(len(ts) - 1)]
    last = statistics.median(gaps) if gaps else _SINGLE_FRAME_S
    ceiling = step * _VISIT_GAP_MS / 1000.0
    durations = [min(max(d, _MIN_DURATION_S), ceiling) for d in gaps + [last]]
    return ClipPlan(frames=list(kept), durations=durations, step=step)


def concat_quote(path: str) -> str:
    """``path`` quoted for a concat-demuxer list line.

    Inside single quotes the list parser takes every character literally — backslashes
    included, which is what keeps a Windows path intact — so the one character needing
    care is the quote itself, closed, escaped and reopened as ``'\\''``.
    """
    return "'" + path.replace("'", "'\\''") + "'"


def write_concat_list(list_path: str, plan: ClipPlan, modern: bool) -> None:
    """Write the concat-demuxer list that carries the per-frame timing.

    One ``duration`` line per file, and the last file listed a second time: the demuxer
    ignores the FINAL entry's duration, so without the repeat the last frame would not be
    held at all.

    ``option framerate 1000`` (modern builds) is load-bearing: each JPEG is opened by an
    image demuxer whose default rate is 25 fps, and the concat demuxer keeps that 1/25 s
    time base — measured, every timestamp snaps to a 40 ms grid and two frames landing in
    one slot are DROPPED by ``-fps_mode vfr``. At 1/1000 s the clip carries `recv_ts`'s own
    millisecond timing and every kept frame survives.
    """
    opt = "option framerate 1000\n" if modern else ""
    lines = []
    for (_, _, path), dur in zip(plan.frames, plan.durations):
        lines.append(f"file {concat_quote(path)}\n{opt}duration {dur:.3f}\n")
    lines.append(f"file {concat_quote(plan.frames[-1][2])}\n{opt}")
    with open(list_path, "w", encoding="utf-8") as fh:
        fh.write("".join(lines))


def frame_size(path: str) -> "tuple[int, int]":
    """The output size: this frame's ``(width, height)``, each rounded down to even, since
    4:2:0 chroma needs both even. Raises ``ClipError`` for an unreadable frame."""
    import cv2

    img = cv2.imread(path, cv2.IMREAD_UNCHANGED)
    if img is None:
        raise ClipError("its first frame can't be read")
    h, w = img.shape[:2]
    w, h = w - w % 2, h - h % 2
    if w < 2 or h < 2:
        raise ClipError("its frames are too small to encode")
    return w, h


def build_command(
    exe: str, list_path: str, out_path: str, size: "tuple[int, int]", modern: bool,
    rate_hint: int,
) -> "list[str]":
    """The ffmpeg argv: an H.264 MP4 an iPhone plays, on bounded threads.

    - ``-threads 1`` BEFORE ``-i`` holds the JPEG decoder to one thread, and
      ``-filter_threads 1`` the scale filter; an output ``-threads`` alone limits only
      x264, leaving the other two free to take every core. x264 gets two.
    - Every frame is scaled to ONE size (the first kept frame's, even), so a mid-visit
      change of the edge's clip rectangle cannot fail the encode.
    - ``out_range=tv`` + ``-pix_fmt yuv420p``: the JPEGs are full range, and with
      ``-pix_fmt`` alone ffmpeg converts the layout but carries the full-range flag
      through — measured, the stream still reads ``yuvj420p(pc)``. This yields plain
      limited-range ``yuv420p(tv)``.
    - ``-fps_mode vfr`` keeps each frame's own timestamp. MP4 otherwise defaults to a
      constant rate and DUPLICATES frames to fill each duration, so the encoded frame
      count would grow with visit length and ``_CLIP_MAX_FRAMES`` would no longer bound it.
    - ``-x264-params fps=<rate_hint>`` tells x264 the visit's REAL average rate. It
      otherwise reads the millisecond time base as 1000 fps and signals an H.264 level to
      match — measured, High@L6.0 for a 1280x720 clip and L5.2 at 640x480, levels a
      phone's decoder may refuse — where ~10 fps at 720p is L3.1. It moves no timestamp.
    - ``+faststart`` moves the index to the front so Safari can start playing early.
    """
    width, height = size
    return [
        exe, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
        "-filter_threads", "1",
        "-threads", "1",
        "-f", "concat", "-safe", "0", "-i", list_path,
        "-vf", f"scale={width}:{height}:out_range=tv",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
        "-threads", "2",
        "-x264-params", f"fps={int(rate_hint)}",
        "-movflags", "+faststart",
        "-an",
        *(("-fps_mode", "vfr") if modern else ("-vsync", "vfr")),
        out_path,
    ]


def make_workdir() -> str:
    """A fresh temporary directory for one clip's list, log and output."""
    return tempfile.mkdtemp(prefix="catdoor-clip-")


def output_path(workdir: str) -> str:
    return os.path.join(workdir, _OUTPUT_NAME)


def start_encode(exe: str, modern: bool, plan: ClipPlan, workdir: str) -> subprocess.Popen:
    """Write the list and start ffmpeg on it at below-normal priority; returns the process.

    stderr goes to a log FILE in ``workdir``, not a pipe: the route only polls the process,
    and an unread pipe would block ffmpeg once its buffer filled.
    """
    size = frame_size(plan.frames[0][2])
    list_path = os.path.join(workdir, _LIST_NAME)
    cmd = build_command(exe, list_path, output_path(workdir), size, modern, plan.rate_hint)
    # An OSError here (a full or unwritable temp dir; on Windows, antivirus blocking the
    # bundled binary) would otherwise reach the phone as a bare "HTTP 500".
    try:
        write_concat_list(list_path, plan, modern)
        with open(os.path.join(workdir, _LOG_NAME), "wb") as log:
            return _popen_low_priority(cmd, log)
    except OSError as exc:
        raise ClipError(f"the encoder could not be started ({exc})") from exc


async def start_encode_supervised(
    exe: str, modern: bool, plan: ClipPlan, workdir: str
) -> subprocess.Popen:
    """``start_encode`` off the event loop, without ever losing the process it starts.

    A plain ``await run_in_threadpool(start_encode, …)`` cancelled while the worker thread
    is inside the start raises at the await, and the thread then returns a live ffmpeg
    nothing holds — measured, the route's ``finally`` saw no process to kill, deleted the
    workdir from under it and released the lock with it still running. (An anyio shield
    does not help: it does not hold against a native ``task.cancel()``, also measured.)

    So the start runs as its own task. On cancellation this waits for it to land — the
    caller's cleanup must not delete the workdir under a start still writing into it —
    kills and reaps what it started, then lets the cancellation through. Cancelled AGAIN
    meanwhile, it leaves a callback that kills the process whenever it appears.
    """
    task = asyncio.ensure_future(run_in_threadpool(start_encode, exe, modern, plan, workdir))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await asyncio.wait({task})
            if not task.cancelled() and task.exception() is None:
                await run_in_threadpool(discard, task.result(), None)
        finally:
            if not task.done():
                task.add_done_callback(_kill_if_started)
        raise


def _kill_if_started(task: "asyncio.Future") -> None:
    if not task.cancelled() and task.exception() is None:
        task.result().kill()


def _popen_low_priority(cmd: "list[str]", stderr) -> subprocess.Popen:
    """Start ``cmd`` below normal priority — ``BELOW_NORMAL_PRIORITY_CLASS`` on Windows,
    ``nice`` elsewhere.

    ``nice`` as a PREFIX rather than renicing after start: on Linux niceness is per-thread
    and inherited at thread creation, so it must be in place before ffmpeg spawns its
    threads. ``nice`` execs the command, so the returned pid is ffmpeg's own and a kill
    reaches it. Without a ``nice`` binary the process is reniced right after start, which
    is the best that is left.
    """
    kwargs = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": stderr}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.BELOW_NORMAL_PRIORITY_CLASS
        return subprocess.Popen(cmd, **kwargs)
    nice = shutil.which("nice")
    if nice:
        return subprocess.Popen([nice, "-n", "10", *cmd], **kwargs)
    proc = subprocess.Popen(cmd, **kwargs)
    try:
        os.setpriority(os.PRIO_PROCESS, proc.pid, 10)
    except (AttributeError, OSError):
        pass
    return proc


def stderr_tail(workdir: str, limit: int = 300) -> str:
    """The end of ffmpeg's stderr log as one line, for the 502's detail ('' if none)."""
    try:
        with open(os.path.join(workdir, _LOG_NAME), "rb") as fh:
            text = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    text = " ".join(line.strip() for line in text.splitlines() if line.strip())
    return text[-limit:]


def discard(proc: "subprocess.Popen | None", workdir: "str | None") -> None:
    """Kill ``proc`` if it is still running, then delete ``workdir``. Safe on any exit path.

    The wait is what lets the delete succeed on Windows, where a file a live process still
    holds open cannot be removed.
    """
    if proc is not None and proc.poll() is None:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    if workdir:
        shutil.rmtree(workdir, ignore_errors=True)


def clip_filename(recv_ts_ms: int) -> str:
    """``catdoor-YYYY-MM-DD-HHMMSS.mp4`` from a frame's ``recv_ts``, in this PC's local
    time — the household clock the feed shows."""
    return time.strftime("catdoor-%Y-%m-%d-%H%M%S.mp4", time.localtime(recv_ts_ms / 1000.0))
