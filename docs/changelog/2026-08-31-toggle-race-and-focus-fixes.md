# Queue load sequence guard and checkbox blur after toggle

<!-- entry 235 -->

Two bugs the browser test caught, both in the new toggle's own wiring: `loadQueue` had no
sequence guard, so a slow earlier fetch overwrote a newer filtered one and the toggle
appeared not to work (mount's own load raced it); and leaving focus on the checkbox made
`onKey` swallow the operator's next label silently, since it ignores keystrokes from
inputs — so the control now blurs itself. Rather than exempt checkboxes from that guard:
Space is bound to skip AND toggles a focused checkbox, so it would fire both.
