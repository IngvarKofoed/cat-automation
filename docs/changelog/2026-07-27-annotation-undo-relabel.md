# Annotation gains undo and re-label in Labelled mode

<!-- entry 58 -->

Annotation tool gains in-tool undo / re-label — a "Labelled" mode on `#annotate` (newest-labelled-first
review via new `Store.labeled_visits` + `GET /api/label/labeled`). Per visit: re-label with 1–9/u/x
(`POST /api/label/relabel`: delete rows+crop files, then re-commit) or send back to the queue with `d`
(`POST /api/label/delete` → `Store.delete_dataset_items`). A mislabel is now fixable without SQL, and both
paths delete the orphaned crop files so the durable set never drifts from the DB.
Also hardened: `POST /api/label` validates per-frame quality BEFORE any crop is written (a bad value
previously left orphan crop files); `*.pt` gitignored (ultralytics drops weights in the repo root).
