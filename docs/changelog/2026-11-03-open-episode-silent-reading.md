# Open episode with nothing played reads silent

<!-- entry 554 -->

An OPEN episode with nothing played reads silent once the Pi refuses or playback fails — it
retries every tick, so it must not read as sounding. Sounding and sounded share one glyph;
only the words (title, player line, spoken label) tell them apart. A missing `alarms` key
(older backend) reads as none, with no notice: it only drops marks.
