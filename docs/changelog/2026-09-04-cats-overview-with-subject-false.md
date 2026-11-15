# cats_overview skips the subject read

<!-- entry 257 -->

`cats_overview` takes a new `with_subject=False` path: it reads only `identity`, and the
subject/detection annotation it never looked at was the call's most expensive part
(964 ms → 17 ms). The reuse-the-feed invariant is intact — identity still comes from the
same code Activity renders, so the two views cannot name a moment differently.
