# Next two visits' crops and frames are prefetched

<!-- entry 217 -->

The next two visits' rep crop + frame now PREFETCH, so the advance paints from cache instead
of a blank stage waiting on a fresh server-side decode. `/api/label/crop` therefore answers
with a short `Cache-Control` (it carries no validator of its own, unlike `/media`, so the
browser would refetch and the prefetch would buy nothing). Deliberately not `immutable`:
`frames.id` is reused after a `clear()`, so a (frame_id, box) URL is only stable per session.
