# activity_signal gains alarm_rev so SSE nudges on alarms

<!-- entry 553 -->

`activity_signal` gained `alarm_rev` (`COUNT(*) + COUNT(end_ts)` over `mode='on'` rows) and the
SSE key tuple carries it — a field only in the dict nudges nothing; a test fails without it.
Moves twice per alarm, unlike entry 285's refused verdict counter. Known limit: the nudged
reload is skipped with the player open or the feed scrolled down, so "Sounding now" can linger.
