# One stage setting replaces three switches

<!-- entry 299 -->

One `stage` setting (`tuning` | `collecting` | `running`) is now the single intent for
capture mode and the two always-on workers, replacing the trio of switches
(`motion_only`, `yolo_oracle`, `live_identify`) an operator assembled by hand.
`POST /api/stage`; the Start page picker replaces three controls. Makes real the `Mode`
entity ARCHITECTURE had claimed since day one and the code never had.
Spec: docs/specs/2026-08-02-operating-stages.md.
