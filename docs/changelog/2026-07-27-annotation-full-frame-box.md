# Annotation stage shows the full frame with box beside the crop

<!-- entry 62 -->

Annotation tool (`#annotate`) rep stage now shows the **full frame with the detection box
overlaid** beside the tight crop, in both Queue and Labelled modes — top-down scene context and
cat scale (a resident-vs-foreign cue) alongside the dorsal-coat detail. Overlay is client-only:
the box is a percentage of the frame's natural dimensions (bbox is in stored-JPEG pixel space),
and the wrapper is pinned to the image's aspect ratio so it can't letterbox and mis-place the box.
