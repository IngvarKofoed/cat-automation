# Prospective grade tally, and a gallery-grade queue filter

The annotation queue tells an operator how many frames a visit has and how confident YOLO
was, but not the thing that decides whether labelling it is worth doing: **what grades the
crops would get**. A line like `2 frames · rep 48% · peak 63%` is undeterminable by eye —
the 63% frame clears the gallery score gate, but whether it clears the *area* gate depends
on a ratio that is nowhere on screen. This adds the tally (`would add 1 gallery / 1 ok`) and,
because that is the predicate that actually matters, makes it a **server-side filter** running
alongside the `min_frames` floor shipped in changelog 403–411.

The enabling move is that **the grading formula becomes Python and the client stops computing
it**. The server already holds every input; once it must grade for the filter anyway, a second
copy in JavaScript is a divergence waiting to happen — the tally an operator reads would be
computed by different code from the filter that hid the visit next to it.

## Key decisions

- **`seed_quality` moves to Python and becomes the single source of truth** (breaking). The
  formula now lives beside the store; every queue/flagged/labelled payload carries a computed
  `quality` per frame, and the client displays and echoes it rather than deriving it. This
  deletes `seedQuality`, `qualityFor` and `canSeed` and simplifies all six JS call sites
  (`index.html:2577, 2687, 2820, 2978, 3191, 3240`). Chosen over duplicating the formula
  because the tally and the filter must agree by construction, not by a pinning test.
- **The filter is server-side, before the page cap** (reuses). It joins `uncertain_only` and
  `min_frames` in the same block (`store.py:5507`), for the reason those are there: the server
  returns one page of 100, so a client-side filter leaves whatever few of that page happen to
  pass, with no way to backfill.
- **The two filters are COMPLEMENTARY, not redundant; both ship ON** (extends). The gallery
  floor does *not* subsume `min_frames` — it is stricter on multi-frame visits and **looser on
  single-frame ones**, because a lone frame's area ratio is 1.0 by construction, so any
  single-frame visit scoring ≥60% grades `gallery` on a test that cannot fail. Retiring
  `min_frames` would hand back exactly the visits it was added to remove, wearing an unearned
  badge. See *The lone-frame asymmetry*.
- **The tally counts grades, not gallery-worthiness** (reuses). It renders in the shape
  Labelled review already uses (`index.html:2924`) — `1 gallery / 1 ok` — because that shape
  is established and the operator reading both pages should not meet two dialects.
- **The filter's predicate is "≥1 gallery-grade crop"** (new). Not "≥1 ok" and not a count
  threshold: a gallery-only build is what the project's own protect-the-gallery rule prescribes
  (`compute/CLAUDE.md`), so that is the contribution worth an operator's attention.
- **Flagged review shows the tally but is never filtered** (reuses). Entry 225 made flagged
  spans deliberately unfloored — a human pointing at one visit is not the bulk case — so the
  tally there is informational only.
- **The ratio=1.0 defect is not fixed here** (new). It has no local repair — `ratio` is
  self-referential for a lone frame, so the choices are an absolute area gate (which re-grades
  every crop) or a rule that a lone frame caps at `ok` (which is `min_frames` restated as a
  grading rule). Both are their own change with their own before/after count; doing either here
  would confound the tally's first reading. Keeping `min_frames` is what makes deferring safe.

## Goals

- Let an operator see, before deciding, what labelling a visit would contribute to a
  quality-filtered gallery build.
- Filter the queue on that contribution directly, instead of only on a frame-count proxy for
  it — the two together, since neither covers the other.
- Have exactly one implementation of the grading formula.

## Non-goals

- **Changing queue membership.** Hidden visits stay undecided and return when the filter comes
  off, as with `min_frames`.
- **Changing how a gallery build selects crops.** `labeled_crops`' grade filter, `cap_per_cat`
  and the Build page are untouched; this only changes *when a grade is computed*, not what a
  build does with one.
- **Re-grading existing `dataset_items` rows.** Stored grades stay as written. Only newly
  committed and re-seeded crops go through the Python formula.
- **Fixing `seedQuality`'s ratio=1.0 defect** — see the Key decision on it, and *The
  lone-frame asymmetry* for what keeping it costs.

## Design

### The formula, in Python

`seed_quality(score, bbox_area, peak_area)` lands beside the store and reproduces today's JS
exactly:

```python
ratio = (bbox_area / peak_area) if peak_area else 0.0
if score >= 0.6 and ratio >= 0.7:
    return "gallery"
if score < 0.35 or ratio < 0.3:
    return "poor"
return "ok"
```

The thresholds are module constants so the two gates are greppable and a future tuning pass
has one place to edit. It is pure — no store, no cv2 — so it is unit-testable on plain numbers,
the same discipline `cap_per_cat` follows (`gallery.py:54`).

**Equivalence with the formula being replaced is the migration's only real risk**, and it is
worth pinning: a test that walks a grid of (score, ratio) pairs across both gate boundaries and
asserts the expected grade, written from the JS source as it stands today, so a transcription
slip fails rather than silently re-grading every future crop.

### Where it is applied

Every payload whose frames the client might grade gains a per-frame `quality`:

- `annotation_queue_page` and the flagged-visit read — computed from the frame's `score` and
  `bbox` against the visit's `peak_area`, all of which those functions already have in hand.
- The relabel path, which is why `canSeed` exists: the client cannot seed a grade when the
  payload predates per-frame scores, and today it degrades to passing the stored grade through
  with a warning banner (`index.html:2942`). With the server grading, that whole class of
  "your compute PC is on an older build" state disappears — the banner and its `canSeed` guard
  come out.

`POST /api/label` keeps accepting `quality` per frame — the wire contract does not change — but
the client now sends back what the payload gave it instead of deriving it. The server is
therefore free to ignore or re-derive it later without another client change.

### The queue filter

`annotation_queue_page` gains `require_gallery: bool = False`, evaluated in the same block as
the other two so the three predicates stay one expression and no ordering question arises:

```python
def _keep_gallery(v):
    return not require_gallery or any(f["quality"] == "gallery" for f in v["frames"])
```

`False` is the API default for the reason `min_frames=1` is: absent the parameter the endpoint
answers as it did before, and the default-ON behaviour lives in the client, so no other caller
changes. Grading happens where each visit's frames are assembled — before this block — since
the predicate reads `f["quality"]` off them.

Its hidden count follows the rule established in changelog 405: **measured with the other
filters still applied**, so unticking it reveals exactly the number quoted. With three filters
the "counted in neither" property from changelog 406 generalises — a visit failing two or more
predicates appears in no per-control count — which is precisely why `hidden_total` (changelog
409) exists and stays the only field the "nothing left" readout gates on.

### The readout

The queue stage's meta line gains the tally between `peak` and `nearest`:

```
2 frames · rep 48% · peak 63% · would add 1 gallery / 1 ok · nearest d=0.321
```

Phrased **"would add"**, not a bare `1 gallery / 1 ok`, because Labelled review's identical
string describes crops that already exist. Same shape, opposite tense; the words are what keep
them apart.

A visit whose tally contains no `gallery` is the skip case, and with the filter on it is simply
absent — so the tally's job on a filtered queue is to distinguish a *strong* visit from a
barely-qualifying one, not to justify a skip.

### The lone-frame asymmetry

The two filters overlap but neither contains the other, and the reason is the known defect:

| Visit | `min_frames=2` | `require_gallery` |
|---|---|---|
| 1 frame, score < 60% | hidden | hidden (grades `ok`/`poor`) |
| 1 frame, score ≥ 60% | **hidden** | **shown** — `ratio` is 1.0 by construction, so it grades `gallery` |
| 2+ frames, no frame clearing both gates | shown | **hidden** |
| 2+ frames, ≥1 gallery crop | shown | shown |

Row 2 is why `min_frames` stays. That visit's `gallery` grade rests on an area comparison
against itself, so it is the *least* trustworthy gallery claim in the dataset — and it is
precisely the case the frame floor was added to remove. Row 3 is what the gallery filter buys
that the floor never could.

Both ship **on** by default. The pair is what makes the queue mean "visits that would
contribute a crop worth enrolling", which neither achieves alone.

### Rollout

Both filters on from the first load, so the tally's first session measures the thing that
motivated this: untick `require_gallery` and read its hidden count to see how many 2-frame
visits carry nothing. Because each hidden count is measured with the other filters still
applied (changelog 405), that number is exactly what unticking reveals — no arithmetic needed.

## Alternatives considered

- **Duplicate the formula in Python, keep the JS copy** (the smaller version of this change).
  Rejected: the tally an operator reads would be computed by different code from the filter
  that hid the neighbouring visit, so a drift shows up as a queue that contradicts its own
  readout. A pinning test can detect that but not prevent it, and the migration to one formula
  is mostly *deletion* on the client — the smaller change is not much smaller.
- **Client-side filtering on the tally.** Considered first and genuinely broken: the page cap
  is applied server-side, so filtering the returned 100 leaves whatever few pass, with no way
  to backfill (`index.html:2696`). This is the same reason `uncertain_only` is server-side.
- **Display-only tally, no filter.** The measurement without the mechanism. Viable and smaller,
  but it leaves `min_frames` as a frame-count proxy for a predicate the code can now express
  directly.
- **Raising `min_frames` to 3.** Cheapest possible response, and wrong: a 2-frame visit whose
  second frame clears both gallery gates is exactly the visit worth labelling, and a frame-count
  floor cannot tell it from one that isn't.
- **Retiring `min_frames` once the gallery filter exists.** The intuition is that the stronger
  predicate should absorb the weaker one, and it was the initial plan here. Rejected on the
  lone-frame asymmetry above: the gallery filter is *looser* on exactly the visits `min_frames`
  was built to remove. Worth revisiting only after the ratio defect is fixed, which is what
  would make the subsumption real.

## Implementation strategy

*Not part of the design — a starting point for whoever builds this.*

- **Single agent, Opus 5.** The formula move is one thread: the Python function, the payloads
  that carry its output, and the six JS call sites that stop computing it have to land together
  or the client grades against a field that isn't there yet.
- The risk sits in one spot — transcribing the gates and removing the JS copy without changing
  what any existing path writes to `dataset_items`. That is interpretation, not transcription,
  which is what keeps it off a cheaper tier despite most of the client work being deletion.
