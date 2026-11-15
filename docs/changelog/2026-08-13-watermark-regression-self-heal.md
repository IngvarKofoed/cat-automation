# Workers self-heal from a frame-id regression

<!-- entry 143 -->

Both always-on workers now self-heal from a frame-id REGRESSION, which previously stranded them silently.
`frames.id` is INTEGER PRIMARY KEY with NO AUTOINCREMENT, so SQLite REUSES rowids once the max row is deleted —
and the non-motion purge deletes THROUGH the current max id, almost always a non-motion frame at ~5 fps. A
watermark left above the horizon made every later tick a no-op: hours of zero oracle coverage, and (user-visible)
the Activity feed silently stopping naming. A tick now clamps `watermark > latest_id()` down and logs it.
