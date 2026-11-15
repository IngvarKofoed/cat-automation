# Before-the-cap regression test was hollow

<!-- entry 411 -->

The before-the-cap regression test was HOLLOW as first written: its "thin/newest" visits
carried the OLDER timestamps, so the thick ones survived truncation whichever order ran
and the test could not fail. Proven by injecting the exact regression — old data passed,
repaired data fails. A test whose data contradicts its own comment asserts nothing.
