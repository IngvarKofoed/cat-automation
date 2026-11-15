# labeled_crops active_only filter is NULL-safe

<!-- entry 192 -->

`labeled_crops(active_only=True)` is NULL-safe (`d.cat_id IS NULL OR c.active = 1`).
The join is a LEFT JOIN, so a catless kind (`unknown_cat`) has a NULL `c.active`; a bare
`c.active = 1` would silently drop every catless crop, which is not a retired cat's crop.
Default stays `False`, so the two opt-in callers are explicit and greppable.
