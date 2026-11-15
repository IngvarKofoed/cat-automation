# GPIO outputs changed to three relay channels

<!-- entry 115 -->

GPIO outputs changed to match the actual door wiring: three relay channels on BCM 26/20/21
— `channel1` (26), `channel2` (20), `channel3` (21) — replacing the earlier `light` (27) /
`aux` (17). Backend/API/UI unchanged (all data-driven off `GPIO_OUTPUTS`); a Pi picks up the
new pins on the next edge restart, releasing 17/27. Still raw HIGH/LOW level control.
