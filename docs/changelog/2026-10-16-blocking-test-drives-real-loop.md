# Blocking test drives run_feasibility rather than re-implementing numpy

<!-- entry 466 -->

That was found by testing the TEST: the first version of the blocking test hand-rolled
its own blocked argsort and compared it to numpy, so it asserted a numpy identity and
passed against an injected stride bug in the real loop. It now monkeypatches
`_KNN_ROWS` and drives `run_feasibility` itself at five block sizes. Entry 411/433's
hollow-test trap, third instance — the tell each time is a test that never reaches
the code it names.
