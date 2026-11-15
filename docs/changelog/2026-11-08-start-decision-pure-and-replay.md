# start_decision is a pure function shared with the replay tool

<!-- entry 581 -->

`deterrent.start_decision` is the WHOLE start rule as a pure function; the worker and the
new read-only `compute.tools.alarm_replay` both call it. A missed stranger writes no
`actions` row, so the replay is the only way to see a miss. It refuses (non-zero exit) on a
truncated feed walk, or a range with no visits or no counted frames under the active model,
rather than print an empty "no added misses".
