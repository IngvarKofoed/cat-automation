"""The Analyzer contract: the offline oracle interface the sweep runner drives.

An *analyzer* is a stronger, slower detector run over already-stored frames to
*validate* the edge's live MOG2 motion gate — never to replace it (MOG2 stays the
gate; see the spec). Each backend — YOLO (COCO cat detector, "is a cat here?") and
BSUV-Net (deep background subtraction, "is there foreground?") — reduces its own
rich output to one uniform verdict, "is the subject present in this frame?", so a
single disagreement query over the ``analysis`` table can serve every oracle
regardless of what "present" happens to mean to it.

Kept deliberately dependency-free: this module — the package's public contract —
imports only the stdlib and ``typing``. The heavy machinery (``cv2``, ``numpy``,
``torch``, ``ultralytics``, BSUV) lives inside the concrete backends and is
imported lazily *there*, the same lazy-import discipline ``StreamFrame.image`` uses
on the ingest side. That is what lets the always-on frame collector import and run
from the lean ``compute/requirements.txt`` while the analysis extras stay opt-in
(``compute/requirements-analysis.txt``): importing ``analysis.base`` must never
drag the CV/ML stack into the collector process.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    # Type-only imports — never executed at runtime, so the "stdlib + typing only
    # at import" guarantee above still holds. ``numpy`` names the BGR frame an
    # analyzer receives; ``Store`` names the handle a windowed analyzer primes its
    # recent-frame window from. Neither is a runtime dependency of this contract.
    import numpy as np

    from compute.collection.store import Store


@dataclass(frozen=True)
class AnalysisResult:
    """One oracle's verdict about one frame — an append-only observation.

    Frozen because a verdict is a fact recorded at a point in time, not mutable
    state: the runner builds it from ``analyze`` and hands it straight to
    ``Store.write_analysis``; nothing should mutate it afterwards.

    - ``verdict`` — the uniform boolean the disagreement query keys on: ``True``
      means "the subject this oracle looks for is present." What "present" *means*
      is oracle-specific and deliberately so (YOLO: a cat was detected; BSUV:
      foreground/motion was detected). The boolean collapses both so one query
      serves both oracles, while the UI keeps each oracle's meaning explicit —
      because a MOG2-motion / YOLO-no-cat frame is *not* wrong, whereas a
      YOLO-cat / MOG2-still frame is a genuine miss.
    - ``score`` — the raw signal behind the boolean (YOLO: max cat confidence;
      BSUV: foreground fraction), or ``None`` when a backend has none. Kept beside
      the boolean so a threshold can be re-judged later without re-running the sweep.
    - ``detail`` — optional JSON-serializable extras for inspection (boxes, the
      model id), or ``None``. Never queried on; purely for eyeballing a frame.
    """

    verdict: bool
    score: "float | None"
    detail: "dict | None"


class Analyzer(Protocol):
    """The structural contract every offline oracle backend satisfies.

    A ``Protocol`` (not an ABC) so the backends are coupled to the runner by
    *shape*, not inheritance — the runner only ever needs ``name``, ``windowed``,
    ``prepare`` and ``analyze``.

    ``windowed`` picks the runner's iteration model, and it branches on it:

    - ``windowed = False`` (YOLO) — *stateless per-frame*: ``analyze`` depends only
      on the frame it is handed, so the runner drives it over just the frames
      *lacking* a verdict (oldest-first), which makes a sweep resumable and cheap
      to re-run because already-done work is skipped.
    - ``windowed = True`` (BSUV) — *must be fed frames in strict time order and
      keeps rolling state*: its verdict depends on temporal neighbours (a recent
      background window), so the runner drives it over the *full* time-ordered set
      and it maintains its own window across calls. It always revisits every
      frame — the price of order-correctness.
    """

    name: str  # stable oracle id; also the ``analysis.analyzer`` column value ('yolo' | 'bsuv')
    windowed: bool  # see the class docstring — selects the runner's iteration model

    def ensure_available(self) -> None:
        """Cheaply verify the backend's optional heavy deps are importable.

        Raise ``ImportError`` (with an install hint naming
        ``compute/requirements-analysis.txt``) if a required extra — ``torch`` /
        ``ultralytics`` / a CUDA device / the BSUV libs — is absent. This does NOT
        load model weights (that stays in ``prepare``); it is the fast synchronous
        check the sweep runner calls *inside* ``AnalysisManager.start`` so a
        missing-deps run fails at request time (the API maps it to a 503 with the
        hint) instead of vanishing into the worker thread as a delayed
        ``status().error``. A backend with no optional deps may no-op.
        """
        ...

    def prepare(self, store: "Store") -> None:
        """Heavy one-time setup at job start, before the first ``analyze``.

        Loads model weights and picks the device. A windowed analyzer also takes
        the ``store`` handle here to prime its recent-frame window (via
        ``recent_before``) so a resumed sweep has no cold-start artifact; a
        stateless analyzer ignores ``store``.
        """
        ...

    def analyze(self, image: "np.ndarray") -> AnalysisResult:
        """Return the verdict for one decoded frame.

        ``image`` is a BGR ``numpy`` ndarray — the ``cv2.imdecode`` convention the
        rest of the pipeline uses. A windowed analyzer both reads *and updates* its
        rolling state here, so it MUST be called in strict time order; a stateless
        analyzer treats each call independently.
        """
        ...
