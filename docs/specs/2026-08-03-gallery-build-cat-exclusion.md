# Exclude a cat from a gallery build

A checkbox list of roster cats on the Model-building page, so a cat that is not yet
worth enrolling — Store Kali today: 17 gallery crops from a single visit, which
captured 2 of Sultan's 49 visits — can be left out of a build without being retired.
Retiring is the only existing mechanism and it is the wrong one here: it also removes
the cat from the annotation picker, so retiring Store Kali would stop the labelling of
the very visits we are waiting for. The selection travels as an **exclude-list** on the
build request, and the same list is offered on Validate, because otherwise the change
cannot be measured.

## Key decisions

- **Exclude-list, not include-list** (new). The wire carries `exclude_cat_ids` — the
  cats to leave out — so an empty/absent field means "enrol everyone" and a cat added
  to the roster later is enrolled by default. An include-list would silently drop a new
  cat from any repeated build. It also matches the intent: this is *temporary*
  exclusion, so the field should fail toward enrolling.
- **Filtered in SQL, mirroring `active_only`** (extends). `Store.labeled_crops` and
  `Store.count_identified_crops` both gain an `exclude_cat_ids` argument, the same shape
  as the existing `active_only` flag they already keep in lockstep. Filtering in
  `build_gallery` after the read would leave the pre-check unfiltered, which is exactly
  the divergence the next decision forbids.
- **The pre-check must apply it** (diverges). `/api/training/gallery/build`'s comment
  currently reasons that the cap needs no re-check because it "can only reduce crops per
  cat, never the number of cats". An exclusion breaks that premise — it can drop the set
  below the two-cat floor — so the guard has to count exactly what the build will embed
  (`CHANGELOG` 191).
- **Part of the artifact's identity** (extends). The exclusion lands in the job's dedup
  `params`, the artifact dir slug and the version's `metrics`, exactly as `max_per_cat`
  does (`CHANGELOG` 234). Without it, a gallery built without Store Kali is
  indistinguishable from one with it, and pressing Build twice with different selections
  silently drops the second.
- **Validate takes the same list** (new). A validation run forecasts the gallery you
  would build at those grades (`CHANGELOG` 207/208). It scores the labelled crops, not a
  gallery — so an exclusion applied only to the build is invisible to the number, and the
  errors the exclusion removes still appear. Parity is what makes the change measurable.
- **The row shows crops AND visits** (new). Recall tracks *visits*, not crops: measured
  over run 7, Store Jihn has the most crops of any cat (2725) and the worst recall (69%,
  13 visits), while Saffi is perfect on 112. A name alone does not support the decision
  the list exists for.
- **Counts follow the grade checkboxes** (extends). Store Kali has 50 labelled crops but
  17 at `gallery` grade. A list showing 50 beside a build that enrols 17 would have the
  operator deciding on the wrong number, so the list is fetched with the current grade
  selection and refetched when it changes.
- **Not persisted** (reuses). Like `qualities` and `max_per_cat`, the selection is a
  per-build parameter, not a stored setting. Default is every active cat checked, so a
  forgotten exclusion cannot quietly keep a cat out of every future gallery.
- **One cat list shared by Build and Validate** (diverges). The grade checkboxes are
  independent per panel; this one is not. Two independent lists would let a build and its
  validation silently score different cat sets — the reading that Goal 3 depends on.

## Goals

- Leave an under-represented cat out of a build while continuing to label it.
- Make the decision from the numbers that predict recall, in the place the decision is
  made.
- Let the effect be measured, so keep-or-roll-back is a reading rather than a guess.

## Non-goals

- **Replacing retire.** Retire still means "stop tracking this cat" — it leaves the
  annotation picker, the user dashboard and every build. This is the narrower
  "not enrolled *yet*".
- **Per-cat crop caps.** One global `max_per_cat` exists and stays global.
- **Excluding a cat at identification time.** This is a build-time choice; nothing reads
  the exclusion at Run time. An excluded cat is simply absent from the promoted gallery.
- **Persisting the selection**, or offering it anywhere except Build and Validate.

## Design

### The wire and the filter

`GalleryBuildRequest` and `FeasibilityRunRequest` each gain
`exclude_cat_ids: list[int] | None`. `null` and `[]` both mean "exclude nothing", the same
collapse `qualities` already applies. Duplicates are harmless. Both endpoints 400 on an id
that names no roster cat at all — a stale UI holding a deleted cat's id should be told,
not silently ignored — but an id naming a **retired** cat is accepted as a no-op, since
`active_only` already excludes it and rejecting it would make a harmless stale tick an
error.

`Store.labeled_crops` and `Store.count_identified_crops` gain
`exclude_cat_ids: tuple[int, ...] | None`, appending `AND (d.cat_id IS NULL OR d.cat_id
NOT IN (...))` to the existing WHERE. The `IS NULL` half matters for the same reason
`active_only` needs it: the join is a LEFT JOIN and a catless kind (`unknown_cat`) has a
NULL `cat_id`, so a bare `NOT IN` would drop every catless crop — which is not an
excluded cat's crop (`CHANGELOG` 192).

`build_gallery` and `run_feasibility_probe` pass it through to their `labeled_crops`
call. Neither grows any other logic: an excluded cat's crops simply never arrive.

### The two-cat floor

Exclusion is the first build parameter that can reduce the *cat* count, so:

- The endpoint's pre-check calls `count_identified_crops(qualities, active_only=True,
  exclude_cat_ids=…)` and returns the existing `enough: False` empty-state when the
  result falls under 2 crops or 2 cats.
- The message names the cause, since "not enough labelled data" would be misleading when
  the operator has plenty and has merely deselected too much.

`build_gallery`'s own cold-start guard already re-checks post-filter and needs no
change; it becomes the second line rather than the first.

Excluding a **resident** is allowed, with no guard and no confirmation. A resident with
too few visits to enrol is precisely the case this exists for — a newly added cat held
back until it has crossings — and the household knows its own cats better than a
threshold would. The row carries `is_resident` so the choice is made informed rather
than prevented.

### Artifact identity

`enqueue_gallery_build`'s `params` becomes the triple
`(qualities, max_per_cat, exclude_cat_ids)` — with the ids **sorted** in the key, so
unticking two cats in either order is one job rather than two. The artifact dir slug gains
a `-ex<n>` fragment (a count, not an id list — the dir name is a human handle, and the
exact ids live in the version's metrics), appended **after** the existing `-max<cap>`
fragment so the slug stays `<ts>-<grades>[-max<cap>][-ex<n>]` and two builds' dir names
are comparable. `metrics["excluded_cat_ids"]` records the ids themselves, so a version row
says what it left out.

The version list's Grades cell, which already carries the cap, names the excluded **cats**
— resolved from those ids against the roster, not shown as a count. A count cannot be
compared between two builds, and comparing two builds is the reason the exclusion is in
the artifact's identity at all. An id whose cat no longer exists renders as `#<id>`
rather than being dropped, so a row never under-reports what it left out.

`feasibility_runs` needs no schema change: the `metrics` JSON column added for the
visit-held-out block carries `excluded_cat_ids` alongside it. The validation-run list names
the excluded cats the same way the version list does — a run that scored a different cat
set is not comparable with one that scored the whole roster, and that has to be visible in
the row rather than only in the stored JSON.

### The checkbox list

A new `GET /api/label/enrollable` returns, per **active** roster cat:

```
{cat_id, cat_name, is_resident, crops, label_commits}
```

It takes `qualities` as a repeated query param (`?qualities=gallery&qualities=ok`), matching
how the grade selection is already spelled elsewhere; absent means **all grades**, the same
`None`-is-no-filter convention `labeled_crops` uses.

`crops` is that cat's `identified` crops with a materialised crop file at the requested
grades — the same universe `labeled_crops` enrols. `label_commits` is the count of distinct
`labeled_ts` values, because `add_dataset_items` stamps it once per commit and one label
keypress commits one visit. It stands in for a visit count and is an approximation:
measured over run 7 it read 330 against the gap-clustered 294, ~12% high. The field is
named for what it actually counts, in the wire and in the UI, rather than for the thing it
proxies — a field called `visits` would invite exactly the over-trust the caveat warns
against.

Retired cats are omitted entirely — they are already excluded by `active_only`, and
showing them as unchecked-but-present would imply ticking one enrols it.

The panel renders one row per cat, all checked, with the counts beside each name.
Unchecking sends that cat's id in `exclude_cat_ids`. Changing the grade checkboxes
refetches the list so the counts keep matching what a build would enrol.

**One list drives both Build and Validate.** This deliberately diverges from the grade
checkboxes, which are independent per panel (`getQualities('b')` vs `getQualities('v')`).
The reason is Goal 3: with two independent lists you can build excluding Store Kali, then
validate *without* excluding it, watch the number not move, and conclude the exclusion
didn't help — having actually measured a different cat set. The version list and the
validation-run list both name their exclusions, so such a mismatch is discoverable after
the fact; sharing the list makes it unreachable instead, which is worth one inconsistency
with how grades work. It remains page state, not a stored setting: a reload resets to every
cat checked.

### What an excluded cat does at Run time

Nothing reads the exclusion. The cat is absent from the promoted gallery, so its visits
resolve to *unknown cat* via the existing threshold path — the same outcome as before it
was ever labelled, and the correct one for a non-resident. The learned `subject_floor`
stamped on the version is deliberately **not** filtered: it measures whether motion is
cat-sized, not which cat, so an excluded cat's visits remain valid evidence for it.

## Alternatives considered

- **Retiring the cat**, as today. Global: it also removes the cat from the annotation
  picker, so it blocks the labelling that the exclusion is waiting for. Rejected on that
  alone.
- **An include-list** (`cat_ids`). More explicit at the call site, but a cat added later
  is silently omitted from any repeated build — a failure that is invisible until a
  gallery is missing a resident.
- **A per-cat minimum-visits threshold** applied automatically at build time. Removes the
  decision entirely, but picks a number nobody has evidence for, and would silently
  change which cats are enrolled between builds as counts cross it. The operator deciding
  from visible counts is both simpler and auditable.
- **Extending `cat_regime_coverage` instead of a new endpoint.** It already returns
  per-cat `{cat_id, cat_name, is_resident, active, total, day, night}` and would need only
  a `qualities` filter and the commit count. Rejected because the two answer different
  questions — that one exists to show which cat needs *night* data, and is consumed by the
  Cats page for exactly that — and folding "is this cat ready to enrol at these grades"
  into it would leave one endpoint with two contracts and a grade filter that silently
  changes what the Cats page reports.

## Implementation strategy

*Not part of the design — a starting point for whoever builds this.*

- **Single agent, Opus 5.** One field threaded end to end — `labeled_crops` /
  `count_identified_crops` → `build_gallery` / `run_feasibility_probe` →
  `enqueue_gallery_build` → both endpoints → the shared panel state. Each step consumes the
  one before it, so there is nothing to parallelise; splitting it would just have two agents
  guessing at the same signature.
- Opus rather than Sonnet for two spots that need interpreting rather than transcribing:
  the pre-check is the first build parameter that can breach the two-cat floor, and the
  dedup key / dir slug / metrics triple is where a wrong choice makes two builds
  indistinguishable.
- No schema change and no new dependency, so the whole thing is revertible by dropping the
  commit.
