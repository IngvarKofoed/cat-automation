# Event subject classification — "what is it", not just "which cat"

Every motion event on the Activity feed today resolves to one of four states —
resident / neighbour / unknown-cat / *nothing* — and the "nothing" state
collapses three very different things into one blank card: a false motion
trigger (a light shift, 17:43), a real non-cat subject (a human, 16:14), and a
cat the detector missed. This feature gives each event a **subject** — YOLO's
answer to *what is it* (cat / person / bird / unrecognized / motion-only) —
layered under the existing **identity** — the gallery's answer to *which cat*.
The funnel the user already
reasons about becomes explicit: MOG2 says *something moved*, YOLO says *what*, the
gallery says *who* — and only a cat gets a *who*.

The detector we already run per visit (`yolo-serial`) is broadened from cat-only
to a small COCO class set; its cat-only verdict/score are preserved untouched so
the motion-gate scorecard contract does not move; and the feed derives the
subject at read-time, exactly the way it already derives identity.

## Key decisions

- **Broaden `yolo-serial`, keep verdict/score cat-only** (extends). `compute/analysis/yolo.py`'s
  `_predict_kwargs` gains `person` (0) and `bird` (14) beside `cat` (15) for the
  *serial* persona only; `_result_from` still computes `verdict`/`score` from the
  **cat** boxes alone, so "verdict=1 ⇒ cat present" is unchanged by construction.
  The batched `yolo` oracle stays cat-only and byte-identical — zero scorecard risk.
- **Class is the primary signal; the size band collapses to a motion floor** (diverges
  from our earlier "size-band-primary" framing). Once YOLO names a person/bird, a
  size *ceiling* is redundant. Size only matters where YOLO recognized nothing —
  separating trivial noise from substantial-but-unnamed motion — and there the only
  signal is `frames.area`. So the "band" is one motion floor, not a bbox band.
- **`subject` sits beside `identity`, not replacing it** (extends). `events()` adds a
  `subject` object per event; the existing `identity` object is unchanged and is
  still computed (from `identifications`) only when the subject is a cat. Two fields,
  two questions — the "what" and the "who" — matching the funnel.
- **Detection boxes gain a class tag** (breaking, soft). `analysis.detail.boxes`
  entries become `[x1,y1,x2,y2,conf,cls]`. `_best_box` filters to the cat class
  (a missing 6th element ⇒ cat), so every existing consumer keeps getting the cat
  box on both old and new rows — a no-op migration for identify and annotation.
- **Motion floor is learned from labelled cat visits** (extends). Computed from the
  motion (`frames.area` + motion-frame count) of `identified`/`unknown_cat`-labelled
  visits and stamped on the active `model_versions.metrics` at gallery-build — so it
  is "below essentially every real cat visit", recomputed as labels grow. `events()`
  reads it from `active_model()`; a hardcoded conservative default applies when there
  is no active model (or it carries no floor), keeping the feed working pre-calibration.
- **Forward-auto via the live worker; history via a manual re-detect sweep** (reuses).
  A broadened `yolo-serial` means new visits gain person/bird boxes with no worker
  change. Old frames re-detect through the existing reanalyze path. Old cat-only
  rows still classify (cat vs not) — only the person/bird *naming* of history needs
  the re-sweep.

## Goals

- Replace the single blank "no chip" state with an honest label on **every** event:
  person / bird / unrecognized-motion / motion-only, in addition to the existing cat
  states. Nothing is hidden (per the chosen product direction — label everything).
- Distinguish the user's two cases: 17:43 (light shift) → *motion only*; 16:14
  (human) → *person* when YOLO sees them top-down, else *unrecognized motion* — never
  a silent blank.
- Leave the motion-gate scorecard's meaning of a `yolo` verdict exactly as it is.
- Keep the change read-time and additive — no change to how events are *formed*
  (they stay pure MOG2 motion clusters).

## Non-goals

- **Naming *who* a person/bird is.** YOLO says "person"; there is no per-individual
  identity for non-cats. Identity stays cat-only.
- **A bbox size *ceiling* / cat-detection sanity band.** Deferred — class already
  answers "not a cat"; a ceiling only guards against YOLO mis-boxing a huge blob as a
  cat, a refinement (Open questions).
- **Tailgating policy** (a person and a cat co-present). We record the cat identity
  and do not surface co-presence; access policy for this is deferred by CONCEPT.
- **Broadening the batched `yolo` oracle.** Not needed for the feed; the live worker
  and identify path both run `yolo-serial`.
- **Changing MOG2 / the motion gate.** Reducing false triggers at the gate is separate
  ongoing tuning work.

## Design

### Detector (compute/analysis/yolo.py)

`_predict_kwargs` currently pins `classes=[15]`. For the serial persona it becomes
`classes=[0, 14, 15]` (person, bird, cat); the batched persona keeps `[15]`. In
`_result_from`, each surviving box records its class id as a 6th element, and
`verdict`/`score` are derived from the **cat-class** subset only:

```
boxes.append([x1, y1, x2, y2, conf, cls])         # all classes retained
cat_boxes = [b for b in boxes if b[5] == 15]
verdict = bool(cat_boxes)                          # unchanged meaning
score   = max((b[4] for b in cat_boxes), default=0.0)
```

For the batched `yolo` oracle every box is already class 15, so this is identical
to today. For serial, a person-only frame now gets a row with `verdict=0` and a
person box in `detail` — so the identify path (`iter_unidentified`, which joins on
`verdict=1`) still ignores it, while the subject classifier can read its box.

### Box readers (compute/collection/store.py)

`_best_box` becomes "highest-confidence **cat** box": it filters `usable` to
`len(b) >= 6 and b[5] == 15`, and — for backward compatibility — treats a 5-element
box (no class, i.e. an old cat-only row) as a cat. Because identify and the
annotation tool only ever wanted the cat crop, this is behaviour-preserving on all
existing rows and correct on mixed rows. A new small helper (e.g. `_subject_classes`)
reads *all* classes present in a `detail`, for the classifier below.

### The `subject` vs `identity` split (events())

`events()` keeps computing `identity` exactly as it does now (the
`_aggregate_identity` join over `identifications`). It additionally derives a
`subject` per returned event from the span's `yolo-serial` `analysis` rows (read
regardless of verdict, since a person box rides a `verdict=0` row) plus the motion
floor:

```
subject = { kind: 'cat' | 'person' | 'bird' | 'unrecognized' | 'motion_only',
            conf?, n_class_frames?, peak_area? }
```

A class counts as **present** only at confidence **≥ `_ANNOTATE_MIN_CONF` (0.3)** —
the same phantom-rejection floor the annotation queue uses, since YOLO's recall-first
0.15 floor hallucinates boxes on empty scenes. The classification ladder, per event:

1. a **cat** box present (≥ 0.3) anywhere in the span → `kind: 'cat'` → `identity`
   carries resident / neighbour / unknown-cat as today.
2. else a **person** box present → `kind: 'person'`.
3. else a **bird** box present → `kind: 'bird'`.
4. else no recognized box, and peak motion `area` (or motion-frame count) **≥ floor**
   → `kind: 'unrecognized'` (real activity YOLO could not name — a missed subject, an
   out-of-vocabulary animal, a large object; worth a look).
5. else → `kind: 'motion_only'` (below the floor — trivially small/brief; almost
   certainly noise, the 17:43 case).

Precedence is deliberate: **a positive cat detection always wins.** The size floor
and non-cat classes only classify events where no cat was detected, so "a cat with
an oddly large box" stays that cat, and identity never gets second-guessed by
geometry. When the span has no `yolo-serial` rows at all (no active model yet, or
un-swept history), only steps 4–5 are reachable (no class info), so the event still
gets an honest motion label rather than a blank.

**Named-identity promotion.** The 0.3 present-floor is *stricter* than the identify
path's 0.15 verdict-floor, so a span peaking at cat-conf in [0.15, 0.3) can carry a
real gallery **identity** yet fail step 1. To honour "a positive cat detection always
wins", after the identity join an event with a **named** identity (`cat_id` set — a
confident resident/neighbour match, the strongest possible cat signal) has its subject
promoted to `cat`, so a low-confidence resident is never hidden behind a motion chip.
An **unknown-cat** identity (`cat_id` null, a far match) is *not* promoted — at low box
confidence it may be an empty-scene phantom, so it stays phantom-safe. (`identity` is
therefore computed for every event, and the promotion is the one place subject depends
on it.)

### Motion floor (learned)

The floor separates steps 4 and 5 only; it never drops an event (label everything).
It is **learned**, not hand-set: at gallery-build, the build job computes the motion
profile of the labelled cat visits — the low-percentile of per-visit peak
`frames.area` and motion-frame count over the `identified` / `unknown_cat` labelled
spans — and stamps `metrics.subject_floor = {min_area, min_frames, n, source}` on the
new `model_versions` row. Because it sits below essentially every *real* cat visit, a
no-detection event **above** it is "cat-scale motion YOLO couldn't name" — worth a
look (a missed subject, an out-of-vocabulary animal), while **below** it is less
motion than any real cat → almost certainly noise.

`events()` reads the floor from `active_model()` (which already returns the `metrics`
JSON). It recomputes on every gallery-build, so it sharpens as more cats are labelled.
When there is no active model — or its metrics carry no floor — a hardcoded
conservative default applies, so the feed still labels steps 4–5 pre-calibration.
`frames.area` is the MOG2 blob area in the gate's own downscaled space (the axis the
tuning workbench already shows), keeping the learned value interpretable there.

*Coupling note:* the floor is conceptually about the *scene's* motion, yet it rides
the *gallery* model — chosen because `model_versions.metrics` is a zero-schema home
recomputed exactly when the labelled set changes. A model swap that changes the floor
is intended (more/better labels → a better floor), not a surprise. One consequence to
accept: because the split is read-time against the *active* model, promoting/retiring a
gallery can re-label historical no-detection events between `unrecognized` and
`motion_only` (and retiring to no-active-model reverts to the low default) — so that
split is a function of the current model, not frozen at event time. The cat/person/bird
labels do not move; only the noise/worth-a-look boundary does.

### Frontend (both feeds)

Admin `#activity` and the user feed both render off `/api/events`. `identityKind()`
becomes `subjectKind(ev)`: for `subject.kind === 'cat'` it falls through to the
existing identity-chip logic (untouched); otherwise it renders a new chip —
`person` / `bird` / `unrecognized` / `motion only`. Chip colors must stay clear of
the verdict palette (caught/missed) and the identity palette; exact tokens are a
styling detail for the build, deferred to the `dataviz` / design pass.

## Open questions

- **Q:** "Big bird" — COCO's `bird` class carries no size. Do we gate bird detections
  by a minimum bbox area so small birds flitting past don't earn a `bird` chip (they'd
  fall to `unrecognized`/`motion_only` instead)? **Default:** no gate for the MVP —
  label any detected bird as `bird`; add a bird-area floor only if small-bird chips
  prove noisy in practice. (A bird-only size gate is cheap to add later and needs real
  data to set sensibly.)

## Alternatives considered

- **Separate `yolo-subject` detector (storage B).** Leaves `yolo-serial` untouched for
  provable scorecard isolation, but buys a whole second detection pass per
  frame/visit and a second detector to keep in sync — to avoid a change (`_result_from`
  staying cat-only) that is small and self-contained. Rejected.
- **First-class `detections` table (storage C).** Cleanest data model (class as a
  column, no JSON parsing), but the most new machinery — table, write path, eviction
  cascade, its own sweep — duplicating what `analysis` already does for cats.
  Over-built for one door. Rejected.
- **Size-band-primary with a bbox ceiling.** The original framing. Grounding it in the
  code showed the ceiling is redundant with the class signal and that the only
  size axis available for the no-detection case is motion `area`, not bbox area — so
  the band reduced to a motion floor and class took primacy.
