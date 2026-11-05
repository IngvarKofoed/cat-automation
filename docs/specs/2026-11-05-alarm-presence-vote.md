# Foreign-cat alarm: decide on the cat's whole presence, not the last 5 seconds

The alarm sounds at our own cats far more often than it should. It re-votes every second over
the last 5 s of counted frames, so a long visit gives it a fresh draw every second. One window
in which a lookalike neighbour edges ahead (8 of 25 frames was enough on 5 Nov 04:15) sets it
off, even when the visit as a whole names a resident. This changes only the **start rule**:
- vote over everything seen of the cat since it appeared;
- require that vote to stay foreign for 1 s of camera time;
- keep a narrow **takeover net** for a stranger who follows one of our cats into the same visit,
  the one case a whole-presence vote is blind to.

Replayed over the stored frames of 25 Oct–5 Nov, this cuts alarms on visits named as ours from
20 to 5–7. It still sounds, while the cat is in view, on every one of the 34 neighbour or
unknown-cat visits the current rule sounds on, deciding about 1 s later. The stop rule, the
processing step and the switches are unchanged.

## Outcome

**What you get:**

- The alarm sounds at our own cats far less often. On the replay of 25 Oct–5 Nov, 5–7 visits
  named as ours set it off instead of 20, and every neighbour or unknown-cat visit that alarms
  today still alarms.
- A stranger who follows one of our cats in still sets it off, through the takeover net.
- Each alarm in the admin log says which path fired it, `presence` or `takeover`. The pass
  readout says how its time splits between detection and identification.
- A replay command re-checks the start rule against stored frames. It is the only way to see a
  miss.

**How to verify:**

- On the compute PC, run
  `python -m compute.tools.alarm_replay --since 2026-10-25 --until 2026-11-05`.
  - It reports model 6 with counted frames on every day of the range.
  - At both tick phases, the added-misses list is empty, and the new rule has no late starts
    on visits not named as ours.
  - Alarms on visits named as ours read about 20 for today's rule against 5–7 for the new
    one.
  - The new rule's median decision delay is within about 1.5 s of the baseline's.

  The figures shift slightly as those days' frames age out.
- In the same output:
  - 5 Nov 04:15 (Kali) is alarmed by today's rule only;
  - 3 Nov 22:24 (Jhinie) is alarmed by both, as `takeover` under the new rule;
  - most of the new rule's starts on visits the card names as a neighbour read `presence`;
  - from 1 Nov 21:45 on, the baseline's starts line up broadly with the 26 real alarm rows
    listed beside them.
- Run it over a range the store holds nothing for, such as a week in 2025. It refuses with a
  non-zero exit instead of printing an empty "no added misses".
- After deploying, with the switch On, the next alarm's row in the admin alarm log shows its
  trigger. Rows from before show none.
- A lone neighbour at ~10 fps, either simulated at the door or seen in the replay, is recorded
  as `presence`, even on a tick where the takeover net was also met.
- With a cat in view, `GET /api/deterrent` returns `alarm.last_pass` with `detect_ms` and
  `identify_ms`, which sum to no more than its `ms`.

## Key decisions

- **Vote over the cat's presence, not a 5 s window** (diverges). The start rule of
  `docs/specs/2026-11-01-foreign-cat-alarm.md` used a 5 s window, and that spec rejected a
  whole-visit vote for three reasons. Each now has an answer:
  - *A departed stranger's frames restarted the alarm at a resident following it in.* The
    vote never reaches back past the previous alarm's end (entry 526's floor, added after
    that spec).
  - *A pause over 2 s reset the count.* "Presence" is not the feed's 2 s visit. It is the run
    of counted frames with no gap over 5 s (`DETER_WINDOW_S`), so it remembers at least as far
    back as today's window.
  - *A resident's earlier frames outvote a tailgating stranger.* That is the takeover net's job.

  The presence reaches back at most 2 minutes, which bounds the read. Past that it slides,
  voting over the last 2 minutes only. No visit in the replay ran longer than 72 s, and the
  replay reports how often the cap was reached.
- **Same voter, same 10-frame minimum** (reuses). The verdict is still
  `Store._aggregate_identity` over counted frames (`Store.alarm_frames`), at the active
  threshold. Foreign still means a neighbour won, or nothing came within the threshold. An
  uncalibrated gallery still never starts an alarm.
- **Foreign for 1 s straight, in camera time** (new). The vote must have stayed foreign for
  every counted frame across the last second of `recv_ts`. It is not counted in ticks, because
  a pass takes 1–4 s while a cat is in view (16 passes measured live on 5 Nov: 4–7 frames in
  1.1–1.6 s, 26–30 frames in 2.4–4.0 s). A tick-counted hold would therefore add a whole
  pass. Under today's processing delay, the frames that satisfy the hold are usually already
  there when the vote first turns foreign.
- **Every window is measured in camera time** (diverges). Today's start window runs back
  5 s from the wall clock, so a 3 s processing delay leaves it ~2 s of identified frames.
  Here every window and gap is measured from the newest counted frame's `recv_ts`: the hold,
  the takeover window, and the presence gap. Wall-clock age is consulted only when the pass
  has caught up, the same condition under which the gone check trusts it.
- **A takeover net** (new). This path starts the alarm with no hold, when in the 5 s up to the
  newest counted frame:
  - there are at least 20 counted frames;
  - and at least 90% of them are *foreign frames* (`deterrent.is_foreign`, the stop rule's
    own test).

  The net catches a neighbour following one of our cats in, whose frames the presence vote
  would hand to the resident. In the replay it costs about two alarms on visits named as ours
  over the two weeks. It is kept because it keeps 3 Nov 22:24: the "Jhinie" frames there show
  a mottled cat, the later "Store Jihn" frames a striped tabby with a white paw, and today's
  rule alarmed on it.
- **One decision function, shared by the worker and a replay tool** (extends).
  - `deterrent.start_decision(...)` is the whole start rule as a pure function, and
    `Alarm.maybe_start` calls it.
  - A new read-only CLI, `compute.tools.alarm_replay`, calls the same function over stored
    frames. A thin Store-reading wrapper like `diagnose_misses`.
  - A miss writes no `actions` row, so "no added misses" is invisible in the alarm log. The
    replay is the only way to check it, now and whenever these numbers are retuned.
- **The record says when the net overrode the vote** (extends). There is a new
  `actions.trigger` (`presence` | `takeover`, NULL on older rows and tests), added by
  `_migrate_schema`'s probe-and-`ALTER` (entry 322). The admin alarm log shows it.
  - `takeover` is written only when the presence vote did *not* call the cat foreign at that
    tick: the net started an alarm the vote would not have. Every other start reads
    `presence`.
  - The net has no labelled data behind it, so how often it overrides the vote, and on what,
    is how its numbers get checked.
- **The processing delay is made measurable, not fixed** (extends).
  - `_alarm_process` times its `_detect` and `_identify` calls separately, so
    `alarm.last_pass` in `/api/deterrent` gains `detect_ms` and `identify_ms` beside `ms`.
  - Each pass has a ~1 s fixed cost, which puts the alarm 1.4–4.5 s behind the camera. Its
    cause can't be read from the code. Candidates:
    - GPU contention with the always-on detection worker;
    - `yolo11x` at 1280 per frame;
    - waits on the shared store lock.
  - The split tells only which half the time goes to, not which of those causes is behind it.
    That is enough to aim the follow-up spec.
- **The previous-alarm floor survives a restart** (extends).
  - `Alarm._last_end_ms` is seeded at start-up from the newest `mode='on'` row's `end_ts`.
  - The launch sweep (`close_open_actions`) therefore runs before the live-naming worker is
    built, so an episode a restart interrupted is already closed. It used to run after.
  - If the newest row is still open, nothing is seeded.
  - Today the floor is memory-only, which is harmless when the vote looks back 5 s. A
    2-minute lookback would let a restart recount the last episode's stranger.
- **Unchanged** (reuses):
  - the stop rule (no foreign counted frame for 3 s);
  - the floor at the previous alarm's end (now also seeded at start-up);
  - the processing step (at most 30 frames a pass; the open visit; the 5 s catch-up floor of
    entry 538);
  - both switches, the clip loop, and how `actions` rows are written and read.

## Goals

- Far fewer alarms on our own cats. In the replay over 25 Oct–5 Nov, the visits named as
  ours that set it off drop from 20 to 5–7.
- **No added misses.** Every neighbour or unknown-cat visit that the current rule sounds on
  while the cat is still in view still sounds while it is in view, in the replay at either
  tick phase.
- Little slower. With no processing delay the new rule decides about 1 s later into a
  foreign visit than today: median 3.5–4.0 s instead of 2.5–2.7 s, and 90th percentile
  9.5–10 s instead of 7.0–8.5 s. A rough model of today's ~2.6 s delay suggested the hold
  mostly overlaps the delay. The tool does not model delay, so that claim is unchecked here
  and belongs to the follow-up spec.

## Non-goals

- **Cutting the processing delay itself.** This spec only splits the pass time (see Key
  decisions); the fix is a follow-up spec.
- Changing the stop rule, the threshold, the gallery, or what counts as a counted frame.
- Using cat size, or any signal beyond the stored identifications.
- Making the alarm's verdict agree with the card in the user app. The player's Alarm line
  keeps saying what the alarm took the cat for.
- Ground truth. The replay judges "ours" by the card (see *The evidence*).

## The evidence

Replay of every stored visit from 25 Oct 00:00 to 5 Nov 06:40:
- 682 visits; per-frame identities from model 6, which made all 26 alarms;
- one continuous timeline of counted frames;
- 1 s ticks at two phases, no processing delay;
- the stop rule and the previous-alarm floor applied to both rules.

| Rule | Alarms on visits named as ours (496) | Neighbour / unknown visits alarmed while in view (46) |
|---|---|---|
| Today: last 5 s, 10 frames, most votes | 20 | 34 |
| Presence vote + 1 s hold | 3–5 | 34 |
| Presence vote + 1 s hold + takeover net (this spec) | 5–7 | 34, none lost, none late |

Visits named as ours that still alarm include 25 Oct 06:11 (Kali). There, the "Store Jihn"
frames are a box on the dark shape in the top-left corner after the walking cat left. Whether
that shape is a cat is a detection question, not a vote one. 5 Nov 04:15 (Kali, 19 of 53) no
longer alarms.

Caveats a reviewer should weigh:
- "Named as ours" is the card's whole-visit vote, not a label. Only 1 visit in the period is
  labelled.
- A presence vote is the card's vote run as the visit unfolds, so part of its agreement with
  the card is by construction.
- A real tailgate looks like a false alarm to this proxy. That is why the net exists, and why
  the "alarms on ours" figure is an upper bound on the false ones.

## Design

### The start decision

`start_decision` takes:
- the counted frames, as `(frame_id, recv_ts, cat_id, distance)` oldest first, with
  `recv_ts` in `[max(now − 120 s, floor), now]`;
- `now`;
- the previous-alarm `floor`;
- `caught_up` (the last pass left no frame unprocessed);
- the threshold, the names, and the resident roster.

It returns nothing, or a start with its verdict, evidence and trigger. Below, `newest` is the
`recv_ts` of the newest frame. First, the **staleness check**: if `caught_up` and
`now − newest > DETER_WINDOW_S`, nobody is present and there is no start. While the pass is
behind, nothing is stale, because the frames still to come may continue the presence. Then
both paths are evaluated, and the alarm starts when either is met. *Which is recorded*, below,
says what the row carries.

1. **Takeover.** Take the frames with `recv_ts ≥ newest − DETER_WINDOW_S` (5 s). The path is
   met when there are at least `ALARM_TAKEOVER_MIN_FRAMES` (20) and at least
   `ALARM_TAKEOVER_SHARE` (0.9) of them are foreign by `is_foreign`. It needs no hold.
2. **Presence.**
   - *The presence.* It is the frames after the last gap over `ALARM_PRESENCE_GAP_S`
     (= `DETER_WINDOW_S`) between consecutive counted frames, measured on `recv_ts`. When the
     2-minute bound cuts it, it starts at the oldest frame read. Its prefixes and `t0` then
     start there too, and move each tick.
   - *The prefix votes.* In one pass with running per-cat tallies, compute the vote over every
     prefix of the presence. A prefix is foreign when it holds at least `DETER_MIN_FRAMES`
     (10) frames and its winner is foreign. The tallies must reproduce `_aggregate_identity`'s
     rules exactly: plurality among below-threshold frames, ties broken by minimum distance,
     *unknown* when none is below. The last prefix's verdict must equal `_aggregate_identity`
     over the whole presence, and a test pins that.
   - *The hold.* If the last prefix is foreign, take the unbroken run of foreign prefixes that
     ends at it. Let `t0` be the `recv_ts` of the frame that *ends* the run's shortest prefix:
     the frame at which the vote turned foreign and has stayed foreign since. It is not the
     presence's first frame, which every prefix shares. The hold is met when
     `newest − t0 ≥ ALARM_HOLD_S` (1 s), or when `caught_up` and `now − t0 ≥ 1 s`.
     The second branch lets a short visit whose cat has already left complete its hold. The
     first stops a lagging pass from meeting the hold on frames it has not yet seen.
   - The path is met when the last prefix is foreign and the hold is met.

**Which is recorded.** The trigger, verdict and evidence follow the presence vote, not the
order in which the paths were checked. When both paths are met on the same tick, which is
routine for a lone neighbour at ~10 fps or with a lagging pass, the row must not read
`takeover`.
- If the last prefix of the presence is foreign, the row reads `presence`. That holds even
  when only the takeover path was met because the hold had not yet run out. The verdict is
  `_aggregate_identity` over the presence.
- Otherwise the net overrode the vote, and the row reads `takeover`. The verdict comes from
  the takeover window:
  - `_aggregate_identity` over those frames when it names a neighbour;
  - otherwise `unknown`. Within-threshold resident matches can still win a vote whose foreign
    frames are mostly beyond the threshold, and the record must not name a resident as the
    alarm's target.

Only the label follows this rule. When an alarm starts is unchanged: on the first tick either
path is met.

The evidence recorded is unchanged in shape, with each field read over the frames the
recorded verdict came from:
- `n_counted`;
- `n_votes`, the winner's below-threshold count, or for a takeover the foreign frames;
- `distance`;
- the deciding frame, the newest frame in that set.

### What the worker changes

- `Alarm.maybe_start` reads `alarm_frames(model, max(now − 120 s, last_end + 1))` instead of
  the 5 s window, and hands it to `start_decision` with the `caught_up` the pass just
  reported.
- During a long presence this returns up to ~1,400 rows a tick instead of ~60. It is the
  pinned `CROSS JOIN` plan (entry 529), probing by primary key per row, so it costs
  milliseconds. Pin the plan again with the new bound.
- `alarm_frames` gains an optional `until_ts`, for the replay.
- `_alarm_process` gains only the detect/identify timing. Its frame selection, the gone check,
  the clip loop and the closed-visit pass are untouched.
- `Alarm` seeds `_last_end_ms` at construction from the newest `mode='on'` row's `end_ts`, or
  seeds nothing if that row is still open. In `app.py` the launch sweep (`close_open_actions`)
  moves above the live-naming worker's construction, so the row is closed by then.
- **Known limit, accepted.** Entry 538's catch-up floor re-applies on every pass with no alarm
  running: the step never reaches back past the 5 s before `now`. So frames older than that
  are never identified by the alarm step, and are absent from the vote, in three cases:
  - after a switch-on mid-presence;
  - after a restart mid-presence;
  - whenever the step lags more than 5 s behind the camera, which would drop frames from the
    middle of a visit.

  The measured delay is 1.4–4.5 s, so the third case is rare today. The follow-up delay spec
  is where to revisit it. Widening the floor brings back entry 538's minutes-long replays at
  switch-on.

### Recording

- `actions.trigger TEXT` is added by the existing probe-and-`ALTER` migration.
- `add_action` writes it; `list_actions` and `/api/deterrent/log` return it.
- In admin's alarm log, each row shows `presence` or `takeover` beside the verdict. Older rows
  show nothing.
- The user app is untouched.

### The replay tool

```
python -m compute.tools.alarm_replay --since 2026-10-25 --until 2026-11-05
```

The tool runs on the compute PC and is read-only by construction, calling no `Store` write
method, as `diagnose_misses` does. Both dates are inclusive local days: the range runs from
00:00 on `--since` to the end of `--until`. It works as follows:
- **Feed.** It walks the feed (`_walk_events`) from the range's first frame for each visit's
  span and card identity. It drops visits that start after the range ends, since the walk
  itself has no upper bound. It uses a page budget large enough for the range, and if the walk
  comes back `truncated` it **refuses** with a non-zero exit rather than report on part of the
  range.
- **Frames.** It reads the counted frames over the same range, using
  `alarm_frames(..., until_ts=)` under the active model. A promotion leaves earlier days with
  no identifications under the new model (entry 337), and eviction removes old frames. Either
  way, both rules would alarm on nothing, so the tool also **refuses** when the range holds no
  visits, or visits but no counted frames.
- **Simulation.** It runs 1 s ticks at two phases with no processing delay. It drives two rules
  through the same episode mechanics: today's rule, frozen in the tool as the baseline, and
  `start_decision`. The mechanics are the gone rule via `is_foreign` and the floor at each
  episode's end.
- **Attribution.** Each start goes to the visit whose `[start_ts, end_ts + _VISIT_GAP_MS]`
  holds its deciding frame, the feed's own attribution rule (entry 550). A visit counts as
  alarmed only if the start's decision time is at or before the visit's `end_ts`, so a start
  after the cat left is no catch. Late starts are listed separately, for both rules.

It prints:
- the model id and threshold it ran under, and per day: visits, visits with counted frames,
  and the starts no visit window claimed;
- the two summary counts from *The evidence*, per rule and phase;
- **added misses**: visits not named as ours that the baseline alarms while in view and the
  new rule does not, which should be none. A presence spanning two feed visits can credit a
  start to the other one, so an entry here is checked in the player before it is believed;
- the late starts, per rule;
- each rule's decision delay into the visits it alarms (start minus visit start, median and
  90th percentile);
- the longest presence and how many times the 2-minute bound was reached;
- visits named as ours that each rule alarms, with time, card name, votes and trigger, so they
  can be checked in the player;
- the real `mode='on'` rows of `actions` in the range, beside the baseline's starts. The
  baseline has no processing delay, so the two will not match one for one. Large differences
  mean the replay does not reflect what the door actually did.

It reports what the rules decide, not when the pipeline would have heard it. Processing delay
is out of its scope by design.

### Docs

- ARCHITECTURE's *The foreign-cat alarm (built)* Start bullet is restated: presence vote,
  hold, net.
- `deterrent.py`'s module docstring is updated to match.
- The user page's comment saying "the alarm decides on ~10 frames in 5 s" is reworded.

## Alternatives considered

- **A takeover net at 80%, or none.** 80% keeps more possible tailgates but gives 8–9 alarms
  on visits named as ours. No net gives 3–5 but drops 3 Nov 22:24, likely an added miss.
- **Wait before deciding** (no decision in a visit's first 4 s). Replay: 20 → 19. The false
  alarms happen ~10 s into long visits, so a pause at the start skips past nothing.
- **A majority instead of a plurality over the same 5 s window.** 20 → 18.
- **Presence vote with a 30-frame minimum.** 20 → 2, but it loses 1 Nov 23:52, a 7.7 s
  neighbour visit with 20 counted frames.
- **A fast path when the whole-presence share is overwhelming.** It did not beat the hold at
  any share tried (70–90%).
- **The hold counted in ticks.** Identical with instant processing, but under today's pass
  times every tick-hold costs a whole pass (~2.5 s).

## Implementation strategy

*Not part of the design — a starting point for whoever builds this.*

- **Single agent, Opus 5.5.** One thread through `deterrent.py` (`start_decision`, the
  constants, the seeded floor), `live_identify.py` (calling it with `caught_up`, the timing
  split) and `store.py` (`alarm_frames(until_ts=)`, `actions.trigger`). The replay tool calls
  that same function, so it has to come after it. The admin log line and the docs are small.
  The design risk sits in getting the prefix tallies to match `_aggregate_identity` exactly,
  and in the camera-time and `caught_up` edges, which parallel agents would only guess at.

## Amendments

- **2026-11-05.** The floor seeding claimed the launch sweep had already closed the newest row
  by the time the worker is built. In `app.py` it ran after. The sweep now runs first, and a
  still-open newest row seeds nothing.
- **2026-11-05.** Checking the takeover net first recorded ordinary neighbour visits as
  `takeover`, which defeated the column's purpose. The label now follows the presence vote:
  `takeover` means the net overrode a vote that did not call the cat foreign. When an alarm
  starts is unchanged.
