# Corrupt settings.json falls back to defaults instead of crashing boot

<!-- entry 4 -->

Edge hardened after code review. A hand-edited or corrupt `settings.json` can no longer
crash boot: invalid or non-object values fall back to defaults, and `POST /api/config`
rejects a non-object body with 400 (was 500). Device switch now persists *before* the
in-memory swap and outside the slot lock, so a failed write can't leak the old capture
handle or diverge live-vs-saved state. Config UI always shows the active device even when
enumeration omits it (default index 0 vs Linux `/dev/video*`).
