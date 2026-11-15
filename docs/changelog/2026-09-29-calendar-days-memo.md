# Motion-tuning calendar memoizes each day in calendar_days

<!-- entry 381 -->

The Motion-tuning calendar MEMOIZES each day in a new `calendar_days` table, so only
days that actually changed are re-earned. Counting a 4-week window exactly means
walking every frame and verdict index entry: measured 11.3 s on a 6M-frame /
16.2M-verdict replica, growing with the store, and paid on every page load.
Now ~0 ms warm, 9.7 s cold, ~0.7 s after a sweep of one day.
