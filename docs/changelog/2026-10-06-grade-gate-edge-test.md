# Grid test pins both grade gates at their exact edges

<!-- entry 417 -->

The grid test pins both gates at their exact edges, transcribed from the JavaScript it
replaces — verified to FAIL against three separate slips (a 0.7→0.75 threshold, `>=`→`>`,
`or`→`and`). Transcription was this change's only real risk: a silent slip would re-grade
every future crop, and nothing else would have noticed.
