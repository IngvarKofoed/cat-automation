# Alarm WAVs ship re-encoded as 16-bit PCM

<!-- entry 530 -->

The alarm WAVs ship under `edge/actuators/sounds/` re-encoded to 16-bit PCM 44.1 kHz
stereo: the originals were 32-bit float and 24-bit, which a HAT's hardware device can
reject outright. No clipping (peaks 0.95 / 0.94). The originals in the repo root are left
for the user to delete. A clip `wave` cannot read is left OFF the list, never listed bare.
