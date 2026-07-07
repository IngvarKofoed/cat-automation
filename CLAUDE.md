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
- `shared/CLAUDE.md` — the cross-tier contracts: data model, event/intent schemas, the Pi control-API shape, constants.

## After making changes

After a non-trivial edit, invoke the **`code-review`** skill with the `medium --fix` arguments to review the touched code for correctness, reuse, clarity, and efficiency, then apply every finding to the working tree automatically. Bump to `high --fix` for large or high-risk changes — broad diffs, or security- / data-integrity-sensitive code. It does not auto-trigger — you must invoke it explicitly.

Once the fixes are applied, report what changed:

1. **Group the applied fixes by severity** — blockers (correctness bugs, data loss, security), should-fix (clear improvements, missed reuse), nits (style, naming, minor clarity).
2. **Summarize each bucket in one line** so the user can see what was fixed without expanding every finding.
3. Do not stop to ask which to fix — all findings are fixed by default. The user can review the diff and revert anything they disagree with.

## Multi-agent workflows

When you fan a task out across subagents — the Workflow tool ("ultracode") — tier each agent's model and reasoning effort to the work, so cost tracks value instead of every agent defaulting to the strongest (most expensive) model:

- **Strongest model** (the session model) — contracts, correctness-critical implementation, adversarial review, per-finding verification, final synthesis. Never downgrade these; they are where quality is won or lost.
- **Mid model** — build/test runners and straightforward mechanical implementation.
- **Cheapest model + low effort** — docs/changelog, i18n, styling, and other boilerplate.

The guardrail: only the mechanical stages get a cheaper model — the stages that *catch* problems stay strong. Set this per `agent()` call (`model` / `effort`); an agent that omits `model` inherits the session model, which is why an untiered fan-out silently runs everything on the most expensive tier. This section is inert unless you actually run a multi-agent workflow.

## Git workflow

**Direct to `main`** — when you commit, commit straight to `main`; don't open branches or PRs unless asked. Commits are local; leave pushing to the user (no remote is configured yet). `main` is created on the first commit.

**This setting only chooses *where* commits go — not *when* to make them.** Commit only when the user asks; finishing a change is not a cue to commit it. When you do commit, each commit is one complete change including its `docs/CHANGELOG.md` entry — never leave the tree half-committed.

<!-- Add additional sections below as the project develops:
  - Project-specific forcing rules (e.g., a policy the agent must follow before touching actuation)
  - Destructive-operation guidance if the agent's defaults aren't enough
  - Naming conventions, code-organization rules
-->
