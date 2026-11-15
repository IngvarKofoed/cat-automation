# An unreadable label probe now says the compute may be older

<!-- entry 489 -->

A probe that could not be READ now says so — "the compute PC may be on an older
build" — instead of leaving the footer blank, which was indistinguishable from "this
visit isn't confirmable". Found by hitting it: `frontend-dev.sh` serves the working
tree's HTML against the REAL compute PC's `/api`, so a new frontend over an
un-deployed backend 404s the probe and the whole feature read as missing, on a visit
that was fine. Entries 164/173/183/365's rule, in a new place.
