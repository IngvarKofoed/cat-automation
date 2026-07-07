"""Pure, fail-safe per-frame transforms: rotate then crop a BGR ndarray.

Both functions never raise on bad/None/malformed input — they fall back to the
identity (rotate) or the full frame (crop), so a hand-edited settings.json
degrades to full-frame/0° rather than erroring at render time.
"""

import cv2

# Clockwise degrees → cv2 rotate code. 90/270 swap width and height; 180 keeps
# the frame size. Anything not in this map (0 or an unrecognized value) is a
# no-op.
_ROTATIONS = {
    90: cv2.ROTATE_90_CLOCKWISE,
    180: cv2.ROTATE_180,
    270: cv2.ROTATE_90_COUNTERCLOCKWISE,
}


def rotate(frame, degrees):
    """Rotate `frame` clockwise by `degrees` (0/90/180/270); else return it unchanged."""
    code = _ROTATIONS.get(degrees)
    if code is None:
        return frame
    try:
        return cv2.rotate(frame, code)
    except cv2.error:
        return frame


def crop(frame, clip):
    """Crop `frame` to the normalized rect `clip` {x,y,w,h} in 0..1; else full frame.

    `clip` is defined against the (already-rotated) frame passed in. Returns the
    full frame unchanged when `clip` is None, not a dict of numbers, or resolves
    to an empty region.
    """
    if not isinstance(clip, dict):
        return frame
    try:
        x, y, w, h = clip["x"], clip["y"], clip["w"], clip["h"]
        height, width = frame.shape[:2]
        x0 = max(0, min(width, round(x * width)))
        y0 = max(0, min(height, round(y * height)))
        x1 = max(0, min(width, round((x + w) * width)))
        y1 = max(0, min(height, round((y + h) * height)))
    except (KeyError, TypeError, ValueError, AttributeError):
        return frame
    if x1 <= x0 or y1 <= y0:
        return frame
    return frame[y0:y1, x0:x1]
