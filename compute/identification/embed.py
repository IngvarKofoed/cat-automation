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
    """

    def __init__(self, model: "str | None" = None, imgsz: "int | None" = None) -> None:
        self.model_name = model or os.environ.get(_ENV_MODEL, _DEFAULT_MODEL)
        self._imgsz = int(imgsz if imgsz is not None else os.environ.get(_ENV_IMGSZ, _DEFAULT_IMGSZ))
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
        """
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
        """
        from compute.dataset.crops import _clamp_box

        def crop(img, item):
            # The only real difference from embed_paths: crop to the detection box.
            _path, box = item
            height, width = img.shape[:2]
            try:
                x1, y1, x2, y2 = _clamp_box(box, width, height)
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

        The batching, progress/cancel contract, decode-skip, ImageNet normalisation,
        and final concat live here ONCE so the two public methods can't drift (which
        would embed gallery vs query crops differently). Per item: ``path_of(item)``
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
