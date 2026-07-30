# Cat Automation

A camera + computer-vision system at a cat door that identifies each resident cat versus strangers, tracks who is in or out, and — in later phases — locks the door against foreign cats, deters intruders, and notifies the owner.

## Always read first

Before doing any work in this repo, **always read all** of these:

- @docs/CONCEPT.md — what the system does: resident vs. foreign cats, individual identification, enter/leave tracking & occupancy, the human-in-the-loop learning loop (collection → annotate → train → run), the optional door lock / sound / light, the dashboard, and notifications. **Early prototype on a trusted LAN — no auth; actuation and its policy are deferred.**
- @docs/ARCHITECTURE.md — how it's built: a thin Raspberry Pi edge (a *pure HTTP server* streaming MJPEG at ~5 fps) plus a networked NVIDIA PC (all vision, the decision engine, the learning loop, the dashboard). Python both tiers; SQLite; monorepo laid out as `edge/` + `compute/` + `shared/`.
- @docs/CHANGELOG.md — running log of changes to this project.

`CONCEPT.md` and `ARCHITECTURE.md` are the source of truth for *what* we're building, what we've built, and *how* it's structured. If something in the code contradicts them, either the code or the doc is wrong — flag it rather than guessing.

## Changelog discipline

Every change you make to this repository must be recorded in `docs/CHANGELOG.md`.

- Each entry is numbered with a monotonically increasing integer (1, 2, 3, ...). Never reuse or reorder numbers.
- Append new entries to the end of the file.
- Write each entry as **durable project memory, not a recap of the diff**: record what is now *true that wasn't before* — new behavior, state, or rule — plus, in a clause and only when it isn't obvious, the *why*, the alternative you rejected (so a future agent doesn't re-introduce it), or a known limit / deferred follow-up. Skip filenames, mechanical edits, and refactors with no behavior change; the diff and commit already hold those. Self-check: *if a future agent reads this entry before the code, does it learn what changed, why it matters, or what's now safe to assume?* If not, it's noise.
- Keep each entry to **1–5 lines, ~20 words per line at most**. The changelog is read at session start to orient — that only works if it stays scannable. The failure mode to avoid is cramming everything onto one unbroken line: a 40-word run-on isn't a short entry, it just hides the bulk on a single line. Break it into a few short lines instead; and if it sprawls past ~5 lines, that's a signal it's really several changes — give each its own numbered entry.
- Write the entry as part of the same change. Do not batch multiple changes into one entry, and do not skip entries.
- When a phase/increment completes, its per-task entries move to `docs/CHANGELOG-archive.md`, leaving only the milestone summary in `docs/CHANGELOG.md`. Numbers are globally unique across both files — never reuse one that already appears in either.

Same change, bad vs. good entry:

- **Bad** (short, but just recaps the diff — zero orientation value): `42. Updated auth files, reworked middleware, added tests, renamed AuthHelper.`
- **Good** (states what's now true, with the why in a clause):
  ```
  42. Auth now rejects expired refresh tokens before session lookup; stale sessions can no longer silently renew.
      Validated at the middleware boundary so handlers can assume requests are current.
  ```

## Nested guidance

Each subtree has its own `CLAUDE.md` with scoped tool/skill rules:

- `edge/CLAUDE.md` — Raspberry Pi thin edge: capture, clip, motion gate, the single HTTP server (`/stream`, `/frame`, control API, config UI), and optional actuator drivers. **No ML.**
- `compute/CLAUDE.md` — NVIDIA PC "brain": detection, tracking, individual re-ID identification, the decision engine, event store, notifications, dashboard, and the learning loop.
- `shared/CLAUDE.md` — the cross-tier **contracts** (data model, event/intent schemas, the Pi control-API shape, constants) *and* the cross-tier **logic** both tiers instantiate: the `MotionGate` core and the frame wire format.

## After making changes

After a non-trivial edit, **the change gets a review-grade review automatically, in the same turn** — nobody should have to ask for it. Never skip it, and never leave it for a later pass.

**Preferred: `/fix-code --fix`.** It resolves the diff scope itself, rates every finding 1–5 by consequence, has an independent verifier refute each one before repairing, takes a restore point first, and applies only the repairs that are both serious and unambiguous — revert the run with `/fix-code --undo`. Prefer it over an ad-hoc pass: it's better calibrated, and its edits are reversible. Two things it deliberately does *not* do: it leaves severity-1 and -2 findings unrepaired, and it treats style / naming / reuse as `/simplify`'s job and deep security work as `/security-review`'s. Run those separately when the change warrants them.

**Fallback, if `fix-code` isn't available: run the same review yourself, inline.** A missing skill changes only *who* performs the review, never whether it happens. Do **not** try to invoke `/code-review` for this routine pass — it is user-invoke-only (`disable-model-invocation`), so the call just fails.

Scale the fallback to the change:

- **Small, contained edits** — read the diff yourself in one careful pass.
- **Substantial or high-risk changes** (a broad diff, or security- / data-integrity-sensitive code) — **fan out across subagents with the Agent tool, one per at-risk dimension (~2–4), and verify each finding before acting on it**. This needs no ultracode opt-in; that gate is only for the Workflow tool. Finders run on the strong model; verification can drop a tier (see *Multi-agent workflows*).

The fallback checks, across the diff: **correctness, security & data-integrity, edge cases & tests, reuse / duplication, clarity, performance, and conformance to this repo's conventions** — then applies **every** fix to the working tree. (`/fix-code` applies its own narrower threshold instead, by design — that's the trade for its per-finding verification.)

Once the fallback's fixes are applied, report what changed (when `/fix-code` ran instead, its own report stands — don't restate it):

1. **Group the applied fixes by severity** — blockers (correctness bugs, data loss, security), should-fix (clear improvements, missed reuse), nits (style, naming, minor clarity).
2. **Summarize each bucket in one line** so the user can see what was fixed without expanding every finding.
3. Do not stop to ask which to fix — all findings are fixed by default. The user can review the diff and revert anything they disagree with.

Say plainly when the change is one you **couldn't fully verify** — you guessed at intent, left a known gap, or nothing covers it (it only runs on the compute PC, or on the Pi). State it as a fact about the change, not as a recommendation to go run something.

### Before committing: offer the fresh-eyes pass

An earlier review never covers later edits — each round of non-trivial changes gets its own pass, above, and a follow-up request ("also add X", "now handle Y") is a new change, not a continuation of one already reviewed. What erodes across rounds is *distance*: by the second or third follow-up you're reviewing your own patch from inside the context that wrote it, with the same blind spots, and each pass only ever saw its own turn's edits. Nothing has read the accumulated diff as one change.

A commit request is where that gets settled, because it's the moment the diff is sealed into history. So **when the user asks you to commit (or commit and push) and the working tree holds non-trivial changes no `/fix-code --fix` run has seen, ask before committing** — `AskUserQuestion`, two options:

1. **Review first** — run `/fix-code --fix` over the working diff, then commit with its repairs included.
2. **Commit now** — the per-round reviews stand; go straight to the commit.

**You** run the pass either way; the user is only choosing whether it happens.

- **Skip the question** when `/fix-code --fix` has already run over this working diff since the last edit (it saw the whole thing), or when the diff is trivial — a typo, a version bump, a changelog line. Asking there is noise.
- Ask **once per commit request**, not per round or per file. If the user picks the review, run it, report as usual, then continue to the commit without asking again.
- **Any** commit request goes through the gate, including a delegated one (`/git commit`, `/git commitandpush`) — check it *before* handing off, not after. A skill or subagent that does the committing never sees this rule.
- Never let the offer stand in for the work: this round's diff still gets its own review and its fixes before you ask anything.

### Suggesting a user-run `/fix-code` pass

**Default: say nothing.** Most turns end with the review report and no suggestion — the review already happened, and a reflexive "you may also want to review this" on every diff is noise that trains the user to ignore it on the one change where it mattered. Suggest a pass only when you can **name the trigger that fired**. Can't name one? Don't suggest.

What a user-run pass adds is **distance, not a different tool**: run fresh — ideally in a new session — `/fix-code` reads the same diff without the context that produced it, and its report-only default (no `--fix`) changes nothing, so it is a safe second look. Deliberately *not* `/code-review`: that skill needs a pull request to exist and only posts a comment, and on this repo's diffs it has been a bad trade (see *Multi-agent workflows* below).

**Triggers — any one is enough:**

- **Heavy review** — a genuine blocker was fixed (a real correctness, security, or data-integrity bug), or should-fix changes landed across most of the files touched. A handful of nits is not churn.
- **High-consequence surface** — in this repo: the store's counter / eviction / accounting paths and anything holding the shared SQLite write lock; schema changes, or anything that deletes or purges frames, crops, or labels; the `shared/` contracts (wire format, `MotionGate`) that both tiers depend on; the durable learning-loop tables (`cats`, `dataset_items`, `model_versions`) that survive eviction; and — once it exists — actuation and its access-decision policy.
- **Large and non-mechanical** — roughly >400 changed lines or >10 files of real logic. A rename, a formatting sweep, or a presentation-only reskin of the same size does not count.
- **Multi-agent fan-out produced the diff** — no single agent ever saw the whole combined change.
- **Genuine uncertainty** — you guessed at intent, left a known gap, or couldn't verify the change (nothing covers it, or it only runs on the compute PC / on the Pi).

**No level to pick.** `/fix-code` triages its own scope and announces the tier it ran at, so suggest the plain pass and let it size itself — don't invent flags for it.

**These do *not* trigger it:** docs- or changelog-only edits, formatting and lint fixes, dependency bumps, adding tests to existing code, a contained single-function change with tests passing, a presentation-only UI tweak, or a review that turned up only nits.

When you do suggest: **one sentence** at the end of the turn — the code is already reviewed with fixes applied, a fresh pass would add assurance, and **which trigger fired**. Frame it as optional reassurance, not a warning that something is wrong. Only claim the code was already reviewed if you actually ran the review above.

**If the user is heading for a commit, don't say it twice.** The *Before committing* gate already offers that pass and asks the question — when a commit request is in play, the gate replaces this sentence.

## Multi-agent workflows

When you fan a task out across subagents — the Workflow tool ("ultracode") — tier each agent's model and reasoning effort to the work, so cost tracks value instead of every agent defaulting to the strongest (most expensive) model:

- **Strongest model** (the session model) — contracts, correctness-critical implementation, adversarial review, and final synthesis. Never downgrade these; they are where quality is won or lost.
- **Mid model** — build/test runners, straightforward mechanical implementation, and verifying concrete already-stated findings or applying decided fixes.
- **Cheapest model + low effort** — docs/changelog, i18n, styling, and other boilerplate.

The guardrail: the stage that *catches* problems (adversarial review) stays strong; the stages that merely *check* an already-caught finding (per-finding verification) drop a tier — unless the finding is subtle or security-/data-integrity-critical, where verification stays strong. Set this per `agent()` call (`model` / `effort`); an agent that omits `model` inherits the session model, which is why an untiered fan-out silently runs everything on the most expensive tier.

**Invoking a named workflow is not authoring one.** The tiers above are yours to set only when *you* write the `agent()` calls. A built-in or named workflow — e.g. `Workflow({ name: 'code-review' })` — runs its own stages on the session model; nothing tiers them for you, so a wide fan-out (the review's per-`(file,line)` verifiers most of all) silently bills every agent at the top tier. Before launching one at `high`+ effort or over a broad diff, check the `scriptPath` the run reports: if a large *checking* stage isn't tiered, edit that script to drop those agents to the mid model (leaving the finders and final synthesis strong) and re-invoke with `{ scriptPath }` instead. Keep them strong only when the diff is security-/data-integrity-critical. For `code-review` specifically (verified 2026-07-30): its script carries **no** `model:` overrides at all — scope, finders, per-`(file,line)` verifiers, sweep and synthesis every one inherit the session model, so the verify fan-out is exactly the stage to tier by hand. Two gotchas when you edit the snapshot: the script is a **per-run** copy, so a fresh `/code-review` starts untiered again (re-invoke with `{ scriptPath }`, or resume with `{ scriptPath, resumeFromRunId }`); and adding `model:` to an agent that already **completed** changes its `(prompt, opts)` cache key, so it re-runs on resume — leave finished stages alone.

**When a workflow returns, the after-edits review applies to its aggregate diff.** No single subagent saw the whole combined change, so treat the workflow's landed edits as one change and run the *After making changes* review over the aggregate diff — give it the substantial-change treatment even when each agent's own slice looked small. (A review stage *inside* the workflow checks its own findings; it does not replace this pass over what landed.) Being a no-single-author change, it also fires the *multi-agent fan-out* trigger in **Suggesting a user-run `/fix-code` pass** above.

This section is inert unless you actually run a multi-agent workflow.

## Git workflow

**Direct to `main`** — when you commit, commit straight to `main`; don't open branches or PRs unless asked. A remote (`origin`) is configured; **push only when the user asks** (e.g. via `/git`), never automatically.

**This setting only chooses *where* commits go — not *when* to make them.** Commit only when the user asks; finishing a change is not a cue to commit it. When you do commit, each commit is one complete change including its `docs/CHANGELOG.md` entry — never leave the tree half-committed.

A commit request first passes through *Before committing: offer the fresh-eyes pass* above — check that gate before staging anything, including when the commit is delegated to `/git`.

<!-- Add additional sections below as the project develops:
  - Project-specific forcing rules (e.g., a policy the agent must follow before touching actuation)
  - Destructive-operation guidance if the agent's defaults aren't enough
  - Naming conventions, code-organization rules
-->
