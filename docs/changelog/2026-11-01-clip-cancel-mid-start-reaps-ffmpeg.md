# Cancel mid-start no longer orphans ffmpeg

<!-- entry 547 -->

Review repair: a request cancelled mid-start (a server shutdown) no longer orphans ffmpeg. The
await raised while the worker thread was inside `Popen`, so `finally` saw no process, deleted
its workdir and freed the lock with it running. The start is now its own task the route waits
out and reaps; an anyio shield does NOT hold against a native `task.cancel()` (measured).
That `finally` cleanup now runs off the event loop, since its kill waits up to 5 s.
