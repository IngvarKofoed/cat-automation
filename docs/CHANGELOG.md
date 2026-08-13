# Changelog

Each entry is numbered with a monotonically increasing integer. Append new entries to the end. Never reuse or reorder numbers. Numbers are globally unique across this file and `docs/CHANGELOG-archive.md` — never reused. Write each entry as durable project memory: what is now true that wasn't before, plus the why in a clause when not obvious — not a recap of the diff (filenames and mechanical edits live there). Keep it to 1–5 lines, ~20 words per line at most; never one packed run-on line.

Entries 1–221 are archived in `docs/CHANGELOG-archive.md`, summarized as milestones below. Numbered
entries resume at 222. When an increment completes, move its per-task entries there and leave a
milestone here.

## Milestones (entries 1–221, archived)

**M1 — The thin edge node** (entries 1–15). The Pi is a pure HTTP server: `/stream` writes *every*
frame continuously as MJPEG, `/frame` serves stills, and MOG2 motion is a separate *pulled* signal
(`GET /status`, `X-Motion`/`X-Area` part headers) that gates the compute's GPU cost, never frame
delivery. Frames are rotated + cropped to the door ROI on the Pi. Module-3 focus is lockable
(`null` = continuous AF, a number = manual dioptres). `shared/wire.py` owns the frame wire format so
the two tiers cannot drift; `edge.sh` bootstraps the venv (system-site-packages on Linux for apt's
picamera2) and bakes `git describe` into `CAT_EDGE_VERSION`.

**M2 — Frame store + offline gate validation** (16–56). Compute stores every edge frame in a
byte-capped, SQLite-indexed ring (WAL, `synchronous=NORMAL`) and validates the edge gate offline:
`yolo-serial` is the trusted oracle, batched `yolo` over-detects, BSUV is CUDA-bound, and `MotionGate`
is shared so an offline re-run cannot drift from the live gate. `gate_scorecard` headlines **visit**
recall — one caught frame wakes the GPU, so a wholly-missed visit is the only miss that costs a
trigger. Sweeps drain a serial FIFO queue, resumable and cancelable.

**M3 — The learning loop's first build** (57–79). Per-visit keyboard annotation writes durable crops
plus `dataset_items`; `cats`, `dataset_items` and `model_versions` survive eviction *and* `clear()` —
labels are the precious output. A DINOv2 probe scores separability; gallery build → promote keeps
exactly one active version, and the identify pass stores per-frame nearest-neighbour distance with the
threshold applied at **read** time so it stays tunable. An uncalibrated (NULL-threshold) gallery names
nobody, and a live worker names new visits without back-identifying history.

**M4 — Two front doors** (80–113). `/admin` and `/` are separate single-file SPAs sharing **no CSS or
JS** — only `/api/*` and `/media/*` — so a helper must be duplicated per file or the two disagree. The
user app is an installable home-screen app with an SSE-pushed Activity feed, a Cats roster with
uploadable version-stamped avatars, event `subject` classification (cat / person / unrecognized /
motion_only) beside identity, and per-frame YOLO box overlays in playback. Entries 102–105 removed the
first collector-starvation class: whole-store scans on the shared write connection.

**M5 — Edge actuators + night light** (114–120). Three GPIO relay channels (BCM 26/21/20) expose raw
HIGH/LOW level, not on/off — the board is active-low, so they boot HIGH (released). An autonomous
astronomical night-light schedule drives a channel from sunset to sunrise with configurable offsets,
computed offline via `astral`. Edge-side deliberately: fixed camera illumination must survive a
compute-PC outage, unlike the deferred intent-based Control API.

**M6 — The admin-next rebuild** (121–221). The workbench was rebuilt page by page beside the old one,
then flipped: `/admin` serves the rebuild and the old console is deleted, with buckets (superseded by
the tuning calendar) and corruption review dropped by decision — their endpoints survive, with no UI.
Six pages: Start, Motion tuning (4-week calendar picker, sweeps, MOG2 re-runs, day/night scorecards,
missed-visit review attributing each miss to a knob), Frame review, Annotation, Model building,
Activity. Conventions later entries assume: a sweep's watermark re-seeds to the frame horizon on every
`start`; `yolo-serial` is renamed only in presentation (it is the stored `analysis.analyzer` value);
`recv_ts` monotonic with `id` is assumed, never enforced; and an unmeasured window must never read as
a clean one.

222. The household can now MARK a visit for labelling from the user app's playback modal
     (⚑ on the card); admin `#annotate` gained a Flagged mode to work the marked set.
     New `label_flags` table keyed by the EVENT's frame span — span-keyed is what makes a
     `motion_only`/`unrecognized` visit flaggable at all, since with no YOLO box it can
     never enter the annotation queue. Spec: docs/specs/2026-07-30-user-flag-for-labelling.md.

223. A flag is a work item, not output: resolving one deletes its row (no history), and
     `clear()` drops the table since frame ids restart at 1. Eviction does NOT cascade —
     a flag whose frames aged out reads `gone` and waits to be dismissed, because a mark
     that silently disappears hides that the work was lost to retention.

224. Flag identity is span OVERLAP on both sides — the client draws the ⚑ for any flag
     overlapping an event, and `add_label_flag` dedups the same way. An event's motion
     cluster GROWS as later frames arrive, so an exact `(start_id, end_id)` key would mint
     a second flag on a re-tap and leave un-mark with no target. So un-mark takes the SPAN
     (`POST /api/label/flags/unmark`) and clears every overlapping flag.

225. Flagged spans are UNFLOORED, diverging from the queue's `_ANNOTATE_MIN_CONF` (0.3):
     YOLO runs recall-first at 0.15, so a faint 0.2 cat box is a real crop the queue hides.
     The floor keeps phantom empty-scene detections out of the BULK queue (entry 73) — a
     human pointing at one visit is not that case, and a faint crop still grades `poor`,
     so a quality-filtered gallery build skips it anyway.

226. A flagged visit's state is derived from COVERAGE (`n_swept` vs `n_live`), never from a
     verdict merely existing — partial sweeps are the norm (entries 76, 142, 149), and
     calling one `no_detection` would claim the detector rejected frames it never saw.
     Analyse is offered only where coverage is incomplete, and sweeps the span WITHOUT
     `motion_only`: coverage counts every live frame, so a motion-only sweep could never
     clear `partial` on a keep-all store.

227. A flagged decision runs on the annotation page's existing serial `writeChain` and
     clears the flag ONLY when the write recorded ≥1 row. `/api/label/relabel` answers 200
     with `inserted: 0` once a span's frames have evicted — the routine aging path, since
     flags are never pruned — so deleting unconditionally would discard both the mark and
     the decision silently. `d` dismisses the flag ALONE, leaving any label untouched.

228. The user app's flag toggle reports onto the modal readout ONLY while its own event is
     still on screen, and re-enables the button for whatever IS. Prev/Next repoints that
     shared button at another event, so a settling write repainted a lie there — "Marked ✓"
     over an unflagged visit, whose next tap then ADDED a mark the user meant to remove.
     A failed write also restores the flag delta, not a whole snapshot that could undo a
     flag made elsewhere meanwhile.

229. `_resolve_flag` hints `+analyzer` to de-index that term. With no ANALYZE stats SQLite
     preferred `idx_analysis_analyzer_verdict` over the `frame_id` range and scanned every
     verdict for the oracle — O(analysis) PER FLAG, the opposite of the per-span read the
     design chose. Measured at 1.5M verdicts: 65 ms → 0.4 ms, flat in store size.
     The same query shape elsewhere (e.g. `events()`) still carries the mis-plan.

230. Corrected the root CLAUDE.md claim that `code-review`'s verifiers "default to the mid
     model": the built-in script sets NO `model:` anywhere, so every stage runs on the
     session model. A `high` run over this ~1000-line diff spent ~743k subagent tokens and
     hit the weekly limit before ANY finder returned — and reported `findings: []`, which
     reads as "clean" rather than "never ran". Tier the verify fan-out by hand; note the
     script is a per-run copy, and re-tiering a COMPLETED agent voids its resume cache.

231. Gallery build takes an optional PER-CAT CAP (`max_per_cat`, blank = uncapped), because
     the door makes the dataset permanently skewed — a resident crosses many times a day,
     a neighbour visits occasionally — and no amount of further collection fixes a ratio the
     door itself sets. 1-NN matching makes imbalance milder than for a classifier but not
     harmless: the dominant cat's vectors blanket more of the embedding space, and the
     suggested threshold is calibrated from a distribution its pairs dominate.

232. `cap_per_cat` picks best-grade-first (gallery → ok → poor → ungraded) and, where a tier
     overflows the budget, samples it EVENLY OVER TIME rather than from the front: a
     contiguous run of crops is one visit in one light, the least useful thing to fill a
     gallery with. Time order is `src_frame_id` order (ids are assigned on receive), so no
     extra column is needed. It discards no labels — a later build may enrol different crops
     — and can never drop a cat, which is why it runs before the cold-start guard.

233. The annotation queue gained "Hide confident matches" (`uncertain_only`), the filter
     ARCHITECTURE always described but the code never had. Queue membership is UNDECIDED
     (no `dataset_items` row), NOT uncertain — so a good model does not shrink the queue at
     all, and weeks of correctly-named visits bury the few worth attention. `uncertain` was
     already computed per visit and merely PRINTED; it is now a filter, applied server-side
     BEFORE the page cap (after it, confident visits would still eat the 100 slots).
     `hidden_confident` is reported, so a short or empty queue never reads as "nothing left".

234. Gallery-build's job `params` is now the `(qualities, max_per_cat)` PAIR, so the cap is in
     the dedup key: changing only the cap and pressing Build again is genuinely different
     work, and with the cap outside the key the double-click guard silently dropped it.
     The cap also lands in the artifact dir name and on the version row's metrics — with
     `n_vectors` alone, a gallery capped at 40 is indistinguishable from one whose cats only
     ever had 40 crops.

235. Two bugs the browser test caught, both in the new toggle's own wiring: `loadQueue` had no
     sequence guard, so a slow earlier fetch overwrote a newer filtered one and the toggle
     appeared not to work (mount's own load raced it); and leaving focus on the checkbox made
     `onKey` swallow the operator's next label silently, since it ignores keystrokes from
     inputs — so the control now blurs itself. Rather than exempt checkboxes from that guard:
     Space is bound to skip AND toggles a focused checkbox, so it would fire both.

236. The after-edits review now PREFERS `/fix-code --fix` — it rates findings 1-5, has a verifier
     refute each before repairing, and is undoable — keeping the inline self-run pass as fallback:
     a missing skill changes who reviews, never whether. New pre-commit gate: a commit request
     over changes no `--fix` run has seen asks once first, delegated `/git commit` included.
     The end-of-turn nudge now points at a user-run `/fix-code`; it buys DISTANCE, not another tool.

237. `/code-review` is no longer what the nudge suggests: it needs a PR to exist, only posts a
     comment, and on this repo it spent ~743k tokens to report `findings: []` (entry 230).
     Its workflow-script tiering note stays — the user can still run it deliberately.

238. COCO `bird` (14) is DROPPED from `yolo-serial`'s class set, reverting half of entry 89:
     from the top-down door view a bird box was almost never a bird, and its own subject chip
     dressed that noise up as a named subject. `person` (0) stays; verdict/score were always
     cat-only, so no scorecard moves. Such motion now files as `unrecognized`/`motion_only`.

239. Every read path that ACTS on a class dropped bird with it — the subject ladder, the
     per-visit detection aggregates, and `_best_detection_box` (playback/grid overlays) — so a
     LEGACY stored bird box is ignored rather than half-honoured. `_subject_classes` stays
     class-agnostic (it still reports the box); the consumers name what they act on.
     Consequence: a swept bird-only visit now reads as a measured detection MISS (ratio 0.0).

240. Review is now ONE point — the commit gate — not a pass after every non-trivial edit, and the
     end-of-turn user-run `/fix-code` nudge is gone with it. A per-turn pass only ever saw its own
     turn's edits, judged from inside the context that wrote them; nothing read the accumulated
     diff as one change. Carve-out, load-bearing now that review waits: tests, build and each
     subtree's verification workflow still run PER CHANGE. A fan-out's diff is flagged AT the gate.

241. Annotation's Labelled mode gained an EVENT overview above the single-visit stage — a grid of
     the decided visits, each a rep CROP with its label chip. Finding a mislabel is a scanning
     task, and the mode offered only a stepper over one visit's crops, so it read as a frame list.
     The crop, not the frame: top-down a cat is a few percent of the ROI, unreadable at tile size.
     Rebuilds only on a SET change; selection is a class toggle, so stepping never flickers tiles.

242. The Labelled keys were reachable only until you touched the mode's own "Show label" dropdown:
     `onKey` drops any keystroke targeting a SELECT, so focus left there killed n/p AND every
     re-label key — the mode read as having no navigation at all. It now blurs on change (entry
     235's fix, same cause), and Prev/Next are visible buttons rather than hint-line-only keys.

243. Labelled review dropped its per-crop filmstrip — a visit can hold ~100 crops, so the strip
     WAS the page, and no reading is made per crop. The grades it carried are now a meta-line
     tally (`2 gallery / 3 ok / 3 poor`): what this visit contributes to a quality-filtered
     build. Suppressed for `not_cat`/`ignored`, which write no crops to grade. Flagged review
     KEEPS its strip — that span is often undecided and unswept, so seeing its crops is the point.

244. Labelled review is always scoped to ONE label — the "all labels" option is gone and the
     first cat is preselected. A homogeneous grid is what makes a mislabel visible: a cat that
     isn't Mittens leaps out of the Mittens tiles and is just another face in a mixed set.
     The set-wide total survives in the grid's "N of M labelled". A label emptied by a requeue
     drops its option and falls back to the first, since there is no all to fall back to.

245. `requeue` drops the visit it DELETED, located by identity, not whatever `labIdx` points at
     after the await. Navigation was never busy-guarded, and the overview's clickable tiles made
     the race easy: navigating mid-delete spliced a different, still-labelled visit out of the
     list while the deleted one stayed on screen looking fine. `labAll` and `dropFlagged` already
     located by identity — Labelled's `lab` was the one place left trusting a live index.

246. A settling re-label repaints ITS OWN tile (`refreshTileChip(v)`), not the selected one. Same
     root cause: the write lands long after the keypress, so if the operator has moved on, the
     tile they just corrected kept its old cat until a full rebuild — in the grid whose whole
     job is showing labels at a glance.

247. The selected tile is scrolled into view by clamping the grid's own `scrollTop`, never by
     `scrollIntoView`, which walks every scrollable ancestor: with the grid scrolled above the
     viewport each Prev/Next hop scrolled the PAGE up by ~84px, yanking away the detail stage
     being read. Measured, not theorised — the prior comment claimed the opposite.

248. The grade tally is suppressed for `not_cat` and `ignored`, which are committed crop-less
     (quality/bbox/crop_path NULL), so a tally there read "8 ungraded" about crops that were
     never written. Keyed on the label KIND, not on "every grade is null": for a real cat
     labelled before grading existed, "N ungraded" is true and worth showing.

249. Admin Activity lost its Analyze button (entries 90/91) — removed as unused. Re-detecting a
     historical window is now only a Motion-tuning sweep, which is also where its progress always
     showed; Identify stays as Activity's one backfill. `/api/analysis/run`'s `reanalyze` +
     `motion_only` are untouched (the tuning sweeps use both), so this dropped a caller, not a path.

250. User playback steps VISIT to visit: the Prev/Next pair moved out of the frame-controls row
     into the footer beside the flag, as `‹ Newer` / `Older ›` around a `12 / 40` position readout.
     They already hopped visits — sitting among Play/scrub/count they READ as frame stepping, and
     they squeezed the scrub slider to ~40px on a phone (measured 158px after). Below 480px the nav
     takes its own full-width line, arrows at the screen edges.
     Spec: docs/specs/2026-07-31-user-visit-swipe-nav.md.

251. A horizontal swipe on the played frame does the same hop, with the frame tracking the finger
     — so the gesture is self-teaching rather than a silent jump. Swipe left = older, matching the
     newest-first feed. Damped to a third at either end.
     The ease-back is the "that didn't take" cue, so it plays ONLY when the visit doesn't change
     (short drag, refusal at an end, cancel); a committed hop drops the transform instantly, since
     animating it back would read as the gesture bouncing off the swipe that worked.

252. Two rails the swipe needs. A second finger DISARMS the gesture: merely ignoring its
     `touchstart` left the first armed, so a pinch ended with a large `dx` and hopped visits.
     And there is deliberately NO `touch-action` on the stage — `pan-y` would kill pinch-zoom on
     the one image worth zooming; the axis latch calls `preventDefault` only once a drag has
     committed to horizontal, which claims the axis without taking the browser's own gestures.

253. `.stage` gained `min-height: 0` so it is the dialog column's shrinking item. A flex item's
     default `min-height: auto` won't shrink below its aspect-ratio height, so the new footer line
     overflowed `max-height: 94vh` and `overflow: hidden` CLIPPED the nav — the one control that
     must stay reachable. Not a `vh` cap: any value low enough to save a 568px phone also shrinks
     the frame on a tall desktop.

254. The visit-position readout never asserts an absolute it can't support, and there are TWO ways
     it couldn't. It carries `/api/events`' `truncated` flag (the user page dropped it) → `40+` and
     "oldest loaded"; and it hedges to "newest/oldest SHOWN" whenever a toggle is hiding events —
     the common case, since `showAll` defaults off and nav walks the FILTERED list, so a bare
     "newest" is a lie on the default view. "shown" subsumes a capped feed, so it wins over "loaded".

255. The playback dialog's `aria-label` carries the same health + flag sentences the feed rows do,
     via one shared `visitAriaExtras` so the two can't claim different things about one visit. The
     flag fragment is elsewhere called the ONLY signal a visit is marked for a screen reader, and
     the dialog is where it gets toggled.
     Known limit: a hop is still silent to a screen reader — re-labelling an already-open
     `aria-modal` dialog isn't reliably announced and focus stays on the pressed button.

256. `events()` reads its annotations PER EVENT SPAN, not over the events' overall
     [min start_id, max end_id] envelope. That envelope stretches across every frame between
     the newest and oldest event, so under continuous capture ~95% of the rows it fetched
     belonged to no event — materialised, then discarded, all under the shared write lock.
     Measured at 1.5M frames with a full YOLO sweep, on a store with NO `ANALYZE` stats
     (what the real one is): `Store.events` 1415 ms → 54 ms for the same 500 events.

257. `cats_overview` takes a new `with_subject=False` path: it reads only `identity`, and the
     subject/detection annotation it never looked at was the call's most expensive part
     (964 ms → 17 ms). The reuse-the-feed invariant is intact — identity still comes from the
     same code Activity renders, so the two views cannot name a moment differently.

258. Remaining headroom on that feed, deliberately not taken: it still fetches the newest
     `_EVENT_SCAN_FRAMES` (200k) motion frames and clusters them all, to keep only the newest
     500. Costs ~90 ms once a store's motion tail exceeds the cap (~165 ms/call measured at
     540k motion frames); an early-stop after `limit` clusters would fix it, at the price of
     reworking the `truncated`/partial-oldest-cluster semantics.

259. The Activity feed is PAGED, 100 visits per page (`/api/events` gained `limit`), on both
     dashboards. Cost scales with the page, so page 1 went ~54 ms → ~20 ms; 500 was never a
     chosen page size, just the store's `_MAX_EVENTS` cap. Below ~50 the gain flattens against
     the feed's fixed floor, so 100 is the knee.
     `Store.events`'s own default stays wide — `cats_overview` walks the feed for the newest
     event naming each cat, so a 100-event window would shorten every "last seen" it reports.

260. Paging is KEYSET, not offset: a page asks for `until_id` = (oldest loaded start_id − 1),
     so page N costs what page 1 costs (measured equal at 1.5M frames) and pages can neither
     overlap nor skip a visit. `truncated` is the "another page exists" test. The user feed
     auto-loads on scroll, admin has a "Show older" button; both report the loaded count and
     an end-stop, because a list that simply stops looks like one showing everything.

261. Appending a page never disturbs what is already on screen: appended visits are strictly
     OLDER, so they land at the end and every existing index — what a row's `data-index` and
     the player's selection mean — still addresses the same visit. That is why a page may be
     appended with playback OPEN, unlike the poll's full reload, which stays guarded off.

262. Rails the paging needed, each a real failure found in a browser. `IntersectionObserver`
     fires on a CROSSING, so a page that didn't push the sentinel off screen (every visit
     filtered out, or a short page) stalled the chain — it now re-checks by geometry and
     continues. The in-flight flag is cleared UNCONDITIONALLY: skipping it when a page-1
     reload superseded the fetch left it stuck true and blocked paging for the session.
     A superseded older page is DISCARDED (its keyset was computed against the old set).

263. Admin's loaded-count + Show-older state renders BEFORE `renderGrid`'s early returns.
     It describes the LOADED SET, not what the filter left visible — and a grid reading
     "all hidden by the filter" is exactly when an operator wants to pull older visits in,
     so freezing the button there stranded them.

264. The player's Older at the last loaded visit now fetches the next page instead of
     refusing outright, so deep swiping still reaches back. It refuses THIS hop rather than
     awaiting the page: the ease-back is the "that didn't take" cue, and holding the frame
     still mid-gesture would read as a freeze. The note clears itself when the page lands,
     since the hop it explained now works.

265. The per-span reads carry `+analyzer`, DE-INDEXING that term as `_resolve_flag` does.
     Without it entry 256 was a 36x REGRESSION, not a fix: the store runs no `ANALYZE`, so
     with no `sqlite_stat1` SQLite prefers `idx_analysis_analyzer_verdict` on the equality
     and scans the whole yolo-serial partition — once PER SPAN. One 100-event page at 1.5M
     frames: 19.5 s unhinted vs 4.8 ms hinted (the old envelope query was 538 ms).
     Entry 229 predicted exactly this and named `events()` as still carrying it.

266. Measure query-plan work on a store with NO `ANALYZE` stats. A bench that runs `ANALYZE`
     gets a plan the real store never gets, which is how entry 256's numbers came out both
     too optimistic and measured against the wrong plan — the mis-plan above was invisible
     until the same queries were re-timed without stats.

267. Paging rails the browser pass missed, all in the append path. The day-group cursor is
     reset beside `railEl.innerHTML = ''`, not in the non-empty branch the empty path returns
     before reaching — a cursor pointing at a detached rail made an appended page's rows
     invisible while still counted in `activityVisible`, so the position readout and the
     player's nav addressed rows not on screen. Appending also clears the empty-state panel,
     which otherwise kept claiming "nothing to show" above the row just added.

268. A superseded page repaints the foot/button UNCONDITIONALLY, on both dashboards. The
     winning render ran while the in-flight flag was still true, so it painted "Loading
     older…" — and with only the winner repainting, that lie stayed until an unrelated
     re-render. The flag itself was already cleared unconditionally; the paint was not.

269. Two smaller paging repairs: a FAILED page fetch triggered from the player now says
     "older visits failed" in the readout (its error note lives on the page BEHIND the modal,
     so the reader saw only a promise that never resolved); and the keyset bound uses `reduce`
     rather than `Math.min(...spread)`, since paging made those arrays unbounded and a large
     enough spread throws RangeError before the try block, as an invisible unhandled rejection.

270. Admin's Identify NAMES the span it enqueues ("over the N loaded visits"), because paging
     silently narrowed that backfill from ~500 visits to one page of 100. The scope follows the
     loaded set by design — "Show older" widens it — but nothing said so at the point of the click.

271. Known limits of the paged feed, deliberately left. An empty page carrying `truncated:true`
     (reachable when a scan-capped window's sole cluster is popped) stops paging: trusting the
     flag instead would re-request the same `until_id` forever, since no new events move the
     keyset — a real fix needs the backend to expose the scanned window's floor.
     Infinite scroll also turns entry 258's fixed floor into one 200k-frame scan per page, on
     the shared write connection; a short-lived WAL read connection (as `tuning_calendar` and
     `labeled_visits` use) is the fix if it bites. And user-page paging exists only where
     `IntersectionObserver` does, with no button fallback.

272. Day/night stays SUN-TIMES driven and the lighting cutoff is deliberately left unset.
     The IR lamp runs on the edge's astronomical schedule (sunset−1min / sunrise+1min) from
     the SAME lat/lon the compute split uses, so the two agree by construction — the spec's
     premise, an illuminator whose photocell drifts from sun times, is not this wiring.
     And were MOG2 params ever to diverge, the switch must happen LIVE ON THE EDGE, which an
     offline read-time flag could not drive regardless of how accurate it got.

273. Measured over 30 July (707,784 frames, full lighting coverage): colourfulness CANNOT
     separate the regimes at this door. Evening daylight reads 0.088–0.100, at or below IR
     night's 0.100–0.103 — the scene is achromatic in daylight too, so the statistic tracks
     direct sun rather than colour-vs-IR. Mean luma does separate cleanly (night 45.6–47.8,
     day 83.4–98.6) and is ALREADY stored in `analysis.detail`, so switching axis later
     would need no re-sweep. The separation depends on the lamp staying dim: auto-exposure
     is railed at night, so a brighter emitter would erode the gap.

274. The replacement IR lamp holds output — night luma flat at ~46 all night, unlike the
     emitter of entry 199 that collapsed at civil dusk. Night crops are readable (tabby
     striping legible at 360×267 px, YOLO 0.95), so the IR-night regime is viable for the
     gallery, not merely populated. Night luma is therefore also a usable lamp-health
     signal, which is the one job the lighting sweep still earns.

275. Edge `fps` is set to 15 but the Pi only grabs ~10.2; on 30 July it ran 7.7–9.4 with no
     outage. Nothing is lost in transit (compute stored 10.24 against the Pi's 10.19), so the
     Pi is the ceiling — though a stored-rate dip can equally be collector lock contention,
     which frame-id continuity cannot distinguish (ids are assigned at insert, so a frame
     dropped before insert leaves no gap). Matters for tuning: `persistence` counts FRAMES,
     so 2 is a ~250 ms window here, not the ~400 ms it would be at the documented 5 fps.

276. The Motion-tuning calendar ("Last 4 weeks") no longer joins `frames` per verdict, and
     carries `+analyzer` — entries 229/265's mis-plan, third instance: with no ANALYZE stats
     SQLite scanned each analyzer's WHOLE partition and fetched every row. Each day's
     verdicts are now COUNTed over that day's own id span off the `(frame_id, analyzer)` PK,
     a pure covering scan — equivalent because recv_ts is non-decreasing with id and
     `analysis` cascade-deletes with its frame.

277. Measured on a 2.5M-frame / 4.3M-verdict replica of the real store: that pass 2590 →
     880 ms, the whole call 3.0 → 1.4 s (the live endpoint was 4.8 s). Known limit — it is
     still O(store), walking every frame's recv_ts index entry (390 ms) plus every verdict
     in the window on EVERY open, and the store sits at 30% of its 1 TiB cap. Structural
     fixes are maintained per-day counters or a cache; the always-on oracle writes verdicts
     continuously, so a whole-store epoch key would never hit.

278. What entry 276 trades away: `recv_ts` monotonic with `id` is ASSUMED, not enforced —
     the collector stamps the wall clock — so a backward step across a local midnight
     overlaps two days' id spans and the earlier day counts the later one's verdicts.
     It fails LOUDLY, one direction only: coverage reads above 100%, never a silent
     under-report. Left unguarded because clamping to the next day's first id regresses
     the OPPOSITE skew into a fully-swept day reading 0%; only the join is immune.

279. New `unanalyzed` subject rung: a visit whose span carries NO `yolo-serial` row says so,
     instead of `unrecognized` — which claims YOLO looked and found nothing nameable, the
     opposite reading. It outranks `corrupted`, whose contract needs "YOLO detected NOTHING"
     that an unswept span cannot support (entry 226's rule). `swept` is free — `events()`
     already holds the rows — and BINARY: a partly-swept visit reads analysed, with
     `detection.ratio` carrying the nuance. Spec: docs/specs/2026-08-01-unanalysed-visits-analyse-identify.md.

280. `swept` counts rows over the WHOLE span, not its motion frames, matching what a
     span-scoped sweep fills — so it can disagree with `ratio`'s motion-only denominator
     (swept, yet "not measured"). Narrowing it to motion frames would call a span
     unanalyzed that a sweep cannot add one verdict to.

281. New `visit-identify` TrainingManager kind + `POST /api/identify/visit`: detect THEN
     identify over ONE visit span, the pair `LiveIdentifyManager._tick` runs per closed
     visit, on demand for a visit the always-on workers never covered. No "did YOLO find a
     cat" branch — `iter_unidentified` yields only frames with a present verdict, so an
     empty visit self-skips. A cancel mid-detect returns NO summary, which is what records
     it `canceled` rather than `done`.

282. That route diverges from `/api/identify/run` twice, both because DETECT is the half
     that resolves `unanalyzed` and is useful with no gallery: no active model is a SUCCESS
     (detect ran, `identified: false`), and the deps check follows the HALVES — the
     analyzer's always, the embedder's only when a model exists. Both span bounds are
     REQUIRED and width-capped (10k ids): elsewhere a missing bound means "whole store",
     and the household's phone calls this route on a no-auth LAN.

283. Both dashboards gained the per-visit "Analyse this visit" button (playback modal), shown
     ONLY on an `unanalyzed` visit — elsewhere the job fills nothing, and a button that does
     nothing is worse than none. `unanalyzed` is EXEMPT from the user feed's noise filter:
     it is not a low-signal reading but the absence of one, and hiding the visits whose
     button you want tapped defeats the feature.

284. The button's completion watch reads `/api/training/status` for its OWN (kind, span)
     across `running` AND `queue` — matching `running` alone calls a job done while it is
     still queued behind another. It then re-reads JUST that visit's span from `/api/events`
     and patches the event IN PLACE, since object identity is what the rail rows, the player
     and the paging state address a visit by. Closing the tab loses the repaint, never the
     analysis.

285. `activity_signal` deliberately does NOT learn about `analysis`, so a visit analysed to a
     NO-DETECTION result pushes nothing over SSE and updates on the next reload. Adding a
     verdict counter would nudge every connected client every tick — the always-on oracle
     writes continuously.

286. A successful flag toggle is now SILENT — the modal footer gained the analyse button and
     has no room to confirm in words what the button's own label already says. The readout
     is kept for DIVERGENCE only ("Already on the labelling list", "It was not on…"), which
     reports the write doing something other than the tap intended and is shown nowhere else.

287. The user dashboard has NO bare `.hidden` rule — only per-element qualified ones
     (`.note.hidden`, `.modal.hidden`, …) — so `classList.toggle('hidden')` on an element
     without its own rule is a silent NO-OP. The analyse button therefore showed on every
     visit, and clicking it on an already-analysed one enqueued a pointless GPU job.
     Verify visibility by COMPUTED STYLE, never by `classList.contains` — the class was
     present the whole time, which is what made the browser check read as passing.

288. The per-visit analyse watch reads its job's OWN history record, not `status.error`:
     that is one sticky field a later promotion clears and a later failure overwrites, so
     it can report another job's failure as this one's. Leaving the queue is not succeeding
     — a failed or canceled job now says so instead of reporting a result never produced.

289. "Analysing…" is per VISIT, not a global flag. The watch runs up to two minutes and
     Prev/Next stays live throughout, so one flag painted the busy label onto whatever
     visit the reader hopped to. Several analyses may now be in flight; the server queue is
     serial and dedups a repeat of the running span.

290. A job can succeed and still record nothing for its span (frames evicted mid-run), so
     admin only retires the analyse button once the visit actually left `unanalyzed` —
     otherwise the retry control vanished under the line "Analysed: not analysed."

291. Annotation can PLAY the visit it is deciding — click either stage image, the new
     "▶ Play frames" button, or `v`, in Queue, Flagged and Labelled alike. A cat is a few
     percent of the top-down ROI, so the stage's single rep still can leave the coat
     ambiguous; the player runs the whole visit through the same crop-beside-frame pair.
     No fetch: it plays the visit record's OWN frames, i.e. exactly the crops a decision
     labels — not Activity's sampled event span, which includes frames carrying no crop.

292. With the player open the keyboard still labels: a bound key closes it and then does
     its normal thing, so a decision always lands on the visit that was on screen (the
     player only ever plays the staged visit, and nothing navigates without closing).
     Space is the exception — play/pause, since skipping a visit because the operator
     reached for pause is the worse surprise. Any stage repaint closes it, and so does a
     mode SWITCH: that load is async and leaves the outgoing mode's list in place, so a
     key pressed over the player would have re-labelled a stale, already-decided visit.

293. The player transport is now module-level and shared with Activity, which supplies its
     own stage (one full frame + box) and stats as before — so play/pause, scrub, position,
     Escape and backdrop-close can't drift between the two. Each cell is FIXED size —
     the frame cell takes the frame's own aspect ratio — since a crop's box changes shape
     frame to frame and content-sized cells would resize 5× a second.

294. The annotation saving indicator moved out of the control row to a pulsing amber pill
     directly above the stage, in a permanently reserved slot (measured: 0px shift, so the
     images never step under the reader). A label is written BEHIND the operator, so this
     is the only thing saying one is still in flight — and it has to register from the
     crop, where the eye is. Motion is what peripheral vision picks up, not a grey word.

295. It NAMES what is in flight ("Saving Mittens · 11/07-2026 06:14…"), because the visit
     left the screen the instant it was submitted — "saving 3…" cannot answer the question
     the operator actually has, which is whether their Mittens went through. All of them
     are listed in the tooltip, which also says the buttons staying enabled is fine: the
     writes are a queue, not a race.

296. A drained queue now CONFIRMS with a brief green "Saved" instead of the chip merely
     vanishing, which looked identical to one that never appeared. Suppressed when the
     batch held a failure or a short write, so it can never contradict the error line
     those already print.

297. Labelled review's re-label and send-back are on the same indicator. A re-label deletes
     the visit's crop files and cuts them all again, making it the SLOWEST write on the
     page — and being awaited rather than optimistic, it previously left the page looking
     completely idle for seconds. Root cause of the wait, unchanged: `_commit_label`
     decodes + crops + writes one JPEG per visit frame, synchronously in the request.

298. The player's scrub is the page's THIRD focusable control to blur itself after use,
     joining the entry-235/242 pair. `onKey` drops any keystroke targeting an INPUT, so
     focus left on the slider swallowed the operator's next label — with the modal over
     the stage to hide why. Blurs on `change` AND `pointerup`: a click landing on the
     thumb's current position changes no value and fires neither.

299. One `stage` setting (`tuning` | `collecting` | `running`) is now the single intent for
     capture mode and the two always-on workers, replacing the trio of switches
     (`motion_only`, `yolo_oracle`, `live_identify`) an operator assembled by hand.
     `POST /api/stage`; the Start page picker replaces three controls. Makes real the `Mode`
     entity ARCHITECTURE had claimed since day one and the code never had.
     Spec: docs/specs/2026-08-02-operating-stages.md.

300. Fixes the hole that motivated it: between switching to motion-only capture and promoting
     a first gallery, NOTHING detected anything. The detection worker bailed under motion-only
     capture and live-identify bails without an active model, so visits landed `unanalyzed`
     and the annotation queue stayed empty — noticed only when the queue you came to work was
     blank.

301. The always-on YOLO worker's coverage now FOLLOWS what is stored instead of idling: every
     frame under keep-all, motion frames under motion-only. Renamed the DETECTION worker
     (operator-facing only — module, class, settings keys and `analysis.analyzer` unchanged,
     per entry 147), because its verdicts feed event subjects, per-visit aggregates, the queue's
     membership and the identify pass — all of which need only motion frames. Only the gate
     scorecard's MISS column ever needed the non-motion half.

302. Coverage mode is read per TICK, never applied via stop/start, and both workers now restore
     unconditionally on a live app. Load-bearing: every `start` re-seeds the watermark to the
     frame horizon (entries 149/150), so driving a stage change through a restart would
     silently discard the un-drained tail. Operator-visible on the first restart: anyone who
     had "YOLO all" off finds it running — the coverage tuning wanted anyway, but a real
     GPU-load change.

303. Eviction is stage-aware: outside `tuning` it reclaims non-motion frames FIRST, so the same
     disk holds annotatable motion history much further back. `tuning` stays byte-for-byte
     plain oldest-first — note non-motion frames are NOT spared there, merely never
     preferentially targeted. This is the auto-cleanup: driven by disk pressure, so no stage
     change is destructive and a leftover keep-all window is shed gradually, not wiped.

304. That preference strips windows PARTIALLY (motion frames present, non-motion gone), which
     plain oldest-first never did — so it advances a `nonmotion_evicted_through` marker that
     `motion_only_spans` folds in as the prefix `[1, N]`. Without it a scorecard reads
     near-perfect gate recall over frames that were deleted: entries 97/126/167's trap, in the
     one place it would be least visible.

305. Three rails that marker needs. `clear()` RESETS it — the settings KV survives a wipe while
     frame ids restart at 1, so a stale value would banner a fresh store as unmeasurable
     (entries 141/143/144, a fourth direction). It is never written via `set_setting` (which
     takes the store lock `_evict_locked` already holds — a non-reentrant deadlock), riding the
     caller's transaction instead so it can't diverge from the deletes. And `add`'s rollback
     re-reads it, or a discarded write would leave it ahead in memory and REGRESS on restart.

306. Orphaned JPEGs are collected automatically at launch — a file with no `frames` row is
     unreachable by construction, so removing it loses nothing and needs no marker,
     confirmation, or stage awareness. Launch is when they exist: the changelog-42 leak is
     created by a hard power loss.

307. The orphan sweep's referenced-row probe now goes through the `recv_ts` INDEX, not a bare
     `WHERE path = ?` — `frames.path` is unindexed, so that was a FULL TABLE SCAN per candidate
     file: O(files x rows), quadratic in store size. Measured at 20k files / 20k rows:
     11 s → 0.46 s, now walk-bound (≈1 min extrapolated to 2.5M files, vs effectively
     unbounded). `add` composes the name as `<date>/<hour>/<recv_ts_ms>_f<id>.jpg`, so the
     millisecond is recoverable; the `path` equality still runs, so the answer is identical and
     only the plan changed. Entries 229/265/276, fourth instance.

308. That was latent while the sweep was manual and cancelable — entry 306 made it automatic at
     every launch, which would have pegged the store lock for hours and starved the collector on
     the real store. Caught by measuring before upgrading, not by the test suite: correctness was
     never wrong, only the plan. An unparseable filename falls back to the slow scan rather than
     being assumed orphaned.

309. Preferential eviction orders victims by `recv_ts`, not `id`. `WHERE motion = 0 ORDER BY id`
     has NO index serving it, so SQLite temp-b-tree-sorted the whole remaining non-motion
     partition to return 64 rows — once per `add` (~10/s) under the write lock, for the whole
     multi-day shedding. Measured on the real schema with no ANALYZE: 7.5 ms at 238k non-motion
     rows → 34 ms at 950k → 63 ms at 1.9M, i.e. linear, vs a flat 0.02 ms ordered by `recv_ts`
     off the existing `idx_frames_motion_recv`. At cap that was ~0.8-2.6 s of lock hold per
     second of capture: total starvation, entries 102-105 again.

310. Chose that over adding a `(motion, id)` index — also fast — because an index costs write
     time on every insert forever to speed a maintenance path, and this one already exists.
     The trade: victims are an exact id-prefix only while `recv_ts` is non-decreasing with `id`
     (entry 278: assumed, not enforced), and if it ever stepped back the `max()`-tracked marker
     OVER-claims unmeasurability rather than under-claiming — the fail-safe direction.
     Victim lists verified byte-identical over 3000 rows.

311. The `running` health card no longer reads green over a broken worker. Both always-on
     workers keep `running` true and set a STICKY `last_error` when a tick throws, so testing
     the intent flag alone showed "verdicts current" while nothing had been analysed for days —
     in the one card that exists because nobody is expected to be watching. It now fails a row
     on `last_error` too, as the fuller readouts below it already did.

312. The orphan sweep CONFIRMS a negative with the unindexed `path` equality before deleting.
     Entry 307's fast probe rests on a filename parse, and this call removes FILES — so a parse
     returning a wrong-but-plausible millisecond would find no row and destroy live frames.
     Proven with a deliberately broken parser: 0 deleted instead of 20,000. The scan runs only
     for candidates that already look orphaned (rare — orphans exist only after a crash), so
     the normal sweep is unchanged at 0.49 s/20k files. Made structural because the failure
     would be silent, total, and is newly AUTOMATIC at launch.

313. The retroactive non-motion purge stays MANUAL by decision. Outside `tuning` no non-motion
     frames are being stored, so every frame that job removes comes from a previous tuning
     window — it is retroactive by construction, and auto-running it on a stage change would
     make a mode toggle irreversibly destructive. The Start page instead links to it whenever
     leftover non-motion frames exist, keyed on `count − motion_count` so the hint appears from
     state and disappears once there is nothing to reclaim.

314. Feasibility run 6's headline "100% kNN" is INFLATED and must not be read as recognition
     accuracy: the leave-one-out masks only the diagonal, so a crop's nearest neighbour is
     usually the adjacent frame of its OWN visit — a near-duplicate. The separation AUC (0.878)
     and the gallery's stamped `threshold_balanced_acc` (0.804) are the numbers that survive.
     A visit-held-out probe is the fix; until it exists, no report scores the real task.

315. Cat SIZE is measurable signal the identify path discards: the embedder resizes every crop
     to 224x224, dropping absolute size AND bbox aspect before DINOv2 sees it. Per-visit peak
     bbox area alone separates Sultan/Store Sultan at AUC 0.84 (Jhinie/Store Jihn 0.78), every
     foreign lookalike measuring bigger — orthogonal to appearance, and aimed at exactly the
     pair DINOv2 is worst at. So it is a missing MECHANISM, not a data gap. Queued in docs/TODO.md.

316. Validation now reports a VISIT-held-out number: each visit is hidden whole, matched
     against the other visits, and named by Run's own rule (`Store._aggregate_identity`,
     injected so the probe cannot drift from what Run decides). Outcomes are correct /
     wrong / **declined** — kept apart because for a resident at the door those mean
     opposite things. The report leads with it; crop-level kNN stays, demoted, as the one
     number comparable with earlier runs.
     Spec: docs/specs/2026-08-03-visit-held-out-validation.md.

317. It rides the SAME embed and the SAME N×N distance matrix `run_feasibility` already
     builds — held-out scoring is that matrix with a visit's own columns masked — so the
     honest number costs numpy, not GPU. Additive: absent `visit_groups` the result dict
     is byte-identical, so nothing that consumed the old shape moved.

318. The visit threshold is calibrated on CROSS-VISIT same-cat pairs, never the crop-level
     one. `_best_threshold` over all same-cat pairs is dominated by same-visit
     near-duplicates at near-zero distance: measured 0.00063 vs 0.99934 on a real-shaped
     fixture (1587x), at which every visit is DECLINED and the report reads 100% unknown.
     Data-dependent — with separable cats the two coincide — so it cannot be assumed benign.

319. Held-out visits are grouped PER CAT, at a coarse 60 s gap of their own
     (`_HELDOUT_GAP_MS`), not the store's 2 s `_VISIT_GAP_MS`. Coarse is fail-safe here:
     over-merging removes more leakage, under-merging splits one physical visit across the
     boundary and lets the near-duplicates back in. A global time-sort would also merge two
     cats at the door in one minute into a group with no true `cat_id` — tailgating is
     expected here, and `dataset_items`' UNIQUE only stops two cats sharing ONE frame.

320. A cat with a single visit is UNSCOREABLE, not wrong: the correct answer is absent from
     the gallery, so it could only ever fail. Excluded from the denominator and named in the
     report (Store Kali today). Degenerate runs — no cross-visit pair to calibrate, or fewer
     than two visits — report `available: false` with a reason instead of numbers, since
     "0 correct / 100% unknown" reads as catastrophe where nothing was measured.

321. Day/night is scored separately, plus a CROSS-REGIME matrix (night visits against a
     day-only gallery and vice versa) that answers whether one gallery spans both regimes:
     night→night strong but night→day collapsed means separate galleries, not more night
     data. Bucketed whole by each visit's first crop, the same rule `gate_scorecard`'s split
     uses. No location or no astral disables it rather than guessing a boundary.

322. `feasibility_runs` gained a `metrics` JSON column (mirroring `model_versions.metrics`),
     carrying the visit block so the runs table can rank by the honest number. This is the
     repo's FIRST additive migration: the schema is `CREATE TABLE IF NOT EXISTS` only, which
     never adds a column to an existing table, so `_migrate_schema` probes `PRAGMA
     table_info` and `ALTER TABLE ADD COLUMN`s. Pre-existing rows read NULL = "not measured",
     which the runs table renders as `—`, distinct from an uncalibrated run's `n/a`.

323. `labeled_crops` now returns `src_recv_ts` + `labeled_ts`. Both come off
     `dataset_items`, NOT `frames`, so grouping and day/night bucketing cover every label
     including those whose frames have aged out. `labeled_ts` is stamped once per commit, so
     distinct values ≈ label keypresses — a free cross-check on the re-derived grouping,
     printed in the report beside it.

324. Probe charts now pin matplotlib's Agg backend (one `_plt()` accessor). They render in
     TrainingManager's WORKER thread, where matplotlib warns a GUI backend "will likely
     fail"; Agg is correct by definition since every figure goes straight to a base64 PNG.

325. The confusion-table f-strings no longer put a BACKSLASH inside `{}`. That is a
     SyntaxError before Python 3.12 (PEP 701 lifted it) and it fails at IMPORT for the
     whole module, so `compute.api.app` would not have started at all — `compute.ps1`
     accepts 3.10+. Invisible here: this dev box runs 3.13.

326. The schema migration survives losing a cross-process race. The `PRAGMA table_info`
     probe and the `ALTER TABLE` are not atomic across processes — the store lock is
     per-instance and `busy_timeout` only bounds waiting for the write lock — and the API
     plus the CLI tools legitimately open the same `index.db`. The loser now no-ops on
     `duplicate column` instead of dying inside `Store.__init__` with what reads like
     schema corruption. Narrow on purpose: "database is locked" and disk-full stay loud.

327. An `available: false` visit block carries NO `auc`/`threshold_balanced_acc`. Both are
     computed before the availability branch, so they leaked past "nothing was measured" —
     the exact distinction the visit scoring exists to keep honest.

328. A report whose visit scoring was UNAVAILABLE keeps its crop-level verdict. `demoted`
     keyed on the visit section rendering any HTML, but it also renders an explanatory
     banner — so an unavailable run suppressed the only verdict it had AND pointed the
     reader at "the visit-level number above", which was never computed.

329. Measured, correcting the spec: visit scoring DOES raise peak memory (n=6000: 2109 →
     2439 MB), via two pair-length `int64` gathers for the cross-visit mask. The largest
     addition is gone — finding the matrix min/max no longer boolean-index-copies the whole
     matrix (~1.16 GB at 12k crops), since `1 - (unit @ unit.T)` is always finite.

330. A cat can be left out of ONE gallery build without being retired: the Model page's new
     shared checkbox list sends `exclude_cat_ids` on build AND validate. Retiring was the
     only mechanism and is the wrong one — it also drops the cat from the annotation picker,
     stopping the labelling the exclusion is waiting for.
     Spec: docs/specs/2026-08-03-gallery-build-cat-exclusion.md.

331. It is an EXCLUDE-list, never an include-list, so absent/empty means "enrol everyone"
     and a cat added to the roster later is enrolled by default — an include-list would
     silently drop a new resident from every repeated build. Per-build only, never
     persisted: a reload re-ticks everyone.

332. Filtered in SQL on `labeled_crops` AND `count_identified_crops`, the pair that must
     agree. It is the FIRST build parameter that can reduce the CAT count, so the pre-check
     applies it too — the cap's "only reduces crops per cat" premise does not hold here, and
     the two-cat floor would otherwise pass on cats the build then drops. The message names
     the exclusion: "not enough labelled data" misreads when it was merely deselected.

333. The exclusion is part of the artifact's identity — dedup key (ids SORTED, so tick order
     is not identity), a `-ex<n>` dir-slug fragment after `-max<cap>`, and the ids on the
     version's / run's `metrics`. Without it a gallery built without Store Kali is
     indistinguishable from one with it, and a second Build with a different selection is
     silently dropped. Both lists NAME the cats (a count cannot be compared between builds).

334. Validate takes the same list, from the SAME shared control — deliberately unlike the
     grade checkboxes, which stay per-panel. Validation scores the crops, not a gallery, so
     an exclusion applied only to the build is invisible to the number; two independent
     lists would let you build without a cat, validate with it, and read the unmoved score
     as "the exclusion didn't help".

335. New `GET /api/label/enrollable` returns, per ACTIVE cat, crops + `label_commits` at the
     requested grades — pre-cap, which is also what a cap is picked against. Counts follow the
     Build grades: a cat with 50 labels may have 17 at gallery grade, so 50 is the wrong
     number to decide on. `label_commits` is named for what it counts, not the visits it
     proxies (~12% high). Retired cats are omitted — unticking implies ticking would enrol.

336. Excluding a RESIDENT is allowed with no guard: a newly added resident held back until
     it has crossings is precisely the case. The row shows `is_resident` and both counts, so
     the choice is informed rather than prevented — recall tracks visits, and the cat with
     the most crops measured the worst recall (Store Jihn 2725 crops, 69%).

337. Admin Activity gained "Identify all" — a WHOLE-STORE identify pass, beside the
     Identify that is scoped to the LOADED visits. Promoting a gallery orphans EVERY
     stored identification (keyed by `model_version_id`), so all history reads unnamed
     until a pass re-runs, and `/api/events`' 500-event cap means "Show older" could
     never widen the scoped button that far. Same endpoint, both bounds omitted.

338. `count_unidentified` moved to its OWN short-lived WAL read connection. UNBOUNDED —
     the identify pass's progress denominator — it measured 4.9 s on a 4.7M-frame replica
     with no `ANALYZE`, stalling a competing writer 7.2 s on the shared connection vs
     0.1 ms off it (measured). The starvation class entries 102-105 removed; latent until
     entry 337's button made the unbounded call routine.

339. That 4.9 s is NOT the entries 229/265/276/307 mis-plan: the plan already seeks
     `idx_analysis_analyzer_verdict` to the detected minority (1 ms). The cost is the
     per-row `frames` probe, and that join is NOT removable — `iter_unidentified` selects
     `f.path` and joins identically, and the count must match its yield exactly, so an
     orphan verdict would leave progress short of 100% forever.

340. New user-dashboard page `#visits` — nav is now Activity · Visits · Cats — answering
     *how much* where Activity answers *what happened*: per-cat visit counts over the
     trailing 6 h and 24 h, each cat's day/night split and share of named traffic, and
     the household totals. `Store.door_stats` + `GET /api/door-stats`.
     Spec: docs/specs/2026-08-04-user-visits-stats-page.md.

341. It counts the events `Store.events` produces rather than querying `identifications`,
     as `cats_overview` does and for the same reason: every visit is clustered by
     `_gap_split` and named by `_aggregate_identity`, so Visits, Cats and Activity can
     never name a moment differently — and the uncalibrated fail-safe (a NULL threshold
     names nobody) carries over for free.

342. The widest window is read ONCE and each visit folded into every window its
     `start_ts` falls inside, so the two columns cannot disagree at their shared
     boundary. `events` is walked in KEYSET pages under a `_MAX_STATS_PAGES` (8) budget
     rather than widening `_MAX_EVENTS`: that cap bounds a FEED RESPONSE, while a busy
     day exceeds 500 visits and this caller returns only counts.

343. A `since_id` of None means the window holds no frame — returned as the zeroed shape,
     never as an unbounded `events` read, which would count the newest visits in the
     WHOLE store as though they fell inside the window. Reachable whenever the newest
     frame predates the window (a stopped collector).

344. Seven EXCLUSIVE totals buckets summing to `door_events`, so every figure the page
     shows is derivable from the published totals. `unidentified` is cat-subject only —
     the number rendered as "couldn't put a name to" — with `person`/`corrupted` in
     `other`; a person at the door gets no figure of its own. A wind trigger is not a
     visit, so `cat_visits` excludes `noise`, `other` and `unanalyzed`.

345. Three honesty flags the page renders rather than swallowing: `covered` false (the
     ring buffer does not reach back that far, so the count is PARTIAL — banner);
     `truncated` (the page budget was spent); and `unanalyzed` separate from
     `unidentified`, since "nothing looked" and "looked, couldn't name" are different
     claims (entries 226/279). Without a usable gallery the per-cat rows are replaced by
     an explanation, not a column of zeroes.

346. `share` divides by NAMED visits (resident + neighbour), and is None — never 0.0 —
     when there are none: no named traffic means unmeasured, not zero. A retired cat's
     visits still count toward the totals (the active gallery may predate its
     retirement), so the listed shares need not sum to 1.

347. A measured zero renders as a dimmed DIGIT, not `—`, diverging from the spec's own
     wording: across these dashboards `—` means "not measured" (entries 106/108/113/322),
     and spending that distinction on a real zero would regress it. Day/night is omitted
     entirely without a location — including from the section's lead, which otherwise
     promised a split the rows did not carry.

348. `last_seen` deliberately does NOT come from `door_stats`. The page already fetches
     `/api/cats/overview` for avatars, and that field spans the whole retained feed while
     anything computed here would span 24 h — two fields of one name meaning different
     things on one page.

349. The retired `Who's home` placeholder is GONE, not relocated: it was blocked on
     direction detection and showed nothing. `#home` falls through the existing
     unknown-route fallback to Activity (verified) — no alias, since redirecting it to
     Visits would misrepresent what it asked for. Occupancy returns as its own page when
     it can answer.

350. Paging stops when the keyset bound `(oldest start_id − 1)` falls below the window's
     own `since_id`, WITHOUT setting `truncated`. That bound crosses the floor whenever
     the oldest cluster starts at the window's first frame; continuing asked for an
     inverted range, got nothing, and reported truncation — putting a "figures are
     incomplete" banner on a complete reading.

351. Review repairs. The page now reports `truncated` — the budget-exhausted UNDERCOUNT —
     as its own banner, distinct from `covered`'s retention partial. It was returned and
     silently dropped, so a busy day (exactly when the figures matter) rendered an
     undercount as complete: entries 97/126/167/304's trap in a new place.

352. `.kicker` is no longer scoped to `.cats-section`: the Visits tally card uses the same
     caption beside its own `h2`, where the descendant rule never applied and it rendered
     as bold heading text (measured 18.4px/700 vs the intended 13.6px/600 muted).

353. The `since_id is None` guard's test asserts `events` is NEVER CALLED, not that the
     counts are zero. The per-event `start_ts < since_ts` filter zeroes those totals with
     or without the guard, so the count-based test passed with the guard deleted — proven,
     then re-proven failing after the repair. What the guard buys is not reading at all.

354. Known limit, left deliberately: `door_stats` pages `events()` on the store's SHARED
     write connection, unlike `tuning_calendar`/`lighting_histogram`/`labeled_visits`,
     which hold their own short-lived WAL connections for exactly this reason. With the
     Visits page open, the SSE nudge refetches every ~3 s while a cat lingers. Two
     defensible fixes — a read connection under `events()` (wide blast radius: Activity
     and `cats_overview` share it) or debouncing the client refetch — so it needs a
     decision, not a patch. Per-span reads are ~5 ms, so this is added latency, not the
     19.5 s class of entries 102-105.

355. The validation-runs table gained a **Visits** column — the held-out visits actually
     scored, which is the sample the accuracy is measured over. Crops was the only count
     on the row and is the wrong x-axis for "did more labelling buy anything": visit-held-out
     scoring hides a visit WHOLE, so crops added inside visits already present move nothing.
     Read off the stored `metrics.visits`; frontend only.

356. That cell reports what the count and the score fail at INDEPENDENTLY. An unavailable
     block still shows `N found` (`n_groups` is measured before the scoring gives up), and a
     dim `+N` names the unscoreable visits — a cat with one visit has its true answer
     structurally absent from the gallery, so no number about it can exist until it visits
     again. That is the sharpest navigation item on the row and was buried in a tooltip.

357. Known blocker for a per-cat trend, found while scoping one: the visit confusion matrix
     IS persisted, but nothing records WHICH CAT each row is. `_run_metrics` keeps only
     `visits` + `excluded_cat_ids`, dropping the top-level `cats` index→id map, and the index
     is positional over the cats present in that run — so excluding one cat shifts every
     index above it and two runs' row 3 are different cats. Existing rows are untrendable
     and cannot be back-filled (a re-run measures today's labels). Fix is additive: persist
     `cats` beside `visits`; the trend then starts from the next run.

358. Per-cat recall now sits on the Model page's "Cats to enrol" table — visits scored,
     recall with its declined share, and a day/night pair — so the weakest cat is visible
     where the enrol/cap decision is made. That card's hint already said recall tracks
     visits rather than crops and a cat can hold the most crops and still be worst
     recognised; it now shows the number instead of only naming it.
     Spec: docs/specs/2026-08-06-per-cat-recall-on-enrol-table.md.

359. `_score_visits` returns `per_cat`, derived beside the `accuracy` built from the same
     counts so the two can never contradict each other. Because `_visits_block` spreads
     that return and each `regimes[name]` IS a `_score_visits` return, one addition lands
     per-cat rows on the mixed block AND both regimes — and the whole `visits` block was
     already persisted and lifted, so `runner.py`, `probe.py` and the store are untouched.
     Rows self-identify by `cat_id`, retiring the positional confusion index (entry 357)
     rather than teaching a second consumer to read it.

360. Deliberately NOT passed to the `cross` cells: per-cat cross-regime is a non-goal, and
     computing rows nothing renders would put a plausible-looking but unread number into
     every stored run.

361. Recall is `correct/(correct+wrong)` with declined reported beside it, never folded in
     — at the door "named the wrong cat" and "declined to name" mean opposite things. So
     a cat whose every visit was declined reads `— · 100%`: the dash is honest (nothing
     was decided, which is not a recall of zero) and the 100% is what explains it.
     Five cell states in all, each distinguishable: scored, nothing-decided, unscoreable
     (`0 +1`), absent from the run, and a run with no per-cat data at all.

362. The frozen columns are stamped with the run behind them and DIM when it no longer
     matches the Build grades or the enrol ticks — both, since an exclusion changes which
     cats every held-out visit was matched against. Dim rather than hide: the numbers stay
     the best available reading, and hiding them on each checkbox toggle would flicker the
     column during exactly the fiddling the table is for.

363. Comparability normalises both sides, because neither is stored in the form the compare
     implies. `all` is NOT equal to `gallery+ok+poor` — an explicit grade filter excludes
     NULL-quality crops, so they score different sets — while an absent, null, and empty
     exclusion list all mean "excluded nobody" (`_run_metrics` omits the key when empty,
     `getExcluded()` returns null), which unnormalised would dim permanently in the
     commonest configuration.

364. Growth in the labelled set is NOTED ("+N crops labelled since"), never dimmed — it
     would grey the columns on the very next label, and watching that number grow is the
     point. Reported only while the run is otherwise comparable, since across different
     grades the two crop counts are over different sets.

365. Three empty stamps that would otherwise render alike as a blank line over three
     dashes: no run yet, a runs fetch that FAILED (`loadRuns` swallows its errors, so
     silence reads as "no run exists"), and runs that carry no per-cat data. The last
     names the deployment gap too — "if a fresh run still shows this, the compute PC is on
     an older build" — rather than only the benign cause, per entries 164/173/183.

366. `loadRuns` gained a sequence guard now that it drives the cats table as well as its
     own; two overlapping fetches landing out of order would paint an older run's recall
     under the newer stamp. A grade tick re-renders WITHOUT refetching it (verified: the
     runs request count is unchanged across a tick) — the ticks change no run, only the
     client-side comparability verdict, and refetching per checkbox would walk back
     entry 201.

367. Review repairs to the above. The "+N crops labelled since" note counted EXCLUDED cats
     on one side only — `enrollable_cats` returns every active cat while the run's
     `n_crops` was counted post-exclusion — so holding a cat back made the stamp report its
     whole crop count as newly labelled, on every render, forever. Now summed over the same
     set the run used, which `runComparable` has already established.

368. Three smaller ones. The 5→8 column change left the flexible kind column 50px at the
     shared 720px table floor and clipped "neighbour"; it has its OWN 780px floor rather
     than a raised shared one, since the other `wtable` users have different geometry.
     Driving `renderCats` from `loadRuns` let it win the mount race and assert "No active
     cats" — a claim, not a blank — until the roster landed; it now waits.

369. A run that RAN and reported `available:false` (no cat with two visits — an ordinary
     early-dataset state) no longer draws the "compute PC is on an older build" stamp: it
     is a data state, re-running Validate reproduces it, and the message sent the operator
     after a deployment problem instead of "label more distinct visits". The older-build
     wording now fires only when no run carries a visits block at all.

370. The feasibility report leads with an "In short" block: five numbers, two charts, and
     what the probe structurally cannot tell you. It had grown five sections with the
     honest number and an inflated one under similar headings, and nothing said which to
     act on. Every figure is a re-reading of the visit block below — never a second
     measurement — so the summary cannot disagree with the section it summarises.

371. The five: visit accuracy with a WILSON 95% interval, declined rate, the weakest cat,
     the largest off-diagonal confusion cell, and the night-vs-day gap. Wilson because
     8-of-8 has a NORMAL interval of exactly zero width, presenting eight sightings as
     certainty. The interval sits ON the headline — its width is what a reader comparing
     two runs most needs and is least likely to seek out.

372. Intervals and gaps are labelled "pts", not "%" — they are differences BETWEEN
     percentages, and "±7%" beside "88%" invites reading it as 7% of 88. The limits block
     states the resolution outright ("one visit is 0.8 points"), which is the direct answer
     to a week of labelling moving the headline the wrong way.

373. The limits are real limits, not a disclaimer. Load-bearing one: NO STRANGER is in the
     probe — every crop belongs to a labelled cat, so nothing here measures whether a
     foreign cat is refused, the half of the job the door needs. It also names the
     unscoreable cats (excluded from every figure) and warns that the demoted per-cat table
     further down is the CROP-level number, not this section's.

374. Two charts, both EMPHASIS rather than categorical — the story is one cat, not six.
     Per-cat recall is worst-first, weakest bar accented, with Wilson whiskers: sorted
     bars invite reading the order as a ranking, and at 4 visits against 40 that order is
     mostly noise. Day/night is a dumbbell, because the reader's question is the GAP and a
     connector draws it instead of asking them to subtract two bar lengths.

375. The dumbbell uses orange over the palette's amber slot: amber measures 2.11:1 here,
     and the validator's relief for that (a table view) does not exist for PER-CAT
     day/night. The chosen pair passes every check, worst adjacent CVD dE 29.5. Its legend
     sits ABOVE the axes — every dot lands at high recall, so the default lower-right
     corner is where the last row's pair falls (measured: it covered them).

376. Both charts return "" when their data is absent (no per-cat rows, no day/night without
     a location) and the block skips a chart it has no PNG for, so a location-less run
     shrinks the summary rather than drawing one regime as if it were both.

377. Review repairs. The TL;DR's "weakest cat" tile and the chart beneath it picked the
     weakest with DIFFERENT tie-breaks — a plain `min` (first row, i.e. lowest cat_id) vs
     the chart's (recall, -scored) — so on a tie one block named two different cats
     weakest. Recall is a ratio of small integers, so ties are routine: 1/2, 2/4 and 3/6
     all land on 0.5. One shared `_WEAKEST_KEY` now orders both.

378. Three smaller ones. The per-cat chart labelled its (n) "visits scored" while plotting
     DECIDED (recall and its interval are both over correct+wrong), which diverges wherever
     a cat has declines. The night-gap tile rounded after signing, so a −0.4 pt gap read
     "-0 pts". `_worst_pair`'s `of` summed the declined column the function otherwise
     drops, quoting a denominator its numerator never came from.

379. The day/night dumbbell now carries each side's decided count, as its sibling bar chart
     already did: a night dot off ONE visit was indistinguishable from one off twenty, and
     the night side is exactly where the counts are smallest. A static PNG has no tooltip
     to hide the number in.

380. Both charts gained tests — a regression to "" would otherwise drop a figure from every
     report silently. They pin the empty cases each returns nothing for (no rows, nothing
     decided, no regimes, a cat present on only one side, mismatched cat_ids across
     regimes) and that the tile and the chart cannot disagree on a tie, verified to FAIL
     against the pre-fix rule rather than merely passing after it.

381. The Motion-tuning calendar MEMOIZES each day in a new `calendar_days` table, so only
     days that actually changed are re-earned. Counting a 4-week window exactly means
     walking every frame and verdict index entry: measured 11.3 s on a 6M-frame /
     16.2M-verdict replica, growing with the store, and paid on every page load.
     Now ~0 ms warm, 9.7 s cold, ~0.7 s after a sweep of one day.

382. Nothing about that is a heuristic: every mutation that can move a day drops it.
     Frame deletes and every `analysis` write/clear match by id span
     (`_calendar_invalidate_locked`); a frame INSERT — whose id is above every existing
     span — matches by timestamp instead. The memo and its invalidation both live in the
     DB, so a second process's write is seen by the running server (verified).

383. A day is reserved PENDING before the slow passes and filled in by a token-guarded
     UPDATE after, so a mutation landing mid-compute deletes the placeholder and the
     fill-in matches nothing. Without it the naive "just write the result" form caches
     numbers already stale — proven by running its test against that form.
     A pending row is never read, so a crash mid-compute leaves no result behind.

384. Every calendar number is now a property of the DAY, not of the requested window —
     which is what makes a day memoizable at all. The event pass therefore looks
     `_VISIT_GAP_MS` back past local midnight for a cluster's predecessor instead of
     treating the window edge as a cluster start. A day the window only CLIPS is not
     memoized, since its numbers would then describe the request.

385. The coverage pass counts with conditional SUMs, retiring the `+analyzer` hint
     (entries 229/265/276/307) rather than depending on it: with no analyzer predicate
     at all there is nothing left for the planner to mis-choose. Measured 10.1 -> 8.0 s
     over 16.2M verdicts — the hint had also forced a temp b-tree for its GROUP BY.
     Dropping the hint from the OLD form instead was 45x worse, so it was load-bearing.

386. Known limits, both accepted. While the collector runs, TODAY is invalidated ~10x/s
     and so is recomputed every load (~1.4 s for a full day on the real store); it
     memoizes like any other day once collection stops. And the first load after this
     ships is still a cold one — the memo is persisted, so a restart does not re-pay it.

387. Review repairs. A day is memoized only if the ids it actually COUNTED lie inside the
     ids the placeholder RESERVED. The reservation comes from a `recv_ts` seek, so the two
     coincide only while `recv_ts` is non-decreasing with `id` — assumed, never enforced
     (entry 278) — and where it isn't, a mutation to the difference slips past the
     placeholder and a wrong count is cached with nothing left to correct it.

388. The memo is re-read PER DAY inside the loop, not snapshotted once before it. A day's
     compute takes seconds, so an invalidation landing during it must still be seen for
     the days not yet reached; otherwise the same call serves numbers that mutation just
     falsified. A day already read stays as read — that is a read's point-in-time
     staleness, not a cache fault, and no memo design removes it.

389. Pruning the memo now reads before it writes, and reaches BOTH ways. A fully memoized
     call previously opened a write transaction and committed on the shared connection to
     delete nothing — the exact traffic this feature moved off it — and days ABOVE the
     window accumulated forever, since only days below were pruned.

390. Entries 1–221 moved to the new `docs/CHANGELOG-archive.md`, the first exercise of the
     archive convention: this file had reached 189k chars, so the session-start orientation
     read it exists for no longer fit. Six unnumbered milestone summaries stand in for them.
     Each carries forward the conventions later entries assume rather than merely naming a
     phase, since a cited entry number now resolves in the archive, not here.

391. A SCOPED gate scorecard pins `frames` as the join's outer loop (`CROSS JOIN`, which
     constrains order in SQLite without changing meaning), so a one-day Compare costs the
     day instead of the store. Entries 229/265/276/307/385, fifth instance and the worst:
     with no ANALYZE stats SQLite drove from `idx_analysis_analyzer_verdict`, walked the
     oracle's WHOLE partition and applied the window bound only after probing `frames` —
     three queries deep, three columns wide.

392. Measured on a 6M-frame / 13.5M-verdict replica over one 750k-frame day: one Compare
     click 126.7 s -> 5.7 s, with every card's visit and missed count identical.
     The honest figure is the VDBE-step ratio, ~3x (live 214M -> 63M, a slot 234M -> 81M):
     wall clock flatters it to 22x because the old plan also random-probes rowids across
     the whole file, which this cache-starved dev box punishes far harder than the compute
     PC will.

393. The warm-up-threshold probe now costs ZERO VDBE steps. `ORDER BY f.id` is free in
     rowid order, so `LIMIT 1` stops at the first scored row instead of materializing the
     window into a temp b-tree — the pin's largest single win, and invisible in the SQL.

394. UNSCOPED cards are deliberately NOT pinned: absent a range there is nothing to seek,
     so pinning buys nothing (live 518M -> 471M steps) and costs a little on a slot column
     (263M -> 276M). The unscoped SQL is byte-for-byte what it was, so that path carries
     none of this change's risk.

395. Two fixes measured and REJECTED, so they are not re-attempted. De-indexing the
     analyzer term with `+` (the entry-229 remedy) is 6.7x scoped but degrades unscoped to
     a full `SCAN o` — worse than today. And dropping the 11-arm aggregate in favour of
     `interesting` + a count buys nothing: a bare `COUNT` costs the same as the full
     aggregate (the SUM arms are free; the join walk is the cost), and an index-only
     `analyzed` count off `analysis` alone measured 3.0-4.6 s against the aggregate's 2.0 s.

396. Plan pinning is now a TEST, not a comment: `test_scorecard.py` asserts each scoped
     query's plan seeks the frames id range and each unscoped one does not. Nothing else
     can catch a regression here — the numbers are identical either way, which is how this
     class has recurred five times. Verified to fail against the reverted pin, and the
     default plan is wrong even on a 12-frame store, so it needs no big fixture.

397. The MOG2/BSUV re-run decodes AHEAD on a small thread pool while inference stays
     strictly serial. Its rolling background needs every frame in order, but decode is a
     pure function of the file, so consuming futures in submission order feeds the analyzer
     the identical sequence. Decode is 74% of a serial iteration (4.79 ms vs the gate's
     1.71 ms at 1280x720 q90), so 2 workers took the re-run 166 -> 332 frames/s: one day of
     capture ~88 -> ~44 min per slot, and a tune compares two slots.

398. Workers/lookahead are env knobs (`CAT_WINDOWED_DECODE_WORKERS`, default 2;
     `CAT_WINDOWED_DECODE_LOOKAHEAD`, 8) because more is NOT monotonically better — 2 beat
     4 and 6 on the dev box, decode being memory-bandwidth bound. The lookahead is what
     bounds the memory: a 2304x1296 BGR frame is ~9 MB, so 8 in flight is ~72 MB.

399. Deliberately NOT the stateless path's producer-thread + queue + sentinel machinery: it
     exists because that consumer batches GPU calls and must drain asynchronously, whereas
     the windowed consumer is a plain in-order loop, where a bounded deque of futures keeps
     order by construction with no anti-wedge protocol to get right.

400. Tests pin the decode-ahead's whole contract: order preserved at five (workers,
     lookahead) settings, byte-identical verdicts vs a forced-serial run, an undecodable
     frame skipped in place (its exception now surfaces at `result()`, not inline), and a
     cancel persisting exactly the prefix it computed. Verified to FAIL against an injected
     reordering — the `(1,1)` serial case correctly still passes, a one-element deque
     making `pop` and `popleft` the same.

401. `count_before_capped` replaces the Compare's exact pre-window frame count, which was a
     rowid scan from id 1 to the window (66 ms at 6M frames, growing) for a number that
     saturates at 500. It matters beyond the milliseconds: that scan held the shared write
     lock against the collector on every click — the starvation class of entries 102-105.
     Named apart from `count_in_range` because it returns `min(preceding, cap)`, not a total.

402. Remaining Compare headroom, deliberately left. Each column still walks its window
     twice (aggregate + `interesting`), ~2 s apiece on the replica; fusing them saves only
     ~20% since the walk, not the arms, is the cost. The structural fix is memoizing a card
     per (day, source, oracle, params, floor) as `calendar_days` does for the calendar
     (entry 381) — a feature with real invalidation subtleties, not a patch.

403. The annotation queue gained "Hide single-frame visits" (`min_frames`), ON by default —
     unlike the confidence filter beside it, because thin visits are the everyday case. A
     one-frame visit yields ONE crop, which will not be gallery-grade, so a gallery-only
     build AND a gallery-only validation run both ignore it entirely: labelling it costs
     attention and returns nothing. Spec: docs/specs/2026-08-09-annotation-queue-min-frames.md.

404. It floors on frame COUNT, deliberately not on "has a gallery-grade frame" — the
     predicate a build actually enrols on. Computing that server-side needs a second copy of
     the client's `seedQuality`, and that formula has a known defect: a one-frame visit's
     area ratio is 1.0 BY CONSTRUCTION (the frame is its own peak), so its area test cannot
     fail and it grades `ok` structurally. Revisit the predicate once the seed is fixed.

405. Each hidden count is measured with the OTHER filter still applied — `hidden_confident`
     = confident AND thick, `hidden_thin` = thin AND uncertain — so unticking either control
     reveals EXACTLY the number quoted. Counting each filter's full catch independently
     would make `hidden_confident` include confident-and-thin visits that unticking would
     not reveal: a promise the page cannot keep, in the readout that exists to stop a
     filtered queue reading as a finished one.

406. Consequence: a visit failing BOTH predicates is counted in NEITHER, so the two counts
     do not sum to the number hidden. Status line and empty stage therefore name each figure
     beside its own control and never show a total; both read one shared composer so they
     cannot describe the same queue differently. Browser-verified on 8 seeded visits: both
     filters on → shown 2, "2 confident, 3 single-frame hidden", not the 3 and 4 totals.

407. The two filters are evaluated TOGETHER, replacing the sequential `uncertain_only`
     block, because sequencing makes each count depend on which ran first. Both orderings
     produce the SAME surviving set and differ only in the counts, so nothing catches a
     re-sequencing by inspection — pinned by a test asserting each untick reveals its quoted
     number, verified to FAIL against the sequential form (`hidden_confident` 3 vs 2).

408. `min_frames` is floored at 1 with NO upper clamp, unlike `limit`: any floor above the
     largest visit empties the queue legitimately, so a bound would not prevent that —
     `hidden_thin` is what keeps it from being SILENT. 1 is the API's no-op default and the
     default-ON behaviour lives entirely in the client, so no other caller of
     `/api/label/queue` changes.

409. `hidden_total` is that same number MEASURED (pre-filter minus post-filter), never summed
     from the two, and is the ONLY field a "nothing left" readout may gate on. Gating on the
     per-control pair declared the queue CLEAR — 🎉 and all — whenever every remaining visit
     failed BOTH predicates, since neither count claims those: reachable from the ordinary
     default floor plus "hide confident matches". Entries 97/126/167/304's trap, self-inflicted.

410. That empty state now names the total and says unticking either control ALONE will not
     reveal them, because no single-control number describes a visit both filters hid. The
     genuine all-clear still celebrates, gated on the measured total. Found by review, not by
     the browser pass — which had walked every OTHER combination.

411. The before-the-cap regression test was HOLLOW as first written: its "thin/newest" visits
     carried the OLDER timestamps, so the thick ones survived truncation whichever order ran
     and the test could not fail. Proven by injecting the exact regression — old data passed,
     repaired data fails. A test whose data contradicts its own comment asserts nothing.

412. The crop-grade formula is now PYTHON (`store.seed_quality`) and the single source of
     truth; every visit payload carries `seed_quality` per frame and the client displays and
     echoes it. Forced by the queue filtering on the grade: a filter server-side and a tally
     client-side computed by two copies of one formula is a queue that can contradict its own
     readout. Spec: docs/specs/2026-08-09-annotation-grade-tally.md.

413. The annotation stage now says what a decision WOULD contribute — "would add 1 gallery /
     2 poor" — in Queue and Flagged alike. `rep`/`peak` never answered the question the
     operator actually has, and for a 2-frame visit the deciding number (the non-rep frame's
     area ratio) was on no screen. Verified end to end: a visit reading "would add 3 gallery"
     wrote exactly three `gallery` rows.

414. `seed_quality` is kept apart from `quality` — the grade a label would write vs the grade
     a `dataset_items` row STORES. Labelled review shows the second and writes the first, so
     collapsing them would make its tally and its re-label disagree about one crop.

415. New `require_gallery` queue filter, ON by default: keep only visits holding a crop a
     quality-filtered build would enrol. It does NOT subsume the frame floor and both are
     kept — a lone frame's area ratio is 1.0 by construction, so a single-frame visit at
     ≥60% grades `gallery` on a comparison with itself, which this filter SHOWS and the
     floor hides. Only the pair means "would contribute a crop worth enrolling".

416. `canSeed` and its "your compute PC is on an older build" banner are GONE with the
     client formula. They existed because the client could not grade a payload lacking
     per-frame scores; the server always has the score and the peak area, so the state
     they warned about is now unreachable.

417. The grid test pins both gates at their exact edges, transcribed from the JavaScript it
     replaces — verified to FAIL against three separate slips (a 0.7→0.75 threshold, `>=`→`>`,
     `or`→`and`). Transcription was this change's only real risk: a silent slip would re-grade
     every future crop, and nothing else would have noticed.

418. ARCHITECTURE now describes all THREE queue filters and says which two ship on; it had
     claimed a single opt-in "hide confident matches". The drift started with entry 403 and
     compounded here — and the doc's job is stating what the DEFAULT view shows, which had
     silently become "two predicates narrower than this paragraph says".

419. Every visit payload asserts it carries `seed_quality`, not just the queue's. The client
     echoes that field straight into `POST /api/label`, where `quality` defaults to None, so
     a builder that stopped attaching it would write NULL-grade crops a quality-filtered
     build then skips — silently. These assertions replace the `canSeed` guard that used to
     catch that state and was deleted with the client formula (entry 416).

420. A flagged span's tally reads "would add N ok SO FAR" while coverage is partial: it
     counts only the frames actually swept, so Analyse can still add to it. Entry 226's rule
     — flagged state is read from coverage, never from verdicts merely existing — applied to
     a new readout.

421. Changelog ordering repaired: entries 409-411 had been inserted ABOVE 407/408, stranding
     two entries after 417 so the file no longer ended at its newest. Now strictly monotonic
     222→421. The convention exists because this file is read top-down at session start.


422. Run 11's 23 visit errors classified by DOOR consequence, which reframes the whole
     identification effort: 13 foreign→foreign, 5 resident→resident, 1 resident→foreign,
     and only **4 foreign→resident** (a stranger let in). The Store Sultan ↔ Store Jihn
     cell everyone was chasing is 57% of errors and ZERO of the door-relevant ones.
     Wrong names on the timeline are a quality problem; the safety problem is elsewhere.

423. Validation can now measure STRANGER REJECTION, which nothing could before: every crop
     the probe scores belongs to a labelled cat, so "how often is an unenrolled cat given
     one of our names" had no number at all. Each cat is held out of the gallery in turn
     via the existing `gal_mask`; its visits then score *unknown* as CORRECT. Impersonations
     split resident vs neighbour — only the first matters at the door.
     Spec: docs/specs/2026-08-09-open-set-scoring-and-calibration.md.

424. That inverts `_score_visits`' `unscoreable` branch, which previously SKIPPED exactly
     these visits — so it is an explicit mode, not a consequence of the mask. Held-out
     residents are scored too and reported apart: that is the "not enrolled yet" case, which
     is what registering a new cat looks like. The threshold grid is computed ONCE on the
     unmasked pass and passed in, or each masked pass would sweep its own range and the two
     curves could not be read against each other.

425. `model_versions.threshold` is now SETTABLE (`POST /api/training/models/{id}/threshold`,
     plus a Model-page field). It was written once by `build_gallery` and only ever read, so
     the operating point could not move without a rebuild — and the active model declines
     0 of 515 visits, i.e. the open-set fail-safe is calibrated off. Applied at read, so a
     change needs no re-identify and RESTATES all history; the UI says so in as many words.

426. Two provenance stamps ride that write because both outlive the run that justified them:
     `metrics.threshold_built` (copied from the column only when ABSENT — no existing row has
     it, so a first override would otherwise destroy the built value) and
     `threshold_source_run_id`. Bounded `[0, 2]`: unbounded, a mistyped 4.36 for 0.436 puts
     every cat AND every stranger below the cutoff, silently, across all history.

427. A validation run now forecasts a CAPPED gallery — `cap_per_cat`'s selection as a
     `gal_mask` over the same matrix, several caps per run — and recalibrates the threshold
     under each mask. Capping is meant to fix both biases `cap_per_cat` names; reusing the
     uncapped threshold would answer only the density half. Both columns are reported so
     which half moved is visible rather than inferred.

428. Crops carry a GEOMETRY: `letterbox` (aspect-preserving pad, at the normalisation-zero
     mean — black would inject a constant into every vector) and an optional context margin,
     replacing the anisotropic 224×224 squash that distorted boxes up to 4.8× and differently
     per frame within one visit. `dataset_items.geometry` stamps it PER CROP; NULL = legacy,
     which every crop cut before this is. Default stays legacy so promoted galleries keep
     matching their own queries.

429. Per-crop, not global, because the second geometry change after any frame eviction would
     otherwise leave two conventions side by side with no record of which is which — and
     1-NN over a blend of two feature spaces is silently worse matching with no symptom.
     `build_gallery` therefore SELECTS one convention rather than filtering to "any", and
     `compute.tools.recut_crops` re-cuts what it can (only rows whose source frame is live)
     through the new `Store.update_dataset_geometry`.

430. That store method returns the ids it MATCHED, never a count: the tool deletes a crop's
     old file only for rows that actually moved, and a row a concurrent relabel replaced
     mid-run no longer owns that path. It is an UPDATE rather than the public
     delete-then-re-insert, which would drop the label for the gap, reset `labeled_ts`
     (entry 323 relies on it) and lose `source` — on the one artifact that must not be lost.

431. Geometry joins the job `params` for BOTH kinds, so the two arms of a crop-shape A/B are
     two jobs rather than one deduped press, and lands in the artifact dir slug. Canonicalised
     first (`m10` == `m10.0`), or two spellings would dedup apart and claim two dirs for one
     crop set. `count_identified_crops` gained the same filter, since a pre-check that
     counted crops the build then discards would wave through a build that finds nothing.

432. Deliberately NOT built: a persisted embedding cache with a `rescore` entry point. Every
     question here is a mask over a matrix the run already holds, a preprocessing arm needs
     a fresh embed regardless, and paired comparison comes from the per-visit outcome list —
     so it bought asking a new cap value later in exchange for a second scoring path to keep
     byte-identical forever. Reversed mid-spec after review; the earlier claim that it made
     paired comparison possible was simply wrong.

433. A dedup test built on a fake that returns IMMEDIATELY asserts nothing: dedup only guards
     against the RUNNING job, so the second press is never deduped whatever the params say.
     Two of this change's tests passed against the injected regression for that reason and
     were rebuilt on a gated fake. Entry 411's hollow-test trap, second instance — the tell
     is the same, a test whose scenario cannot reach the condition it names.

434. NO annotation mode shows a per-crop strip any more: Flagged review's was the last, and
     the player (entry 291) answers "what does this span hold" better — a 72px thumbnail of
     a top-down crop is legible only by luck. The per-crop reading a decision rests on
     survives twice: the grade TALLY on the meta line, and the player's caption, which names
     each frame's grade and the rep. Also one crop decode per stage, not one per span frame.

435. The capped forecast reports the DENOMINATOR each rate is read off (`n_scored` + a dim
     `+N` unscoreable), because a cap can flatter — the direction entry 427's docstring
     denied. A tight cap can leave a cat's every surviving vector inside the visit being
     held out, so that visit becomes unscoreable and LEAVES the denominator instead of
     counting wrong: measured 20 visits → 16 and recall 40% → 56% with no gallery change.
     Compare rows only at equal visit counts; both docstrings corrected.

436. Nulling a threshold now confirms from ALL THREE entry points, not just the Uncalibrated
     button. A blank field submitted with Enter or Apply reached the identical write — the
     ordinary slip being select-all, delete, Enter on the way to retyping — and on the active
     model that names every visit "unknown cat" household-wide. One shared helper, so the
     three cannot drift about what they warn.

437. `recut_crops` probes for `dataset_items.geometry` before reading it. The column is added
     by `Store._migrate_schema`, which runs only in `Store.__init__` — and the tool builds a
     Store only on `--apply`, so its DRY RUN (the first command its own docstring gives) died
     with a bare `no such column` on any store this build had not yet opened. Probe, not
     migrate: a dry run keeps its write-nothing contract, and the answer is an instruction.

438. `metrics.threshold_source_run_id` is rewritten on every `set_model_threshold` and nulled
     when no run is named. It was only ever written, so a later hand-set value carried the
     earlier run's id — attributing an operating point to grades and exclusions that produced
     a different one. A stale justification is worse than the absent one the stamp was added
     to fix. `threshold_built` still copies once, and is unaffected.

439. Two message repairs, both the same class — a readout naming one cause of several. The
     Validate/Build pre-check now lists EVERY blocker (geometry AND exclusion) via
     `gallery.build_gallery`'s hints-list shape, instead of a ternary chain that sent the
     operator to re-cut and re-run before meeting the exclusion that also blocked; and the
     dimmed-recall stamp names crop shape, the third term `runComparable` already tested.

440. Two review findings left UNFIXED by decision, so they are not re-litigated as new. A
     letterbox-only flip strands crops whose source frames evicted — `letterbox` is a
     read-time choice touching no stored pixel, yet the tool needs a live frame to move the
     stamp; the honest fix is to stop stamping it on a file at all, which is a redesign worth
     taking BEFORE a second geometry arm ships. And the probe CLI's pre-count print
     overstates on a mixed-geometry store; the probe's own count is authoritative.

441. Known gaps in the open-set increment, from the review's coverage sweep. The cap ladder
     and stranger pass are default-ON with their memory cost UNMEASURED — entry 329 measured
     this exact axis at 2109 → 2439 MB, and this adds four sequential pair-length passes, so
     one real-dataset run on the compute PC should precede trusting the default. New labels
     land LEGACY, so a store re-cut to letterbox starts re-splitting immediately; the spec is
     silent on the write path. And `visit_outcomes.json` has no reader, so the spec's paired
     comparison ships as data with none of its arithmetic.

442. Entry 325's exact trap, SHIPPED this time: entry 435's new cell put a backslash inside
     an f-string `{}`, a SyntaxError before 3.12 that fails at IMPORT — so `compute.api.app`
     would not start at all on the 3.11 compute PC, reporting a bare `)` as the error. Built
     outside the f-string now. `compileall` on this 3.13 dev box accepts it and all 1059
     tests passed, so nothing local could see it.

443. New `test_python_floor_syntax.py` guards the PYTHON FLOOR, not just this interpreter:
     it walks every module's f-string replacement fields and rejects the two constructs PEP
     701 legalised (a backslash, or reuse of the enclosing delimiter). `ast.parse` cannot
     catch these — on 3.13 they are valid — so the check inspects the fields itself and
     reports the same violations on any interpreter. Verified by re-injecting entry 442.

444. That guard keeps the delimiter WHOLE (`\"\"\"` ≠ `\"`). Inside a triple-quoted f-string a
     lone `"` was always legal and probe.py's report builders rely on it, so collapsing the
     delimiter to one character reported three false positives there — a guard that cried
     wolf would be deleted, taking the real check with it.

445. New admin **Tools** page (last tab) resolves the geometry blocker from the browser: a
     census of what crop shapes the labelled set holds, a target picker, a per-branch plan,
     and a cancellable job. The four messages that sent the operator to `compute.tools.recut_crops`
     now name the page instead. Housekeeping moved here too (non-motion purge, orphan sweep,
     Clear all frames); Start narrows to stage + collection.
     Spec: docs/specs/2026-08-10-admin-tools-page.md.

446. Cutting is now the LAST resort, not the only path. `recut` routes each row three ways:
     **relink** (a file already sits at the target path — move the stamp, touch no pixels),
     **copy** (another kept file carries the target's MARGIN, so the pixels are right and only
     the path is wrong), **recut** (decode the frame). Only the last needs a bbox and a live
     frame, so a `letterbox`-only flip strands nothing — it alters no pixel. For a fixed
     margin exactly TWO geometries exist (letterbox on/off), so finding a pixel-equal file is
     two stats, not a directory walk.

447. Superseded crops are KEPT, for the CLI too, so a geometry move is reversible without a
     source frame — and returning to an arm already visited is a pure relink, long after those
     frames evicted. That is what makes A/B-ing several crop shapes a sequence of quick hops.
     Cost: one copy of the labelled set per shape tried, outside the frame store's byte cap,
     reclaimed only by hand. `old_files_removed` became `old_files_kept`.

448. Keeping them broke an invariant two other paths relied on: a crop file may outlive a
     geometry MOVE but never its ROW. `_delete_crop_files` removed only the row's current
     `crop_path`, so a relabel left the other shapes' files orphaned and a later hop would
     `relink` one cut from the PREVIOUS bbox — stamp, bbox column and pixels disagreeing, with
     nothing downstream able to notice. It now globs every variant.

449. `update_dataset_geometry`'s compare-and-swap gained `bbox`. Path alone could not catch a
     relabel to the SAME cat: it re-commits at the identical legacy path, so id and path both
     still match a row the run never read. The box is the exact predicate — it is what must be
     unchanged for the cut pixels to still belong to that row. Compared with `IS`, so a NULL
     box is null-safe rather than matching nothing.

450. `recut`'s `on_progress` is now the cancel signal too (falsy stops), the convention
     `embed_paths` defines and the managers produce — but it BREAKS and returns the partial
     summary rather than raising as that one does, since a canceled run still has to report
     what it moved. Load-bearing consequence: the CLI's own `progress` returned `None`, which
     is falsy, so it had to start returning True or the CLI would stop after one batch and
     print it as a clean success.

451. `POST /api/cleanup/run` takes a third kind, `recut`, with `GET /api/recut/plan` as its
     separate reader (a census plus a per-branch breakdown is nothing like the estimate
     endpoint's `{count, bytes}`). `geometry` is REQUIRED there — absent means legacy for a
     READ and would mean an unrequested whole-set move here — so legacy gets the CLI's own
     `legacy` sentinel, or it becomes the one target reachable onto but never back from.
     Absent on the PLAN endpoint still means census-only.

452. Re-cut and the training queue now refuse each other (409, both directions). A build
     embedding the labelled set while a re-cut moves it reads a set that no longer exists as
     read, and `_embed_items` skips a missing file in SILENCE — a quietly smaller gallery, not
     an error. Advisory only: the two managers share no lock and the CLI bypasses it, which is
     acceptable now that superseded crops are kept.

453. Two UI traps the browser pass caught. The census refresh was keyed on a running->idle
     TRANSITION, which an instant relink job never shows — so the pre-move census sat there
     reading "nothing happened"; it now keys on a flag set when we start the job. And the
     re-cut button owns its own `disabled`: `renderCleanup`'s shared sweep assigns
     `b.disabled = c.running` unconditionally, re-enabling on every idle poll a button the
     card had disabled for having no target.

454. Chasing a phantom stale census cost most of that browser pass: the browser was serving a
     CACHED `/admin` from before the edit, so every check ran against the old JS. Verify the
     page under test is the page on disk (`innerHTML.includes('<a new symbol>')`) before
     believing any UI symptom — a cache-busting query param is the cheap fix.

455. Review repairs. `relink` now re-checks the target file exists at WRITE time. `_route`
     decided it at read time and the branch does no I/O, so unlike copy/recut nothing else
     would notice it had gone — a manual reclaim or a concurrent relabel's variant-delete
     landing mid-run would stamp the row onto a missing file and report it MOVED, the one
     outcome the module promises cannot happen. Narrows the window rather than closing it.

456. The recut/training lockout is narrowed to the kinds that actually embed crops
     (`feasibility`, `gallery-build`), mirroring `_refuse_during_recut`'s own narrowing.
     `identify`/`visit-identify` read frames and write identities, never touching
     `dataset_items` files, so a whole-store Identify pass was refusing a re-cut for no
     reason. A QUEUED embed kind still blocks even when nothing is running yet.

457. Two Tools-page repairs. A failed plan's error was written and then overwritten by
     "Planning…" in the same tick (no `return` before the shared re-render tail), so the
     card stuck on a progress word with no hint the request failed. And the crop-shape
     select is disabled while any job runs — changing it mid-run re-planned a different
     target over a card whose job readout belonged to the running one, and the
     post-completion re-plan reads the select, so it then described a shape that job never
     moved to.

458. `CleanupManager.start_recut` gained the end-to-end test its two sibling kinds already
     had. Every other recut test drove `recut_crops` directly or was blocked by a busy
     fake before `start_recut` was reached, so nothing checked the manager's own wiring —
     `recut_plan`'s target staying in lockstep with the one handed to `recut`, and the
     progress closure's cancel contract. Targets `letterbox` so the move is a copy, keeping
     the test cv2-free like the rest of that file.

459. The feasibility probe runs in FLOAT32 and no longer materialises what it can index
     around. A real run at ~27k crops died allocating 2.72 GiB inside `_best_threshold`;
     the labelled set had simply outgrown an O(n²) design nobody had re-measured since
     entry 329 sized it at n=6000. Four changes, none touching a reported number:
     float32 throughout, no similarity/`d_knn` twin matrices, blockwise kNN argsort, and
     row-wise upper-triangle construction in place of `np.triu_indices`.

460. Sizes each one removed, at n=27,011: `triu_indices`' int64 index PAIR is 5.8 GB —
     larger than the 1.5 GB of distances it addresses, and gathered through twice per cap;
     `np.argsort(dist, axis=1)` allocates n² int64 (5.8 GB) to keep n×k of it; and
     `dist = 1.0 - sim` as two statements keeps both matrices alive. The kNN's diagonal is
     now masked on `dist` itself and restored from a `.copy()` — `np.diagonal` returns a
     VIEW, so saving without copying restores the `+inf` it just wrote.

461. `_best_threshold`'s candidates are the SAME distances only, never `same ∪ diff`, and
     that is exact rather than a sampling: between consecutive same-values TPR is flat
     while TNR is non-increasing, so the optimum always sits at a same-value, and the
     argmax tie-break agrees because the dominating candidate is the smaller one. `diff`
     is ~95% of pairs here, so this alone is what the fatal 2.72 GiB allocation was.

462. Measured on synthetic 768-d embeddings with the visit block on, peak RSS: n=3996
     1335 -> 465 MB, n=7998 5111 -> 1305 MB — a 4.5x smaller n² coefficient, with `auc`,
     `suggested_threshold` and kNN accuracy identical to 1e-6 (the float32 change). Do NOT
     read peak RSS above ~3 GB on the dev Mac: at n=12000 the old code ran 36 s against
     14 s and reported LESS memory than the fit predicts, because macOS compressed pages.

463. Known limit, and the reason this is a reprieve rather than a fix: the design is still
     O(n²), so the same fit puts ~27k crops at ~13 GB (against ~57 GB before). Whether a
     run fits is now a question about the compute PC's RAM. The next lever is subsampling
     the `diff` side — 365M samples estimates a distribution 10M would — but that moves
     the numbers, so it is not a silent change.

464. Review repairs to the above. `_stats`' `np.asarray(a, dtype=float)` was a no-op while
     the pair arrays were float64 and became a full COPY the moment they went float32 —
     ~2.4 GB for the `diff` side at 27k crops, allocated while everything else is still
     live, i.e. the float32 change partly undoing itself. It now accumulates in float64
     (`mean(dtype=)`) without touching the array: n=7998 peak 1541 -> 1305 MB.

465. The kNN pass builds `nn` by CONCATENATING its blocks rather than assigning into a
     pre-sized `np.empty`. A loop that failed to cover every row left uninitialised memory
     behind, so the same bug read as a wrong `knn.accuracy` on one machine and as nothing
     at all on another — it survived a deliberate stride injection here because the
     allocator handed back the previous run's buffer. Miscovering now changes the row
     count, which raises.

466. That was found by testing the TEST: the first version of the blocking test hand-rolled
     its own blocked argsort and compared it to numpy, so it asserted a numpy identity and
     passed against an injected stride bug in the real loop. It now monkeypatches
     `_KNN_ROWS` and drives `run_feasibility` itself at five block sizes. Entry 411/433's
     hollow-test trap, third instance — the tell each time is a test that never reaches
     the code it names.

467. New labels are cut and stamped at a persisted `crop_geometry` setting
     (`GET`/`POST /api/crop-geometry`; the editable control is Tools → Crop shape).
     `_commit_label` cut with no margin and stamped nothing, so every label landed LEGACY
     no matter what the labelled set held — a store re-cut to one shape re-split the moment
     anyone labelled again, entry 441's open gap. Unset still means legacy, so an untouched
     install is byte-identical. Spec: docs/specs/2026-08-11-crop-geometry-for-new-labels.md.

468. `crop_rel_path` moved from `compute.tools.recut_crops` into `compute.dataset.crops`,
     which both the commit path and the tool depend on — the API importing a CLI tool was
     backwards. Load-bearing, not tidying: a fresh crop must land on exactly the path a
     re-cut to that shape would write, or the re-cut cannot RELINK it and re-cuts from the
     frame instead, which fails outright once that frame has evicted.

469. The read path CANONICALISES the stored stamp, and an unreadable value falls back to
     legacy while reporting `readable:false`. A build compares its target with
     `geometry = ?`, so stamping `m10.0` where a build asks `m10` makes every new crop
     invisible to it — reported as zero crops, never as an error. Labelling deliberately
     survives a bad value: a session is the operator's attention, a legacy crop is re-cuttable.

470. "The setting disagrees with the store" means it is not the census's DOMINANT shape,
     never that other shapes merely exist — a store legitimately holds several at once
     (today 10 at `letterbox+m25`, 2 legacy), so difference would warn permanently and
     train the operator to ignore the one readout that matters. Dominance is exactly the
     condition under which new labels stop joining the set a build enrols.

471. `.note.warn` / `.note.bad` are SAME-ELEMENT rules, so a nested `<span class="warn">`
     inside a `.note` container styles nothing, and bare `.dim` has no rule at all. Every
     new readout puts the state on the container's own class list. Entry 287's trap in CSS
     form; caught only by reading COMPUTED STYLE in the browser (amber `rgb(216,166,87)`,
     red `rgb(217,100,122)`), which is the check that rule demands.

472. A canonicalisation test that drove `POST /api/crop-geometry` asserted NOTHING: the
     endpoint canonicalises before storing, so the read path could stamp `raw` verbatim and
     the test still passed — proven by injecting exactly that. It now writes the
     non-canonical value the way a hand-edit would and reads the stamp off the committed
     CROP. Entries 411/433/466's hollow-test trap, fourth instance, same tell.

473. `parse_geometry` now rejects a NON-FINITE margin. `< 0` does not catch inf or nan
     (both compare False) and `%g` renders them back as `minf`/`mnan`, so such a stamp
     round-tripped as valid and reached the cutter: margin=inf raises OverflowError inside
     `_clamp_box`, which `materialize` does NOT catch — a 500 on EVERY label — and nan
     returns False for every frame, so a label silently writes no rows. Guarded at the
     parser both the request path and the stored-value read go through, not at one route.

474. The Tools crop-shape select NAMES a stored shape it cannot offer. `POST` accepts any
     parseable descriptor, not just the three `GEOMETRY_ARMS`, so a value another LAN client
     set (there is no auth) leaves `selectedIndex` -1 and the control BLANK — and when that
     value is also the census's dominant shape, the dominance branch prints nothing, so
     blank was the only signal. Reading blank as "unset" is how an operator would replace a
     working setting with the exact divergence the card exists to prevent.

475. `POST /api/crop-geometry` ECHOES the value it validated rather than re-reading the
     store for the margin, matching `/api/lighting` and `/api/location`. `set_setting` and
     `get_setting` take the store lock separately and the route runs in Starlette's
     threadpool, so a second client's write landing between them returned this request's
     geometry paired with that one's margin.

476. The user app's playback modal can now LABEL a visit: `Yes` confirms the identity the
     card already shows, writing `identified` rows for the span's undecided detected
     frames. The household already steps through visits hunting mislabels, so the
     confirmation is a byproduct of scrutiny already being spent — the desk keeps every
     case a yes/no cannot express.
     Spec: docs/specs/2026-08-13-user-app-visit-labelling.md.

477. The phone never PICKS a cat — no roster, no stranger, no not-a-cat. `⚑ Label later`
     (shortened from "Mark for labelling" to fit the row) is the whole other half, so the
     dispute path needed no new machinery. Rejected a picker: it would put identity
     decisions on a phone-sized top-down crop, which is the reading the desk's full-size
     crop-beside-frame player exists for.

478. New `Store.visit_label_state` is the SINGLE rule behind both the probe
     (`GET /api/label/visit`) and the write, so the button can never be drawn for a tap the
     server then refuses. Reasons, in order: `no_crop`, `all_labelled`, `unnamed`,
     `retired`, `contested`. Identity comes from `_aggregate_identity` — the same voter
     `events()` uses — so a confirm cannot name a cat the feed did not show.

479. A CONTESTED span refuses the tap: `_aggregate_identity` returns one winner and its
     result cannot distinguish "the other frames were too far" from "the other frames named
     another cat", so the below-threshold vote spread is computed alongside it. Tailgating
     is expected at this door (entry 319) and one tap must never file one cat's crops under
     another cat's name. Nothing downstream could notice if it did.

480. `cat_id` in the body is a CONCURRENCY CHECK, not an instruction — the server
     re-resolves and 409s when the two disagree. The threshold is applied at read time and
     restates all history when changed (entry 425), so the name a phone displays can
     genuinely differ from the name the server would give the same span a moment later.
     "Yes" has to mean yes to what was on screen.

481. A RETIRED cat cannot be confirmed. The feed still shows such a name (a gallery can
     predate the retirement), but the desk's picker offers only active cats and no build
     enrols a retired one (entry 335) — so the tap would write a label the operator could
     not have written by hand and nothing will ever read. Also covers a cat_id whose `cats`
     row is gone.

482. A PART-LABELLED span stays confirmable for its undecided remainder. Event spans GROW
     as later motion lands (entry 224), so a cat lingering past a confirmation routinely
     leaves a tail; gating on "nothing labelled yet" would strand exactly the longest
     visits, which carry the most crops. Grades stay per annotation visit, so the rows are
     graded as the desk queue would have graded them.

483. Rows are stamped `source = 'user-confirm'`, the first use of a `dataset_items` column
     that has defaulted to 'detector' since day one with nothing reading it. A confirmation
     against a phone-sized frame with the model's own name on it is a different act from a
     desk label, so the two stay separable — auditable, excludable from a build, and
     A/B-able later.

484. The tap ADVANCES to the next older visit immediately and the write drains on a serial
     chain behind it — `_commit_label` cuts one JPEG per frame (~1-2 s per visit, entry
     297), and a spinner per tap is what would make a 40-visit pass not worth doing. A
     failed write therefore NAMES its own visit on a non-visit-scoped pill; the ⚑ does not
     advance, being a reversible toggle whose success is deliberately silent (entry 286).

485. The 0.3 confidence floor comes along, DIVERGING from entry 225's unfloored flagged
     spans. That exemption rests on a human reviewing the span crop by crop at the desk; a
     confirm is a blind bulk write where a faint 0.2 box could be an empty scene. Recorded
     because the precedent points the other way and would otherwise invite a "fix".

486. `_validate_visit_span` now holds the required-both-bounds + 10k-id width cap shared by
     `/api/identify/visit` and the new route, with a caller-supplied hint. Refusal reasons
     are a module-level table keyed by `visit_label_state`'s own `reason`, so a rung added
     there without wording fails loudly instead of reaching a phone as a bare 409.

487. Three defects the verification found, each invisible to the other half. A test asserting
     the ⚑ survives `inserted: 0` PASSED against an unconditional flag delete — its span had
     evicted, so the route 409'd at `no_crop` and never reached the flag code; it now removes
     the JPEGs and keeps the rows, which is the only way to be confirmable and still insert
     nothing. Entry 411/433/466/472's hollow-test trap, fifth instance, same tell.

488. The other two were browser-only. `renderSaving()` ran AFTER `flashSaved()` and
     overwrote the green "Saved" with the empty state, so a drained queue looked identical
     to one that never appeared — the exact thing entry 296 added the flash for. And
     `flex: none` on the new label-state line pushed the visit nav onto its own row on a
     wide screen, undoing what `.msg { flex: 1 }` is documented to do; it shrinks with an
     ellipsis now, full text in `title`.

489. A probe that could not be READ now says so — "the compute PC may be on an older
     build" — instead of leaving the footer blank, which was indistinguishable from "this
     visit isn't confirmable". Found by hitting it: `frontend-dev.sh` serves the working
     tree's HTML against the REAL compute PC's `/api`, so a new frontend over an
     un-deployed backend 404s the probe and the whole feature read as missing, on a visit
     that was fine. Entries 164/173/183/365's rule, in a new place.

490. `visit_label_state`'s `existing` keys on the (`src_frame_id`, `src_recv_ts`) PAIR
     like every sibling reader, not on the id alone. `frames.id` has no AUTOINCREMENT and
     `clear()` deliberately spares `dataset_items`, so a new visit on a reused id range
     matched a stale pre-clear row and the phone read "Labelled: OldCat" over a visit
     nobody had labelled — in the feature whose purpose is finding mislabels. A row whose
     frame merely EVICTED is still kept: eviction never reuses an id.

491. Three UI-state repairs to the confirm flow, all in the refused-advance case (a
     confirm on the last loaded visit, where `moveEvent` declines and the visit stays on
     screen). The ⚑ button now repaints after a confirm clears the flag — stale, its next
     tap took the ADD branch and re-created the mark (entry 228's class); the button paints
     its busy state before the hop rather than after the write; and `probeConfirm`'s
     sequence guard now gates the STATE WRITE, not just the paint, so a slow first probe
     could no longer overwrite the fresher post-write answer and bring the button back on
     an already-labelled visit.

492. The new routes validate `oracle` against `ANALYZER_NAMES` as every sibling
     oracle-taking route does. An unregistered name reached `_present_frames` as a
     predicate matching no rows, so a typo read as an honest "no crop to label here"
     rather than a bad request.
