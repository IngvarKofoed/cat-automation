# Cat Automation

A camera + computer-vision system at a cat door that identifies each resident cat versus strangers, tracks who is in or out, and — in later phases — locks the door against foreign cats, deters intruders, and notifies the owner.

## Always read first

Before doing any work in this repo, **always read all** of these:

- @docs/CONCEPT.md — what the system does: resident vs. foreign cats, individual identification, enter/leave tracking & occupancy, the human-in-the-loop learning loop (collection → annotate → train → run), the optional door lock / sound / light, the dashboard, and notifications. **Early prototype on a trusted LAN — no auth; actuation and its policy are deferred.**
- @docs/ARCHITECTURE.md — how it's built: a thin Raspberry Pi edge (a *pure HTTP server* streaming MJPEG at ~5 fps) plus a networked NVIDIA PC (all vision, the decision engine, the learning loop, the dashboard). Python both tiers; SQLite; monorepo laid out as `edge/` + `compute/` + `shared/`.

Then the changelog, as **actions** rather than loads — no `@`-reference can express "the files of a folder", so this part is yours to fetch. There is no summary file to load in its place: `docs/changelog/` is the entire record, and these steps are the only way into it.

1. `ls docs/changelog` — the index, **in full, not a tail**, so that every entry the project has ever written is named once: recent work at the bottom, everything older above it. Each entry is its own file, `YYYY-MM-DD-<slug>.md`. This is not free — it costs roughly a dozen tokens per entry and grows for the life of the repo — but a name is the cheapest possible handle on an entry, and being able to see that something exists is what makes step 4 findable rather than guesswork. Past **~1000 entries** the balance tips: hand the full listing to step 3's subagent instead and read a generous tail here. An absent folder just means no entries yet.
2. Read the **newest 10** in full, or **everything from the last seven days**, whichever is more. Ten is a **floor, not a measure**: at four changes a day it is two days of memory, and in a heavy week barely one. Step 1 already gave you the dates, so you can see which of those two the floor lands on before you start — and if the work you are about to do reaches back further than either, keep reading back until it doesn't.
3. **Delegate a digest of everything behind the window** to a cheap, fast subagent (Haiku or equivalent) — never the main session, since the whole point is that the reading cost lands somewhere other than this context. **Skip this step** when delegating to a subagent isn't available here, or when step 1 showed little behind the window — with thirty entries the window already covers the repo and there is nothing to summarize. Give the subagent `ls -r docs/changelog | tail -n +11` (every entry older than the ten you just read, newest first; written this way because a negative `head -n` count is GNU-only and errors out on macOS) and this brief: read each **lead**, open a `## Detail` only when the lead signals a decision, and return **at most 20 lines** of what a session must not re-litigate — a decision with a rejected alternative, something tried and removed, a constraint that still binds, an entry that reversed an earlier one. Not a recap of what changed; the entries are already that. Leads are 1–5 lines by rule, so this stays affordable across the whole history rather than a recent band. **Never write its output to a file.** Regenerating it per session is what makes it unable to rot, and what keeps anything in git from contending for it.
4. **Before you touch an area, grep the folder for it** — `grep -rli <term> docs/changelog/`, using the words you would pick to describe what you are about to change. This is the only path to a decision made months ago, and the index in step 1 is what tells you which terms are worth trying.

**The grep is not optional and it is not a fallback**, with or without the delegated digest. Nothing here will *show* you a decision from six months back on its own — the index names it, the grep opens it, and skipping both means re-litigating it with nothing signalling that anything is missing. The digest helps but does not replace it: 20 lines distilled from hundreds of leads is necessarily lossy, and it reads leads rather than detail, so the thing you need may be in a `## Detail` it never opened. So before you write a changelog entry, confirm you did both: *did I list `docs/changelog/` this session, and grep it for what I touched?*

Then read `docs/CLAUDE_TODO.md` — the list of work this project owes and hasn't done. Also an action rather than a load: the file doesn't exist until a session writes the first item, so an `@`-reference would dangle in every repo that owes nothing. Absent means nothing is outstanding. It is short by design, so read all of it — and read *Owed follow-ups* below before you act on any of it, because the one thing that section insists on is that finding an item there is never a reason to start on it.

`CONCEPT.md` and `ARCHITECTURE.md` are the source of truth for *what* we're building, what we've built, and *how* it's structured. If something in the code contradicts them, either the code or the doc is wrong — flag it rather than guessing.

## Changelog discipline

Every change you make to this repository must be recorded in `docs/changelog/` as **its own file**, named `YYYY-MM-DD-<slug>.md` — the date you write it, then a 2–4 word kebab-case slug. Same shape as `docs/specs/`. There is no single changelog file and there are no entry numbers: one file per change is what makes two parallel changes unable to collide, because no two can reach for the same path.

- **The file has two tiers, bounded differently.** A `#` heading, then a **lead** of **1–5 lines, ~20 words per line at most** — never one packed run-on line, since a 40-word run-on isn't a short entry, it just hides the bulk on a single line. Below it, a **`## Detail`** section — **owed whenever one of these exists, omitted only when none does**: an alternative you rejected (so nobody re-introduces it), a constraint that still binds, a known limit or deferred follow-up, a gap a future session would otherwise assume your verification covered. Write it now or not at all: these live in the context of the session that made the change and are gone when it ends, which is why the section is owed here rather than merely allowed. **It never restates the lead** — padding, not omission, is what goes wrong with a section nothing bounds by length. The lead is bounded because it is what a session reads to orient; the detail is bounded by *purpose*, because nothing else bounds it now that entries no longer share a file. Past ~40 lines of detail the change wanted a spec — write one and link it.
- **Write the lead as durable project memory, not a recap of the diff**: what is now *true that wasn't before* — new behavior, state, or rule — plus, in a clause and only when it isn't obvious, the *why*. Skip filenames, mechanical edits, and refactors with no behavior change; the diff and commit already hold those. Self-check: *if a future agent reads this before the code, does it learn what changed, why it matters, or what's now safe to assume?* If not, it's noise.
- **A landed entry file is never renamed, moved, or deleted.** Its path is its address — other docs, specs and code comments cite it by slug — and this is the one thing here that can break silently. Two changes claiming the same path is a merge conflict, which git blocks; a rename produces no conflict and breaks every citation to that entry with nothing noticing. **Cite in the other direction too:** when your change revisits, narrows or reverses a decision an earlier entry recorded, name that entry's slug in yours. That makes the chain greppable instead of something a future session has to reconstruct — and with no digest carrying it, this is *the* mechanism by which rationale reaches back past the recent window, without any entry growing.
- Write the entry as part of the same change. One file per change: don't batch several changes into one file, and don't skip entries. If a change is genuinely several, give each its own file.
- **Where `HEAD` is doesn't matter.** On `main`, on a PR branch, in a worktree — the entry file is written the same way in all of them, with nothing staged or moved at any point, because the file you write is the file that lands.
- **A deferred follow-up gets written twice, deliberately.** Once in this entry's `## Detail`, where it is a permanent statement about what this change left undone, and once as a line in `docs/CLAUDE_TODO.md`, where it is a claim about what the project owes *right now* — see *Owed follow-ups* below for why neither can do the other's job.
- **Write so the index and a grep can find it, because nothing else will.** There is no digest and no curated decision list — deliberately, since any such file is one every change appends to and every compaction rewrites, which is the collision the folder exists to remove. Your entry file is the only copy, reachable exactly two ways: by its slug in `ls docs/changelog`, and by grep over the folder. So the **slug names the thing, not the activity** — `expired-refresh-tokens`, not `auth-fixes` — because the slug is the only part of your entry every future session sees. And when the change carries a decision that must not be re-litigated, say it in the words someone would search for: name the alternative you rejected, the constraint that still binds, the thing you tried and removed. Don't allude to it. An entry nobody can find is an entry nobody wrote.
- **Entries from before the per-entry folder (numbered 1–612) carry synthetic dates.** They were split out of the old single-file `docs/CHANGELOG.md` + `docs/CHANGELOG-archive.md` at five per day, so their filename dates give order and rough vintage only — never read them as when something happened. Each records its old number as `<!-- entry N -->`, and the six milestone summaries as `<!-- milestone M1: entries 1–15 -->`.
- **An `entry N` or `CHANGELOG N` citation** — in code comments, tests, specs, docs, or other entries — resolves with `grep -rl -- '<!-- entry N -->' docs/changelog/`. New entries have no number; cite them by slug.

Same change, bad vs. good entry — `docs/changelog/2026-03-14-expired-refresh-tokens.md`:

- **Bad** (short, but just recaps the diff — zero orientation value):
  ```
  # Auth changes

  Updated auth files, reworked middleware, added tests, renamed AuthHelper.
  ```
- **Good** (states what's now true, with the why in a clause, and puts the rejected alternative where it can't rot):
  ```
  # Expired refresh tokens rejected before session lookup

  Auth now rejects expired refresh tokens before session lookup; stale sessions can
  no longer silently renew. Validated at the middleware boundary, so handlers can
  assume every request they see is current.

  ## Detail
  - Rejected checking this in the session store: the store can't tell "expired" from
    "never existed", so the error the client got would have been wrong.
  ```

## Owed follow-ups

`docs/CLAUDE_TODO.md` is one list of work this project **owes and has not done** — things a session found while doing something else and deliberately left alone. You write it and you drain it — the shape of the whole file is below. It doesn't exist until the first item needs writing, and it goes back to not existing when the last one is done.

**It is not a work queue. Never begin an item because it is in the file.** The user asking is the only thing that starts work, here as everywhere. What the list is for is the moment *before* you start: you've been asked to change an area, and the list tells you what an earlier session already knew was wrong with it. Surface the relevant item then — one line, as context for the change you were actually asked to make — and let the user decide whether it comes into scope. An item stays on the list however long it sits there; age is not a mandate.

**If you find an item you didn't write, treat it as a request, not a to-do.** The file is yours by convention, not by permission — nothing stops the user typing into it. Don't leave such a line sitting silently: say it's there and ask whether they want it done now.

**How the user drives it.** Writing the list is yours; *acting* on it is always theirs. Four things they'll say, to honor plainly whenever they come up:

- **"What's outstanding?"** / "anything owed in here?" — read the list back, narrowed to the area if they named one. If the file doesn't exist, say nothing is outstanding rather than that a file is missing.
- **"Do that one."** — an ordinary request that happens to have a handle. Do the work, write the changelog entry, delete the line: one change.
- **"Drop it."** / "we're not doing that" — delete the line *without* doing the work. This is the one deletion that isn't a completion, and it earns no changelog entry, because nothing changed. Don't argue for keeping it; a list whose items have to be defended out of it stops being a list anyone reads.
- **"Add X to the TODO."** — write it in the item shape below, from what they said. This is them asking you to record something, which is not the same as them typing a line in themselves.

**Say it in the turn when you add an item** — one line, *"noted a follow-up about X"*. Don't ask permission; deferring the work was already the decision, and the entry in the file is a consequence of it. But a file the user isn't watching that grows silently is a file they stop trusting.

**What earns a line.** Something genuinely owed, that you found, that was out of scope for the change you were making — a limitation you hit, a gap in coverage you noticed, work the change you just landed implies but doesn't contain. The bar is *would a future session want to know this is still outstanding*, not *could this be better*. Three things that don't belong: work the user just asked for and you're about to do anyway; refactors and ideas with nothing behind them, which is how a list becomes something nobody reads; and anything small enough that writing it down costs more than doing it — if it's in scope and cheaper to fix than to record, fix it.

**The whole file, and the shape of an item.** A heading, a short note for whoever opens the file without having read this section, then bullets — each one bullet, no nesting: what is owed in a bold clause phrased as the thing rather than the activity, a sentence of why it matters, then where it came from — the slug of the changelog entry that surfaced it, whenever there was one.

```markdown
# Owed follow-ups

Work this project owes and hasn't done, written and drained by Claude — the rules are
under *Owed follow-ups* in `CLAUDE.md`. Items are deleted when the work lands, so this
is not a record of anything; `docs/changelog/` is. A line you add here is read as a request.

- **`SessionStore.purge` drops sessions it can't decode** — a decode failure is read as
  expiry, so a schema change would silently purge live sessions. Surfaced by
  `2026-03-14-expired-refresh-tokens`.
```

The note is there because every rule about this file lives here, in a `CLAUDE.md` that someone browsing the repo isn't reading — without it they find a bare list with no sign of who wrote it or that lines get deleted. It's fixed text: it doesn't grow, and it can't go stale.

**Structure stops there.** No grouping by area, no priorities, no owners, no dates, no status column, no done section, no item numbers. Each of those will look like an obvious improvement at some point, so:

- **Grouping by area** — a list short enough to read whole doesn't need an index into itself. And when it does get long enough for headings to help, *the length is the finding*: the bar for a line has slipped. Reach for the drain, not the heading.
- **Dates** — the origin slug already carries one (`2026-03-14-expired-refresh-tokens` says when this surfaced). A second date field adds nothing and invites deciding by age, which *not a work queue* above already rules out.
- **Priorities and owners** — everything here is owed and nothing here is scheduled. A priority column is a claim about sequencing that only the next request can actually make.
- **A done section or item numbers** — these are the two ways the file starts behaving like a record, and it isn't one. Numbers are the worse of the two: *Never cite it* below works only because there is no stable anchor here to cite.

**Delete the item when the work lands** — in the same change that does the work. Not struck through, not moved to a done section, not kept with a note. The changelog entry is the permanent record and the item was never a record of anything; keeping both means the list stops draining and starts having to be read *past*. Deleting is what makes the file worth reading at session start: everything still in it is outstanding **now**, by construction.

**Why this exists when a changelog entry's `## Detail` already records deferred follow-ups.** They are the same discovery in two different tenses, and neither does the other's job. The entry says *we deferred this* — permanently, at the address of the change that deferred it — and because a landed entry is never edited, it goes on saying so long after the work is done. Nothing in an append-only record can tell you what is *still* owed. This file can, because it is mutable and because deleting is half the convention. So write both: the entry, because that's where the reasoning survives being deleted from here, and the line, because that's the only place the present tense lives. An item with no entry behind it loses its reasoning the moment the session ends.

**Never cite it.** No spec, doc, changelog entry or code comment points at this file or at a line in it. Every line is temporary by design, so citations to it break silently as it drains — the exact inverse of a changelog entry, whose path is a permanent address. Cite the entry instead.

**One file is deliberate.** A list you can't see at once isn't a list, so this doesn't get the per-entry folder treatment `docs/changelog/` gets — and doesn't need it. What makes a shared file collide is the periodic rewrite, and nothing rewrites this one: two changes appending both touch the last lines, git reports it, and the resolution is always *keep both*.

**If it only ever grows, the bar is too low.** A list that drains is working. Thirty items nobody has looked at is a list that gets skimmed and then ignored, at which point every line in it is a false claim that something is being tracked. When it stops draining, say so and name the lines you think nobody was ever going to do — the *drop* handle above is how they leave, so this is a prune to propose, not one to perform. Carrying them silently is the only wrong answer.

## `TODO.md` is the user's

`TODO.md` — here that is `docs/TODO.md`, and the rule covers any file of that name anywhere in the repo — is the user's own list, and **you never write to it**. Don't add, tick off, strike through, reword, reorder or delete items; don't create the file when it's absent, and don't move or delete it. That holds when your change finishes something it lists: say so in the turn — *"this completes 'X' in `docs/TODO.md`"* — and leave the ticking to the user. Reading it is fine.

It is not `docs/CLAUDE_TODO.md`. That one is yours, under *Owed follow-ups* above, and it's where a follow-up you find goes — never into `docs/TODO.md`. So "add X to the TODO" means `docs/CLAUDE_TODO.md`. If the user asks for a change to `docs/TODO.md` itself, give them the exact lines to paste instead of making the edit: the file stays theirs because they are the only one who ever writes to it.

## Nested guidance

Each subtree has its own `CLAUDE.md` with scoped tool/skill rules:

- `edge/CLAUDE.md` — Raspberry Pi thin edge: capture, clip, motion gate, the single HTTP server (`/stream`, `/frame`, control API, config UI), and optional actuator drivers. **No ML.**
- `compute/CLAUDE.md` — NVIDIA PC "brain": detection, tracking, individual re-ID identification, the decision engine, event store, notifications, dashboard, and the learning loop.
- `shared/CLAUDE.md` — the cross-tier **contracts** (data model, event/intent schemas, the Pi control-API shape, constants) *and* the cross-tier **logic** both tiers instantiate: the `MotionGate` core and the frame wire format.

## Reviewing changes

**This repo has exactly one review point: the commit.** Do not review after every edit, and do not run an unrequested review pass mid-session. A pass per prompt costs more than it catches — it only ever sees its own turn's edits, reviews them from inside the context that just wrote them, and the next follow-up ("also add X", "now handle Y") re-opens what it just looked at. Nothing in that scheme ever reads the accumulated diff as one change.

What does *not* wait for the commit is verifying your own work: run the tests, the build, and whatever the subtree's `CLAUDE.md` mandates — the edge's on-device / fake-source checks, compute's pytest suite plus the Playwright MCP pass on a changed dashboard view — per change, as always. Deferred here is the *review*, not the checking. Never report a change complete on the grounds that its review comes later.

The commit is the right moment because it's when the diff is sealed into history, and because it's the first point where the accumulated diff can be read as **one change** — which is what a review needs.

**So when the user asks you to commit (or commit and push) and the working tree holds non-trivial changes, ask before committing** — `AskUserQuestion`, two options in this order:

1. **`/code-review high --fix`** — review the accumulated diff: broad coverage, correctness plus reuse / simplification / efficiency, repairs applied to the working tree.
2. **Commit now** — skip the review and go straight to the commit.

Recommend option 1 for the diff this gate usually sees — accumulated over several rounds, or landed by a fan-out. `/code-review` ships with the harness, so it is always available exactly as written — keep the explicit `high`, because without a level the command silently reuses whatever level was typed last.

**You** run the pass; the user is only choosing whether it happens. This is the one place the review flow stops to ask — there is no end-of-turn nudge to go run a review elsewhere.

- Ask **once per commit request**, not per round or per file. If the user picks the review, run it, report, then continue to the commit without asking again.
- **Skip the question** when the diff is trivial — a typo, a version bump, a changelog line — or when a full review pass has already covered this working diff since the last edit. Asking there is noise.
- **Any** commit request goes through the gate, including a delegated one (`/git commit`, `/git commitandpush`) — check it *before* handing off, not after. A skill or subagent that does the committing never sees this rule.
- If the user picks *Commit now*, that's the answer — commit as asked, and don't re-offer or hedge about it afterwards.

### Running the review pass

`/code-review high --fix` reviews the current diff at high effort: broader coverage than the lower levels, including findings it isn't fully certain of, which is what you want at a gate that fires once per commit. Its brief covers correctness bugs *and* reuse / simplification / efficiency cleanups in the same pass, and `--fix` applies what it finds to the working tree after the review. Its own report stands; don't restate it. Being a broad pass, it will also repair things that aren't blockers, so read the resulting diff before committing rather than assuming every edit was one.

**The surfaces that most need a reviewer's eye in this repo**, when the diff touches them — name them when the gate asks, so the choice is informed: the store's counter / eviction / accounting paths and anything holding the shared SQLite write lock; schema changes, and anything that deletes or purges frames, crops, or labels; the `shared/` cross-tier logic (wire format, `MotionGate`) both tiers depend on; the durable learning-loop tables (`cats`, `dataset_items`, `model_versions`) that survive eviction; and actuation — today the foreign-cat sound alarm — and its decision policy.

Close by saying plainly what the review couldn't settle — you guessed at intent, left a known gap, or nothing covers it (it only runs on the compute PC, or on the Pi) — so the user knows where their own judgement is still needed. State it as a fact about the change, not as a recommendation to run anything.

## Multi-agent workflows

When you fan a task out across subagents — the Workflow tool ("ultracode") — tier each agent's model and reasoning effort to the work, so cost tracks value instead of every agent defaulting to the strongest (most expensive) model:

- **Strongest model** (the session model) — contracts, correctness-critical implementation, adversarial review, and final synthesis. Never downgrade these; they are where quality is won or lost.
- **Mid model** — build/test runners, straightforward mechanical implementation, and verifying concrete already-stated findings or applying decided fixes.
- **Cheapest model + low effort** — docs/changelog, i18n, styling, and other boilerplate.

The guardrail: the stage that *catches* problems (adversarial review) stays strong; the stages that merely *check* an already-caught finding (per-finding verification) drop a tier — unless the finding is subtle or security-/data-integrity-critical, where verification stays strong. Set this per `agent()` call (`model` / `effort`); an agent that omits `model` inherits the session model, which is why an untiered fan-out silently runs everything on the most expensive tier.

**Invoking a named workflow is not authoring one.** The tiers above are yours to set only when *you* write the `agent()` calls. A built-in or named workflow — e.g. `Workflow({ name: 'code-review' })` — runs its own stages on the session model; nothing tiers them for you, so a wide fan-out (the review's per-`(file,line)` verifiers most of all) silently bills every agent at the top tier. Before launching one at `high`+ effort or over a broad diff, check the `scriptPath` the run reports: if a large *checking* stage isn't tiered, edit that script to drop those agents to the mid model (leaving the finders and final synthesis strong) and re-invoke with `{ scriptPath }` instead. Keep them strong only when the diff is security-/data-integrity-critical. For `code-review` specifically (verified 2026-07-30): its script carries **no** `model:` overrides at all — scope, finders, per-`(file,line)` verifiers, sweep and synthesis every one inherit the session model, so the verify fan-out is exactly the stage to tier by hand. Two gotchas when you edit the snapshot: the script is a **per-run** copy, so a fresh `/code-review` starts untiered again (re-invoke with `{ scriptPath }`, or resume with `{ scriptPath, resumeFromRunId }`); and adding `model:` to an agent that already **completed** changes its `(prompt, opts)` cache key, so it re-runs on resume — leave finished stages alone.

**A workflow's aggregate diff is what the review gate is for.** A fan-out edits files across several subagents — often in separate worktrees — so no single agent ever saw the whole combined change, and any review stage *inside* the workflow checked its own findings, not the landed diff. This doesn't earn an extra pass on return; the commit is still the review point. But when the gate asks, say the diff came out of a fan-out — it's where the review earns its cost most clearly, so recommend it there even when every agent's own slice looked small.

This section is inert unless you actually run a multi-agent workflow.

## Git workflow

**Direct to `main`** — when you commit, commit straight to `main`; don't open branches or PRs unless asked. A remote (`origin`) is configured; **push only when the user asks** (e.g. via `/git`), never automatically.

**This setting only chooses *where* commits go — not *when* to make them.** Commit only when the user asks; finishing a change is not a cue to commit it. When you do commit, each commit is one complete change including its changelog entry file — never leave the tree half-committed.

A commit request first passes through the review gate in *Reviewing changes* above — check it before staging anything, including when the commit is delegated to `/git`.

<!-- Add additional sections below as the project develops:
  - Project-specific forcing rules (e.g., a policy the agent must follow before touching actuation)
  - Destructive-operation guidance if the agent's defaults aren't enough
  - Naming conventions, code-organization rules
-->
