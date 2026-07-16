"""Individual cat identification — the embedding/re-ID side of the learning loop.

Phase-1 scope is the *feasibility probe*, not the run-time gallery: ``embed`` wraps
a pretrained embedding backbone (torch-gated, lazy-imported) and ``feasibility``
turns labelled-crop embeddings into a separability scorecard answering "can we tell
our cats apart at all?" (see ``docs/CONCEPT.md`` Phase 1). Nothing here is imported
by the lean always-on collector; the heavy stack loads only when a probe runs.
"""
