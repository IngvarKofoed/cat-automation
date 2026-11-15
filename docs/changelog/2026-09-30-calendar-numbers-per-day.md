# Calendar numbers are properties of the day, not the window

<!-- entry 384 -->

Every calendar number is now a property of the DAY, not of the requested window —
which is what makes a day memoizable at all. The event pass therefore looks
`_VISIT_GAP_MS` back past local midnight for a cluster's predecessor instead of
treating the window edge as a cluster start. A day the window only CLIPS is not
memoized, since its numbers would then describe the request.
