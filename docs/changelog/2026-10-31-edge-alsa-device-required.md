# Deployed edge must set CAT_EDGE_ALSA_DEVICE

<!-- entry 541 -->

A deployed edge MUST set `CAT_EDGE_ALSA_DEVICE`: Pi OS makes HDMI (card 0) ALSA's
default, and headless `aplay` there fails with "audio open error: Unknown error 524" —
every Play refused, the alarm silent. ARCHITECTURE had called the default "the HAT".
Set host-locally via a systemd drop-in naming the card by name
(`plughw:CARD=wm8960soundcard,DEV=0`), since card indices can shift between boots.
