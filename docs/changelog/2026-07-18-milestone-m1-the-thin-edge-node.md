# Milestone M1 — The thin edge node

<!-- milestone M1: entries 1–15 -->

**M1 — The thin edge node** (entries 1–15). The Pi is a pure HTTP server: `/stream` writes *every*
frame continuously as MJPEG, `/frame` serves stills, and MOG2 motion is a separate *pulled* signal
(`GET /status`, `X-Motion`/`X-Area` part headers) that gates the compute's GPU cost, never frame
delivery. Frames are rotated + cropped to the door ROI on the Pi. Module-3 focus is lockable
(`null` = continuous AF, a number = manual dioptres). `shared/wire.py` owns the frame wire format so
the two tiers cannot drift; `edge.sh` bootstraps the venv (system-site-packages on Linux for apt's
picamera2) and bakes `git describe` into `CAT_EDGE_VERSION`.
