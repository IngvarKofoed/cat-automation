# Two admin control bars are one line tall

<!-- entry 215 -->

Two admin control bars are now ONE line tall, with their readout on the controls' own band.
The Lighting cutoff field takes the `.fld.inline` caption (beside the input, not stacked
above it), so "Night below" no longer reads as a row of its own above Save/Clear.
Frame review's `store: … → …` note joins Load in a bottom-aligned group so it centres on the
button: that row IS two lines tall (the Start/Timespan captions stack), and the row's
`align-items:center` centred the note against the full height, floating it above the inputs.
