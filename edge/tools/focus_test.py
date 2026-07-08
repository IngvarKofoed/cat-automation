#!/usr/bin/env python3
"""Standalone Pi Camera Module 3 focus diagnostic — runs WITHOUT the edge stack.

Isolates one question the config UI can't answer: does the lens actually move?
The edge's focus path (`edge/capture/picamera_source.py`) applies focus
best-effort and swallows errors, so a failed `set_controls` looks identical to a
lens that won't physically move. This script talks straight to Picamera2 /
libcamera — no edge code — so a difference (or lack of it) points cleanly at
either the hardware or the edge code.

IMPORTANT: only one process can hold the CSI camera at a time, so **stop the
edge server first** (e.g. Ctrl-C the `./edge.sh` process) before running this.

Usage (on the Pi):
    python3 edge/tools/focus_test.py            # camera 0, lens 0.0 vs 10.0
    python3 edge/tools/focus_test.py --camera 1 --near 20

It writes focus_0.jpg / focus_near.jpg / focus_af.jpg to the current directory,
and prints the LensPosition control range and the lens position libcamera
reports after each set. Read the results:

  * focus_0.jpg vs focus_near.jpg clearly DIFFER  -> lens works; the bug is in
    the edge focus code (a swallowed set_controls), not the hardware.
  * They look IDENTICAL but the "meta lens" prints track the requested values
    -> libcamera accepts the position but the lens motor isn't moving -> a
    hardware issue (reseat the camera ribbon; the Module 3 lens is a tiny voice
    coil).
  * The "meta lens" values DON'T change, or this script raises -> a
    libcamera/driver problem, and the traceback is the error the edge code was
    hiding.
"""
from __future__ import annotations

import argparse
import time


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--camera", type=int, default=0, help="CSI camera index (default 0)")
    parser.add_argument(
        "--near",
        type=float,
        default=10.0,
        help="near lens position in dioptres to compare against 0/infinity (default 10.0)",
    )
    parser.add_argument(
        "--settle",
        type=float,
        default=2.0,
        help="seconds to wait for the lens to settle after each set (default 2.0)",
    )
    args = parser.parse_args()

    # Imported here (not at module top) so the file is at least importable off a
    # Pi; these are Pi-only, apt-installed packages.
    from libcamera import controls
    from picamera2 import Picamera2

    picam = Picamera2(args.camera)
    # A light preview configuration (not the edge's full-res still config): fast,
    # and manual LensPosition applies the same way on any configuration.
    picam.configure(picam.create_preview_configuration(main={"size": (1280, 720)}))
    picam.start()
    time.sleep(1.0)

    lens_ctrl = picam.camera_controls.get("LensPosition")
    print(f"LensPosition control (min, max, default): {lens_ctrl}")
    if lens_ctrl is None:
        print("This camera reports NO LensPosition control — it is fixed-focus "
              "(e.g. Module 1/2), not a Module 3. Nothing to focus.")
        picam.stop()
        return 1
    print(f"AfMode present: {'AfMode' in picam.camera_controls}")

    # Manual at infinity (0 dioptres).
    picam.set_controls({"AfMode": controls.AfModeEnum.Manual, "LensPosition": 0.0})
    time.sleep(args.settle)
    print(f"meta lens @0    : {picam.capture_metadata().get('LensPosition')}")
    picam.capture_file("focus_0.jpg")

    # Manual at a near position.
    picam.set_controls({"LensPosition": args.near})
    time.sleep(args.settle)
    print(f"meta lens @{args.near:<4}: {picam.capture_metadata().get('LensPosition')}")
    picam.capture_file("focus_near.jpg")

    # One autofocus cycle.
    picam.set_controls({"AfMode": controls.AfModeEnum.Auto})
    converged = picam.autofocus_cycle()
    print(f"autofocus_cycle converged: {converged}")
    print(f"meta lens @AF   : {picam.capture_metadata().get('LensPosition')}")
    picam.capture_file("focus_af.jpg")

    picam.stop()
    print("wrote focus_0.jpg / focus_near.jpg / focus_af.jpg — compare focus_0 vs focus_near")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
