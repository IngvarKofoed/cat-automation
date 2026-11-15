# Backslash-in-f-string trap shipped again, now fixed

<!-- entry 442 -->

Entry 325's exact trap, SHIPPED this time: entry 435's new cell put a backslash inside
an f-string `{}`, a SyntaxError before 3.12 that fails at IMPORT — so `compute.api.app`
would not start at all on the 3.11 compute PC, reporting a bare `)` as the error. Built
outside the f-string now. `compileall` on this 3.13 dev box accepts it and all 1059
tests passed, so nothing local could see it.
