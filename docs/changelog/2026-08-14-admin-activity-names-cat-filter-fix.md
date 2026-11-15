# Admin Activity names the identified cat and hides residents properly

<!-- entry 152 -->

admin-next Activity now NAMES the cat it identified, and "Hide our cats" actually hides them.
`events()`'s identity carries no `resolved` key (only the per-frame overlay does), so the
`id.resolved` test never matched: every visit fell through to its subject chip and the
resident filter was a silent no-op. A shared `identityKind` derives the kind from
`cat_id`/`is_resident` when `resolved` is absent, so both feeds agree.
