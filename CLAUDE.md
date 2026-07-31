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

## Reviewing changes

**This repo has exactly one review point: the commit.** Do not review after every edit, and do not run an unrequested review pass mid-session. A pass per prompt costs more than it catches — it only ever sees its own turn's edits, reviews them from inside the context that just wrote them, and the next follow-up ("also add X", "now handle Y") re-opens what it just looked at. Nothing in that scheme ever reads the accumulated diff as one change.

What does *not* wait for the commit is verifying your own work: run the tests, the build, and whatever the subtree's `CLAUDE.md` mandates — the edge's on-device / fake-source checks, compute's pytest suite plus the Playwright MCP pass on a changed dashboard view — per change, as always. Deferred here is the *review*, not the checking. Never report a change complete on the grounds that its review comes later.

The commit is the right moment because it's when the diff is sealed into history, and because it's the first point where the accumulated diff can be read as **one change** — which is what a review needs.

**So when the user asks you to commit (or commit and push) and the working tree holds non-trivial changes, ask before committing** — `AskUserQuestion`, two options:

1. **Review first** — review the working diff, apply the repairs, then commit with them included.
2. **Commit now** — skip the review and go straight to the commit.

**Label option 1 with the path that will actually run**, so the choice is concrete rather than abstract: **"Run `/fix-code --fix`"** when that skill is installed, **"Review inline"** when it isn't. The two differ in cost and calibration — don't hide which one the user is about to get behind a generic label.

**You** run the pass; the user is only choosing whether it happens. This is the one place the review flow stops to ask — there is no end-of-turn nudge to go run a review elsewhere.

- Ask **once per commit request**, not per round or per file. If the user picks the review, run it, report as usual, then continue to the commit without asking again.
- **Skip the question** when the diff is trivial — a typo, a version bump, a changelog line — or when a full review pass has already covered this working diff since the last edit. Asking there is noise.
- **Any** commit request goes through the gate, including a delegated one (`/git commit`, `/git commitandpush`) — check it *before* handing off, not after. A skill or subagent that does the committing never sees this rule.
- If the user declines, that's the answer — commit as asked, and don't re-offer or hedge about it afterwards.

### Running the review pass

**Preferred: `/fix-code --fix`, whenever that skill is installed.** It resolves the diff scope itself, rates every finding 1–5 by consequence, has an independent verifier refute each one before repairing, takes a restore point first, and applies only the repairs that are both serious and unambiguous — revert the run with `/fix-code --undo`. Prefer it over an ad-hoc pass: it's better calibrated, and its edits are reversible. Two things it deliberately does *not* do: it leaves severity-1 and -2 findings unrepaired, and it treats style / naming / reuse as `/simplify`'s job and deep security work as `/security-review`'s. Run those separately when the change warrants them.

**Fallback, when `fix-code` isn't installed: run the same review yourself, inline over the working diff.** A missing skill changes only *who* performs the review, never whether it happens. Do **not** try to invoke `/code-review` for this pass — it is user-invoke-only (`disable-model-invocation`), so the call just fails.

Scale the fallback to the diff:

- **Small, contained diff** — read it yourself in one careful pass.
- **Substantial or complex diff** (what an accumulated multi-round working tree usually is) — **fan out across subagents with the Agent tool, one per dimension actually at risk in this diff (~2–4), and verify each finding before acting on it**. This needs no ultracode opt-in; that gate is only for the Workflow tool. Finders run on the strong model; verification can drop a tier (see *Multi-agent workflows*).

**The surfaces worth their own finder in this repo**, when the diff touches them: the store's counter / eviction / accounting paths and anything holding the shared SQLite write lock; schema changes, and anything that deletes or purges frames, crops, or labels; the `shared/` cross-tier logic (wire format, `MotionGate`) both tiers depend on; the durable learning-loop tables (`cats`, `dataset_items`, `model_versions`) that survive eviction; and — once it exists — actuation and its access-decision policy.

The fallback checks, across the diff: **correctness, security & data-integrity, edge cases & tests, reuse / duplication, clarity, performance, and conformance to this repo's conventions** — then applies **every** fix to the working tree. (`/fix-code` applies its own narrower threshold instead, by design — that's the trade for its per-finding verification.)

Once the fallback's fixes are applied, report what changed (when `/fix-code` ran instead, its own report stands — don't restate it):

1. **Group the applied fixes by severity** — blockers (correctness bugs, data loss, security), should-fix (clear improvements, missed reuse), nits (style, naming, minor clarity).
2. **Summarize each bucket in one line** so the user can see what was fixed without expanding every finding.
3. Do not stop to ask which to fix — all findings are fixed by default. The user can review the diff and revert anything they disagree with.

Either path closes the same way: say plainly what you **couldn't fully verify** — you guessed at intent, left a known gap, or nothing covers it (it only runs on the compute PC, or on the Pi). State it as a fact about the change, not as a recommendation to go run something.

## Multi-agent workflows

When you fan a task out across subagents — the Workflow tool ("ultracode") — tier each agent's model and reasoning effort to the work, so cost tracks value instead of every agent defaulting to the strongest (most expensive) model:

- **Strongest model** (the session model) — contracts, correctness-critical implementation, adversarial review, and final synthesis. Never downgrade these; they are where quality is won or lost.
- **Mid model** — build/test runners, straightforward mechanical implementation, and verifying concrete already-stated findings or applying decided fixes.
- **Cheapest model + low effort** — docs/changelog, i18n, styling, and other boilerplate.

The guardrail: the stage that *catches* problems (adversarial review) stays strong; the stages that merely *check* an already-caught finding (per-finding verification) drop a tier — unless the finding is subtle or security-/data-integrity-critical, where verification stays strong. Set this per `agent()` call (`model` / `effort`); an agent that omits `model` inherits the session model, which is why an untiered fan-out silently runs everything on the most expensive tier.

**Invoking a named workflow is not authoring one.** The tiers above are yours to set only when *you* write the `agent()` calls. A built-in or named workflow — e.g. `Workflow({ name: 'code-review' })` — runs its own stages on the session model; nothing tiers them for you, so a wide fan-out (the review's per-`(file,line)` verifiers most of all) silently bills every agent at the top tier. Before launching one at `high`+ effort or over a broad diff, check the `scriptPath` the run reports: if a large *checking* stage isn't tiered, edit that script to drop those agents to the mid model (leaving the finders and final synthesis strong) and re-invoke with `{ scriptPath }` instead. Keep them strong only when the diff is security-/data-integrity-critical. For `code-review` specifically (verified 2026-07-30): its script carries **no** `model:` overrides at all — scope, finders, per-`(file,line)` verifiers, sweep and synthesis every one inherit the session model, so the verify fan-out is exactly the stage to tier by hand. Two gotchas when you edit the snapshot: the script is a **per-run** copy, so a fresh `/code-review` starts untiered again (re-invoke with `{ scriptPath }`, or resume with `{ scriptPath, resumeFromRunId }`); and adding `model:` to an agent that already **completed** changes its `(prompt, opts)` cache key, so it re-runs on resume — leave finished stages alone.

**A workflow's aggregate diff is what the review gate is for.** A fan-out edits files across several subagents — often in separate worktrees — so no single agent ever saw the whole combined change, and any review stage *inside* the workflow checked its own findings, not the landed diff. This doesn't earn an extra pass on return; the commit is still the review point. But when the gate asks, say the diff came out of a fan-out — it's where *review first* earns its cost most clearly, and it gets the substantial-diff treatment above even when each agent's own slice looked small.

This section is inert unless you actually run a multi-agent workflow.

## Git workflow

**Direct to `main`** — when you commit, commit straight to `main`; don't open branches or PRs unless asked. A remote (`origin`) is configured; **push only when the user asks** (e.g. via `/git`), never automatically.

**This setting only chooses *where* commits go — not *when* to make them.** Commit only when the user asks; finishing a change is not a cue to commit it. When you do commit, each commit is one complete change including its `docs/CHANGELOG.md` entry — never leave the tree half-committed.

A commit request first passes through the review gate in *Reviewing changes* above — check it before staging anything, including when the commit is delegated to `/git`.

<!-- Add additional sections below as the project develops:
  - Project-specific forcing rules (e.g., a policy the agent must follow before touching actuation)
  - Destructive-operation guidance if the agent's defaults aren't enough
  - Naming conventions, code-organization rules
-->
