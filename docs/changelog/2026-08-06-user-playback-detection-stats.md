# User playback shows per-visit detection aggregates

<!-- entry 108 -->

User dashboard playback modal shows the per-visit YOLO detection aggregates below the filmstrip
— rate / peak / mean, one per line, values right-aligned, all as % (same `event["detection"]` and
`—`=not-measured distinction as the admin cards, entry 106), refreshed on open + Prev/Next hop.
Also fixed the scrub slider overflowing on a narrow phone (`flex:1` lacked `min-width:0`, so it stayed
wide and shoved the frame count off the dialog edge). Presentation only.
