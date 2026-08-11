"""DINOv2 embedding backbone for the feasibility probe — torch-gated, lazy-imported.

Mirrors ``compute/analysis/yolo.py``'s discipline: ``torch``/``torchvision``/``cv2``
are imported only inside ``ensure_available()``/``prepare()``/``embed_paths()``,
never at module scope, so importing this module (or the ``identification`` package)
stays free on the lean always-on collector. The heavy stack is touched only once an
embedding run actually starts, and only if the opt-in
``compute/requirements-analysis.txt`` extras are installed.

The default backbone is Meta's self-supervised **DINOv2 ViT-S/14** (``dinov2_vits14``),
loaded via ``torch.hub``. It needs NO training and NO labels — exactly what an "are
our cats even separable?" probe wants — and it has never seen a top-down cat-door
view, so it is an honest, unbiased baseline. First use downloads the hub repo +
weights (like YOLO's first run). Swap the backbone with ``CAT_EMBED_MODEL``.
"""
from __future__ import annotations

import logging
import math
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Callable

    import numpy as np

logger = logging.getLogger(__name__)

_ENV_MODEL = "CAT_EMBED_MODEL"
_ENV_IMGSZ = "CAT_EMBED_IMGSZ"
_DEFAULT_MODEL = "dinov2_vits14"
# DINOv2's patch size is 14, so the input side must be a multiple of 14; 224 = 16×14.
_DEFAULT_IMGSZ = 224
_IMAGENET_MEAN = (0.485, 0.456, 0.406)
_IMAGENET_STD = (0.229, 0.224, 0.225)
# The ImageNet mean carried back into 0–255 pixel space — the letterbox PAD colour.
# After ``(x/255 − mean)/std`` a pixel of exactly this colour is 0.0 in every channel,
# so the padding contributes nothing to the vector. Black (0,0,0) would instead land at
# ≈(−2.12, −2.04, −1.80) and inject a large constant into every embedding, weighted by
# how much of the square the pad covers — which varies with each box's aspect ratio, so
# it would be a per-crop bias rather than a shared offset the model could ignore.
_IMAGENET_MEAN_255 = tuple(v * 255.0 for v in _IMAGENET_MEAN)

# The one non-legacy resize token. Kept as a constant because the descriptor string is
# a persisted value (``dataset_items.geometry``) — a typo here would silently split one
# convention into two that never match each other.
_GEOMETRY_LETTERBOX = "letterbox"


def geometry_descriptor(letterbox: bool, margin: float) -> "str | None":
    """The stable string naming a crop convention — ``None`` for **legacy**.

    Legacy is squash-resize with no context margin: what every crop cut before this
    existed follows, and what an absent ``dataset_items.geometry`` stamp means. It is
    deliberately spelled ``None`` rather than ``"legacy"`` so a NULL column value and
    an omitting caller mean exactly the same thing without a translation step.

    Non-legacy renders as ``+``-joined tokens, resize first: ``"letterbox"``,
    ``"m10"``, ``"letterbox+m10"``. The margin token is the fraction as a PERCENT
    (``m10`` = 0.10) via ``%g``, so 0.125 reads ``m12.5`` and no value round-trips to a
    different string than it came from. A negative margin is rejected here rather than
    quietly shrinking the box — it would clip the cat, which is the opposite of what a
    context margin is for.
    """
    if margin < 0:
        raise ValueError(f"margin must be >= 0 (a margin EXPANDS the box), got {margin!r}")
    parts: "list[str]" = []
    if letterbox:
        parts.append(_GEOMETRY_LETTERBOX)
    if margin:
        parts.append(f"m{margin * 100:g}")
    return "+".join(parts) or None


def parse_geometry(descriptor: "str | None") -> "tuple[bool, float]":
    """Inverse of ``geometry_descriptor``: a stamp → ``(letterbox, margin)``.

    ``None`` / ``""`` is legacy → ``(False, 0.0)``. An unknown token raises
    ``ValueError`` rather than being skipped: a stamp this build cannot read names a
    convention it cannot reproduce, and embedding those pixels under a *guessed*
    geometry is the silent feature-space blend the stamp exists to prevent.
    """
    if descriptor is None or not str(descriptor).strip():
        return False, 0.0
    letterbox = False
    margin = 0.0
    for token in str(descriptor).strip().split("+"):
        token = token.strip()
        if token == _GEOMETRY_LETTERBOX:
            letterbox = True
        elif token.startswith("m") and len(token) > 1:
            try:
                percent = float(token[1:])
            except ValueError:
                raise ValueError(f"bad margin token {token!r} in geometry {descriptor!r}") from None
            # `< 0` does NOT reject inf or nan (both compare False), and `%g` renders them
            # back as `minf`/`mnan`, so a non-finite margin round-trips as a "valid" stamp.
            # It must die here, at the parser both the request path and the stored-value read
            # go through: with margin=inf `_clamp_box` raises OverflowError, which
            # `crops.materialize` does not catch — a 500 on EVERY label — and with nan it
            # returns False for every frame, so a label silently writes no rows at all.
            # Rejecting is also just this function's own contract: a margin it cannot
            # reproduce is a convention it cannot reproduce.
            if not math.isfinite(percent):
                raise ValueError(
                    f"non-finite margin token {token!r} in geometry {descriptor!r}"
                )
            if percent < 0:
                raise ValueError(f"negative margin token {token!r} in geometry {descriptor!r}")
            margin = percent / 100.0
        else:
            raise ValueError(f"unknown geometry token {token!r} in {descriptor!r}")
    return letterbox, margin


def canonical_geometry(value: "str | None") -> "str | None":
    """Canonical form of a STORED geometry stamp, for comparing two conventions.

    Falsy → ``None`` (legacy). A parseable stamp is re-rendered through
    ``geometry_descriptor``, so ``"m10"`` and ``"m10.0"`` compare equal and token order
    can't split one convention in two. An UNPARSEABLE stamp is returned **unchanged**
    rather than raising: it names a convention written by some other build, and the
    only safe reading is "not mine" — which excludes those crops from a build here
    instead of blending them into it.
    """
    if value is None or not str(value).strip():
        return None
    try:
        return geometry_descriptor(*parse_geometry(value))
    except ValueError:
        return str(value)


def _letterbox_square(img, size: int):
    """Aspect-preserving resize of ``img`` into a ``size``×``size`` float32 canvas.

    The alternative to ``cv2.resize(img, (size, size))``, which SQUASHES: real door
    boxes run from 191×46 to 538×298, so a squash distorts by up to ~4.8× and — worse —
    by a *different* factor per frame within one visit, which is variation the embedder
    has to spend capacity on. Here the crop keeps its shape and the leftover is padded.

    Padded with ``_IMAGENET_MEAN_255`` (see there): the canvas is float32 so the pad
    value is exact, while the resized CONTENT is assigned in from cv2's uint8 result, so
    the pixels that came from the image carry the same 8-bit quantisation the legacy
    path gives them. Filter follows direction — ``INTER_AREA`` shrinking (the legacy
    path's choice, and correct there since a detection crop is almost always larger than
    224), ``INTER_LINEAR`` enlarging, where ``INTER_AREA`` degenerates to a blurry
    nearest-neighbour.

    ``img`` is RGB at this point (``_embed_items`` converts before calling), which is the
    channel order ``_IMAGENET_MEAN_255`` is written in.
    """
    import cv2
    import numpy as np

    height, width = img.shape[:2]
    scale = min(size / float(width), size / float(height))
    new_w = max(1, min(size, int(round(width * scale))))
    new_h = max(1, min(size, int(round(height * scale))))
    interpolation = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
    resized = cv2.resize(img, (new_w, new_h), interpolation=interpolation)
    canvas = np.empty((size, size, 3), dtype=np.float32)
    canvas[:, :] = _IMAGENET_MEAN_255
    top = (size - new_h) // 2
    left = (size - new_w) // 2
    canvas[top:top + new_h, left:left + new_w] = resized
    return canvas


class EmbedCancelled(Exception):
    """Raised by ``Embedder.embed_paths`` when its ``progress`` callback asks it to
    stop — the cooperative-cancel signal a long embedding phase honors at the next
    batch boundary, so Cancel/Stop actually interrupts rather than no-op'ing."""


class Embedder:
    """Crop → feature-vector, via a pretrained DINOv2 ViT loaded on demand.

    Construction is cheap and dep-free (just reads config); the model and its heavy
    imports arrive in ``prepare()``. ``embed_paths`` returns raw (un-normalised)
    embeddings — the feasibility metrics L2-normalise themselves, so the vectors
    can be cached/reused for a future gallery without a baked-in normalisation.

    ``letterbox`` and ``margin`` are the crop GEOMETRY (see ``geometry_descriptor``),
    and both default to **legacy** — squash resize, no margin. That default is
    load-bearing, not a shrug: every already-promoted gallery was embedded this way, and
    flipping the default would silently mismatch each of those galleries against its own
    queries. It is the exact failure the ``backbone``/``imgsz`` stamps exist to prevent,
    which is why geometry is stamped the same way rather than assumed.
    """

    def __init__(
        self,
        model: "str | None" = None,
        imgsz: "int | None" = None,
        letterbox: bool = False,
        margin: float = 0.0,
    ) -> None:
        self.model_name = model or os.environ.get(_ENV_MODEL, _DEFAULT_MODEL)
        self._imgsz = int(imgsz if imgsz is not None else os.environ.get(_ENV_IMGSZ, _DEFAULT_IMGSZ))
        self._letterbox = bool(letterbox)
        self._margin = float(margin)
        # Validate at CONSTRUCTION, where the caller's stack is still on screen — a
        # negative margin shrinks the box, so it would silently clip the cat rather than
        # fail. ``geometry_descriptor`` owns the rule; calling it here also means the
        # descriptor property below can never raise.
        geometry_descriptor(self._letterbox, self._margin)
        self._model = None
        self._device: "str | None" = None

    @property
    def backbone(self) -> str:
        """The resolved backbone identifier (``CAT_EMBED_MODEL`` / default, fixed at
        construction). A gallery build stamps this on its ``model_versions`` row so
        identify can rebuild the SAME embedder — query vectors must share the
        gallery's feature space."""
        return self.model_name

    @property
    def imgsz(self) -> int:
        """The resolved square input side fed to the backbone (``CAT_EMBED_IMGSZ`` /
        default). Persisted alongside ``backbone`` for the same rebuild-exactly reason."""
        return self._imgsz

    @property
    def letterbox(self) -> bool:
        """Whether the resize preserves aspect and pads (vs. the legacy squash)."""
        return self._letterbox

    @property
    def margin(self) -> float:
        """Context-margin fraction applied to a detection box before cropping (0 = off)."""
        return self._margin

    @property
    def geometry(self) -> "str | None":
        """This embedder's crop convention as the stamp string — ``None`` for legacy.

        The third member of the ``(backbone, imgsz, geometry)`` triple that identifies a
        feature space. Callers stamp it: ``build_gallery`` onto the version's ``metrics``
        and the re-cut tool onto each ``dataset_items`` row, so a later build can filter
        to ONE convention instead of blending two."""
        return geometry_descriptor(self._letterbox, self._margin)

    def ensure_available(self) -> None:
        """Verify the heavy deps import; raise ``ImportError`` with the fix if not."""
        try:
            import cv2  # noqa: F401
            import torch  # noqa: F401
            import torchvision  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "Embedder requires 'torch', 'torchvision' and OpenCV, which are the opt-in "
                "analysis extras (NOT in the collector's lean compute/requirements.txt). "
                "Install them with: pip install -r compute/requirements-analysis.txt"
            ) from exc

    def prepare(self) -> None:
        """Load the backbone once and pick the device (cuda > mps > cpu)."""
        self.ensure_available()
        import torch

        if torch.cuda.is_available():
            self._device = "cuda"
        elif torch.backends.mps.is_available():
            self._device = "mps"
        else:
            self._device = "cpu"
        # torch.hub downloads facebookresearch/dinov2 + weights on first use.
        self._model = torch.hub.load("facebookresearch/dinov2", self.model_name)
        self._model.eval().to(self._device)

    def embed_paths(
        self,
        paths: "list[str]",
        batch_size: int = 32,
        progress: "Callable[[int, int], bool] | None" = None,
    ) -> "tuple[np.ndarray, list[int]]":
        """Embed crop files → ``(embeddings (M,D) float32, kept_indices)``.

        A path that fails to decode (missing/corrupt file) is SKIPPED, so ``M`` may
        be < ``len(paths)``; ``kept_indices`` gives, in order, the input indices
        that produced a row, so the caller can align its labels to the embeddings.
        Decodes via ``np.fromfile`` + ``cv2.imdecode`` (Windows-path-safe, matching
        ``ingest.client``), converts BGR→RGB, resizes to the patch-aligned square,
        and ImageNet-normalises. Runs under ``torch.no_grad`` in batches.

        ``progress``, when given, is called ``progress(done, total)`` — once with
        ``(0, len(paths))`` before the loop to set the denominator, then after every
        batch with the cumulative count of input paths consumed so far (reaching
        ``len(paths)`` at the end) — driving the ETA UI. If a call returns a FALSY
        value the run aborts at that batch boundary by raising ``EmbedCancelled``,
        so a Cancel interrupts the long phase instead of running to completion.
        ``progress=None`` leaves behavior byte-identical to a plain embed.

        A non-zero ``margin`` is a hard ``ValueError`` here rather than being ignored:
        there is no detection box left in a stored crop FILE to expand, because the
        margin was already baked into its pixels when ``dataset.crops.materialize`` cut
        it. Silently dropping it would make ``Embedder(margin=0.1).embed_paths`` and
        ``.embed_crops`` produce two different feature spaces from one object — so a
        caller on the gallery side builds the embedder letterbox-only and lets the row's
        ``geometry`` stamp record that the pixels already carry the margin. The
        ``letterbox`` half applies here exactly as it does to a live query crop.
        """
        if self._margin:
            raise ValueError(
                f"embed_paths() cannot honour margin={self._margin!r}: a stored crop file "
                "has no detection box left to expand — the margin was baked in when the "
                "crop was cut. Build the embedder with margin=0.0 for the gallery side; "
                "the row's `geometry` stamp is what records that its pixels carry it."
            )
        # A path is its own item; no crop — the shared engine embeds the full frame.
        return self._embed_items(
            paths, batch_size, progress,
            path_of=lambda p: p, crop=lambda img, _item: img, caller="embed_paths",
        )

    def embed_crops(
        self,
        items: "list[tuple[str, list[int]]]",
        batch_size: int = 32,
        progress: "Callable[[int, int], bool] | None" = None,
    ) -> "tuple[np.ndarray, list[int]]":
        """Embed detection crops → ``(embeddings (M,D) float32, kept_indices)``.

        Like ``embed_paths`` but each item is ``(path, box)`` where ``box`` is
        ``[x1, y1, x2, y2]`` in the STORED JPEG's own pixel space (the ``yolo-serial``
        detection box): the decoded frame is cropped to ``box`` BEFORE the shared
        BGR→RGB → resize → ImageNet-normalise → batched ``no_grad`` forward, so
        gallery crops (via ``embed_paths``) and live query crops (via this) land in
        the same feature space and their distances are comparable. The box is clamped
        to the image bounds with the exact ``dataset.crops._clamp_box`` semantics —
        each pair ordered, rounded to int, clipped to ``[0, w]``/``[0, h]``.

        An item whose file won't decode (missing/corrupt), or whose box is degenerate
        after clamping (zero area, fewer than four coords), is SKIPPED, so ``M`` may be
        < ``len(items)``; ``kept_indices`` gives, in order, the input indices that
        produced a row, so the caller can align its frame ids/boxes to the embeddings.
        The ``progress``/cancel contract is identical to ``embed_paths`` (both route
        through ``_embed_items``).

        A non-zero ``margin`` expands the box BEFORE clamping — outward on all four
        edges by that fraction of the box's own width/height — so a tail tip or an
        extended paw that YOLO's box clipped is still in the crop. Expanding before the
        clamp (rather than after) is what keeps the expansion honest at a frame edge: the
        clamp then trims the part that fell outside the image instead of the margin
        pushing the whole box out of bounds.
        """
        from compute.dataset.crops import _clamp_box, _expand_box

        def crop(img, item):
            # The only real difference from embed_paths: crop to the detection box.
            _path, box = item
            height, width = img.shape[:2]
            try:
                x1, y1, x2, y2 = _clamp_box(_expand_box(box, self._margin), width, height)
            except ValueError:
                logger.warning("embed: degenerate box %r for %s (skipped)", box, item[0])
                return None  # skip — same effect as an undecodable file
            return img[y1:y2, x1:x2]

        return self._embed_items(
            items, batch_size, progress,
            path_of=lambda it: it[0], crop=crop, caller="embed_crops",
        )

    def _embed_items(self, items, batch_size, progress, *, path_of, crop, caller):
        """Shared embedding engine behind ``embed_paths``/``embed_crops``.

        The batching, progress/cancel contract, decode-skip, RESIZE, ImageNet
        normalisation, and final concat live here ONCE so the two public methods can't
        drift (which would embed gallery vs query crops differently). The resize is the
        newest member of that list and the reason it matters most: letterbox-vs-squash
        applied on one side only is a feature-space mismatch with no symptom other than
        worse matching. Per item: ``path_of(item)``
        gives the JPEG path; after decode, ``crop(img, item)`` transforms the BGR image
        (identity for ``embed_paths``, crop-to-box for ``embed_crops``) or returns
        ``None`` to skip it — an undecodable file skips the same way. ``caller`` names
        the public method for the not-prepared error. Returns ``(embeddings (M,D)
        float32, kept_indices)`` where ``kept_indices`` are the input indices that
        produced a row, in order.
        """
        if self._model is None:
            raise RuntimeError(f"Embedder.{caller}() called before prepare()")
        import cv2
        import numpy as np
        import torch

        mean = torch.tensor(_IMAGENET_MEAN).view(1, 3, 1, 1)
        std = torch.tensor(_IMAGENET_STD).view(1, 3, 1, 1)
        total = len(items)
        vecs: "list[np.ndarray]" = []
        kept: "list[int]" = []
        buf: "list[torch.Tensor]" = []
        buf_idx: "list[int]" = []

        def report(done: int, allow_cancel: bool = True) -> None:
            if progress is None:
                return
            cont = progress(done, total)
            # Only the in-progress reports honor cancellation. The final report (after the
            # last flush) is purely informational — all forward passes are done — so a cancel
            # arriving at that instant must NOT discard the completed embeddings.
            if allow_cancel and not cont:
                raise EmbedCancelled(f"embedding cancelled at {done}/{total} crops")

        def flush() -> None:
            if not buf:
                return
            x = ((torch.stack(buf) - mean) / std).to(self._device)
            with torch.no_grad():
                out = self._model(x)
            vecs.append(out.detach().cpu().float().numpy())
            kept.extend(buf_idx)
            buf.clear()
            buf_idx.clear()

        report(0)
        for i, item in enumerate(items):
            path = path_of(item)
            try:
                data = np.fromfile(path, dtype=np.uint8)
            except OSError:
                continue
            img = cv2.imdecode(data, cv2.IMREAD_COLOR) if data.size else None
            if img is None:
                logger.warning("embed: could not decode crop %s (skipped)", path)
                continue
            img = crop(img, item)
            if img is None:
                continue  # crop rejected this item (e.g. degenerate box) — already logged
            img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            if self._letterbox:
                # float32 canvas — see `_letterbox_square`; `.float()` below is then a
                # no-op and the pad lands at exactly 0.0 after the normalisation.
                img = _letterbox_square(img, self._imgsz)
            else:
                # Legacy: squash to the square. Left byte-identical on purpose — every
                # already-promoted gallery's vectors came through this line.
                img = cv2.resize(img, (self._imgsz, self._imgsz), interpolation=cv2.INTER_AREA)
            t = torch.from_numpy(img).permute(2, 0, 1).float() / 255.0
            buf.append(t)
            buf_idx.append(i)
            if len(buf) >= batch_size:
                flush()
                report(i + 1)
        flush()
        report(total, allow_cancel=False)

        emb = np.concatenate(vecs, axis=0) if vecs else np.zeros((0, 0), dtype="float32")
        return emb, kept
