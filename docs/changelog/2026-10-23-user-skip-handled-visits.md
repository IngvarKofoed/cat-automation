# Player nav can skip visits already labelled or flagged

<!-- entry 502 -->

The user app's playback nav can now SKIP visits already dealt with: "Skip done" in the
modal footer makes Older/Newer (and the swipe) land only on visits carrying neither a
label nor a ⚑. A NAVIGATION filter, not a list filter — the rail still shows every
visit, so `12 / 40` keeps its meaning and a handled visit stays one tap away.
Off by default, in-memory. Spec: docs/specs/2026-08-17-user-skip-handled-visits.md.
