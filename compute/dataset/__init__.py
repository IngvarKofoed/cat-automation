"""Compute-tier dataset: durable labelled crops for the identification model.

The output side of the cat-identity annotation tool (see its spec). The store
owns the ``dataset_items`` label rows and hands out ``Store.dataset_root``; this
package owns the *media* under that root — cropping a stored frame's JPEG to a
detection box and materialising it as a durable training crop.

The public surface is deliberately tiny and stateless: ``crops.crop_bytes``
(crop-on-read, for the annotation UI's rep/filmstrip previews) and
``crops.materialize`` (write a durable crop, for the commit path). ``cv2`` is
imported lazily inside those functions — it is a base compute dependency
(``opencv-python-headless``), but keeping the import off module load matches the
ingest/analysis convention so nothing pays for it until a crop is actually cut.
"""
from __future__ import annotations

from compute.dataset import crops

__all__ = ["crops"]
