# New admin cats page owns the roster

<!-- entry 188 -->

New admin `#cats` page owns the roster: rename, resident/foreign, retire, and a `notes`
field (the schema column existed and was never written). Edits go through the existing
`PATCH /api/cats/{id}`, which had no caller — a rename used to need SQL. Reads reuse
`/api/cats/overview` + `/api/label/regime-coverage`; no new endpoint.
Annotation keeps only the digit picker; add-cat and the day/night coverage card moved.
Spec: docs/specs/2026-07-28-cats-roster-page.md.
