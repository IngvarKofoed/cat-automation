# Replay baseline is the frozen shipped presence rule

<!-- entry 601 -->

The replay's baseline is the SHIPPED presence rule (1 s hold, 5 s presence, takeover net), frozen
with its own constants; "new" is `start_decision` over visits bridged across the whole range
(`bridge_since_ms=0`). Its walk starts a lookback early, so a visit under way at the first
midnight is reported whole, from its true start. Neither frontier fix can show in it.
