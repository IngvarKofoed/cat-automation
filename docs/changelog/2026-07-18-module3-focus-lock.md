# Module 3 lens focus is lockable, null meaning continuous autofocus

<!-- entry 14 -->

Edge now controls Module-3 lens focus, fixing blurry close-ups (the lens sat near-infinity
by default). New `focus` config: `null` = continuous autofocus, a number = manual dioptres
LOCKED there — a fixed door scene beats hunting AF. Capability-gated (`focus_capabilities()`),
so the UI focus slider shows only on a focus-capable camera. New endpoints
`GET /api/capabilities` + `POST /api/focus/autofocus` (locks & persists the AF result).
