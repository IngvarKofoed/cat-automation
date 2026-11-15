# Compute UI split into separate /admin and / pages sharing no CSS

<!-- entry 80 -->

Compute UI split into two independently-styled front doors: the workbench SPA moved to `/admin`,
a near-blank user page now serves `/`. Separate HTML files (own inline `<style>`) share NO CSS,
so the coming user dashboard styles free of the admin look; only `/api/*` + `/media/*` stay shared.
Admin moved verbatim (absolute API calls + hash routing → `/admin#activity`); user page has a
stopgap `/admin` link, legacy bookmarks not redirected, no auth. Spec: docs/specs/2026-07-22-admin-user-area-split.md.
