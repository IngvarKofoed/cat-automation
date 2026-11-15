# Analysing state is per visit, not global

<!-- entry 289 -->

"Analysing…" is per VISIT, not a global flag. The watch runs up to two minutes and
Prev/Next stays live throughout, so one flag painted the busy label onto whatever
visit the reader hopped to. Several analyses may now be in flight; the server queue is
serial and dedups a repeat of the running span.
