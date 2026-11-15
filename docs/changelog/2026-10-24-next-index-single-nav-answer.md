# One nextIndex answers button, swipe damping and hop

<!-- entry 507 -->

One `nextIndex(delta)` now answers for all three nav consumers — each button's
`disabled`, the swipe's rubber-band damping, and the hop itself — where each used to
re-derive `index ± 1` inline. The swipe reads it DURING `touchmove`, which is why the
labelled state had to travel with the feed rather than be probed per hop: an async
answer cannot drive a gesture already under the reader's thumb.
