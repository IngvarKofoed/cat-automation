"""The edge's sound player: one WAV clip per call, through ALSA's ``aplay``.

The Pi's speaker is the system's first actuator (docs/specs/2026-11-01-foreign-cat-alarm.md).
The Pi holds NO policy beyond two refusals: it never overlaps two plays (a second play
while the previous ``aplay`` is still alive is refused, not queued), and it refuses every
play while sound is disabled here. Looping, and when to stop, are the compute's job: it
asks for the next clip once the previous one has ended. That keeps the loop fail-safe by
construction — if the compute dies, the clip in progress finishes and the Pi goes quiet.

Sounds are the ``*.wav`` files in ``sounds/``, named by their stem (``alarm1``,
``alarm2``) and listed once at construction. A request names a sound, never a path, so
nothing from a request reaches the filesystem. The shipped clips are 16-bit PCM 44.1 kHz
stereo, because a HAT's hardware device can reject float or 24-bit formats outright.

The process side sits behind an injectable ``runner`` (``which`` + ``spawn``), the way
``GpioOutputs`` hides ``gpiozero``: tests pass a fake, and a host with no ``aplay`` (the
dev Mac) reports ``available: false`` and refuses plays rather than pretending to sound.
``aplay`` writes to ALSA's default device unless ``CAT_EDGE_ALSA_DEVICE`` names another
(``-D``) — the default is the HAT the Pi is already configured for.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import threading
import wave
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)

# Where the shipped clips live. One directory, listed once at construction.
SOUNDS_DIR = Path(__file__).resolve().parent / "sounds"

# Env var naming a non-default ALSA device for ``aplay -D``. Unset → ALSA's default.
ALSA_DEVICE_ENV = "CAT_EDGE_ALSA_DEVICE"

# How long a play waits for ``aplay`` to fail before answering 200. A device that is
# busy or rejects the format makes ``aplay`` exit non-zero almost at once; waiting this
# long means the compute never records a play that didn't happen, at the cost of a
# short pause in the request.
SETTLE_S = 0.3

# How much of ``aplay``'s stderr a failure reports (its tail — the useful line is last).
_STDERR_TAIL_CHARS = 400

# How long disabling waits for a terminated ``aplay`` before killing it outright.
_STOP_WAIT_S = 1.0


class SoundError(Exception):
    """A refused or failed play. ``status`` is the HTTP code the route answers with."""

    status = 500


class SoundDisabled(SoundError):
    """Sound is switched off on the Pi — every play is refused, test plays included."""

    status = 423


class UnknownSound(SoundError):
    """The request named a sound that is not one of the listed clips."""

    status = 404


class SoundBusy(SoundError):
    """The previous play's ``aplay`` is still alive — plays never overlap."""

    status = 409


class SoundUnavailable(SoundError):
    """No ``aplay`` on this host (not a Pi, or alsa-utils missing)."""

    status = 503


class SoundFailed(SoundError):
    """``aplay`` exited non-zero inside the settle window; the message is its stderr tail."""

    status = 502


class Process(Protocol):
    """The slice of ``subprocess.Popen`` the player uses."""

    returncode: "int | None"

    def poll(self) -> "int | None": ...

    def wait(self, timeout: "float | None" = None) -> int: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


class Runner(Protocol):
    """Finds and starts the player process. The real one is ``AplayRunner``."""

    def which(self, program: str) -> "str | None": ...

    def spawn(self, argv: "list[str]", stderr) -> Process: ...


class AplayRunner:
    """The real runner: ``shutil.which`` + a non-blocking ``subprocess.Popen``."""

    def which(self, program: str) -> "str | None":
        return shutil.which(program)

    def spawn(self, argv: "list[str]", stderr) -> Process:
        return subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=stderr
        )


def _wav_duration_s(path: Path) -> float:
    """Clip length in seconds, from the WAV header. Raises on a file ``wave`` can't read."""
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def _read_tail(f) -> str:
    """The last ``_STDERR_TAIL_CHARS`` of a stderr temp file, stripped; '' if unreadable."""
    try:
        f.seek(0)
        text = f.read().decode("utf-8", "replace").strip()
    except (OSError, ValueError):
        return ""
    return text[-_STDERR_TAIL_CHARS:]


class SoundPlayer:
    """Plays one listed clip at a time through ``aplay``, refusing overlap and while disabled.

    State is small and lock-guarded: the enable switch, the live ``aplay`` process (and
    which sound it is playing), and ``last_error``. ``enabled`` is NOT persisted here —
    the app owns persistence (it lives in the edge settings file with the camera config)
    and pushes the value in via ``set_enabled``.
    """

    def __init__(
        self,
        sounds_dir: "Path | str" = SOUNDS_DIR,
        runner: "Runner | None" = None,
        alsa_device: "str | None" = None,
        enabled: bool = True,
        settle_s: float = SETTLE_S,
    ) -> None:
        self._runner = runner or AplayRunner()
        self._device = alsa_device if alsa_device is not None else os.environ.get(ALSA_DEVICE_ENV) or None
        self._settle_s = settle_s
        self._lock = threading.Lock()
        self._enabled = bool(enabled)
        self._aplay = self._runner.which("aplay")
        self._sounds: "dict[str, dict]" = {}
        for path in sorted(Path(sounds_dir).glob("*.wav")):
            try:
                duration = _wav_duration_s(path)
            except (wave.Error, EOFError, OSError) as e:
                # A clip `wave` can't read (a float WAV, a truncated file) is left out
                # rather than listed with no duration: the compute times its loop off
                # duration_s, so a listed clip must carry one.
                log.warning("sound %s skipped: %s", path.name, e)
                continue
            self._sounds[path.stem] = {"path": path, "duration_s": round(duration, 3)}
        self._proc: "Process | None" = None
        self._proc_name: "str | None" = None
        self._proc_err = None
        # True when WE ended the current process (disable / close), so its non-zero exit
        # is not reported as an aplay failure.
        self._stopped = False
        self._last_error: "str | None" = None
        if self._aplay is None:
            log.warning("sound: aplay not found — plays will be refused (not a Pi?)")

    # --- queries ---------------------------------------------------------------------

    @property
    def available(self) -> bool:
        """True when ``aplay`` exists on this host."""
        return self._aplay is not None

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def names(self) -> "list[str]":
        return list(self._sounds)

    def status(self) -> dict:
        """The ``GET /api/sound`` shape: availability, the switch, the clips, now playing."""
        with self._lock:
            self._reap_locked()
            return {
                "available": self.available,
                "enabled": self._enabled,
                "sounds": [
                    {"name": name, "duration_s": s["duration_s"]} for name, s in self._sounds.items()
                ],
                "playing": self._playing_locked(),
                "last_error": self._last_error,
            }

    # --- control ---------------------------------------------------------------------

    def play(self, name: str) -> dict:
        """Start ``name`` and return ``{sound, duration_s}`` once ``aplay`` survives settling.

        Refusals, in order: unknown name (404), disabled (423), no ``aplay`` (503), the
        previous play still sounding (409). A spawn that dies non-zero inside the settle
        window raises ``SoundFailed`` (502) with its stderr tail, so a busy device or a
        rejected format is never reported as played.
        """
        with self._lock:
            sound = self._sounds.get(name)
            if sound is None:
                raise UnknownSound(f"unknown sound: {name}")
            if not self._enabled:
                raise SoundDisabled("sound is disabled on the Pi")
            if self._aplay is None:
                raise SoundUnavailable("aplay not found on this host")
            self._reap_locked()
            if self._playing_locked() is not None:
                raise SoundBusy(f"still playing {self._proc_name}")
            argv = [self._aplay, "-q"]
            if self._device:
                argv += ["-D", self._device]
            argv.append(str(sound["path"]))
            # stderr to a temp file, not a pipe: nothing reads it while aplay runs, so a
            # pipe that filled would block aplay mid-clip.
            err = tempfile.TemporaryFile()
            try:
                proc = self._runner.spawn(argv, err)
            except OSError as e:
                err.close()
                self._last_error = f"failed to start aplay: {e}"
                raise SoundUnavailable(self._last_error) from e
            self._close_err_locked()
            self._proc, self._proc_name, self._proc_err = proc, name, err
            self._stopped = False

        # Settle OUTSIDE the lock, so GET /api/sound and a disable stay responsive.
        try:
            rc = proc.wait(timeout=self._settle_s)
        except subprocess.TimeoutExpired:
            rc = None  # still sounding — the normal case
        with self._lock:
            if self._proc is not proc:
                # Replaced underneath us; can only happen after this play ended, so it
                # played — report it as such.
                return {"sound": name, "duration_s": sound["duration_s"]}
            if self._stopped:
                # Disabled while we settled: the clip was cut, so it must not be reported
                # as played (the compute would count it and time the next clip off it).
                # Checked before `rc`, which is stale: the wait timed out before the stop.
                raise SoundDisabled("sound was disabled while starting")
            if rc is not None and rc != 0:
                tail = _read_tail(err) or f"aplay exited with code {rc}"
                self._last_error = tail
                self._proc = None
                self._proc_name = None
                self._close_err_locked()
                raise SoundFailed(tail)
            self._last_error = None
        return {"sound": name, "duration_s": sound["duration_s"]}

    def set_enabled(self, enabled: bool) -> None:
        """Set the switch. Disabling stops a clip that is sounding right now.

        A mute that let up to 6.5 s of siren finish is not what the switch promises, so
        the current ``aplay`` is terminated (killed if it ignores that).
        """
        with self._lock:
            self._enabled = bool(enabled)
            if not enabled:
                self._stop_locked()

    def close(self) -> None:
        """Stop anything sounding. Safe to call more than once."""
        with self._lock:
            self._stop_locked()
            self._close_err_locked()

    # --- internals (caller holds the lock) ---------------------------------------------

    def _playing_locked(self) -> "str | None":
        if self._proc is not None and self._proc.poll() is None:
            return self._proc_name
        return None

    def _reap_locked(self) -> None:
        """Forget a finished process, recording a non-zero exit we did not cause."""
        if self._proc is None or self._proc.poll() is None:
            return
        rc = self._proc.returncode
        if rc not in (0, None) and not self._stopped:
            self._last_error = _read_tail(self._proc_err) or f"aplay exited with code {rc}"
        self._proc = None
        self._proc_name = None
        self._close_err_locked()

    def _stop_locked(self) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        self._stopped = True
        proc.terminate()
        try:
            proc.wait(timeout=_STOP_WAIT_S)
        except subprocess.TimeoutExpired:
            proc.kill()

    def _close_err_locked(self) -> None:
        if self._proc_err is not None:
            try:
                self._proc_err.close()
            except OSError:
                pass
            self._proc_err = None
