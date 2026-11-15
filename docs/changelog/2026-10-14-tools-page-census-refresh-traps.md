# Tools page census refresh and button-disabled traps

<!-- entry 453 -->

Two UI traps the browser pass caught. The census refresh was keyed on a running->idle
TRANSITION, which an instant relink job never shows — so the pre-move census sat there
reading "nothing happened"; it now keys on a flag set when we start the job. And the
re-cut button owns its own `disabled`: `renderCleanup`'s shared sweep assigns
`b.disabled = c.running` unconditionally, re-enabling on every idle poll a button the
card had disabled for having no target.
