# Clear keeps old labels, mixing sensors on a NoIR swap

<!-- entry 184 -->

Recorded a decision the NoIR swap forces: `cats`/`dataset_items` survive `clear()` by
design, so wiping frames to "start fresh" LEAVES the old labels — and a later
gallery-build silently mixes crops from two different sensors, whose daylight colour
differs. Worse than ordinary bad crops, since the shift correlates with capture date
rather than with the cat. Keep them for threshold tuning only, or retire them — but
decide before the first build, when the mix is still visible. In docs/NOIR_SWAP.md.
