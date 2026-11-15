# admin-next Start page wires capture mode, collection and cleanup

<!-- entry 127 -->

admin-next Start page (Wave 2 P1 frontend) — the first real page in the shell: capture-mode toggle
(Tuning=keep-all vs Collecting=motion-only), collection controls (start/stop/resume, live-naming,
clear-all), active-model+threshold readout, store stats, the lat/lon location setting, and the two
cleanup purges (non-motion + orphan) with estimate/run/progress/cancel — wiring the Wave-1 backend.
Shell pages are now a mount(view)→teardown model, so a live page polls (3s) and stops its timers on nav.
