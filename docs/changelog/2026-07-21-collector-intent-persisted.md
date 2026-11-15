# Collector start/stop intent persists across restart

<!-- entry 31 -->

Collector intent persisted across restart (settings KV table in index.db): on-launch restore, one-click Resume.
Intent written on user-initiated start/stop only — never by shutdown hook (preserves changelog 28's safety property).
Collection survives mid-run restart; CAT_COLLECT_AUTOSTART=1 still forces immediate-start for unattended runs.
