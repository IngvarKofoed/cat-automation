# Running health card fails on a worker's sticky last_error

<!-- entry 311 -->

The `running` health card no longer reads green over a broken worker. Both always-on
workers keep `running` true and set a STICKY `last_error` when a tick throws, so testing
the intent flag alone showed "verdicts current" while nothing had been analysed for days —
in the one card that exists because nobody is expected to be watching. It now fails a row
on `last_error` too, as the fuller readouts below it already did.
