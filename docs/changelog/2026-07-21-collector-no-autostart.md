# Collector no longer auto-starts at launch

<!-- entry 28 -->

Compute collector no longer auto-starts at launch — a fresh `compute.sh` / `compute.ps1` run
wires the collector but stays stopped, so the operator clicks Start in the browse UI before
any frame is written to the store (avoids silently filling the store on every launch).
`create_app`'s `start_collector` now only *wires* the live client + shutdown hook; a separate
`autostart` flag — default off, resolved from `CAT_COLLECT_AUTOSTART` — gates begin-immediately.
