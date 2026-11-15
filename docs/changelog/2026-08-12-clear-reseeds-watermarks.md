# Clear re-seeds both workers' watermarks

<!-- entry 141 -->

`/api/clear` re-seeds both workers' frame watermarks to the post-wipe horizon. `clear()` keeps the settings KV while
frame rowids restart at 1, so a pre-wipe watermark sat AHEAD of every new frame and the worker looked enabled while
covering nothing. The idempotent resume queries can't save this — they are only consulted INSIDE the watermark's window.
