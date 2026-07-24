"""Manual GPIO output control for the edge tier.

The door Pi has a relay board wired to three BCM pins (channels on GPIO 26, 20,
21). This driver exposes them as named outputs the config UI can drive HIGH or
LOW by hand — a hardware bring-up / testing tool, distinct from (and simpler
than) the deferred intent-based Control API (lock/unlock/sound/light) in
docs/ARCHITECTURE.md.

Raw pin *level*, not "on/off": relay boards differ on active-high vs active-low,
so the switch drives the pin HIGH or LOW and the operator maps level → relay
behavior at the wiring. State is NOT persisted — pins initialize HIGH on boot
(see ``_INITIAL_HIGH``), because the door's relay board is active-low (HIGH =
released/off), so idling HIGH keeps relays off at startup rather than energizing
them. The UI reflects that HIGH-at-boot state.

Backend is pluggable behind ``PinBackend`` exactly as the camera sits behind
``CaptureSource``: the real backend (``GpioZeroBackend``) lazily imports
``gpiozero`` — absent on the dev Mac and any non-Pi host — so the driver reports
``available=False`` there and refuses writes rather than silently pretending to
actuate. Tests inject a fake backend.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable, Protocol

log = logging.getLogger(__name__)

# BCM pin assignments for the relay channels wired at the door, presented in the
# config UI as manual HIGH/LOW switches. Channels 1/2/3 → GPIO 26 / 21 / 20.
# `name` is the API/state key, `label` the human-facing name.
GPIO_OUTPUTS: "tuple[dict, ...]" = (
    {"name": "channel1", "pin": 26, "label": "Channel 1"},
    {"name": "channel2", "pin": 21, "label": "Channel 2"},
    {"name": "channel3", "pin": 20, "label": "Channel 3"},
)

# Level each channel comes up at on boot (state is not persisted). HIGH, because
# the door's relay board is active-low: HIGH = relay released/off, so the pins
# idle HIGH and nothing is energized at startup. Both the tracked state and the
# real backend's OutputDevice initial_value read this, so they can't drift.
_INITIAL_HIGH = True


class GpioUnavailable(RuntimeError):
    """Raised by ``set()`` when no GPIO backend is present to drive the pin."""


class PinBackend(Protocol):
    """A backend that can drive already-registered BCM pins high/low."""

    def write(self, pin: int, high: bool) -> None:
        """Drive ``pin`` HIGH (True) or LOW (False)."""

    def close(self) -> None:
        """Release the pins. Safe to call more than once."""


class GpioZeroBackend:
    """Real backend: one ``gpiozero.OutputDevice`` per pin, initialized HIGH.

    ``gpiozero`` is imported lazily so importing this module never fails off a
    Pi; construction raises (import error, no ``/dev/gpiochip``, permissions)
    and ``GpioOutputs`` catches that to report unavailability. ``active_high``
    with ``initial_value=_INITIAL_HIGH`` brings each pin up HIGH (relays idle
    off on an active-low board); value=1→HIGH, value=0→LOW thereafter.
    """

    def __init__(self, pins: "list[int]") -> None:
        from gpiozero import OutputDevice  # lazy: absent/unusable off a Pi

        self._devices = {
            pin: OutputDevice(pin, active_high=True, initial_value=_INITIAL_HIGH)
            for pin in pins
        }

    def write(self, pin: int, high: bool) -> None:
        self._devices[pin].value = 1 if high else 0

    def close(self) -> None:
        for device in self._devices.values():
            device.close()


BackendFactory = Callable[["list[int]"], PinBackend]


class GpioOutputs:
    """Named GPIO outputs, each drivable HIGH/LOW, with pluggable backend.

    Tracks the last-commanded level per output in memory (the driver's writes
    are the only mutations). When the backend can't be built, ``available`` is
    False and ``set()`` raises ``GpioUnavailable`` — the UI shows the switches
    as inert rather than lying about actuation.
    """

    def __init__(
        self,
        outputs: "tuple[dict, ...]" = GPIO_OUTPUTS,
        backend_factory: "BackendFactory | None" = None,
    ) -> None:
        self._outputs = [dict(o) for o in outputs]
        self._pin_by_name = {o["name"]: o["pin"] for o in self._outputs}
        # name -> is_high; HIGH at boot (see _INITIAL_HIGH), matching the level
        # the backend brings the pins up at. State is not persisted across restarts.
        self._state = {o["name"]: _INITIAL_HIGH for o in self._outputs}
        self._lock = threading.Lock()
        self._backend: "PinBackend | None" = None
        self._error: "str | None" = None
        factory = backend_factory or (lambda pins: GpioZeroBackend(pins))
        try:
            self._backend = factory([o["pin"] for o in self._outputs])
        except Exception as e:  # noqa: BLE001 - any import/init failure → unavailable
            self._error = str(e) or e.__class__.__name__
            log.warning("GPIO backend unavailable (%s): %s", e.__class__.__name__, e)

    @property
    def available(self) -> bool:
        """True when a backend is present and pins can actually be driven."""
        return self._backend is not None

    def names(self) -> "list[str]":
        return [o["name"] for o in self._outputs]

    def outputs(self) -> "list[dict]":
        """Each output as ``{name, pin, label, high}`` — the API/UI shape."""
        with self._lock:
            return [{**o, "high": self._state[o["name"]]} for o in self._outputs]

    def set(self, name: str, high: bool) -> None:
        """Drive output ``name`` HIGH/LOW.

        Raises ``KeyError`` for an unknown name and ``GpioUnavailable`` when no
        backend is present. State only advances after a successful write, so a
        failed drive doesn't report a level the pin never reached.
        """
        if name not in self._pin_by_name:
            raise KeyError(name)
        with self._lock:
            if self._backend is None:
                raise GpioUnavailable(self._error or "no GPIO backend on this host")
            self._backend.write(self._pin_by_name[name], high)
            self._state[name] = high

    def close(self) -> None:
        """Release the backend. Safe to call more than once."""
        with self._lock:
            if self._backend is not None:
                self._backend.close()
                self._backend = None
