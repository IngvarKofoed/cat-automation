# Dev proxy serves the local admin-next rebuild

<!-- entry 129 -->

Frontend dev proxy now serves the LOCAL admin-next rebuild at `/admin-next` (beside `/` and `/admin`),
so its pages iterate on the dev box against the compute PC's live backend. `/api/*` still proxies to
`CAT_COMPUTE_URL`, so the compute PC must already have the admin-next BACKEND endpoints (pull + restart)
for the pages to work.
