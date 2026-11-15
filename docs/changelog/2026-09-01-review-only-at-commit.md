# Review is one point, the commit gate

<!-- entry 240 -->

Review is now ONE point — the commit gate — not a pass after every non-trivial edit, and the
end-of-turn user-run `/fix-code` nudge is gone with it. A per-turn pass only ever saw its own
turn's edits, judged from inside the context that wrote them; nothing read the accumulated
diff as one change. Carve-out, load-bearing now that review waits: tests, build and each
subtree's verification workflow still run PER CHANGE. A fan-out's diff is flagged AT the gate.
