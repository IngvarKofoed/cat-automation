# Config POST returns 422 when the candidate device fails to open

<!-- entry 3 -->

`POST /api/config` now returns 422 (not 503) when the candidate device fails to open.
503 stays reserved for `/frame`'s already-working camera failing at read time; a rejected
device selection is a client-input problem, not a service outage — per the MVP spec.
