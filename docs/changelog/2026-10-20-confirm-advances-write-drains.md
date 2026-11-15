# Confirm advances at once while writes drain on a serial chain

<!-- entry 484 -->

The tap ADVANCES to the next older visit immediately and the write drains on a serial
chain behind it — `_commit_label` cuts one JPEG per frame (~1-2 s per visit, entry
297), and a spinner per tap is what would make a 40-visit pass not worth doing. A
failed write therefore NAMES its own visit on a non-visit-scoped pill; the ⚑ does not
advance, being a reversible toggle whose success is deliberately silent (entry 286).
