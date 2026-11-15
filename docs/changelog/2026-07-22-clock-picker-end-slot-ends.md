# Clock-picker End dropdown shows slot ends

<!-- entry 37 -->

Clock-picker End dropdown shows slot ENDS (03:00 … 24:00), not slot starts. Previously the end
bound was inclusive-through-the-3 h-slot but the dropdown still displayed the slot start, so
picking "21:00" misleadingly meant "through 24:00". The End value now IS the end instant (no
hidden +step at load); Start still shows slot starts (00:00 … 21:00).
