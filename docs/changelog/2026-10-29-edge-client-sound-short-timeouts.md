# Sound client calls use short timeouts and return status

<!-- entry 531 -->

`EdgeClient.get_sound` / `play_sound` use a 1 s + 2 s timeout, not the 5 s + 5 s
defaults, which would stall the 1 s tick and the Start page's readiness poll behind an
unreachable Pi. `play_sound` RETURNS the status rather than raising on non-200: the
Pi's codes are the answer the alarm acts on.
