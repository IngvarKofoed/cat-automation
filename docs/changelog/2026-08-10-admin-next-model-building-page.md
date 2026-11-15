# admin-next Model building page builds, validates and promotes

<!-- entry 132 -->

admin-next Model building page (Wave 2 P5 frontend): build gallery → validate (DINOv2 feasibility probe)
→ promote. Quality checkboxes (gallery-only default) feed `/api/training/gallery/build` +
`/api/training/feasibility/run`; a shared training-job line polls status; the validation-run list shows
kNN/AUC/threshold with a report link; the version list promotes/rolls-back, with a night-coverage warning
(a resident with day crops but zero night crops) confirmed before promote. Not-enough-data + missing-torch handled.
