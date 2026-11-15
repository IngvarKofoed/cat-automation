# Older-build notice is re-asserted after toggles clear the footer

<!-- entry 516 -->

Review repair: the older-build notice is re-asserted after `toggleFlag` and
`analyseVisit` clear the shared footer line. A plain success prints nothing over it, so
the one visible statement of why the checkbox is dead vanished on the first tap and
stayed gone for the rest of the visit — on the device with no hover to reach a `title`.
