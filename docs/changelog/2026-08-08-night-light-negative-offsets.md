# Night-light offsets accept negative minutes

<!-- entry 120 -->

Night-light offsets now accept NEGATIVE minutes (range widened to −240..240): a negative value
shifts the transition the other way — lamp on AFTER sunset / off BEFORE sunrise, a narrower
on-window — vs the prior 0..240 which only ever widened coverage (on before dark / off after light).
Validation + UI min-bound only; the schedule math already handled negative timedeltas.
