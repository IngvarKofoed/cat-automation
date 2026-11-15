# Pi plays one clip per call; compute sequences the loop

<!-- entry 522 -->

The Pi plays ONE clip per call (`POST /api/sound/play`) and the compute sequences the
loop, asking for the next clip once the last is due to end. Fail-safe by construction: a
dead compute leaves one clip (≤ 6.5 s) finishing, then silence. Codes the alarm acts on:
423 disabled on the Pi, 409 still playing (never queued — routine, not a failure).
Rejected: the Pi looping the playlist itself, which would need a lease + stop endpoint.
