# User app can download a visit as an H.264 MP4

<!-- entry 542 -->

The user app's player can DOWNLOAD a visit as an H.264 MP4: ⬇ beside the ×, a two-tap icon
(fetch, then an `<a download>`, so the save is a fresh gesture iOS accepts). `GET /api/clip`
encodes every stored frame at real speed with `imageio-ffmpeg`, sends it, deletes it — never
stored. Rollout: an existing compute PC needs one manual `pip install -r compute/requirements.txt`.
Spec: docs/specs/2026-11-02-user-visit-clip-download.md.
