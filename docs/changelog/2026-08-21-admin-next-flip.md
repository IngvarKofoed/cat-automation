# /admin now serves the admin-next rebuild

<!-- entry 187 -->

The flip (NEW_ADMIN_PLAN.md P8): `/admin` now serves the admin-next rebuild, and the
workbench it replaced answers `/admin-old`. Kept, not deleted — buckets and corruption
review have no admin-next equivalent, so it is a working fallback, not only a rollback.
`/admin-next` stays an alias (same page) so a bookmark still resolves; the dev proxy
mirrors the mapping. Directories keep their build-time names: renaming `admin-next/`
would churn every spec reference, and `admin/` is deleted outright when it is retired.
