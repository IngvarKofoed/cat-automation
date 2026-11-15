# Review prefers fix-code with a pre-commit gate

<!-- entry 236 -->

The after-edits review now PREFERS `/fix-code --fix` — it rates findings 1-5, has a verifier
refute each before repairing, and is undoable — keeping the inline self-run pass as fallback:
a missing skill changes who reviews, never whether. New pre-commit gate: a commit request
over changes no `--fix` run has seen asks once first, delegated `/git commit` included.
The end-of-turn nudge now points at a user-run `/fix-code`; it buys DISTANCE, not another tool.
