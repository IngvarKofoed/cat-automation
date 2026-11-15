# Live worker seeds its watermark and caps spans per tick

<!-- entry 75 -->

Hardened the live-identify worker (review). First enable seeds the watermark to the frame
horizon: it names only NEW visits, never back-identifies the whole store (history = manual pass).
Each tick re-checks stop/`is_busy` and caps spans (`_MAX_SPANS_PER_TICK`), so a manual job or a
backlog can't be starved or monopolize the GPU; a stop mid-detect/identify no longer advances the
watermark (idempotent resume finishes the span). Resident gallery + idempotent
`YoloAnalyzer.prepare()` end the last per-visit model/gallery reloads.
