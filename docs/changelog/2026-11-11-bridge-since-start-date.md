# Bridging applies only after a saved start date

<!-- entry 595 -->

Bridging applies only to gaps ending at or after `visit_bridge_since_ms`, which a LIVE API app
saves at its first launch of this build — never a CLI tool; a timestamp, so it survives a
Clear; absent bridges nothing. So no visit, count, label or flag from before changes.
Computed on read, never saved: timing can only make a bridge MISSING, never wrong.
