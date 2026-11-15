# A flag is a work item deleted on resolve

<!-- entry 223 -->

A flag is a work item, not output: resolving one deletes its row (no history), and
`clear()` drops the table since frame ids restart at 1. Eviction does NOT cascade —
a flag whose frames aged out reads `gone` and waits to be dismissed, because a mark
that silently disappears hides that the work was lost to retention.
