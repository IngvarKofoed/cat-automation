# Visit span validation and refusal reasons are shared and table-driven

<!-- entry 486 -->

`_validate_visit_span` now holds the required-both-bounds + 10k-id width cap shared by
`/api/identify/visit` and the new route, with a caller-supplied hint. Refusal reasons
are a module-level table keyed by `visit_label_state`'s own `reason`, so a rung added
there without wording fails loudly instead of reaching a phone as a bare 409.
