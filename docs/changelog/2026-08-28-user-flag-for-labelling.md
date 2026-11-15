# Household can mark a visit for labelling from the user app

<!-- entry 222 -->

The household can now MARK a visit for labelling from the user app's playback modal
(⚑ on the card); admin `#annotate` gained a Flagged mode to work the marked set.
New `label_flags` table keyed by the EVENT's frame span — span-keyed is what makes a
`motion_only`/`unrecognized` visit flaggable at all, since with no YOLO box it can
never enter the annotation queue. Spec: docs/specs/2026-07-30-user-flag-for-labelling.md.
