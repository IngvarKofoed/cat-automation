# Admin-next — Implementation Plan

Build sequencing for the streamlined compute admin UI. The **design** lives in
`docs/specs/2026-07-25-admin-next-redesign.md` — this doc is only the *how and in
what order*, plus which phases can run in parallel under multi-agent
orchestration ("ultracode"). If this plan and the spec disagree, the spec wins.

## Principles

- **Vertical slices.** Each phase lands at `/admin-next`, is verifiable in a
  browser against live data, and is **one commit + one `docs/CHANGELOG.md`
  entry**. No half-built trees.
- **`/admin` is untouched** until the final flip (P8). The old admin keeps
  working byte-for-byte the whole build.
- **Reuse `/api/*`; adapt old-admin logic; additive backend only.** Frontend
  pages are ported/adapted from the old admin, not rewritten from scratch.
- **Dark, minimal styling** for now — structure over looks; the reskin is later.

## Dependency graph

Most phases are independent once the scaffold exists; only two edges are
load-bearing:

```
P0 scaffold ──▶ everything
P1 lat/lon setting ──▶ P2 day/night split
P3 identity read API ──▶ P3 Frame-review overlay  AND  P2 "misses" deep-link target
```

Everything else can be ordered by preference. The natural solo order is P0 → P8.

## Phases

Each phase is split into its **backend** slice (additive API/store work) and its
**frontend** slice (the page in the single `admin-next/index.html`). This split
matters for fan-out (next section): backend slices are independent; frontend
slices all touch one file.

### P0 — Scaffold
- **Work:** `compute/api/web/admin-next/index.html` (dark shell, hash router,
  6-route nav stub, stopgap `/admin` link) + `@app.get("/admin-next")` handler
  cloned from `admin()`.
- **Depends on:** nothing. **Blocks:** everything.
- **Verify:** page loads, routes switch, `/api/*` reachable.

### P1 — Start
- **Backend:** compute lat/lon setting (seed from edge `/api/night-light`,
  editable, CRUD).
- **Frontend:** phase blurb; mode control (`motion_only`); collection controls
  (start/stop/resume, live-identify toggle, clear-all); active-model + threshold
  indicator; store stats; location setting; cleanup UI (P7 backend).
- **Depends on:** P0. **Provides:** lat/lon → P2.
- **Verify:** mode switch persists; collector start/stop works; location saves.

### P2 — Motion tuning
- **Backend:** add `astral` dep; day/night split param on `/api/tuning/compare`
  + scorecard builder (per-visit bucketing, warm-up once).
- **Frontend:** day picker; YOLO-serial sweep + job queue + coverage; six MOG2
  params; baseline/candidate; Day/Night visit-recall scorecards; winning-params
  copy-out; "misses" deep-link to Frame review.
- **Depends on:** P0, P1 (lat/lon). Deep-link target completes with P3.
- **Verify:** scorecards render, split matches sun times, params copy out.

### P3 — Frame review
- **Backend:** identity read API (per-frame nearest-gallery match — no such read
  endpoint exists today).
- **Frontend:** time-interval frame browser; overlays (motion / YOLO / corruption
  / identity); filters (motion / misses / false-triggers / corrupt / has-identity);
  folds in corruption review.
- **Depends on:** P0. **Provides:** identity overlay + P2's deep-link target.
- **Verify:** overlays correct against known frames; deep-link from P2 lands.

### P4 — Annotation
- **Backend:** bounded/paginated queue; `ignored` `dataset_items` label handling;
  per-cat day/night coverage; below-threshold distance-sort (`identifications`
  join).
- **Frontend:** port keyboard flow; queue; ignore key; undo/relabel (Labelled
  mode); quality grading; per-cat coverage readout.
- **Depends on:** P0.
- **Verify:** label/ignore/undo round-trip; queue bounded; coverage counts right.

### P5 — Model building
- **Backend:** minimal (reuses `TrainingManager` endpoints); night-coverage
  check for the promote warning.
- **Frontend:** port build / validate (DINOv2 probe) / promote; warn-on-no-night-
  crops; threshold; version list + rollback.
- **Depends on:** P0.
- **Verify:** validate report renders; promote flips active/retired; warn fires.

### P6 — Activity
- **Backend:** none new (reuses `/api/events`, `/api/identify/run`, reanalyze).
- **Frontend:** port event cards + playback + identity chips; manual backfill
  controls (Identify pass, Analyze, per-event re-analyze).
- **Depends on:** P0.
- **Verify:** cards + playback work; backfill enqueues.

### P7 — Cleanup purges
- **Backend:** non-motion purge + orphan sweep as **batched background jobs**
  through the eviction accounting path (`_count`/`_motion_count`/`_total_bytes`);
  purge-span recording. **Data-destructive — strong tier + adversarial verify.**
- **Frontend:** the cleanup UI on Start + the Tuning→Collecting inline offer.
- **Depends on:** P0 (UI sits on P1's Start page).
- **Verify:** purge never touches durable crops/labels/models; counts stay exact;
  purged window warns "misses unmeasurable"; lock released between batches.

### P8 — The flip — **DONE**
- **Planned work (historical):** point `/admin` at `admin-next/index.html`; move
  the old file to `/admin-old`; delete later once trusted. Verified at the time by
  `/admin` serving the new UI and `/admin-old` the old one.
- **Depends on:** all prior phases done + trusted.
- **Outcome — both halves are now done.** The flip landed, then the old console was
  **deleted**: no `/admin-old` route, no `web/admin/`, no appbar link to it, and so
  no rollback target. Buckets and corruption review went with it BY DECISION, not
  oversight — buckets is superseded by the tuning calendar, and corruption review is
  simply dropped. Their `/api` endpoints remain, so a corruption sweep is still
  curl-able; rebuild the page from
  `docs/specs/2026-07-23-corruption-review-page.md` if the NoIR module brings the
  IMX708 artifacts with it.

## Which phases can be fanned out (ultracode)

**Read this first: you don't "ultracode a *phase*."** Ultracode operates in three
places, and only one is inside a single phase:

1. **Across phases — Wave 1 (the big win).** Run the *backend slices* of P1, P2,
   P3, P4, P7 **in parallel**, each a worktree-isolated agent. This is the real
   fan-out, and it dissolves both dependency edges before any frontend work.
2. **Around every phase — verification & review.** Per-slice adversarial verify
   (P7 most of all) and a dimension-fanned review over the aggregate diff.
3. **Inside Wave 2 — port-analysis.** One agent per old-admin page produces a
   port-map, even though the single-file frontend edit itself stays serial.

**The limiter:** `admin-next/index.html` is one self-contained file (entry-80: no
shared CSS/JS), so **no individual frontend page fans out** — parallel agents
would collide on the file. That's why most phases read "backend fans out, frontend
serial."

**Per-phase verdict** — can you point ultracode at this phase on its own?

| Phase | Ultracode play | Verdict |
|---|---|---|
| **P0** Scaffold | none — one foundational file, must land first | ❌ serial |
| **P1** Start | backend (lat/lon) joins Wave 1; frontend serial | ⚠️ backend only |
| **P2** Motion tuning | backend (astral + day/night split) joins Wave 1; frontend serial | ⚠️ backend only |
| **P3** Frame review | backend (identity API) joins Wave 1; frontend serial | ⚠️ backend only |
| **P4** Annotation | backend (queue/ignore/coverage) joins Wave 1; frontend serial | ⚠️ backend only |
| **P5** Model building | thin backend (reuse); frontend serial | ❌ little to gain |
| **P6** Activity | all reuse; frontend serial | ❌ little to gain |
| **P7** Cleanup | backend (purge jobs) joins Wave 1 **+ adversarial-verify fan-out** | ✅ strongest case |
| **P8** Flip | trivial | ❌ serial |

In practice: **P1–P4 + P7 contribute their backends to one parallel Wave 1**; **P7
additionally earns a verify fan-out** (it's the only data-destructive phase);
**P0, P5, P6, P8 are effectively serial** — nothing worth parallelizing. Anything
else that "fans out" is the cross-cutting review / port-analysis, not a phase you
aim ultracode at.

**Execution as waves:**

- **Wave 0 — Scaffold (serial).** P0. One agent. Lands the file + route.
- **Wave 1 — Backend fan-out (parallel).** The five backend slices — lat/lon (P1),
  day/night split (P2), identity API (P3), annotation backend (P4), purge jobs
  (P7) — each a worktree-isolated agent.
- **Wave 2 — Frontend (serial, or partial-then-assemble).** File edits serialize;
  **port-analysis** (one agent per old-admin page) and **verification** fan out.
- **Wave 3 — Review + flip.** Dimension-fanned adversarial review over the
  aggregate diff (no single agent saw the whole change), then P8.

## Model tiering (per CLAUDE.md multi-agent rules)

- **Strong (session model):** purge accounting (P7), identity API (P3), day/night
  split math (P2), final synthesis, adversarial review.
- **Mid:** frontend page ports (mechanical transplant), verifying already-stated
  findings, build/test runners.
- **Cheap + low effort:** changelog entries, styling stubs.
- **Guardrail:** purge / data-integrity verification stays **strong**, not mid.

## Verification & review

- Per phase: the phase's own **Verify** line, in a real browser via the Playwright
  MCP (`mcp__playwright__*`) for frontend, plus pytest for backend/store work.
- Data-destructive P7 gets **adversarial verification** — independent agents each
  trying to construct an input where a purge deletes a durable crop/label or
  drifts the counts.
- After any multi-agent wave, run the **After-making-changes self-review** over
  the aggregate diff, and end a significant wave by suggesting a user-run
  `/code-review medium`.
