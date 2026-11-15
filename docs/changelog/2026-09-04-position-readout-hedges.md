# Visit-position readout never asserts unsupported absolutes

<!-- entry 254 -->

The visit-position readout never asserts an absolute it can't support, and there are TWO ways
it couldn't. It carries `/api/events`' `truncated` flag (the user page dropped it) → `40+` and
"oldest loaded"; and it hedges to "newest/oldest SHOWN" whenever a toggle is hiding events —
the common case, since `showAll` defaults off and nav walks the FILTERED list, so a bare
"newest" is a lie on the default view. "shown" subsumes a capped feed, so it wins over "loaded".
