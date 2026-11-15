# Verify the browser serves the page on disk before trusting symptoms

<!-- entry 454 -->

Chasing a phantom stale census cost most of that browser pass: the browser was serving a
CACHED `/admin` from before the edit, so every check ran against the old JS. Verify the
page under test is the page on disk (`innerHTML.includes('<a new symbol>')`) before
believing any UI symptom — a cache-busting query param is the cheap fix.
