# update_dataset_geometry returns matched ids, as an UPDATE

<!-- entry 430 -->

That store method returns the ids it MATCHED, never a count: the tool deletes a crop's
old file only for rows that actually moved, and a row a concurrent relabel replaced
mid-run no longer owns that path. It is an UPDATE rather than the public
delete-then-re-insert, which would drop the label for the gap, reset `labeled_ts`
(entry 323 relies on it) and lose `source` — on the one artifact that must not be lost.
