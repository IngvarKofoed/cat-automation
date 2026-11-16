# Night lighting priority in gallery builds

> **Status: parked, 2026-11-15. Not handed off.** The priority only changes a build that has a
> per-cat cap, and no model has ever been built with one. A build with no cap already enrols every
> double-LED night crop. So the next step is a plain rebuild, checked against the 11–14 Nov false
> alarms. Pick this up only if Validate shows a cap is worth using. The fresh-eyes review left two
> design points open; they are under Open questions.

A second IR LED went in at the door on Sunday 8 Nov 2026 around 21:00, so every night frame
since then is lit differently from every night frame before it. Night brightness (mean luma of
the clipped frame, 00:30–03:00) was ~31–38 on every single-lamp night from 30 Jul to 8 Nov. It
has been ~60–65 every night since 9 Nov. The active gallery, model v6, was built on 13 Aug from
single-lamp night crops only, so it has no picture of the door as it looks at night now. Under
IR the alarm then reads Sultan, Saffi and Jhinie as the neighbour Store Jihn. The 11–14 Nov
false alarms were analysed on 2026-11-15; the frontier bug in one of them is out of scope here.

This change adds one persisted instant, "night lighting since". When it is set and a build or
Validate caps crops per cat, each cat's places are split between day and night in the
proportion its labelled crops already have. The night places go to crops from after the
instant first, and to older night crops only with whatever is left. Old night crops stay
eligible, as a fallback while a cat's new-lamp coverage is thin. Unset, or uncapped, a build
enrols exactly what it does today.

## Outcome

**What you get:**

- A "Night lighting" setting on Tools. Once it is set, a capped build fills each cat's night places
  with double-LED crops first and old-lamp crops after, keeping each cat's own day/night mix.
- Before building, "Cats to enrol" shows each cat's count of new-lamp night crops.
- Validate's capped forecast rows describe the same gallery a Build at that cap would enrol.
- With the setting unset, or with no cap, builds and Validate runs are unchanged.

**How to verify:**

- Set Tools → Night lighting to 8 Nov 2026 20:30, then reload. It reads back as set, the Model page
  echoes it, and "Cats to enrol" shows a New night column at the Build grades. Store Jihn reads 0,
  each resident reads above 0, and a resident at 0 would be marked.
- Build at `gallery` grade with Max per cat 1000. For each cat that had more than 1000 crops, the
  version's metrics show `day + new_night + old_night = 1000`. `day` is within one of
  1000 × the cat's day share. `new_night` is the smaller of the cat's New night count and its night
  places. The versions table shows "night since 8 Nov 20:30".
- Leave Max per cat blank with the setting set: the Build card says the priority has no effect,
  and the version has no `-n` slug fragment and no night fields.
- Clear the setting and build capped again: no `-n` fragment and no night fields, and the
  selection is today's rule.

## Key decisions

- **One persisted instant, not a date** (reuses). Setting key `night_lighting_since_ms`: epoch ms
  in the `settings` table, like `visit_bridge_since_ms` (store.py:191), so it survives a Clear and
  needs no time-zone conversion server-side. A date would cut at midnight and drop the evening of
  8 Nov, which holds 279 of Jihnie Two's 366 crops. It is one instant, not a history of changes:
  "night since the last lighting change" against everything older. Unset means today's behaviour,
  byte for byte.
- **Each cat keeps its own day/night mix under the cap** (extends `cap_per_cat`,
  gallery.py:54). A capped cat's night places are `cap × night crops / all crops`, rounded, and
  day gets the rest. The proportion is taken over what this build could enrol, after grades,
  geometry and exclusions. There is no new number to tune, and new night crops cannot crowd out
  day. Chosen over a fixed night share set by the operator.
- **Within night, new-lamp crops come before old ones, ahead of grade** (extends). The night places
  are filled by today's rule (grade tiers, the overflowing tier spread evenly over time) over the
  new-lamp crops first, then by the same rule over the old-lamp crops with what is left. Day places
  are filled by today's rule, unchanged. Builds have only ever enrolled `gallery` grade, so in
  practice the grade order rarely meets the regime order.
- **Night by sun times, on `src_recv_ts`** (reuses). Through `probe._night_classifier` (probe.py:143):
  the same classifier, gate and timestamp as `/api/label/regime-coverage`, and the edge IR lamp
  runs on the same sun times (`2026-09-07-daynight-sun-times-driven`). With no location, or no
  astral, the rule falls back to today's, and the page and the build's metrics say so. Luma is not
  used: it moved with the lamp, which is the very thing being tracked.
- **One selection for Build and Validate** (reuses). Both already go through `cap_per_cat`: Build
  at gallery.py:224 and Validate's capped forecast through `_cap_masks` (probe.py:115;
  `2026-10-08-capped-gallery-forecast`). Both pass it the same classifier and instant, so a
  forecast row still describes the gallery a Build at that cap would enrol.
- **Snapshotted at enqueue and part of the identity** (extends). The instant joins the job params
  (dedup key), the artifact slug and `metrics` for both job kinds, the way the cap, the exclusions
  and the geometry did (`2026-08-31-build-params-include-cap`, `2026-10-09-geometry-in-job-params`).
  A job reads its instant from its params, never from the setting at run time.
- **A per-cat "New night" count before building** (extends). A column in the Model page's "Cats to
  enrol" table, counted by `Store.enrollable_cats` at the Build grades and geometry, before the cap,
  like the Crops column beside it.

## Goals

- A capped build enrols each cat's crops of the door as it looks at night now ahead of its older
  night crops, without taking places from day.
- Validate's capped forecast describes that same gallery.
- Before building, the operator sees how many new-lamp night crops each cat has.
- With the setting unset, every build and Validate run is unchanged.

## Non-goals

- **Dropping old-lamp night crops.** They stay eligible and fill whatever night places the new
  ones leave. A hard cut-off was rejected (see Alternatives).
- **Choosing or defaulting a cap.** No model has ever been built capped. This change makes the cap
  the lever, but its value stays the operator's, informed by Validate's 2000/1000/500 forecast.
- **Scoring double-LED nights separately in Validate.** Night recall still mixes old-lamp and
  new-lamp visits. Checking tonight's door is a before/after on real visits once a build is
  promoted.
- **Changing the cap's time order or grade order when the setting is unset**, including moving it
  from `src_frame_id` to `src_recv_ts` order.
- **A history of lighting regimes**, or detecting the change from the images.
- **The alarm**: its rule, its frontier bug, and its thresholds.

## Design

### The setting

`GET /api/night-lighting` returns `{since_ms, readable}`. `POST /api/night-lighting {since_ms}`
takes an integer epoch ms, or `null` to clear. It follows `/api/crop-geometry`'s shape (app.py:3282,
3296): a required, nullable field, validation before anything is written, and the response echoes
the stored value. A non-integer, a negative value or a future instant is a 422. Clearing deletes
nothing else.

On Tools, a "Night lighting" card beside "Crop shape" holds a local date-and-time field with Save
and Clear. The browser converts it to epoch ms, so the household's time zone is the one typed in.
The card says what the instant does: "Night pictures from after this time fill each cat's night
places first in capped builds." The Model page shows it as a read-only line in the "Cats to
enrol" card's note area (`#mCatsNote`), always visible while set, with the edit left to Tools.

The value for this door is 8 Nov 2026 20:30. Nothing was stored between 17:30, the last
single-lamp frame, and the first double-LED frame, which falls in the 20:30 half-hour. The
operator types it; nothing seeds it.

### The selection rule

`cap_per_cat(labels, max_per_cat, *, is_night=None, night_since_ms=None)`. When either keyword is
`None`, or the cap is `None`, the function returns exactly what it returns today. Otherwise, for
each cat with more crops than the cap:

1. Split its rows into **day**, **new night** (night, `src_recv_ts ≥ night_since_ms`) and **old
   night**, classifying each row's `src_recv_ts`.
2. Night places = `round(cap × (new + old) / all)`; day places = `cap − night places`. Because
   the cap is below the cat's total, neither side is asked for more crops than it has, so nothing
   needs to spill over.
3. Fill day places with today's rule over the day rows.
4. Fill night places with today's rule over the new-night rows. Any places still free get today's
   rule over the old-night rows.

A cat at or under the cap keeps every crop, as today. A cat with no night crops gets all day, and
one with no new-night crops fills its night places from old night. Night places are the cat's
night share of the cap, rounded. With `gallery` grade only, as every build so far, that is close
to what today's even spread over time takes. With wider grades it can be fewer: today's rule takes
whole grade tiers first, so a cat whose night crops are mostly in the better tier keeps more of
them. A cat whose night share rounds below half a place gets none. The result stays
deterministic, as today's is.

### Build and Validate

- **Enqueue.** Both routes read the setting once when the job is queued and put it in the params,
  next to the cap, exclusions and geometry.
- **Build** stamps it whenever a cap is set; without a cap it changes nothing, so it stays out of
  the identity. **Validate** stamps it whenever it is set, because its forecast ladder always has
  capped rows.
- **Slug.** Stamped jobs get a `-n<YYYYMMDDHHMM>` fragment, after the exclusions fragment.
- **At run time,** `build_gallery` and `probe` build the classifier with `_night_classifier(store)`
  and pass it and the params' instant to `cap_per_cat` (gallery.py:224; `_cap_masks`,
  probe.py:115).
- **No classifier** (no location, or no astral): the selection falls back to today's rule, and
  the job records that the priority was not applied rather than failing.

### What a job records

A stamped job's `metrics` carry:
- `night_lighting_since_ms`;
- `night_lighting_applied` (false, with the reason, when no classifier was available);
- for a Build, each capped cat's enrolled split `{day, new_night, old_night}`.

The Model page's versions table adds "night since 8 Nov 20:30" beside the existing "≤N/cat" note.
That makes a priority-built gallery distinguishable from a plain capped one at a glance.

### Model page

- **New night column.** "Cats to enrol" gains a **New night** column whenever the setting is set and
  day/night is available. It counts each cat's crops at night after the instant, at the Build
  grades and geometry, before the cap. `enrollable_cats` takes the classifier and the instant and
  adds `new_night` to each row; with either one missing, the field is null and the column is hidden.
- **A resident at zero** is marked. Its title says its night places will hold old-lamp crops only.
  This is a warning, not a block: old-lamp crops still fill those places.
- **The Build card** notes, when the setting is set and "Max per cat" is blank, that every crop will
  be enrolled and night lighting priority has no effect.
- **With no location set,** the echo says the priority is off and why.

### Known limits

- **Location is not part of a job's identity**, as today. Changing it moves the day/night line and so
  the selection, without changing the dedup key.
- **Build caps before decoding and Validate caps after**, so a crop that fails to decode can make the
  two selections differ. This divergence already exists.
- **A Build cap other than 2000, 1000 or 500 has no forecast row**, as today.
- **The first double-LED night (8 Nov 20:30 – 9 Nov 06:30) ran brighter** (luma ~95–100) than every
  night since (~60–65). It counts as new.
- **Store Jihn has no new-lamp night crops yet**, so his night places will hold only old-lamp crops
  until some are labelled.

## Open questions

From the fresh-eyes review, and still open when the spec was parked:

- **Q:** Should a lower-grade double-LED crop go in ahead of a better-grade old-lamp one? As
  drafted it does, which contradicts compute/CLAUDE.md's "Protect the gallery" rule. It is also
  why a cat's night side can come out thinner than today's rule gives with wider grades.
  **Default:** grade first, as today. Only within the grade tier that overflows the cap are the
  places split day/night, with double-LED crops taking the night places before old-lamp ones.
  That also keeps ARCHITECTURE's "the cap enrols the best-graded crops per cat" true.
- **Q:** How does Validate show whether the priority helps? Its capped forecast scores every
  held-out visit together: about 100 old-lamp nights against 7 double-LED ones. Replacing old-lamp
  night crops with new ones will therefore read as neutral or worse. **Default:** for each capped
  row, also report night accuracy on visits after the instant. Each run's report and run list also
  show the instant it used, or "not applied". The alternative is to state on the card that the
  forecast cannot show the priority's effect.

## Alternatives considered

- **No cap at all: label the double-LED nights and rebuild uncapped.** It enrols every new-lamp
  crop, and every version so far was built that way. Under 1-NN, tonight's frames land on new-lamp
  crops before old ones, because the lighting matches. This is the step taken first; this spec is
  parked behind it (see Status).

- **A hard cut-off: old-lamp night never enrolled.** Rejected. A cat with few new-lamp crops would
  lose its night side, read as unknown at night, and unknown sets off the alarm.
- **A fixed night share the operator sets** (e.g. 50%). Rejected. It is one more number in every
  build's identity, and no single value fits a cat seen mostly by day as well as one seen mostly at
  night.
- **Weighting old-lamp crops lower.** Not available: 1-NN matching has no per-vector weight, so
  priority can only mean which crops get the places.
- **A date setting, cut at midnight.** Rejected; it would drop the evening of 8 Nov (see Key
  decisions).
