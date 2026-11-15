# Annotation queue is bounded and sorted worst-first

<!-- entry 125 -->

Annotation backend (admin-next P4): the queue is now bounded/paginated (no whole-store scan); an
`ignored` `dataset_items` label (no crop) drops an event from the queue reversibly via the existing
relabel/delete (no new table); per-cat day/night crop coverage; and, with an active model, the queue
sorts below-threshold matches worst-first by distance (never-identified events after).
