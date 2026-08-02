# Unanalysed visits: an honest state, and a one-tap analyse + identify

Two changes to the same surface — the Activity feed on both dashboards. First, a
visit whose frames YOLO has never looked at stops claiming to be `unrecognized`
and gets its own `unanalyzed` subject. Second, such a visit gains a single button
that runs detection and identification over just that visit, as one server-side
job.

The gap it fills: `Store._classify_subject` (`compute/collection/store.py:4194`)
reaches the `unrecognized` rung whenever `class_conf` is empty — and that dict is
empty both when YOLO swept the span and found nothing *and* when YOLO never ran on
it. The two are opposite readings (a measured miss vs. no measurement), and the
feed renders them with the same violet chip. That conflation is the reason the
button is needed at all: today the only way to analyse one visit is a windowed
sweep from the admin Motion-tuning page, which nobody does for a single card, and
which the user dashboard has no access to at all.

## Key decisions

- **`unanalyzed` is a new rung in the subject ladder** (extends). Added to
  `_classify_subject`, firing when the span carries no `yolo-serial` row at all.
  Its input is free: `events()` already materialises every such row per span
  (`store.py:3951`), so "was anything swept" is `len(rows) > 0` — no extra query.
- **It sits ABOVE `corrupted`, below `cat`/`person`** (extends). `corrupted`'s
  contract is "YOLO detected NOTHING *and* a glitch explains the motion"
  (`store.py:4246`); an unswept span cannot support the first clause. Ranking
  `corrupted` below the new rung keeps the entry-226 rule — never claim the
  detector rejected frames it never saw. Rungs 1–2 are naturally unreachable when
  unswept.
- **Wire value `unanalyzed`, UI text "not analysed"** (reuses). The payload
  vocabulary is American (`analyzer`, `analyzed`, `reanalyze`, and the existing
  `unrecognized` kind); the operator-facing wording is British throughout
  admin-next ("NOT ANALYSED", "partly analysed"). Same split as entry 159.
- **The button is one server-side job, not a client-driven chain** (new). A new
  `TrainingManager` kind — `visit-identify`, params `(start_id, end_id)` — whose
  body is `LiveIdentifyManager._tick`'s per-span pair (`live_identify.py:333-350`):
  `run_analysis(yolo-serial, lo, hi)`, then `run_identify(lo, hi)`. One POST, one
  job, and it survives the phone locking mid-run — which a client-side chain, the
  obvious cheaper option, does not.
- **No "did YOLO find a cat" conditional** (reuses). `run_identify` drives off
  `Store.iter_unidentified` (`store.py:6032`), which yields only frames carrying a
  present `yolo-serial` verdict. With no cat it visits zero frames. The condition
  in the original request is already structural — writing it out would be a second,
  drift-prone copy of the rule.
- **It rides the `TrainingManager`, not a new queue** (reuses). That queue already
  owns `identify`, and both always-on workers already yield the GPU to it via
  `is_busy` (`app.py:895`). A fourth kind costs one dispatch branch
  (`runner.py:393`); a fourth manager would need its own busy-wiring on both
  workers.
- **No active model ⇒ detect only, still a success** (diverges).
  `POST /api/identify/run` 409s without a promoted gallery (`app.py:2719`). Here
  the detect half is the half that resolves `unanalyzed`, and it is useful with no
  gallery at all — so the endpoint runs it and reports that naming was skipped.
- **The span is required and width-capped** (new). Both bounds mandatory, and a
  span wider than `_MAX_VISIT_SPAN` ids is rejected. `since_id=None` means
  "whole store" everywhere else in this API; on a no-auth LAN endpoint that the
  household app calls, an omitted bound must not silently enqueue a full sweep.
- **`unanalyzed` is exempt from the user feed's noise filter** (diverges). The
  other two floor-derived kinds are hidden with `showAll` off
  (`user/index.html:1063`); this one always shows. It is the only kind with an
  action attached, and hiding the visits whose button you want tapped defeats the
  feature. Accepted cost: with the oracle worker off, every new visit shows a
  "not analysed" chip until something sweeps it.

## Goals

- A visit YOLO has never looked at says so, on both dashboards, distinctly from
  one it looked at and found nothing in.
- From either dashboard, one tap analyses and names a single visit, with no
  admin sweep and no knowledge of frame ids.
- The work survives the tab closing, the phone locking, and a compute restart
  mid-job (as a re-runnable no-op, not a wrong result).

## Non-goals

- Backfilling unanalysed visits in bulk. That is the always-on oracle worker
  (forward-only by design, entries 142/149) plus a manual Motion-tuning sweep.
- Re-analysing an already-swept visit. `reanalyze` stays an admin sweep
  concern; this button fills missing verdicts only.
- A per-visit progress bar on the user dashboard. Progress lives where job
  progress already lives — the admin Model page's Jobs table.
- Changing `activity_signal` / the SSE push. See *After it finishes*.

## Design

### The `unanalyzed` subject

`_classify_subject` gains a `swept: bool` parameter and one rung:

```
1. cat          → {kind: 'cat'}
2. person       → {kind: 'person', conf}
3. NOT swept    → {kind: 'unanalyzed', peak_area, n_frames}
4. corrupted    → {kind: 'corrupted'}
5. peak_area >= floor.min_area OR n_frames >= floor.min_frames
                → {kind: 'unrecognized', peak_area, n_frames}
6. else         → {kind: 'motion_only', peak_area, n_frames}
```

`events()` passes `bool(subj_by_event[i])` — it already has the rows in hand.
`peak_area`/`n_frames` ride along so the new kind renders with the same motion
readout as the two rungs below it.

Coverage is deliberately binary: **any** `yolo-serial` row in the span counts as
swept. A partly-swept visit therefore reads as analysed. `_resolve_flag` splits
`partial` from `unswept` (`store.py:5148`), but it can afford to — it already
reads the span's live frames to build its record, while `events()` would need an
extra per-span count on the shared write connection for every event on the page.
The existing `detection.ratio` (`store.py:4013`) already deflates on a partial
visit, so the honest signal survives in the readout the cards render.

`swept` counts rows over the **whole span**, not just its motion frames — matching
what the button sweeps. That differs from `detection.ratio`'s denominator (motion
frames only, `store.py:4011`), so a span swept only on its non-motion frames is
`swept` with `ratio: null`: a real subject chip above a "—" detection rate. Rare
and correct in both halves; do not "fix" it by switching `swept` to
`n_swept_motion > 0`, which would call a span unanalysed that the button cannot
add a single verdict to.

The identity-promotion rung in `events()` (a confident named match promotes a
non-cat subject to `cat`) runs after this and still outranks it, so a visit that
somehow holds identifications without verdicts — `reanalyze` clears `analysis`
but not `identifications` — shows its cat rather than a "not analysed" chip.

### The `visit-identify` job

`_Job(kind="visit-identify", params=(start_id, end_id), label="visit identify")`,
dispatched in `_run` (`runner.py:393`) to a new `_run_visit_identify`:

1. `run_analysis(store, analyzer, DetectAdapter(self.stop_event), since_id=lo,
   until_id=hi)` — fill-missing (`reanalyze=False`), **not** `motion_only`: a cat
   often pauses at the flap, and those calm `motion=0` frames identify best (the
   live worker's reasoning, `live_identify.py:267`).
2. `store.active_model()` — `None` ⇒ return with `identified=False`, no error.
3. `run_identify(...)` with the same progress+cancel callback `_run_identify`
   builds (`runner.py:611`).

Returns `{kind, n_identified, identified, since_id, until_id}`.

Torch reaches this module the same way it reaches `live_identify`: an
`analyzer_factory` seam defaulting to `lambda: get_analyzer("yolo-serial")`, plus
`detect`/`identify` callables, so the queue stays exercisable with fakes on the
GPU-less dev box and importing `runner.py` stays torch-free.

`DetectAdapter` is progress-less, so the Jobs row shows counters only during the
identify half. Accepted: a visit span is tens of frames, seconds of detect. Both
halves honour `self.stop_event`, so Cancel works throughout, and both are
resumable — a cancel or a crash leaves partial verdicts that the next run fills.

Dedup is `(kind, params)` against the **running** job only (`runner.py:263`), so a
double-tap on the visit being processed collapses. A second tap while some *other*
job runs enqueues a duplicate; harmless, because the re-run finds nothing missing.

### `POST /api/identify/visit`

Body `{start_id, end_id}`, both **required** request fields — `_validate_bounds`
(`app.py:712`) only rejects an inverted range, it permits a `None` side, so
presence is a separate guard here. Then `end_id - start_id <= _MAX_VISIT_SPAN`
(10,000 ids — ~16 minutes of continuous capture at the ~10 fps this door actually
runs, entry 275, so it clears any real visit by a wide margin while refusing a
store-wide span).

The deps check follows the halves, not the endpoint: `get_analyzer("yolo-serial")
.ensure_available()` (`yolo.py:157`) always → 503, and `Embedder()
.ensure_available()` **only when a model is active**, since that is the only case
where the identify half runs. Gating on the embedder unconditionally, as
`/api/identify/run` does, would refuse the detect-only job that the no-model
decision above exists to allow.

Returns the enqueue snapshot plus `will_identify: bool` so the UI can say which
halves will run.

### Both dashboards

Chip and colour are duplicated per file — no shared CSS or JS between the two
front doors (entry 80). `unanalyzed` gets its own token, visually distinct from
`unrecognized`'s violet, in `SUBJECT_LABEL`/`SUBJECT_CLASS`
(`admin-next/index.html:708`) and `subjectKind`/`subjectCssClass`
(`user/index.html:1048`). Admin's `KIND_NAME` map (`admin-next/index.html:3534`)
gains the job kind too, or the Jobs row renders it as `undefined`.

`unanalyzed` is added to neither dashboard's noise set, so it survives both
toggles and shows by default (see the key decision).

The button sits in the playback modal on both: beside `#playerFlag` in the user
footer (`user/index.html:861`), and in the admin player, where the deleted old
admin had exactly this control per event (entry 107). It renders **only** on an
`unanalyzed` visit — on an already-swept one the job is a no-op, and a button that
does nothing is worse than no button.

With no promoted gallery the button still shows on both dashboards, worded for
what it will actually do ("Analyse this visit" rather than naming anything), from
the endpoint's `will_identify: false`. Through Phase 1 that is the normal state,
so a button that waited for a model would be a button nobody ever sees.

### After it finishes

`activity_signal` (`store.py:2330`) moves on new motion frames, a new
`identifications` row, or a promotion. So a job that **names** a cat does push the
feed over SSE; one that detects **nothing** does not, and the
`unanalyzed → unrecognized` transition would sit unseen until the next reload.

Adding an `analysis` counter to the signal is the obvious fix and is wrong: the
always-on oracle writes verdicts continuously, so that counter would nudge every
connected client every tick. Instead the client that pressed the button refetches
the feed once the job leaves `/api/training/status`, best-effort — if the phone
locks first, the existing foreground refresh (entry 86) covers it. The durable
outcome is the verdict rows, not the repaint.

The client recognises its own job by `(kind, params)` — `visit-identify` plus its
own span — across the status snapshot's `running` fields and its `queue`
(`runner.py:654`), and stops watching when neither holds it. Matching on `running`
alone would call the job finished while it was still queued behind another.

## Alternatives considered

- **Client chains `/api/analysis/run` then `/api/identify/run`.** No backend
  change at all. Rejected: the chain breaks when the tab closes or the phone
  locks — exactly the surface it is worst on — and it would be written twice,
  once per dashboard, with no shared JS to hold the sequencing rule.
- **An on-demand span queue on `LiveIdentifyManager`.** Cheapest per tap, since
  that worker holds a resident detector and embedder. Rejected: a tap would do
  nothing while the worker is toggled off — invisible to the household — and it
  blurs the worker's forward-only "names NEW visits" contract.
- **A sibling coverage field instead of a new subject kind.** Leaves the subject
  taxonomy (and its spec and tests) untouched, but makes every renderer compose
  two fields to draw one chip, in two files that share no code.
- **A `partial` state beside `unanalyzed`,** mirroring `_resolve_flag`. Rejected
  for the read cost: a live-frame count per span, on the shared write connection,
  for every event on the page — the contention class entries 102–105 removed.

## Implementation strategy

*Not part of the design — a starting point for whoever builds this.*

- **Single agent, Opus 5.** Five files, but one thread: the new subject kind and
  the endpoint's `will_identify` are the contract both dashboards render, so a
  fan-out would have every stream waiting on the same decisions. The two HTML
  files are genuinely independent of each other, but neither can be
  browser-verified until the backend runs, so splitting them buys coordination,
  not wall-clock.
- The judgment is concentrated in one place, which is why it stays on Opus: the
  new rung lands in a ladder with existing tests that assert the OLD answer —
  `test_subject_no_yolo_rows_above_floor_is_unrecognized` and
  `test_subject_no_detection_no_corruption_falls_back_to_unrecognized`
  (`compute/tests/test_event_subject_classification.py:219,291`) both describe
  spans that are now `unanalyzed`. Those are the change landing correctly, not
  regressions — but each needs reading before it is rewritten.
