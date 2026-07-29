# Cats roster page

A new `#cats` page in admin-next that owns the household roster: rename a cat,
flip resident/foreign, retire one, and edit its notes — plus the per-cat crop
and last-seen readouts that are currently scattered. The backend already
supports every edit (`PATCH /api/cats/{cat_id}`, `compute/api/app.py:2022`); it
has no callers, so today a rename needs SQL. This page is mostly the missing
frontend, plus giving `active` real consumers so "retire" means something.

## Key decisions

- **Flat editable table, not master-detail** (new). One `#cats` route rendering
  one row per cat, reusing the existing `.qtable` styling
  (`admin-next/index.html:280`). Every stat named so far is one value per cat,
  so the shape is a table; a per-cat detail panel is a refactor to do when a cat
  grows a genuine sub-view (crop gallery, visit history), not before.
- **No new read endpoints** (reuses). `GET /api/cats/overview` already returns
  the roster + `last_seen_ts`, and
  `GET /api/label/regime-coverage` already returns per-cat `total`/`day`/`night`
  and `active`. The page issues those two calls and joins `overview.id` to
  `regime-coverage.cat_id` — the two payloads name the key differently.
- **`notes` becomes editable** (extends). The column exists (`store.py:542`) and
  has never been written. `CatUpdateRequest` and `Store.update_cat` gain
  `notes`; it is the slot for per-cat detail that isn't worth a schema change.
- **Retire filters the annotation picker and the user Cats view** (extends).
  Both currently show every cat. The user dashboard already *tries*
  (`user/index.html:1514`) and fails — see Design.
- **Retire filters gallery build and the feasibility probe** (extends).
  `Store.labeled_crops` gains `active_only=False`; `gallery.py:77` and
  `probe.py:213` pass `True`. Default `False` keeps the store method's existing
  contract byte-identical, so the two opt-ins are explicit and greppable.
- **Roster management leaves the Annotation page** (diverges). Add-cat and the
  day/night coverage card move to `#cats`. The digit picker stays — it is the
  keybinding legend, not a management control.
- **Retire is reversible and lands only on a rebuild** (reuses). Un-retiring is
  the same checkbox. A retired cat keeps its labels and leaves identification
  only when the operator builds and promotes a new gallery — the deliberate
  action that already gates model changes. Until then the promoted gallery
  still names it, on **new** visits as well as historical ones.

## Goals

- Rename a cat, flip resident/foreign, retire, and edit notes without SQL.
- Make `active` mean "stop offering and stop enrolling this cat", consistently.
- Put the per-cat crop coverage and last-seen where roster management happens.
- Leave room for future per-cat stats as columns.

## Non-goals

- Deleting a cat. Retire is the exit; `dataset_items.cat_id` must keep resolving.
- ~~Avatar management of any kind — no upload, no thumbnail column.~~
  **Reversed after the first build, at the user's request.** The roster now has a
  Photo column that sets, replaces, and removes a cat's avatar, reusing the
  existing avatar endpoints. The original reasoning ("a photo earns little on a
  management table") was wrong: when you are renaming and retiring rows, the photo
  is how you tell which cat a row *is*.
- Merging two cats, or bulk-reassigning one cat's labels to another.
- Changing what the user dashboard's Cats view *looks* like.

## Design

### The page

A seventh entry in `ROUTES` (`admin-next/index.html:667`), `{ id: 'cats', title:
'Cats', sub: 'The household roster' }`, mounted with the same
`mount(view) → teardown` contract as the other six. Placed after `annotate`,
before `model` — roster edits feed annotation and precede a build.

One card holding the table, one card holding add-cat. Columns:

| Key | Name | Resident | Active | Notes | Crops | Day / Night | Last seen |
|---|---|---|---|---|---|---|---|

`Key` shows the 1–9 digit binding, computed over the **active** cats in id-ASC
order so it matches the Annotation picker exactly; retired rows show nothing.
`Name` and `Notes` are text inputs; `Resident` and `Active` are checkboxes.
`Crops`, `Day / Night` and `Last seen` are read-only, from the two GETs. Rows
are id-ASC (creation order, never reordered — the table stays a stable mirror of
the roster) with retired rows dimmed in place.

Editing: text fields PATCH on blur, checkboxes PATCH on change, each sending
only its own field. Enter blurs the field rather than submitting separately, so
one edit is one request. On failure — a duplicate name is a 400 from the UNIQUE
constraint — the field reverts to the server value and the row shows the error
text, so the table never displays an edit that didn't persist. A successful
PATCH returns the updated row, which replaces that row's state.

Retiring needs no confirmation: it is one checkbox away from undone, and it
changes nothing live. The Active column carries a hint that a retired cat leaves
identification at the next gallery build.

Add-cat (name + resident checkbox) moves here verbatim from
`admin-next/index.html:1950`.

### Making `active` real

Three consumers, none of which work today:

**The annotation picker** (`admin-next/index.html:2036`) renders all of
`/api/cats`. It filters to `active` cats before building the digit list.
Consequence: retiring a cat shifts the digits of every cat after it. Accepted —
retirement is rare and deliberate, and a departed cat holding key `2` forever is
worse. The empty-state message ("No cats yet — add one below") no longer has a
"below" to point at, and must now distinguish two cases it never could before:
an empty roster, and a roster whose every cat is retired. Both link to `#cats`;
only the second says why the picker is empty.

**The user dashboard Cats view** (`user/index.html:1514`) already reads
`.filter(c => c.active !== 0)`. `_cat_to_dict` (`store.py:5549`) emits `active`
as a JSON **boolean**, so `false !== 0` is `true` and nothing is ever filtered.
The fix is `.filter(c => c.active)`. This is a latent bug independent of this
page; it is in scope because the page is what finally makes retiring possible.

**Gallery build and the feasibility probe** both call `Store.labeled_crops`,
which LEFT JOINs `cats` and ignores `active`. It gains an `active_only: bool =
False` parameter adding `AND c.active = 1`; `gallery.build_gallery` and
`probe` pass `True`. Default `False` means no existing caller changes behaviour
by omission. Retiring every cat now makes a build reach the existing
not-enough-data path without a single label being deleted — already handled, but
newly reachable.

Note the asymmetry, which the page should not hide: `is_resident` takes effect
**immediately** (it is read at event time via the `events()` join, so a chip
changes colour on the next load), while `active` waits for a rebuild. The two
checkboxes sit in adjacent columns and behave differently on purpose — resident
is a fact about the cat, retirement is a change to what the model enrols.

`Store.list_cats` and `cats_overview` keep returning retired cats — this page
needs them to un-retire, and the docstring already states that filtering is the
caller's job (`store.py:5561`).

### What moves off Annotation

The Roster card keeps only the picker buttons. The add-cat form and the entire
"Per-cat day/night coverage" card (`admin-next/index.html:1957`, fed by
`/api/label/regime-coverage`) move to `#cats`, where the same fetch now also
feeds the table's Crops and Day/Night columns.

## Alternatives considered

- **Master-detail cat profiles.** A roster list plus a per-cat detail panel with
  avatar, gallery membership, and recent visits. Rejected for now: it spends
  selection state and a second layout on six rows whose every attribute is a
  single value. Revisit when a cat needs a sub-view.
- **Leave `active` cosmetic.** Ship the editor, let retire stay a stored flag
  nobody reads. Rejected — a control that does nothing is worse than no control,
  and the user dashboard already has a broken attempt to honour it.
- **Filter retired cats at read time in identification.** Drop matches to
  retired cats when resolving an event, rather than at gallery build. Rejected:
  it silently changes live naming the instant a checkbox flips, where the
  build-time filter is auditable and gated behind promote.

## Implementation strategy

*Not part of the design — a starting point for whoever builds this.*

- **Single agent, Opus 5.** Six files, but one thread: the `active` semantics
  run from `store.labeled_crops` through `gallery.py`/`probe.py` to two
  frontends, and the new page depends on the `notes` support landing first.
  Splitting it would mean agents guessing at each other's half of the same
  decision.
- The builder has to *interpret* the `(extends)` decisions — which caller opts
  into `active_only`, how the picker's two empty states read — rather than
  transcribe them, so this isn't Sonnet-tier mechanical work.
