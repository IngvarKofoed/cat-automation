# Config UI gains manual GPIO output switches

<!-- entry 114 -->

First edge actuator: manual GPIO output switches in the config UI (`GET /api/gpio`,
`POST /api/gpio/<name>` with `{high}`). New `edge/actuators/gpio.py` — a light on BCM 27, a spare
relay on BCM 17 — drives raw pin LEVEL (HIGH/LOW), NOT "on/off": relay boards differ on active-
high vs -low, so the operator maps level→behavior at the wiring. State is NOT persisted (pins
init LOW on boot — the safe/neutral default).
Backend pluggable like `CaptureSource`: real `GpioZeroBackend` lazily imports `gpiozero`
(apt/system-site-packages, absent off a Pi), so `available:false` there and `set()` refuses
(503) rather than faking actuation; the UI disables the switches with a note. Distinct from the
deferred intent-based Control API (lock/unlock/sound/light) — this is a hardware bring-up tool.
