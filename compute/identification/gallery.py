"""Runtime gallery + identify — the learning loop's Train → Run payoff.

Where ``probe`` answers "can we tell our cats apart?" read-only, this builds and
uses the model: ``build_gallery`` turns the labelled ``identified`` crops into a
versioned on-disk gallery (an ``.npz`` of enrolled vectors + their ``cat_id``s),
and ``run_identify`` matches freshly-detected crops against a promoted gallery by
k=1 nearest-neighbour cosine distance and hands the store one identification per
frame. Both are still **offline over collected frames** — the live door loop, the
decision engine, and actuation stay deferred (see ``docs/CONCEPT.md`` Phase 1).

Discipline mirrors ``probe.py`` / ``embed.py``: the kNN + threshold maths is
pure-numpy (``numpy`` is a base compute dep), and the only heavy stack — torch —
is reached solely through ``Embedder``'s own lazy-imported methods, so importing
this module stays torch-free on the lean collector. ``build_gallery`` writes the
``gallery.npz`` file but NEVER touches the DB (the ``TrainingManager`` inserts the
``model_versions`` row after the file is on disk, so a crash orphans a harmless
artifact, never a row without its file); ``run_identify`` writes rows only through
the store's batched, eviction-guarded, idempotent ``write_identifications_batch``.

The gallery is stored RAW (un-normalised) vectors; ``load_gallery`` L2-normalises
on read and ``match`` L2-normalises the queries, so gallery and query distances
share the same cosine space regardless of how the vectors were saved. The suggested
threshold is computed here but applied only at read (``Store.events()``), so it
stays a tunable number rather than a value baked into idempotent rows.
"""
from __future__ import annotations

import os
from collections import namedtuple
from typing import TYPE_CHECKING

import numpy as np

from compute.identification import feasibility
from compute.identification.embed import EmbedCancelled, Embedder
from compute.identification.probe import _quality_slug

if TYPE_CHECKING:
    from typing import Callable


# A loaded gallery: L2-NORMALISED vectors (N,D float32), their parallel cat_ids
# (N,), and the resolved backbone/imgsz the queries MUST be embedded with to land
# in the same feature space. Immutable and tiny — passed straight into ``match``.
Gallery = namedtuple("Gallery", ["vectors", "cat_ids", "backbone", "imgsz"])


def build_gallery(
    store,
    out_dir: str,
    qualities: "tuple[str, ...] | None" = None,
    progress: "Callable[[int, int], bool] | None" = None,
) -> dict:
    """Embed the labelled ``identified`` crops into a versioned gallery under ``out_dir``.

    Reads ``store.labeled_crops(("identified",), qualities)``, embeds the crop files
    with a fresh ``Embedder`` (first run downloads the backbone), computes a suggested
    same/different distance threshold, and writes ``<out_dir>/gallery.npz``
    (``vectors`` RAW float32 ``(N,D)``, ``cat_ids`` int64 ``(N,)``, plus the resolved
    ``backbone``/``imgsz`` so identify rebuilds the SAME embedder). Returns a summary
    dict for the caller to persist as a ``model_versions`` row — this function does
    **not** touch the DB.

    Cold-start / decode guards mirror ``run_feasibility_probe``: fewer than 2 crops
    OR fewer than 2 distinct cats (before embedding) returns
    ``{'enough': False, 'reason': 'insufficient_labels', ...}``; if decode failures
    collapse the *surviving* set below those floors it returns
    ``reason='decode_failure'`` — either way no file is written. ``progress`` is
    forwarded to ``Embedder.embed_paths`` (it also carries the cancel signal — a
    falsy return raises ``EmbedCancelled``, left to propagate here as in the probe).
    """
    quality_label = "all" if qualities is None else _quality_slug(qualities)
    labels = store.labeled_crops(("identified",), qualities)
    n_crops = len(labels)
    n_cats = len({row["cat_id"] for row in labels})
    if n_crops < 2 or n_cats < 2:
        return {
            "enough": False,
            "reason": "insufficient_labels",  # benign cold-start — nothing labelled yet
            "n_crops": n_crops,
            "n_cats": n_cats,
            "quality": quality_label,
            "message": (
                f"Not enough labelled data yet: {n_crops} crops across {n_cats} cat(s). "
                "Grade representative crops as gallery, or widen the selection."
            ),
        }

    embedder = Embedder()
    embedder.prepare()
    emb, kept = embedder.embed_paths([row["crop_path"] for row in labels], progress=progress)
    kept_labels = [labels[i] for i in kept]
    # Decode failures can drop the surviving crops below the floor even though the
    # pre-embed counts passed (e.g. every crop of one cat is corrupt); re-check on the
    # decoded set so a gallery is never built from one cat or a single vector.
    n_decoded = int(emb.shape[0])
    n_decoded_cats = len({row["cat_id"] for row in kept_labels})
    if n_decoded < 2 or n_decoded_cats < 2:
        return {
            "enough": False,
            "reason": "decode_failure",  # had enough labels but crops wouldn't decode — a real fault
            "n_crops": n_crops,
            "n_cats": n_cats,
            "quality": quality_label,
            "message": (
                f"Only {n_decoded} crops across {n_decoded_cats} cat(s) decoded — "
                "cannot build a gallery."
            ),
        }

    cat_ids = [int(row["cat_id"]) for row in kept_labels]
    ids = np.asarray(cat_ids)
    # Suggested threshold from the enrolled vectors' same- vs different-cat pair
    # cosine distances — the exact upper-triangle computation ``run_feasibility``
    # uses, so the build value matches what a Validate run would report on these
    # crops. ``_best_threshold`` returns ``(None, None)`` when there is no same-cat
    # pair (every cat has a single crop); stored as NULL — no calibrated cutoff.
    e = feasibility._l2_normalize(np.asarray(emb, dtype=np.float64))
    dist = 1.0 - (e @ e.T)
    iu, ju = np.triu_indices(e.shape[0], k=1)
    pair_d = dist[iu, ju]
    same_pair = ids[iu] == ids[ju]
    threshold, bal_acc = feasibility._best_threshold(pair_d[same_pair], pair_d[~same_pair])

    names = {row["cat_id"]: (row["cat_name"] or f"cat #{row['cat_id']}") for row in kept_labels}
    per_cat = [
        {"cat_id": int(c), "cat_name": names[c], "n": int((ids == c).sum())}
        for c in sorted(set(cat_ids))
    ]
    metrics = {
        "per_cat": per_cat,
        "backbone": embedder.backbone,
        "imgsz": embedder.imgsz,
        "threshold_balanced_acc": bal_acc,
    }

    os.makedirs(out_dir, exist_ok=True)
    np.savez(
        os.path.join(out_dir, "gallery.npz"),
        vectors=emb.astype(np.float32),  # RAW (un-normalised); load_gallery normalises on read
        cat_ids=ids.astype(np.int64),
        backbone=embedder.backbone,
        imgsz=int(embedder.imgsz),
    )
    return {
        "enough": True,
        "n_crops": n_decoded,
        "n_cats": n_decoded_cats,
        "n_vectors": n_decoded,
        "backbone": embedder.backbone,
        "imgsz": int(embedder.imgsz),
        "threshold": threshold,
        "quality": quality_label,
        "metrics": metrics,
        "out_dir": out_dir,
    }


def load_gallery(npz_path: str) -> Gallery:
    """Load a ``gallery.npz`` into a ``Gallery`` with L2-NORMALISED vectors.

    The file stores RAW vectors (see ``build_gallery``); they are L2-normalised here
    so ``match`` only has to normalise the queries. ``backbone``/``imgsz`` come back
    as 0-d arrays from ``np.savez`` and are coerced to ``str``/``int``.
    """
    with np.load(npz_path, allow_pickle=False) as data:
        vectors = np.ascontiguousarray(data["vectors"], dtype=np.float32)
        cat_ids = np.ascontiguousarray(data["cat_ids"])
        backbone = str(data["backbone"])
        imgsz = int(data["imgsz"])
    return Gallery(
        vectors=feasibility._l2_normalize(vectors),
        cat_ids=cat_ids,
        backbone=backbone,
        imgsz=imgsz,
    )


def match(gallery: Gallery, query_vecs) -> "list[tuple[int, float]]":
    """k=1 nearest-neighbour match of ``query_vecs`` against ``gallery``.

    Returns one ``(cat_id, cosine_distance)`` per query row, in order — the nearest
    gallery vector's cat and its distance (``1 − cosine similarity``). Queries are
    L2-normalised here; the gallery is already normalised (``load_gallery``). Pure
    numpy. An empty gallery or empty query set returns ``[]``.
    """
    q = np.asarray(query_vecs, dtype=np.float32)
    if q.size == 0 or gallery.vectors.size == 0:
        return []
    dists = 1.0 - (feasibility._l2_normalize(q) @ gallery.vectors.T)
    nn = np.argmin(dists, axis=1)
    return [(int(gallery.cat_ids[j]), float(dists[i, j])) for i, j in enumerate(nn)]


def _check_progress(progress: "Callable[[int, int], bool] | None", done: int, total: int) -> None:
    """Report ``(done, total)`` and honor the cancel signal, like ``embed_paths``.

    A falsy return from ``progress`` raises ``EmbedCancelled`` so a Cancel/Stop
    interrupts the identify pass at a batch boundary rather than running to
    completion. ``progress=None`` is a no-op."""
    if progress is not None and not progress(done, total):
        raise EmbedCancelled(f"identify cancelled at {done}/{total} frames")


def run_identify(
    store,
    model: dict,
    gallery_path: str,
    since_id: "int | None",
    until_id: "int | None",
    progress: "Callable[[int, int], bool] | None" = None,
    embedder: "Embedder | None" = None,
    gallery: "Gallery | None" = None,
) -> dict:
    """Identify not-yet-identified detected frames against ``model``'s gallery.

    Rebuilds an ``Embedder`` from the model's STORED ``backbone`` + ``imgsz`` (never
    env defaults — a drift would embed queries in a different feature space than the
    gallery, a silent garbage-match; if that backbone won't load the error
    propagates and the job fails clearly). Then, over
    ``store.iter_unidentified(model['id'], since_id, until_id)`` in batches: crop +
    embed each frame to its ``yolo-serial`` box (``Embedder.embed_crops``), match k=1
    against the gallery, and persist ``(frame_id, model_id, cat_id, distance, bbox)``
    via ``store.write_identifications_batch``. No threshold is applied — the nearest
    cat + its distance are stored verbatim; "unknown" is derived at read.

    ``embedder`` lets a long-lived caller (the ``LiveIdentifyManager``, which identifies
    a fresh cluster every few seconds) inject a RESIDENT, already-``prepare()``d embedder
    so the DINOv2 weights are not ``torch.hub.load``ed per call. Default ``None``
    preserves the manual pass's behavior exactly — build + prepare a fresh one. When
    supplied it is used verbatim (never rebuilt, never re-prepared: the caller owns its
    lifecycle), guarded by ``backbone``/``imgsz`` matching ``model``: a mismatch would
    embed queries in a different feature space than the gallery — the same silent
    garbage-match the model-stamped rebuild exists to prevent — so it is a hard
    ``ValueError`` rather than quiet wrong answers.

    Every visited frame gets a row so the pass converges and never re-attempts it:
    a MATCH row (nearest cat + distance) for a frame that embedded, or a MARKER row
    (``cat_id=None``) for a frame that could not be embedded — no ``yolo-serial`` box
    (``bbox is None``) or a crop ``embed_crops`` skipped (undecodable/degenerate). The
    marker records the frame as processed without inventing an identity, so
    ``count_unidentified`` reaches 0 and the progress bar reaches 100%.

    ``gallery`` is the same optimization for the vector set: a resident caller passes an
    already-``load_gallery``-ed ``Gallery`` so the (small) ``.npz`` is not re-read off disk
    per call; ``None`` loads it from ``gallery_path`` as before. The caller is responsible
    for passing the gallery that belongs to ``model`` (the worker keys its resident copy on
    ``gallery_path``), so no separate guard is needed here.

    Resumable and idempotent: only frames without a row for this model are visited,
    and the batched writer ``INSERT OR REPLACE``s on the PK. ``progress`` drives the
    ETA (``progress(done, total)`` where ``total`` is ``count_unidentified``); a
    falsy return raises ``EmbedCancelled`` at a batch boundary — safe because writes
    are per-batch and idempotent. Returns ``{'n_identified': <MATCH rows actually
    inserted>}`` — the store's truthful insert count (markers and frames evicted
    mid-pass are excluded), so it never over-reports how many frames were named.
    """
    if embedder is None:
        embedder = Embedder(model=model["backbone"], imgsz=model["imgsz"])
        embedder.prepare()
    elif embedder.backbone != model["backbone"] or embedder.imgsz != model["imgsz"]:
        raise ValueError(
            "injected embedder does not match the model's feature space: embedder "
            f"({embedder.backbone!r}, imgsz={embedder.imgsz}) vs model "
            f"({model['backbone']!r}, imgsz={model['imgsz']}) — matching queries to a "
            "gallery embedded differently is a silent garbage-match"
        )
    if gallery is None:
        gallery = load_gallery(gallery_path)
    model_id = int(model["id"])

    total = store.count_unidentified(model_id, since_id, until_id)
    _check_progress(progress, 0, total)

    written = 0
    done = 0
    batch: "list[tuple[int, str, object]]" = []

    def flush() -> None:
        nonlocal written, done
        if not batch:
            return
        # Split the batch: frames WITH a box go to the embedder; boxless frames get a
        # marker straight away (they can never be embedded).
        embeddable = [(fid, path, box) for fid, path, box in batch if box is not None]
        marker_rows = [
            (fid, model_id, None, None, box) for fid, path, box in batch if box is None
        ]
        match_rows = []
        if embeddable:
            emb, kept = embedder.embed_crops([(path, box) for _fid, path, box in embeddable])
            kept_set = set(kept)
            for (cat_id, distance), input_i in zip(match(gallery, emb), kept):
                fid, _path, box = embeddable[input_i]
                match_rows.append((fid, model_id, cat_id, distance, box))
            # Embeddable frames embed_crops dropped (undecodable / degenerate crop)
            # also get a marker, so they are not re-attempted every future pass.
            marker_rows += [
                (embeddable[i][0], model_id, None, None, embeddable[i][2])
                for i in range(len(embeddable))
                if i not in kept_set
            ]
        written += store.write_identifications_batch(match_rows)
        store.write_identifications_batch(marker_rows)
        done += len(batch)
        batch.clear()
        _check_progress(progress, done, total)

    for frame_id, abs_path, bbox in store.iter_unidentified(model_id, since_id, until_id):
        batch.append((frame_id, abs_path, bbox))
        if len(batch) >= 64:
            flush()
    flush()

    return {"n_identified": written}
