# Two scorecard fixes measured and rejected

<!-- entry 395 -->

Two fixes measured and REJECTED, so they are not re-attempted. De-indexing the
analyzer term with `+` (the entry-229 remedy) is 6.7x scoped but degrades unscoped to
a full `SCAN o` — worse than today. And dropping the 11-arm aggregate in favour of
`interesting` + a count buys nothing: a bare `COUNT` costs the same as the full
aggregate (the SUM arms are free; the join walk is the cost), and an index-only
`analyzed` count off `analysis` alone measured 3.0-4.6 s against the aggregate's 2.0 s.
