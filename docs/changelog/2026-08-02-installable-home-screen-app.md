# User dashboard is an installable home-screen app

<!-- entry 88 -->

User dashboard is now an installable home-screen app: `apple-mobile-web-app-capable` (+ modern
`mobile-web-app-capable`), a real `/apple-touch-icon.png` (served at the root paths iOS probes; a
door-mark PNG generated once with cv2), an app title, and per-theme `theme-color`. Status bar is
`default`, not black-translucent — the latter forces white text, unreadable over the light interior.
Also: the SPA shells (`/`, `/admin`) now send `Cache-Control: no-cache`, so a redeployed single-file
shell is picked up on the next launch (revalidates against FileResponse's ETag; unchanged → 304).
