# Filmstrip separates a detection miss from an unswept frame

<!-- entry 113 -->

Playback filmstrip now separates YOLO "ran but found nothing" from "never swept": a grey bar
(`band-none`, `analyzed=true` + no box) marks a real detection MISS, while an unswept frame
(`analyzed=false`) still shows NO bar. A bar now means "YOLO ran here" — colour = confidence,
grey = ran-but-empty. Previously both cases were blank, hiding a genuine miss among the (majority)
not-measured non-motion frames. Frontend-only (entry 111's payload already carried `analyzed`);
user + admin. Bars gained a confidence / "no detection" title tooltip.
