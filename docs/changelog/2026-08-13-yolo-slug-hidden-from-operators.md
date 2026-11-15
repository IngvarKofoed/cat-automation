# yolo-serial slug is hidden from operators but kept in storage

<!-- entry 147 -->

The internal analyzer slug `yolo-serial` is no longer shown to operators: `/admin`'s
tooltips, help text, empty states, and its oracle-picker label now say "YOLO". The slug
stays the registry/DB analyzer name (it is the value in `analysis.analyzer`, half of
`PRIMARY KEY (frame_id, analyzer)`), so this is presentation-only — renaming the
identifier would orphan every stored verdict without a migration. Deliberately not done.
