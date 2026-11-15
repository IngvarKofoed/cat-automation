# New tool plots a night's luma and colourfulness

<!-- entry 198 -->

New `compute/tools/ir_lamp_timeline.py` plots a night's mean luma + colourfulness with sun
events, and spectrally tests the luma series for a RIPPLE: thermal shutdown cycles over
minutes, foldback settles flat, and in a dark scene the two look identical by eye.
Periods are capped to those the window holds 4+ cycles of — else leftover trend reads as a
confident ripple. Reads on its OWN connection, never the write-locked `Store` one.
