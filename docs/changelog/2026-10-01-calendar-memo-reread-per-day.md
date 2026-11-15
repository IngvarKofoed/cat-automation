# Calendar memo is re-read per day inside the loop

<!-- entry 388 -->

The memo is re-read PER DAY inside the loop, not snapshotted once before it. A day's
compute takes seconds, so an invalidation landing during it must still be seen for
the days not yet reached; otherwise the same call serves numbers that mutation just
falsified. A day already read stays as read — that is a read's point-in-time
staleness, not a cache fault, and no memo design removes it.
