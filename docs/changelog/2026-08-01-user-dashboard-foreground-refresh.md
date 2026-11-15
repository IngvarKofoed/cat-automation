# User dashboard refreshes on foreground for iOS home-screen apps

<!-- entry 86 -->

User dashboard now refreshes its data on foreground, fixing the stale feed on a pinned/home-screen
iOS web app — iOS resumes the frozen WebView from memory rather than reloading, so the feed never
updated. `visibilitychange`→visible and `pageshow(persisted)` re-run the active view's loader (data
only, no shell reload — keeps scroll/route), plus a 60s visible-only poll of the Activity feed.
Guarded: never while playback is open (won't yank frames) or scrolled down (a rebuild jumps to top).
