# Crop geometry for new labels

A persisted **crop-shape setting** that the label-commit path cuts and stamps new crops
at, so a freshly re-cut store stops re-splitting the moment the operator labels again.
Today `_commit_label` calls `crops.materialize` with no margin and never sets
`row["geometry"]`, so every new label lands **legacy** regardless of what the rest of the
labelled set holds — the open gap left by changelog entry 441. Moving *existing* crops
stays the Tools page's re-cut job; this spec only settles what a **new** cut does.

## Key decisions

- **One setting, read at commit time** (extends). A `settings` KV key `crop_geometry`
  holding a canonical descriptor string (`null` = legacy, `letterbox`, `letterbox+m10`).
  Read inside `_commit_label`, which forwards the margin half to `crops.materialize` and
  stamps the full descriptor on the row. Both `POST /api/label` and
  `POST /api/label/relabel` already funnel through `_commit_label`
  (`compute/api/app.py:2781,2810`), so labelling and re-labelling are covered by one change.
- **Split the geometry the way the code already splits it** (reuses). `parse_geometry`
  gives `(letterbox, margin)`; only `margin` reaches `materialize`, because only margin
  touches pixels. The row's stamp carries both. This is exactly what `recut_crops` does,
  so a crop cut here and a crop re-cut there are indistinguishable.
- **Crops land in the geometry subdirectory, via the shared `crop_rel_path`** (reuses).
  Not a second copy of the path rule. This is what lets a later re-cut *relink* a
  new crop rather than re-cut it, and it is the same path a re-cut to that geometry
  would have chosen — the two conventions cannot diverge.
- **`crop_rel_path` moves down into `compute/dataset/crops.py`** (extends). It currently
  lives in `compute/tools/recut_crops.py`, and the API app importing a CLI tool module
  is backwards layering. `dataset/crops.py` is where the path rule belongs — it already
  owns cutting and writing crops, and both the app and the tool depend on it. The tool
  imports it from there; behaviour is unchanged.
- **The store row is already ready** (reuses). `add_dataset_items` reads
  `row.get("geometry")` (`compute/collection/store.py:6235`); nothing in the store or
  the schema changes. This is a write-path fix, not a data-model change.
- **Unset means legacy — no auto-seed** (reuses). An unset setting reproduces today's
  behaviour byte-for-byte. Seeding it from whatever geometry the store mostly holds
  would change what gets cut without anyone asking for it, and on a mixed store there
  is no single right answer to seed from.
- **Divergence is surfaced, never prevented** (new). Nothing stops the setting from
  disagreeing with the labelled set, or with the geometry a build asks for — reconciling
  that is the re-cut's job. What changes is that the app *says so*: the setting is shown
  against the crop-shape census, so "my new labels are landing somewhere else" is visible
  instead of being discovered at build time.
- **A re-label follows the current setting, not the crop's old stamp** (new). A re-label
  is a fresh cut, so it lands at the current shape and `_delete_crop_files`' variant glob
  (entry 448) removes the superseded file. The alternative — preserving each crop's
  existing geometry — would make the setting quietly non-authoritative, which is the
  failure this spec exists to end.

## Goals

- New labels land at a shape the operator chose, not at legacy by default.
- A store re-cut to one shape stays at that shape while labelling continues.
- No new failure mode: a shape the frame cannot satisfy must not lose a label.
- The operator can see when the setting and the labelled set disagree.

## Non-goals

- **Moving existing crops.** That is the Tools page's re-cut, unchanged.
- **A background worker** draining crops that aren't at the current shape.
- **The canonical-margin redesign** — storing one wide crop and narrowing at read time.
  Recorded under Alternatives; deliberately not taken here.
- **Changing how a build selects crops.** `build_gallery`'s exact-match geometry filter,
  and `model_versions.metrics.geometry`, keep their current meaning entirely.
- Per-cat, per-visit, or per-stage geometry. One setting, store-wide.

## Design

### The setting

`crop_geometry` in the existing `settings` KV, via `store.get_setting` /
`store.set_setting`. Stored as the canonical descriptor `geometry_descriptor` produces,
so the value is the same shape as `dataset_items.geometry` and a typo cannot mint a
convention nothing will ever match. `GET`/`POST /api/crop-geometry` validates by round-
tripping through `parse_geometry` → `geometry_descriptor` and rejects an unparseable
value with a 400, matching how `POST /api/stage` guards its own enum.

**It survives `clear()`**, unlike the id-relative settings that must be reset with the
frame store (`nonmotion_evicted_through`, entry 305). This one is a shape *preference*,
not a statement about ids that restart at 1 — and the labelled crops it governs survive a
wipe too, so resetting it would silently return new labels to legacy.

### The commit path

In `_commit_label`, once per request rather than per frame:

```python
geometry = store.get_setting("crop_geometry")       # None = legacy
try:
    letterbox, margin = parse_geometry(geometry)
    # CANONICALISE: a build compares its target with `geometry = ?`, so stamping `m10.0`
    # where a build asks `m10` would make every new crop invisible to it.
    geometry = geometry_descriptor(letterbox, margin)
except ValueError:
    geometry, margin = None, 0.0                    # unreadable → legacy, and say so
```

An **unreadable** stored value falls back to legacy and is cut and stamped honestly as
legacy — it never guesses at the convention it cannot parse, which is the same reading
`canonical_geometry` takes ("not mine"). It should be nearly unreachable, since
`POST /api/crop-geometry` validates on write; the fallback exists for a value some other
build or a hand-edit put there. Labelling deliberately keeps working, because losing a
labelling session costs the operator's attention while a legacy-cut crop is recoverable
with a re-cut. What must not happen is silence: `GET /api/crop-geometry` reports the raw
value plus `readable: false`, and every surface below renders that as an error rather than
as "legacy".

Then per frame, replacing the current flat path and bare `materialize` call:

```python
rel_path = crop_rel_path(cat_id, decision, fr.frame_id, recv_ts, geometry)
if not crops.materialize(src_path, fr.bbox, dest_abs, root=dataset_root, margin=margin):
    continue
row["crop_path"] = rel_path
row["geometry"] = geometry
```

`letterbox` is deliberately unused on this side — it is a read-time resize the embedder
applies, so it reaches the stamp and the path but never the pixels.

**No new failure mode.** `_commit_label` already skips a frame whose source JPEG is gone,
and cuts only from live frames; a margin merely expands a box that `_clamp_box` then
trims at the frame edge. So a non-zero margin cannot fail where a legacy cut would have
succeeded. A row is still only written when its crop file was written
(`crops_written`), which is the ordering contract the store depends on.

### Surfacing divergence

The Tools page already renders the crop-shape census (`GET /api/recut/plan` with no
target) and owns the re-cut picker. The setting is shown there, beside the census, so one
card answers both "what shape are new crops cut at" and "what shapes does the store hold"
— and a disagreement between the two reads as a warning with the re-cut as its remedy.

**"Disagrees" means the setting is not the census's dominant shape** — not merely that
other shapes exist. A store legitimately holds several conventions at once (today: 62,075
at `letterbox+m25`, 25,192 at `letterbox`), so warning on any second shape would warn
permanently and train the operator to ignore the one readout that matters. Dominance is
the right test because it is exactly the condition under which new labels are *not*
joining the set a build will enrol from. It fires correctly during a deliberate
transition — between choosing a shape and running the re-cut — which is when the re-cut
button sitting beside it is the answer.

The Model page's build panel is **read-only about the setting but not silent about a
mismatch**: when the build's target geometry differs from `crop_geometry`, it adds a
warning to the existing pre-check hints (entry 439's hints-list shape). That combination
matters — the two disagreeing means the set this build enrols has stopped growing, which
is precisely the silent failure this change closes, so it earns a warning rather than a
line of information. The Annotation page shows the setting as a small static readout, so
the operator can see what shape their keystrokes are producing.

## Alternatives considered

- **A background worker draining crops not at the current shape.** Rejected as scope:
  the operator already has a re-cut tool and asked for the setting alone. It would also
  make a margin change *quietly* destructive, since it would strand every crop whose
  frame had aged out without anyone pressing anything.
- **The canonical-margin redesign** — store each crop once at a generous margin and
  derive any narrower margin (and letterbox) at embed time by centre-cropping, using the
  row's own `bbox` to stay exact through frame-edge clamping. Strictly better: A/B-ing a
  crop shape would need no re-cut at all, nothing could ever be stranded by frame
  eviction, and one file per crop would replace one per arm tried. Deferred because the
  measured evidence (runs 12–15: margin weakly negative overall, and night recall
  91.4% → 86.6% from m0 to m10) says the margin arms are finished — and at zero margin
  nothing can be stranded anyway, since every row can reach plain `letterbox` with no
  decode. Revisit if margin A/Bs ever resume. See entry 440, which named this fix first.
- **Auto-seeding the setting from the store's dominant geometry.** Rejected: it changes
  what gets cut without being asked, and a mixed store has no single right answer.

## Implementation strategy

*Not part of the design — a starting point for whoever builds this.*

- **Single agent, Opus 5.** One code path across four files, and the edits are ordered:
  `crop_rel_path` has to move into `compute/dataset/crops.py` before `_commit_label` can
  call it, and the three UI surfaces all read the one new endpoint. Nothing here splits
  into independent streams.
- Opus rather than a cheaper tier because three of the decisions need interpreting rather
  than transcribing — the dominance test, the unreadable-value fallback, and where the
  warning lands in the existing pre-check hints list. The admin SPA also carries known
  traps worth holding in context (entry 287's missing bare `.hidden` rule, entry 454's
  cached-page check).
