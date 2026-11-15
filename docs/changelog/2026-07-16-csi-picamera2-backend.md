# CSI cameras are captured through a Picamera2 backend

<!-- entry 6 -->

CSI camera support: a Picamera2 backend drives the Pi Camera Module, which
OpenCV's V4L2 path cannot capture from on current Pi OS (libcamera). Backend is
chosen from the opaque device id — `csi[:N]` → Picamera2, else OpenCV — and
`/api/cameras` lists detected CSI cameras. Picamera2 is apt-only, so the Pi venv
needs `--system-site-packages` (`EDGE_VENV_SYSTEM_SITE_PACKAGES=1 ./edge.sh`).
