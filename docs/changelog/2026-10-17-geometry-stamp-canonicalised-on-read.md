# Stored geometry stamp is canonicalised; unreadable falls back to legacy

<!-- entry 469 -->

The read path CANONICALISES the stored stamp, and an unreadable value falls back to
legacy while reporting `readable:false`. A build compares its target with
`geometry = ?`, so stamping `m10.0` where a build asks `m10` makes every new crop
invisible to it — reported as zero crops, never as an error. Labelling deliberately
survives a bad value: a session is the operator's attention, a legacy crop is re-cuttable.
