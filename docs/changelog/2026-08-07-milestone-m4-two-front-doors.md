# Milestone M4 — Two front doors

<!-- milestone M4: entries 80–113 -->

**M4 — Two front doors** (80–113). `/admin` and `/` are separate single-file SPAs sharing **no CSS or
JS** — only `/api/*` and `/media/*` — so a helper must be duplicated per file or the two disagree. The
user app is an installable home-screen app with an SSE-pushed Activity feed, a Cats roster with
uploadable version-stamped avatars, event `subject` classification (cat / person / unrecognized /
motion_only) beside identity, and per-frame YOLO box overlays in playback. Entries 102–105 removed the
first collector-starvation class: whole-store scans on the shared write connection.
