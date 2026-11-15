# Flag clears only when the write recorded rows

<!-- entry 227 -->

A flagged decision runs on the annotation page's existing serial `writeChain` and
clears the flag ONLY when the write recorded ≥1 row. `/api/label/relabel` answers 200
with `inserted: 0` once a span's frames have evicted — the routine aging path, since
flags are never pruned — so deleting unconditionally would discard both the mark and
the decision silently. `d` dismisses the flag ALONE, leaving any label untouched.
