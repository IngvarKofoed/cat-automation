# Analyse watch reads the job's own history record

<!-- entry 288 -->

The per-visit analyse watch reads its job's OWN history record, not `status.error`:
that is one sticky field a later promotion clears and a later failure overwrites, so
it can report another job's failure as this one's. Leaving the queue is not succeeding
— a failed or canceled job now says so instead of reporting a result never produced.
