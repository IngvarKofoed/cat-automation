# Admin alarm log shows trigger and detect/identify split

<!-- entry 588 -->

Admin's alarm log shows the trigger under the verdict, and its pass readout splits the time
into `detect_ms` + `identify_ms` (never summing past `ms`) — enough to aim the follow-up
processing-delay spec at the half the ~1 s fixed cost goes to, not at its cause.
