# One visit for the feed and the alarm; a 2 s hold that holds

Since the presence vote shipped, the alarm has still sounded at our own cats, and the feed and
the alarm disagree about what a visit is, which makes each such alarm hard to read. Of the 17
visits that set it off between 8 Nov 17:00 and 10 Nov 06:00, five carry a resident's card:
Sultan at 8/11 17:01, 9/11 04:45, 9/11 18:47 and 10/11 05:47, and Saffi at 9/11 18:55. They
have three causes:

- **A cat sitting still splits the visit.** Collecting stores motion frames only, so Saffi
  sitting still for 5.4 s left a gap. The feed split her into three cards. The alarm's presence
  ended at the gap and restarted its vote from zero, discarding 144 Saffi frames.
- **The 1 s hold is used up by processing time.** When a pass catches up, the start rule takes
  "now", read after the pass, as the point through which every frame has been processed. A pass
  with a cat in view takes 1–2 s, so the hold is met before the step has looked at any frame
  from that time.
- **Sultan at night.** Under IR, 32% of Sultan's frames match a neighbour, against 1–3% for the
  other residents. In the first seconds of a visit, Store Jihn can lead the vote for more than 1 s.

This change makes the feed and the alarm share one definition of a visit, which bridges a cat
sitting still. The alarm then votes over that visit so far, and the player says how its verdict
relates to the card's. It also fixes the frontier in both the start and the stop check, and
raises the hold to 2 s. The past is not regrouped: bridging applies only to gaps after a start
date the app saves the first time it runs this build.

## Outcome

**What you get:**

- From the start date, a cat sitting still at the door stays one visit in the feed, and the
  alarm votes over that same visit. A sit like Saffi's at 9/11 18:55 is one Saffi card with no
  alarm.
- The alarm needs its foreign vote to hold for 2 s of footage it has actually checked, and it
  stops only after 3 s of checked footage with no foreign cat. On 8–10 Nov this removes the
  alarms on all five resident cards and keeps all twelve on neighbour cards, about 1 s later.
- For alarms from the start date on, the player's Alarm row says when and on what the alarm
  decided, and what the whole visit says when the two differ.
- Nothing before the start date changes: its visits, counts, labels and flags read as before.

**How to verify:**

- On the compute PC, run
  `python -m compute.tools.alarm_replay --since 2026-11-08 --until 2026-11-10`.
  - The baseline starts on the visits at 8/11 17:01, 9/11 04:45, 9/11 18:47 and 9/11 18:55. The
    new rule starts on none of them.
  - Every visit not named as ours that the baseline starts on, the new rule starts on too: the
    added-misses list is empty.
  - 10/11 05:47 shows in neither, because the replay models no processing delay.
- After deploying, open the 9/11 18:55:49 visit in the user app. It is still its own card,
  and its Alarm row reads as before, since both predate the start date.
- On the first alarm after deploying, the Alarm row adds "Decided N s into this visit, on M
  frames (…)". On a card naming a different cat, it adds "The whole visit says …". Its N and M
  match that visit's own frames.
- Drive the alarm step with a pass that takes over 2 s and begins the moment the vote turns
  foreign. That pass does not start the alarm; a later pass that has checked 2 s of frames past
  the turn does. With a sounding alarm and the stranger's newest frames not yet checked, the
  alarm does not end as gone until a pass has checked them.
- Within a day of deploying, the feed shows at least one visit spanning a gap of over 2 s with
  no stored frames in it, with the cat in the same place on either side.

## Key decisions

- **One bridge rule, shared by every reader of a visit** (new). Two consecutive motion clusters
  are one visit when all of these hold:
  - the last motion frame before the gap and the first after it both carry a `yolo-serial` cat
    box (`Store._best_box`) at or above `_ANNOTATE_MIN_CONF` (0.3);
  - those two boxes overlap by IoU ≥ 0.5;
  - **no new cat:** every cat box stored for the frame after the gap, at any confidence, overlaps
    some cat box stored for the frame before it by IoU ≥ 0.5;
  - the gap is at most 2 minutes;
  - the later frame's `recv_ts` is at or after the bridge start date.

  The two frames are consecutive motion frames, so the gate saw nothing move between them. The
  no-new-cat check stops a stranger who walks up to a resident sitting still from being folded
  into the resident's visit, whenever the stranger shows in that first frame. It uses every
  stored box, not just the strongest, so a faint entering cat still blocks the bridge. A blocked
  bridge only falls back to today's split. The rule is transitive, so a long sit with several
  pauses becomes one visit.
- **Computed on demand from stored data, never saved** (reuses). Visits are already rebuilt from
  stored frames on every read (`events()`, `open_visit`), so the bridge is too. It is a pure
  function of frame times and stored boxes, and every caller asking about the same frames gets
  the same answer. Timing can only make a bridge *missing*: a boundary frame not yet detected
  has no box, which falls back to today's split. Timing can never produce a wrong bridge.
  Rejected: saving each bridge decision in a table. It needs two writers (the alarm step and the
  detection worker) and changes no answer.
- **Only from now, via a saved start date** (new). The API app writes `visit_bridge_since_ms` to
  the settings table at launch if it is absent. No bridge is made across a gap that ends before
  that date, so every visit, count, flag and label from before reads exactly as today. CLI tools
  never write it. It is a timestamp, not an id, so it stays valid across a Clear.
- **The alarm votes over the visit so far** (diverges). The alarm-presence-vote spec defined its
  own presence: counted frames split on a 5 s gap, chosen because the feed's 2 s visit "reset the
  count on a pause". The bridge now covers the pause that mattered, a cat sitting still. Over the
  9 days measured, only 2 gaps of 2–5 s had a cat that moved. On the 17 alarmed visits since
  8 Nov, voting over the bridged feed visit and voting over a bridged 5 s presence decide
  identically. `ALARM_PRESENCE_GAP_S` is retired.
- **The alarm keeps its own filters on that visit** (reuses). It still counts only frames with a
  cat box ≥ 0.3 and a gallery match. It still needs 10 of them, reads back at most 2 minutes,
  never votes on frames from before the previous alarm's end, and keeps the takeover net. The
  card votes on every identified frame, so on a visit with faint boxes the two can still count
  slightly different frames. Dropping the 0.3 floor would let empty-scene boxes vote, which is
  why the floor exists (entry 525).
- **The frontier is the tick's start, not the clock after the pass** (diverges). The previous
  spec said the frontier "is `now` once the pass has caught up", and that was wrong:
  `_alarm_process` reads `latest` at the start of the pass, so "caught up" means "every frame
  received by the tick's start is processed". When caught up, the frontier is the `now` the tick
  began with. When behind, it stays the last processed frame's `recv_ts`.
- **The gone check measures to the same frontier** (diverges). It has the same flaw the other
  way round. It asks for no foreign frame in the 3 s before the *current* clock, using the
  previous pass's `caught_up`, so frames received since that pass started read as absent. That
  can end an alarm on a stranger still in view. It now measures its 3 s up to the previous
  pass's frontier. A running alarm may last up to one tick longer; no start changes.
- **The player explains an alarm against its card** (extends). The user app's Alarm row gains
  one sentence: how far into the visit the alarm decided, on how many frames, what it counted,
  and, when the card names someone else, what the whole visit says. It is the readout of the
  shared visit definition, shown where the confusion arose. It needs four more fields on each
  `alarms[]` entry, all already stored on the `actions` row. Only alarms decided under this
  rule carry it; an older alarm's vote could reach into earlier cards, so the sentence would be
  false about it.
- **A 2 s hold** (extends). `ALARM_HOLD_S` goes from 1.0 to 2.0. Replayed over the 17 alarmed
  visits since 8 Nov, with frames checked on time and the bridge on:

  | Hold | Resident-card alarms (5) | Neighbour-card alarms (12) |
  |---|---|---|
  | 1 s | 3 still sound | all 12 sound |
  | 2 s | none | all 12 sound, about 1 s later |

  Requiring a vote share of 50% or 60% instead also removes the resident alarms, but it drops 3
  or 6 neighbour-card alarms whose truth we don't know. The margin is thin: at 18:47 the wrong
  name led for 1.5 s.
- **The replay compares the shipped rule with this one** (extends). `compute.tools.alarm_replay`
  is the only way to see a missed stranger. Its baseline becomes the shipped presence rule (1 s
  hold, 5 s presence), frozen, replacing the retired 5 s window rule. Its "new" rule is
  `start_decision` as changed here. It bridges across its whole range, ignoring the start date,
  because it simulates and stores nothing.

## Goals

- Stop the three causes above from sounding the alarm at a resident.
- The feed and the alarm agree on where a visit starts and ends, so an alarm can be read against
  the card it is shown on.
- An alarm on a stranger is not cut short by frames nobody has checked yet.
- No visit, count, label or flag from before the start date changes.

## Non-goals

- **Telling Sultan from Store Jihn under IR.** That is identification: more labelled night
  crops and a gallery rebuild. The 2 s hold only narrows the window.
- **Seeing a still stranger.** In collecting, a still foreign cat makes no frames and reads gone
  after 3 s (entry 533). Unchanged.
- **Regrouping the past.** No backfill, no migration.
- **Other groupings.** These keep their current grouping:
  - the annotation queue and the Labelled and Flagged modes;
  - the Motion-tuning calendar's event counts;
  - gate scorecards and `visits()`;
  - held-out validation's 60 s groups;
  - the live-naming worker's work units (`closed_visits`, `open_visit`).

  These are processing or tuning units, not the visit the household reads. The calendar's daily
  event count can therefore read slightly above the Visits page's from the start date.
- **Any change to the stop rule beyond where the gone check measures to**, and none to the clip
  loop or the switches.
- **The admin dashboard.** Its alarm log already shows the verdict, the counts and the trigger.

## Design

### The bridge rule

A single `Store` helper decides whether two adjacent motion clusters join. It takes the
boundary frames' ids, `recv_ts` and all their stored cat boxes (from `_detail_boxes` and
`_box_class`), and the start date, and applies the five conditions above. Constants sit beside
`_VISIT_GAP_MS`:

- `_BRIDGE_MIN_IOU = 0.5`;
- `_BRIDGE_MAX_GAP_MS = 120_000`, which matches the alarm's 2-minute lookback.

Every caller that builds the household's visit goes through it.

**Measured on 1–10 Nov.** There were 111 gaps of over 2 s between consecutive motion frames
under the 2-minute cap, with a cat box on both sides overlapping by ≥ 0.5:
- 99 join cards with the same name, and 1 joins a card with no name.
- 11 join cards with different names. In all 11, the frames either side show the same cat in
  the same pose and spot. One side often has only 2–9 frames, the naming noise this change
  fixes.

Those counts compared the strongest box only. The no-new-cat check can only remove bridges
from them.

**The two risks the data can't rule out, and their guards:**
- *An outage looks like stillness.* Under motion-only storage, a Pi or collector dropout stores
  nothing either, and a cat could leave and another arrive meanwhile. The 2-minute cap bounds
  this, and past it the visit splits as today.
- *A stranger walks up to a resident sitting still.*
  - The no-new-cat check blocks the bridge whenever the stranger shows in the first frame after
    the gap.
  - A stranger that appears only in later frames is still folded in. Its alarm comes slower or
    not at all, never falsely, with the takeover net as the guard. The card can name the
    resident and be hidden by "Hide our cats", and a one-tap Yes would file its crops under the
    resident.
  - The desk's Labelled review is the backstop for that, as it is for any two-cat visit today
    (entry 500).

### The feed

`events()` applies the bridge after `_gap_split` and before the `min_frames` filter and the page
cap. Bridging changes how many visits there are, so it must run before either.

- **Boundary boxes:** one batched read of `analysis.detail` for the boundary frames of candidate
  gaps, by the `(frame_id, analyzer)` primary key. A candidate gap is ≤ 2 minutes and ends at or
  after the start date.
- **A bridged visit's record** is the union of its clusters:
  - `start_id` and `start_ts` from the first cluster, `end_id` and `end_ts` from the last;
  - `n_frames` summed;
  - `rep_frame_id` the peak-area frame over all of them.

  Every per-span read that follows (subject, detection, identity, labels, alarms) runs over that
  span, unchanged.
- **Scan cap:** when the scan hits `_EVENT_SCAN_FRAMES`, the oldest visit in the window is
  dropped as possibly partial, as today. That now covers a bridged visit whose head lies past
  the window.

Everything built on `events()` follows without changes of its own: `_walk_events`, `door_stats`,
the daily counts, `cats_overview`, the user and admin feeds, the clip download, flags, visit
confirm, and the alarm mark.

**Consequences:**
- The alarm mark's window `[start_ts, end_ts + _VISIT_GAP_MS]` stays disjoint, since bridged
  visits are still separated by more than 2 s.
- From the start date, a sit that read as several visits reads as one, so the Visits page and
  daily charts count it once.
- **Known limit, accepted:** the phone's Yes, "Analyse this visit" and clip download refuse a
  span over `_MAX_VISIT_SPAN` (10,000 ids). In Tuning every frame is stored, so a bridged nap
  of about 16 minutes or more reaches that, and the routes refuse it with their existing
  message. That is the refusal a continuous 16-minute visit in Tuning already meets. In
  Collecting a nap stores far fewer frames. The bridge does not cap total length: a cap would
  make where a nap splits depend on grouping order, so the feed and the alarm could disagree.

`events()` takes an optional override of the start date, which only the replay passes ("bridge
everything").

**Performance:** the extra read runs under the shared lock, on every feed page and on each of
the up to 40 pages a daily-count walk takes. Measure it with no `ANALYZE` stats (entry 266),
and pin its plan to a primary-key probe by a test, as `alarm_frames`' plan is.

### The alarm's vote

A new read, `Store.visit_start(frame_id, floor_ts)`, returns the start of the household visit
holding a given frame. It walks back over motion frames applying the bridge rule, and stops at
`floor_ts`, returning it when the visit reaches back further.

`maybe_start` passes the vote's own floor: the 2-minute lookback, or the previous alarm's end
if later. Nothing older can vote, so the read once a second costs at most two minutes of motion
frames. A cat napping by the door for hours, bridged at every twitch, cannot grow it. That is
the starvation class of entries 102–105, which `open_visit`'s bounded walk exists to avoid.
When an alarm starts, one unbounded walk records the exact `visit_start_ts`.

It cannot be `open_visit`. That walk is floored at the live-naming watermark, which sits above
the earlier, already-named part of a still cat's visit: the very frames the vote needs.

`maybe_start` calls it for the newest counted frame and passes the result to `start_decision` as
`visit_start_ts`. `start_decision` replaces its 5 s gap split with "counted frames at or after
`visit_start_ts`", inside the existing bounds: the 2-minute lookback and the previous-alarm
floor. Everything after that is unchanged:
- the prefix vote, the 10-frame minimum and the hold rule (now 2 s);
- the staleness check (5 s to the frontier);
- the takeover net;
- what is recorded.

`actions.visit_start_ts` records the bridged visit's start, so the admin log's latency reads
against the visit the feed shows. The stored `trigger` value stays `presence`; renaming it would
split the log's history for no reader.

For counted frames after a visit's last motion frame (non-motion frames, in Tuning), the visit
is the latest one starting at or before the newest counted frame.

### The frontier, for both checks

The frontier is the `recv_ts` through which every stored frame has been processed. It is:
- the tick's own start (`now` as `_alarm_step` received it) when the pass caught up;
- the last processed frame's `recv_ts` when the pass is behind.

The worker keeps the last pass's frontier, and both checks read it:

- **Start.** `maybe_start` receives that frontier, not a fresh `_now_ms()`. `now_ms`, the
  vote's upper bound, may stay the fresh clock. Only the frontier claims "processed through
  here".
- **Gone.** `Alarm.bookkeeping` runs before this tick's pass, so it reads the previous pass's
  frontier. The alarm is gone when that pass caught up and no foreign counted frame lies in the
  `ALARM_GONE_S` before that frontier. The episode's recorded end time stays the tick's clock.

Neither check can be seen in the replay, which models no processing delay.

### The player's Alarm row

Each `alarms[]` entry in `/api/events` gains `frame_recv_ts`, `n_votes`, `n_counted` and
`trigger`, read off the same `actions` row as the existing fields. They are added only for
episodes decided under this rule, those whose `start_ts` is at or after the bridge start date.

An older episode voted over a presence or window that could reach into earlier cards. The
9/11 18:55:49 alarm counted 13 frames, 8 of them from the 18:55:44 card, and its row does not
record where its vote began. So "decided 0.4 s into this visit, on 13 frames" would be false
about it, and its row reads as today.

The user app's Alarm row appends one sentence per episode that carries the fields:

- **How far in:** "Decided 0.4 s into this visit", the deciding frame's `recv_ts` minus the
  card's `start_ts`. The attribution window keeps this at zero or above.
- **On what:**
  - a `presence` (or older, NULL) row naming a neighbour: "on 13 frames (Store Jihn 8)";
  - a `presence` row with an `unknown` verdict: "on 13 frames, none close to a known cat";
  - a `takeover` row: "on 24 frames in 5 s, 22 of them foreign".
- **Against the card:** when the card's identity names a different cat from the alarm's,
  including a resident, it adds "The whole visit says Saffi." There is nothing to add when the
  card agrees or names no one.

If an entry lacks the new fields (an older episode, or an older compute build), the sentence is
left out and the row reads as today.

## Alternatives considered

- **Keep saving frames for ~10 s after motion stops**, so a still cat leaves real frames rather
  than a gap. No box-overlap check, and it would let the alarm see a still stranger. Rejected
  for now: it touches capture, detection coverage and the grouping rule together, and sits
  longer than the extra time still split.
- **Saving bridge decisions in a table.** Same answers, plus a second writer and a schema
  change.
- **Moving the feed's gap to 5 s instead.** It regroups every visit, including the past, and
  doesn't bridge a sit over 5 s.
- **A vote-share bar (50% or 60%) instead of a longer hold.** Measured above: it also removes
  neighbour-card alarms whose truth is unknown.

## Implementation strategy

*Not part of the design — a starting point for whoever builds this.*

- **Single agent, Opus 5.5.** One thread through four layers that depend on each other. The
  bridge rule in `compute/collection/store.py` feeds `events()`, `visit_start` and the replay,
  and `start_decision` / `bookkeeping` in `compute/learning/deterrent.py` and the frontier in
  `live_identify.py` change together. Only the user app's Alarm sentence is independent, and
  it is too small to be worth its own agent.
- The bridge read and `visit_start` run under the shared lock. Measure both on a no-`ANALYZE`
  replica, per entry 266.
