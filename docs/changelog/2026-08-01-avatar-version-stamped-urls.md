# Avatar URLs are version-stamped for cache without staleness

<!-- entry 85 -->

Avatar URLs are version-stamped for caching without staleness: `/api/cats/overview` returns each
cat's `avatar_version` (the served avatar file's mtime, ms) and the UI stamps it on the URL
(`…/avatar?v=<mtime>`) on both the Cats and Activity views. An unchanged avatar keeps one cacheable
URL (big images stay cached); a re-uploaded one gets a fresh URL that auto-busts, so the new photo
shows everywhere at once — no `Cache-Control` change needed. `has_avatar` now derives from real file
existence (a crop row whose file is gone reads false); the per-session `avatarBust` hack is gone.
