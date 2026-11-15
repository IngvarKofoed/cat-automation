# New label routes validate oracle against ANALYZER_NAMES

<!-- entry 492 -->

The new routes validate `oracle` against `ANALYZER_NAMES` as every sibling
oracle-taking route does. An unregistered name reached `_present_frames` as a
predicate matching no rows, so a typo read as an honest "no crop to label here"
rather than a bad request.
