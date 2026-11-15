# Real user dashboard with Activity feed and Cats roster

<!-- entry 81 -->

Built the real user dashboard at `/` (warm "Threshold" SPA, own CSS, no admin sharing): an Activity
feed and a Cats roster. Activity is a day-grouped time-rail of door events reusing `/api/events`
(no backend change) with identity chips (resident/neighbour/unknown) and click-to-play.
Cats shows residents-first cards; each cat's "last seen" is DERIVED from the same `events()` feed
(new `Store.cats_overview`), so Cats and Activity can never name the same moment differently, and it
inherits the uncalibrated fail-safe (an uncalibrated gallery names no resident).
Per-cat avatars are uploadable — a file convention `<dataset_root>/avatars/cat_<id>.jpg` (no schema
column; survives eviction/clear), served with an auto labelled-crop fallback; upload is a raw-body
POST re-encoded via base-dep cv2 (no new dependency). "Who's home" is a deferred placeholder — needs
direction detection. Spec: docs/specs/2026-07-22-user-activity-cats.md.
