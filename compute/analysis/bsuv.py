"""``BsuvAnalyzer`` — the "is there foreground?" oracle, deep background subtraction.

The second of the two offline oracles the motion-gate-oracles spec validates MOG2
against (see ``compute/analysis/base.py`` for the shared contract, and
``compute/analysis/yolo.py`` for the sibling cat-detector oracle). Where YOLO asks
"is a *cat* here?", BSUV-Net asks the softer question the edge's live gate itself
asks — "is there *foreground/motion* here?" — but with a learned deep model
instead of MOG2's frame differencing, so a disagreement pins where the cheap gate
is wrong. ``verdict``/``score``/``detail`` reduce to the uniform shape
``AnalysisResult`` defines; "present" here means "the foreground fraction of the
frame reached ``threshold``."

**Chosen approach: a sliding *recent* background.** BSUV-Net needs a reference
"background" image to subtract the current frame against. Rather than a single
fixed empty-scene reference (which drifts wrong across day/night/weather at an
outdoor door), this analyzer keeps a rolling window of the most recent decoded
frames and rebuilds the reference as their **per-pixel temporal median** every
frame — a background that self-heals as the light changes and that a briefly
present cat can't poison (it occupies only a few frames of the window, so the
median rejects it). This is why the analyzer is ``windowed = True``: its verdict
depends on temporal neighbours, so the runner must feed it frames in strict time
order (``Store.iter_time_order``) and it carries rolling state across ``analyze``
calls.

**Two deliberately separated halves — testable numpy vs. deferred CUDA:**

- *Pure-numpy, importable & unit-testable without torch/CUDA*: the windowing
  (a ``deque``), the temporal-median background (``_temporal_median``), and the
  foreground-fraction → verdict/score reduction (``_foreground_fraction``). These
  are free functions with no ML dependency, so the windowing/background math can
  be exercised on any box (including this dev box and CI) without the heavy stack.
- *Deferred to the CUDA compute box*: loading the BSUV-Net network
  (``_build_network``) and the forward pass that turns (frame, background) into a
  foreground mask (``_forward``). BSUV-Net is CUDA-bound research code and the
  exact variant + input-tensor layout are settled on the NVIDIA box where it can
  actually run — the one remaining unknown, per the spec's non-goals. Both are
  clearly-marked TODO stubs that raise, so on any box lacking CUDA/the deps
  ``prepare()`` fails loudly with an actionable message instead of half-working.

Lazy-import discipline: ``torch`` and the BSUV model libs are imported only inside
``prepare()``/the forward path, and ``cv2``/``numpy`` only inside the functions
that use them — never at module scope — mirroring ``compute/ingest/client.py``'s
``StreamFrame.image``. That is what lets the always-on collector (and the runner's
analyzer registry, which merely imports this module) load from the lean
``compute/requirements.txt``; the opt-in ML stack
(``compute/requirements-analysis.txt``) is only touched once a sweep starts.
"""
from __future__ import annotations

import collections
import logging
import os
from typing import TYPE_CHECKING

from compute.analysis.base import AnalysisResult

if TYPE_CHECKING:
    # Type-only — see the module docstring; never imported at runtime here, so the
    # "no CV/ML at module import" guarantee holds. ``np`` names the BGR frame
    # ``analyze`` receives and the window/background arrays; ``Store`` names the
    # handle ``prepare`` primes its window from.
    import numpy as np

    from compute.collection.store import Store

logger = logging.getLogger(__name__)

# Env overrides, per the spec — read once in __init__ and cached, so a sweep's
# per-frame ``analyze()`` never touches the environment. Same explicit-arg → env →
# default precedence YoloAnalyzer and EdgeClient use.
_ENV_WINDOW = "CAT_BSUV_WINDOW"
_ENV_THRESH = "CAT_BSUV_THRESH"

# Rolling-window length: how many recent frames the temporal-median background is
# built from. 32 at ~10 fps is a few seconds of scene — long enough that a passing
# cat is a minority of frames (so the median rejects it) yet short enough that the
# background still tracks changing outdoor light.
_DEFAULT_WINDOW = 32
# Foreground-fraction verdict threshold: a frame is "present" once at least this
# fraction of its pixels are foreground. 0.02 (2%) ignores speckle/sensor noise
# while still catching a small, partial top-down cat (per the project memory, the
# camera is ceiling-mounted, so a cat is a modest blob, not a full-frame subject).
_DEFAULT_THRESH = 0.02

# ``recent_before(id, n)`` returns the ``n`` frames with ``id < id`` — so a
# deliberately huge sentinel id selects the newest ``n`` frames in the store to
# prime the window with. See ``_warm_start`` for why the *newest* frames are an
# acceptable prior even though the sweep itself runs oldest-first.
_WARMSTART_ID = 1 << 62

# Binarization threshold for the forward pass's foreground map: BSUV-Net emits a
# per-pixel foreground *score* in [0, 1], so a pixel counts as foreground at ≥ 0.5.
# (A plain boolean mask also works — ``True`` is ``1.0`` ≥ 0.5.)
_MASK_FG_THRESHOLD = 0.5


# --- Pure-numpy background math (no torch/CUDA — importable & unit-testable) ---
#
# These free functions are the testable half of this module: the windowing and
# background estimation carry no ML dependency, so they can be exercised on any
# box without the heavy stack. numpy is imported *inside* each (lazy discipline —
# see the module docstring), which keeps ``import compute.analysis.bsuv`` free of
# even numpy at load time while leaving the functions perfectly testable (a test
# env for the analysis extras has numpy; torch is what it need not have).


def _temporal_median(frames: "collections.abc.Iterable") -> "np.ndarray":
    """Per-pixel temporal median over the recent window → the background estimate.

    ``frames`` is an iterable of same-shape BGR ``uint8`` ndarrays (the rolling
    window). The per-pixel median across time is the classic robust background:
    a subject that moves through the scene occupies only a minority of the window
    at any given pixel, so the median falls on the static background behind it,
    while genuine lighting changes (which affect the whole window) are tracked.

    Returns a background ndarray of the same shape and dtype as the inputs — cast
    back from ``np.median``'s float result so it can be differenced against, or
    fed to the network alongside, a ``uint8`` frame without a dtype mismatch.

    Precondition: at least one frame. ``analyze`` guarantees this (it appends the
    current frame before calling), so no empty-guard is needed here.
    """
    import numpy as np

    stack = np.stack(list(frames), axis=0)  # (T, H, W, C)
    return np.median(stack, axis=0).astype(stack.dtype)


def _foreground_fraction(mask: "np.ndarray") -> float:
    """Fraction of pixels flagged foreground in a BSUV-Net foreground map.

    ``mask`` is the forward pass's per-pixel foreground score in [0, 1] (or a
    boolean mask); a pixel is foreground at ``>= _MASK_FG_THRESHOLD``. Returns a
    plain Python ``float`` in [0, 1] — this is both the analyzer's ``score`` and
    the quantity its ``verdict`` thresholds, kept beside the boolean so the
    threshold can be re-judged later without re-running the sweep. An empty mask
    reads as 0.0 (no foreground) rather than dividing by zero.
    """
    import numpy as np

    m = np.asarray(mask)
    if m.size == 0:
        return 0.0
    return float(np.count_nonzero(m >= _MASK_FG_THRESHOLD) / m.size)


def _decode_frame(path: str) -> "np.ndarray | None":
    """Decode one stored JPEG from ``path`` to a BGR ndarray, or ``None`` on failure.

    Used only to prime the window in ``_warm_start``. ``cv2`` is imported here (not
    at module scope) for the same lazy reason as everything else. ``cv2.imread``
    returns ``None`` for a missing or corrupt file — a frame that retention may
    have just evicted, or a truncated write — which the caller simply skips, so a
    stale warm-start path can't crash a sweep before it begins.
    """
    import cv2

    return cv2.imread(path, cv2.IMREAD_COLOR)


class BsuvAnalyzer:
    """Windowed deep-background-subtraction oracle; satisfies the ``Analyzer`` protocol.

    ``windowed = True`` because ``analyze()`` depends on temporal neighbours (the
    recent-frame window it maintains), so the runner drives it over the *full*
    time-ordered set (``Store.iter_time_order``) and it always revisits every
    frame — the price of order-correctness (see ``compute/analysis/base.py``).
    Construction takes optional overrides purely for tests; production callers rely
    on the env vars above.
    """

    name = "bsuv"
    windowed = True

    def __init__(
        self,
        window_size: "int | None" = None,
        threshold: "float | None" = None,
    ) -> None:
        # Explicit arg wins, then the env var, then the default — the same
        # precedence YoloAnalyzer/EdgeClient use. A malformed env value raises here
        # (int()/float()), failing loud at construction rather than mid-sweep.
        self._window_size = int(
            window_size if window_size is not None else os.environ.get(_ENV_WINDOW, _DEFAULT_WINDOW)
        )
        self._threshold = float(
            threshold if threshold is not None else os.environ.get(_ENV_THRESH, _DEFAULT_THRESH)
        )
        # A window shorter than one frame can't build a background and would fail
        # confusingly deep inside analyze(); reject it at construction instead.
        if self._window_size < 1:
            raise ValueError(f"{_ENV_WINDOW} / window_size must be >= 1, got {self._window_size}")
        # Populated by prepare(); analyze()/methods before prepare() are a caller
        # bug, not a runtime condition to design around, so they fail loud.
        self._store: "Store | None" = None
        self._window: "collections.deque | None" = None
        self._model = None
        self._device: "str | None" = None

    def prepare(self, store: "Store") -> None:
        """Load the network, pick the device, then build and warm-start the window.

        Order is deliberate: **load the model first, prime the window second.**
        BSUV-Net is CUDA-bound, so on any box without CUDA/the ML deps this must
        fail — and failing *before* decoding up to ``window_size`` warm-start JPEGs
        keeps a mistaken run on the dev box cheap. When it does succeed (the CUDA
        compute box), the freshly-built deque is primed from the store so the first
        analyzed frames aren't garbage.

        A missing/unavailable model (deps absent, no CUDA, or the variant not yet
        wired up) raises ``ImportError`` with an actionable message. All three
        unavailability causes funnel into ``ImportError`` on purpose, so the API
        layer maps any of them to one clean "install compute/requirements-analysis.txt
        / run on the CUDA box" response instead of a 500.
        """
        self._store = store
        # Fail fast: import torch, require CUDA, then construct the network. Any of
        # these raising ImportError leaves the (possibly stale) window untouched —
        # harmless, since a raised prepare() means analyze() is never called.
        self._load_model()
        # Fresh window per job so a re-run never inherits a prior sweep's frames,
        # then prime it from recent stored frames (best-effort).
        self._window = collections.deque(maxlen=self._window_size)
        self._warm_start(store)

    def ensure_available(self) -> None:
        """Verify ``torch`` imports AND a CUDA device is present; raise if not.

        The cheap synchronous dep/hardware gate the runner calls in ``start`` (see
        ``Analyzer.ensure_available``) — no model construction, just "can this box
        run BSUV at all." ``torch`` absent → the analysis extras aren't installed;
        ``torch`` present but no CUDA → this is the dev box, where BSUV is
        intentionally not exercised (YOLO is the oracle that runs here — see the
        spec's non-goals). Both raise ``ImportError`` with a fix, so a Run BSUV on
        the wrong box fails at request time (→ 503) rather than mid-sweep.
        """
        try:
            import torch
        except ImportError as exc:
            raise ImportError(
                "BsuvAnalyzer requires 'torch' (plus the BSUV-Net model libs), "
                "which are NOT part of the always-on collector's lean "
                "compute/requirements.txt (the analysis oracles' ML deps are "
                "opt-in). Install them with: pip install -r "
                "compute/requirements-analysis.txt — and note BSUV must run on the "
                "CUDA compute box, not this dev box."
            ) from exc

        if not torch.cuda.is_available():
            raise ImportError(
                "BsuvAnalyzer needs an NVIDIA CUDA GPU, which is not available on "
                "this machine. BSUV-Net is CUDA-bound and is exercised only on the "
                "compute box (per the spec's non-goals); use YoloAnalyzer for "
                "offline analysis on this dev box."
            )

    def _load_model(self) -> None:
        """Ensure deps + CUDA (``ensure_available``), then build the network on CUDA.

        Device selection is *not* cuda>mps>cpu like YOLO: BSUV-Net requires CUDA in
        practice, so ``ensure_available`` refuses anything else rather than run it
        slowly/wrong. The heavy build lives in ``_build_network`` (still a TODO
        pending the CUDA box).
        """
        self.ensure_available()
        import torch

        self._device = "cuda"
        self._model = self._build_network(torch)

    def _build_network(self, torch) -> object:
        """DEFERRED to the CUDA box — construct/load the BSUV-Net network.

        TODO(cuda-box): The exact BSUV-Net variant (the original vs. BSUV-Net 2.0,
        and its background-input configuration — empty-background, recent-
        background, and/or semantic channels) and the checkpoint to load are
        finalized on the NVIDIA compute box, where this can actually run and be
        validated; they are not guessable here (the package is intentionally left
        unpinned in compute/requirements-analysis.txt). When settled: pin the
        package there, import it here, load the checkpoint, move it to
        ``self._device`` ('cuda'), call ``.eval()``, and record the input
        normalization + tensor layout the forward pass below must match. Until
        then this raises so the failure is honest rather than silently no-op.
        """
        raise ImportError(
            "BSUV-Net model wiring is not finalized: the exact variant, package, "
            "and checkpoint are chosen and pinned on the CUDA compute box (see the "
            "TODO in compute/analysis/bsuv.py:_build_network and the placeholder in "
            "compute/requirements-analysis.txt). YoloAnalyzer is the working oracle "
            "meanwhile."
        )

    def _warm_start(self, store: "Store") -> None:
        """Prime the rolling window with recent stored frames (best-effort).

        Without priming, the first ``analyze`` calls would build a background from
        just the current frame, so background == frame → foreground fraction ≈ 0 →
        a *missed* subject exactly when we care most. Priming gives those early
        frames a real background prior.

        We prime from the *newest* ``window_size`` frames (``recent_before`` with a
        huge sentinel id) even though the sweep runs oldest-first. This is a
        deliberate, bounded approximation: there is nothing before the oldest frame
        to prime from, and at a fixed door the scene is largely static, so any
        recent window is a far better prior than an empty one. Its only effect is
        on the first ``window_size`` verdicts — the primed frames age out of the
        deque as the oldest-first sweep advances and the window refills with the
        true temporal neighbours. Frames are appended in chronological order (the
        order ``recent_before`` returns), and an empty store simply yields an empty
        prime (graceful cold start).
        """
        try:
            paths = store.recent_before(_WARMSTART_ID, self._window_size)
        except Exception:
            # Priming is an optimization, never a correctness requirement — a store
            # read that trips must not abort the whole sweep before it starts.
            logger.warning("bsuv: warm-start read failed; starting with a cold window", exc_info=True)
            return
        primed = 0
        for path in paths:
            image = _decode_frame(path)
            if image is not None:
                self._window.append(image)
                primed += 1
        logger.info("bsuv: warm-started window with %d/%d recent frames", primed, self._window_size)

    def analyze(self, image: "np.ndarray") -> AnalysisResult:
        """Return the foreground-present verdict for one BGR frame.

        Must be called in strict time order (windowed): it pushes ``image`` into
        the rolling window, so successive calls both read *and* update state. The
        background is the temporal median of the window *including* the current
        frame — one frame is a negligible fraction of the window, so it doesn't
        bias the median, and it keeps the cold-start case graceful (a lone frame
        yields background == frame → fraction 0 → no false positive).
        """
        if self._window is None or self._model is None:
            raise RuntimeError("BsuvAnalyzer.analyze() called before prepare()")

        self._window.append(image)
        background = _temporal_median(self._window)  # pure numpy — testable half
        mask = self._forward(image, background)  # deferred CUDA — the model half
        fg_fraction = _foreground_fraction(mask)  # pure numpy — testable half

        verdict = fg_fraction >= self._threshold
        detail = {"fg_fraction": fg_fraction, "window": len(self._window)}
        return AnalysisResult(verdict=verdict, score=fg_fraction, detail=detail)

    def _forward(self, frame: "np.ndarray", background: "np.ndarray") -> "np.ndarray":
        """DEFERRED to the CUDA box — the BSUV-Net forward pass.

        Contract for the implementer: given the current ``frame`` and the temporal-
        median ``background`` (both BGR ``uint8``, same shape), return a per-pixel
        foreground map as an ndarray of scores in [0, 1] (a boolean mask is also
        accepted) of the frame's height×width — ``_foreground_fraction`` reduces it
        to the verdict, so the network's job ends at the mask.

        TODO(cuda-box): finalize the exact BSUV-Net variant and its **input-tensor
        layout** here — channel order (RGB vs the BGR we hold), normalization, the
        (current, background[, empty-background/semantic]) channel stacking the
        chosen variant expects, resize to the network's input size, N/C/H/W
        batching, ``torch.no_grad()`` inference on ``self._device``, then argmax/
        sigmoid → foreground map resized back to the frame's H×W. Kept behind this
        method so the whole windowing/median/fraction pipeline above is exercisable
        without the model; only this one seam is CUDA-bound. Raises until wired up
        (and ``_build_network`` raises first, so this is unreachable on a box where
        the model can't load anyway).
        """
        raise NotImplementedError(
            "BSUV-Net forward pass is finalized on the CUDA compute box — see the "
            "TODO in compute/analysis/bsuv.py:_forward"
        )
