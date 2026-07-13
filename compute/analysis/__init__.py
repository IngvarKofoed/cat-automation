"""The analysis package: the offline-oracle registry the API and runner resolve through.

The compute tier validates the edge's live MOG2 motion gate by running a stronger,
slower oracle over already-stored frames *offline* and recording a per-frame verdict
(see the motion-gate-oracles spec). This ``__init__`` is deliberately thin: it is the
*registry* — ``get_analyzer(name)`` maps a stable oracle id to a freshly constructed
backend — and nothing else, so importing ``compute.analysis`` drags in neither the
CV/ML stack nor even the backend modules.

Why lazy, per-name imports inside ``get_analyzer`` rather than importing the backends at
module top: the two backends carry heavy, *opt-in* dependencies (``ultralytics``/``torch``
for YOLO; a CUDA-bound deep-background-subtraction net for BSUV — installed from
``compute/requirements-analysis.txt``, not the lean collector requirements). A top-level
import would make the whole always-on collector process — which imports the store and the
ingest client, never the analyzers — fail to start whenever those extras are absent.
Importing a backend only when its oracle is actually requested keeps that cost off
everyone who never runs a sweep, and lets a missing dependency surface as an
``ImportError`` from exactly the ``get_analyzer`` call that needs it (the API layer maps
that to a 503 "install compute/requirements-analysis.txt"), while an unknown *name* is a
client mistake surfaced as ``ValueError`` (→ 400).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Type-only: name the return type without importing ``base`` at runtime, so the
    # registry stays as dependency-light as ``base.py``'s own stdlib-only contract.
    from compute.analysis.base import Analyzer

# The stable oracle ids, in the order the UI offers them; also the full set of valid
# ``analysis.analyzer`` column values. YOLO = COCO cat detector ("is a cat here?");
# BSUV = deep background subtraction ("is there foreground?"). Adding a third oracle is
# a new name here + a new branch in ``get_analyzer`` + a new backend module — no schema
# migration, because verdicts are just new rows in the shared ``analysis`` table.
#
# ``yolo-serial`` is NOT a new detector: it is the SAME YOLO backend in its serial
# (pre-batching) call shape, registered as a distinct oracle so its verdicts sit in
# their own ``analysis`` rows. It exists to A/B the batched sweep — sweep both over the
# same frames, then compare each against MOG2 in the scorecard — so a "did batching move
# a verdict?" question is answerable in the existing UI instead of by argument.
ANALYZER_NAMES = ("yolo", "yolo-serial", "bsuv")


def get_analyzer(name: str) -> "Analyzer":
    """Construct the backend for ``name``, importing its module lazily on demand.

    Each branch imports its backend only when that oracle is requested (see the module
    docstring for why): a fresh ``Analyzer`` instance is returned with its heavy model
    weights unloaded — those load later in ``prepare``, not here — so construction stays
    cheap and the registry can front every oracle uniformly. An unknown ``name`` raises
    ``ValueError`` (a caller/client mistake). An ``ImportError`` from a backend whose
    optional deps aren't installed is deliberately left to propagate unchanged, so the
    caller (the API layer) can turn it into an actionable 503 rather than a bare 500.
    """
    if name == "yolo":
        from compute.analysis.yolo import YoloAnalyzer

        return YoloAnalyzer()
    if name == "yolo-serial":
        from compute.analysis.yolo import YoloAnalyzer

        # Same backend, serial (bare-per-frame) call shape; its own name → own rows.
        return YoloAnalyzer(serial=True)
    if name == "bsuv":
        from compute.analysis.bsuv import BsuvAnalyzer

        return BsuvAnalyzer()
    raise ValueError(f"unknown analyzer {name!r}; known analyzers: {ANALYZER_NAMES}")


__all__ = ["ANALYZER_NAMES", "get_analyzer"]
