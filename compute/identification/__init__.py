"""Individual cat identification — the embedding/re-ID side of the learning loop.

Two layers, all torch-gated and lazy-imported so the heavy stack loads only when an
embedding run actually starts (the lean always-on collector never imports these):

- **Feasibility probe** (``embed`` + ``feasibility`` + ``probe``): a pretrained
  embedding backbone turns labelled crops into a separability scorecard answering
  "can we tell our cats apart at all?" (see ``docs/CONCEPT.md`` Phase 1) —
  read-only, no model built.
- **Runtime gallery** (``gallery``): the Train → Run payoff. ``build_gallery``
  embeds the labelled ``identified`` crops into a versioned on-disk gallery
  (``gallery.npz``); ``run_identify`` matches detected crops against a promoted
  gallery by k=1 nearest-neighbour cosine distance and persists one identification
  per frame, which the activity feed then names. Still offline over collected
  frames — the live door loop, decision engine, and actuation stay deferred.
"""
