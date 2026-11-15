# GPIO channels boot HIGH for the active-low relay board

<!-- entry 116 -->

GPIO channels now boot HIGH, not LOW (new `_INITIAL_HIGH` — the one source both the tracked
state and the backend's `OutputDevice(initial_value=)` read, so they can't drift). The door's
relay board is active-low, so HIGH = released/off: idling HIGH keeps relays de-energized at
startup instead of switching them on. `/api/gpio` and the UI report HIGH from first load.
