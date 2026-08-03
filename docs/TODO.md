# Model building
- [x] Right align buttons

# Motion tuning
- [x] Scorecards, The compare button should be right alligned, CHECK other buttons for right alignments
- [ ] Run lightning button not disabled while its starting -> confusing. Same with YOLO?

# Annotation
- [x] I would like to see labeled events 
- [ ] Flagged should not show all frames

# Identification
- [ ] Use cat SIZE as a second discriminator, to separate a resident from its bigger foreign
      lookalike (Sultan vs Store Sultan). Every crop is resized to 224x224 before DINOv2, so
      absolute size AND bbox aspect are discarded — this is a missing mechanism, not a data gap:
      more crops of Store Sultan cannot add a cue the resize deletes.
      Measured over the 354 labelled visits, per-visit PEAK bbox area alone separates
      Sultan/Store Sultan at AUC 0.84 and Jhinie/Store Jihn at 0.78, every "Store" cat bigger —
      orthogonal to appearance's 0.878, which is what makes it worth combining.
      Only ~0.84 because a top-down axis-aligned bbox conflates body size with pose extension and
      heading; a cleaner estimate wants an oriented box's minor axis or a segmentation mask.
      HOW to combine is unsettled (per-cat size prior / re-rank the NN on appearance+size /
      tie-break only for lookalike pairs / size as an extra vector dim) — needs its own spec.
      Do this AFTER the visit-held-out probe exists, or there is no way to tell whether it helped.

# General
- [x] Drop bird for YOLO
- [x] We still have a folder called admin-next, what did we flip?'
- [ ] Activity page in admin-new, its limited to x amount of visits, right? Could we have a selector on which day to start
