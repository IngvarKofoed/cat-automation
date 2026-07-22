"""Crop a stored frame's JPEG to a detection box — on the fly, or to a durable file.

Two stateless helpers backing the annotation tool's crop needs (see the
cat-identity annotation-tool spec):

- ``crop_bytes(jpeg_path, box)`` — decode the stored JPEG, crop to ``box``
  (clamped to the image bounds), re-encode, return JPEG bytes. Serves the UI's
  crop endpoint (rep crop + filmstrip) with no file written.
- ``materialize(jpeg_path, box, dest_abs, root=...)`` — the same crop, written to
  a durable file under the dataset root. Best-effort: returns ``True`` on success,
  ``False`` on any failure (unreadable frame, degenerate box, write error, or a
  ``dest_abs`` that escapes ``root``) so the caller can simply skip a frame whose
  crop couldn't be cut rather than record a ``dataset_items`` row pointing at a
  missing file.

Boxes are ``[x1, y1, x2, y2]`` in the STORED JPEG's own pixel space (that is what
the YOLO oracle wrote into ``analysis.detail``), so a crop of that JPEG is
coordinate-consistent with the box. ``cv2``/``numpy`` are imported lazily (see the
package docstring). Decode goes through ``cv2.imdecode`` over the file bytes —
matching ``compute.ingest.client`` — rather than ``cv2.imread``, which mishandles
non-ASCII paths on Windows (the compute tier's real host).

Re-encode quality is a deliberate q95: unlike the collector's verbatim frame
store (which must never re-encode the wire bytes), these crops are a small,
hand-curated training set where preserving detail beats saving bytes.
"""
from __future__ import annotations

import os

# JPEG quality for materialised/served crops — see the module docstring.
_JPEG_QUALITY = 95


def _clamp_box(box, width: int, height: int) -> "tuple[int, int, int, int]":
    """Normalise + clamp ``box`` to integer pixel bounds inside ``width``x``height``.

    Orders each pair (so a reversed x1/x2 or y1/y2 still yields a valid rect),
    rounds to int, and clips to ``[0, width]`` / ``[0, height]``. Raises
    ``ValueError`` when the box has fewer than four coordinates or collapses to
    zero area after clamping (a degenerate box the caller can't crop).
    """
    if box is None or len(box) < 4:
        raise ValueError(f"box must be [x1,y1,x2,y2], got {box!r}")
    x1, y1, x2, y2 = (float(box[0]), float(box[1]), float(box[2]), float(box[3]))
    lo_x, hi_x = sorted((x1, x2))
    lo_y, hi_y = sorted((y1, y2))
    x1 = max(0, min(int(round(lo_x)), width))
    x2 = max(0, min(int(round(hi_x)), width))
    y1 = max(0, min(int(round(lo_y)), height))
    y2 = max(0, min(int(round(hi_y)), height))
    if x2 <= x1 or y2 <= y1:
        raise ValueError(f"degenerate crop box after clamping to {width}x{height}: {box!r}")
    return x1, y1, x2, y2


def crop_bytes(jpeg_path: str, box) -> bytes:
    """Crop the JPEG at ``jpeg_path`` to ``box`` and return re-encoded JPEG bytes.

    ``box`` is ``[x1, y1, x2, y2]`` in the JPEG's pixel space, clamped to the
    image bounds (so a box slightly outside the frame still crops the visible
    part). Raises ``ValueError`` if the file can't be decoded or the box is
    degenerate after clamping; raises ``OSError`` if the file can't be read (the
    caller distinguishes "no such frame" upstream via ``store.path_for`` +
    ``os.path.isfile``).
    """
    import cv2
    import numpy as np

    with open(jpeg_path, "rb") as fh:
        raw = fh.read()
    img = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"failed to decode JPEG: {jpeg_path!r}")
    height, width = img.shape[:2]
    x1, y1, x2, y2 = _clamp_box(box, width, height)
    crop = img[y1:y2, x1:x2]
    ok, buf = cv2.imencode(".jpg", crop, [int(cv2.IMWRITE_JPEG_QUALITY), _JPEG_QUALITY])
    if not ok:
        raise ValueError("failed to encode crop JPEG")
    return buf.tobytes()


def materialize(jpeg_path: str, box, dest_abs: str, root: "str | None" = None) -> bool:
    """Crop ``jpeg_path`` to ``box`` and write it to ``dest_abs``; return success.

    Best-effort by contract: any failure (unreadable source, degenerate box,
    encode/write error, or a traversal-guard rejection) returns ``False`` rather
    than raising, so the label route can skip a frame whose crop couldn't be cut
    and never record a ``dataset_items`` row pointing at a missing file. Creates
    the destination's parent directory.

    ``root`` is the traversal guard: when given, ``dest_abs`` must resolve to a
    path inside ``root`` (after ``realpath``), so a crafted crop path can't escape
    the dataset tree; a ``dest_abs`` outside ``root`` returns ``False`` without
    writing. Pass the store's ``dataset_root`` here.
    """
    if root is not None and not _within(dest_abs, root):
        return False
    try:
        data = crop_bytes(jpeg_path, box)
        parent = os.path.dirname(dest_abs)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(dest_abs, "wb") as fh:
            fh.write(data)
        return True
    except (OSError, ValueError):
        return False


def normalize_avatar_bytes(data: bytes, max_dim: int = 512) -> "bytes | None":
    """Validate + downscale + re-encode an uploaded avatar image to JPEG bytes.

    Backs the user-dashboard's raw-body avatar upload (see the user-activity-cats
    spec): the household POSTs an arbitrary image as the request body and we store
    a small, uniform JPEG rather than the original. Decodes ``data`` with the same
    lazy-``cv2`` / ``cv2.imdecode`` path the rest of this module uses (robust to any
    input format OpenCV can read, and — unlike ``cv2.imread`` — never touches the
    filesystem). Returns ``None`` when ``data`` is empty or not a decodable image,
    so the caller maps an undecodable upload to a 400 rather than writing garbage.

    If the longest side exceeds ``max_dim`` the image is downscaled to fit
    (aspect preserved, ``cv2.INTER_AREA`` — the right filter for shrinking); a
    smaller image is left untouched. The result is re-encoded JPEG at the module's
    q95. Note the re-encode drops any EXIF orientation tag, so a caller feeding
    phone photos should normalise orientation before upload.
    """
    if not data:
        return None
    import cv2
    import numpy as np

    img = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None
    height, width = img.shape[:2]
    longest = max(height, width)
    if longest > max_dim:
        scale = max_dim / float(longest)
        new_size = (max(1, int(round(width * scale))), max(1, int(round(height * scale))))
        img = cv2.resize(img, new_size, interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), _JPEG_QUALITY])
    if not ok:
        return None
    return buf.tobytes()


def _within(dest_abs: str, root: str) -> bool:
    """Whether ``dest_abs`` resolves to a path at or under ``root`` (traversal guard).

    Both sides go through ``realpath`` so ``..`` segments and symlinks are
    resolved before the containment check — a ``dest_abs`` of
    ``<root>/../secret`` collapses out of ``root`` and is rejected.
    """
    root_real = os.path.realpath(root)
    dest_real = os.path.realpath(dest_abs)
    return dest_real == root_real or dest_real.startswith(root_real + os.sep)
