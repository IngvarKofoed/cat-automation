# Activity feed drops its date picker for recent visits

<!-- entry 82 -->

Activity feed dropped its from/to date picker — it now just shows the recent visits, newest first
(`/api/events` unbounded, capped server-side). The user view is a glance at recent door activity,
not a searchable log (date-scoped browsing lives in `/admin`). The non-residents-only filter stays.
