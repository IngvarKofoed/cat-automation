# Activity playback opens in a full-page modal on double-click

<!-- entry 47 -->

Activity playback now opens in an almost-full-page modal, replacing the in-view panel that
read as cramped. **Double-click** an event to open (single click is a no-op, so scanning the
grid never launches it); Esc, the × button, or a backdrop click closes it. Opening locks
body scroll and moves focus into the dialog; leaving the route fully closes it. Same player
logic (same element ids) — only the container changed.
