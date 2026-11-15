# Superseded page repaints the foot unconditionally

<!-- entry 268 -->

A superseded page repaints the foot/button UNCONDITIONALLY, on both dashboards. The
winning render ran while the in-flight flag was still true, so it painted "Loading
older…" — and with only the winner repainting, that lie stayed until an unrelated
re-render. The flag itself was already cleared unconditionally; the paint was not.
