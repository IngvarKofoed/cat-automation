# Decode workers and lookahead are env knobs

<!-- entry 398 -->

Workers/lookahead are env knobs (`CAT_WINDOWED_DECODE_WORKERS`, default 2;
`CAT_WINDOWED_DECODE_LOOKAHEAD`, 8) because more is NOT monotonically better — 2 beat
4 and 6 on the dev box, decode being memory-bandwidth bound. The lookahead is what
bounds the memory: a 2304x1296 BGR frame is ~9 MB, so 8 in flight is ~72 MB.
