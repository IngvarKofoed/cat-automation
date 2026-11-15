# The old admin workbench is deleted

<!-- entry 221 -->

The old workbench is DELETED, completing NEW_ADMIN_PLAN P8: no `/admin-old` route, no
`web/admin/`, and no "old admin ↗" link in the appbar. `/admin` (+ the `/admin-next`
alias) is the only console; there is no rollback target left.
Its two unported pages went with it BY DECISION, not oversight: buckets (superseded by
the tuning calendar, entry 135) and corruption review. Their endpoints survive
(`/api/corruption*`, `/api/groups`, `/api/timeline`, `/api/visits`), so a corruption
sweep is still curl-able — but it has no UI, which matters if the NoIR module carries the
IMX708 artifacts over. Rebuild the page from docs/specs/2026-07-23-corruption-review-page.md
if it does.
