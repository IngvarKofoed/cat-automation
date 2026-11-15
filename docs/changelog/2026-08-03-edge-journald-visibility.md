# Edge grab failures and liveness are visible in journald

<!-- entry 95 -->

Edge grab failures + liveness are now visible in journald (were only in the invisible `last_error`): the
grabber logs the first failure + throttled repeats + recovery, the watchdog logs a 30s heartbeat, and
routine `GET /stream` 200 access lines are filtered so a reconnect storm can't bury them. New `README.md`
documents running the edge (bare + systemd), the reliability env knobs, and reading the crash cause from
the journal / exit code.
