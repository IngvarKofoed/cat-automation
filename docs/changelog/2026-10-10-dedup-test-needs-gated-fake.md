# A dedup test needs a gated fake, not an immediate one

<!-- entry 433 -->

A dedup test built on a fake that returns IMMEDIATELY asserts nothing: dedup only guards
against the RUNNING job, so the second press is never deduped whatever the params say.
Two of this change's tests passed against the injected regression for that reason and
were rebuilt on a gated fake. Entry 411's hollow-test trap, second instance — the tell
is the same, a test whose scenario cannot reach the condition it names.
