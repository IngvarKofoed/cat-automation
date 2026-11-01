# Sound an alarm at a foreign cat while it is still at the door

When the compute sees a foreign cat at the door (a named neighbour or an unknown cat), it has
the Pi play `alarm1`, then `alarm2`, then `alarm1` again, alternating through the Pi's audio
HAT, which plain ALSA already drives. The alarm stops once the foreign cat has been gone for
3 seconds. This is the system's first actuator: the `DETER` → `sound` mapping ARCHITECTURE has
always described but nothing implemented.

The Pi stays a pure server. The compute calls a new `POST /api/sound/play` on it, the same way
it already connects for `/stream` and `/api/config`, and the Pi never connects out. The Pi's
own config page also gets a test button, for bring-up without the compute, and an enable
switch that silences the speaker whatever the compute asks for.

The load-bearing part is **when** the compute knows. Today a visit is named only after it
*closes*: `LiveIdentifyManager._tick` waits until a motion cluster has had no motion for
`_VISIT_GAP_MS` (2 s), checks every 5 s, and then detects and identifies the whole span. An
alarm hooked in there would play 5–15 s after the cat had left the frame, which is too late to
deter anything. So this adds an **alarm step** to the live-naming worker. Once a second it
detects and identifies the newest frames while motion is happening, and it starts the alarm
as soon as the last few seconds of frames agree the cat is foreign.

## Outcome

**What you get:**

- With the admin switch On, a named neighbour or an unknown cat at the door sets off
  `alarm1`, `alarm2`, `alarm1`, … through the Pi's HAT. It starts ~1.5–2.5 s after the cat
  comes into view and stops once the cat has been gone 3 s (the playing clip finishes). It
  keeps working while manual jobs run.
- A **Foreign-cat alarm** card on the admin Start page: Off / On, readiness (Pi sound
  reachable and enabled, calibrated gallery, sounding now), test buttons, and the last 20
  alarms with verdict, duration and the deciding frame.
- A **Sound** section on the Pi's config page: an enable switch that silences the speaker
  whatever the compute does, and a Play button per sound that works with the compute down.

**How to verify:**

- On the Pi's config page, *Play alarm1* sounds from the HAT. Disable sound: *Play* greys
  out with the reason, `POST /api/sound/play` answers `423`, and disabling mid-clip cuts it.
- On the admin card, *Play alarm2* sounds on the Pi and adds a `test` row to *Recent*. With
  the Pi disabled or unreachable, the readiness line and the row say so instead.
- On the compute PC, with the switch On and a calibrated gallery, a neighbour's visit starts
  the alarm; *Recent* reads "started N s after the visit's first frame", target ~2.5 s. One
  visit writes one row: the alarm does not restart once the cat has gone. It alternates clips, also with a
  motion-tuning re-run in progress, and stops ≤3 s (plus the clip in progress) after the cat
  leaves. *Recent* shows the verdict, the duration and `gone`. A resident's visit adds no
  row, and neither does a resident following a stranger once the stranger's alarm has ended.
- With the switch Off, or with an uncalibrated active gallery, the same neighbour visit sounds
  nothing and adds no row. The card says why it can't sound, and the feed still names the
  visit as before.

## Key decisions

- **The Pi plays one clip per call; the compute sequences the loop** (reuses). New Pi control
  endpoints `GET /api/sound` and `POST /api/sound/play {sound}` sit beside the existing
  `/api/gpio`. The Pi holds no policy beyond refusing a second play while one is still
  sounding. The compute requests each next clip once the previous one has ended. That makes
  the loop fail-safe by construction: if the compute dies, the Pi finishes the current clip
  (at most 6.5 s) and goes quiet. Rejected: having the Pi loop the playlist itself. That would
  be gapless, but it needs a lease or keepalive and a stop endpoint to avoid a siren that
  outlives a crashed compute.
- **Decided during the visit, inside the live-naming worker** (extends). The alarm step runs
  at the start of every `LiveIdentifyManager._tick`. While the switch is On the worker ticks
  at a fixed 1 s rate (it waits `max(0, 1 s − elapsed)`, rather than a full interval after
  each tick); while Off it keeps today's 5 s. The step reuses the worker's resident embedder,
  its `yolo-serial` analyzer and its active model. Rejected: a separate worker. It would load
  a second DINOv2 and a third YOLO (the always-on detection worker already runs one), and
  duplicate detection this worker already does.
- **The alarm step never yields to manual jobs** (diverges). The live worker yields the GPU
  to `is_busy` (any analysis or training job). That includes the ~44-min-per-slot
  motion-tuning re-runs, "Identify all", and the phone's "Analyse this visit", all of which run
  unattended, so yielding would silently disarm the door for hours. The alarm step handles at
  most 30 frames a pass and keeps running, as the always-on detection worker already does.
  Naming closed visits keeps yielding as today.
- **Closed visits wait while a visit is open** (diverges). While the switch is On with a
  calibrated gallery, the closed pass runs only when no visit is open. It also re-checks
  between closed spans and stops as soon as one opens. Naming one closed span (detect plus
  identify over the whole visit) can take seconds, and a 50-span backlog after a restart can
  take far longer. That would stall the trigger and the "gone" check behind feed
  housekeeping that is in no hurry.
- **The start rule votes over the last few seconds, not the visit** (extends). The alarm
  starts once at least `_DETER_MIN_FRAMES` (10) *counted* frames have arrived in the last
  `_DETER_WINDOW_S` (5 s) and `Store._aggregate_identity` (the feed's voter) calls them
  foreign. The window never reaches back past the end of the previous alarm. A `gone` fires
  3 s after the last foreign frame, so a full 5 s window would still hold ~20 of the departed
  cat's frames and restart the alarm in the same tick it ended. A new alarm needs 10 fresh
  frames. Visit boundaries are ignored, as the stop rule ignores them. Voting over the whole
  in-progress visit was rejected. It kept a departed stranger's frames in the vote, so a
  resident following within 2 s re-started the alarm over and over. It also let a resident's
  earlier frames outvote a tailgating stranger, and it reset the count each time a cautious
  stranger paused for more than 2 s.
- **A frame counts only if the feed would call it a cat** (reuses). A *counted* frame is an
  identified frame whose `yolo-serial` cat box is at or above `_ANNOTATE_MIN_CONF` (0.3), the
  floor the feed's subject rule uses. Identification runs on any box ≥ 0.15
  (recall-first), and boxes between 0.15 and 0.3 are what the feed files as noise. Without the
  floor, phantom boxes on leftover motion could start an alarm, or keep one going with no time
  cap. *Foreign* means the vote's winner is a non-resident (`is_resident = 0`), or nothing came
  within the threshold (*unknown*). An **uncalibrated** gallery (NULL threshold) never starts
  an alarm: there, every cat reads *unknown*, so every resident would trip it.
- **"Gone" is measured on the frames, and a resident frame never prolongs the alarm** (new).
  The loop stops once no *foreign frame* has arrived for `_ALARM_GONE_S` (3 s). A foreign
  frame is a counted frame whose identification is a non-resident within the threshold, or
  beyond the threshold altogether. A frame that matches a resident doesn't extend the alarm. If
  a resident follows the stranger in, the noise stops at the resident, and because the start
  window then holds the resident's frames, it doesn't restart.
- **The compute's switch, Off / On, gates every stage** (diverges). ARCHITECTURE says
  actuators fire only in `running`. Instead, a persisted `deterrent_mode` setting (default
  **Off**) is the compute's only gate, in any stage.
- **A second switch on the Pi silences the speaker** (new). A persisted `sound_enabled` edge
  setting (default **enabled**) makes the Pi refuse every play while it is off, its own test
  button included. That is the "Pi enforces safe behaviour locally" principle: the speaker can
  be silenced at the door, with the compute down or misbehaving, without touching the
  compute's decision. The compute stays the off-by-default gate, so a fresh install still
  sounds nothing until the admin switch is turned On.
- **Every alarm is recorded, one row per episode, and kept** (new). A new `actions` table
  makes ARCHITECTURE's long-documented *Action* entity real. A row is written when an alarm
  starts and completed when it stops. It is an audit log, so, unlike `label_flags` (a work
  list), it survives `clear()` as the labels do. It points at its deciding frame by the
  (`frame_id`, `recv_ts`) **pair**: `frames.id` has no AUTOINCREMENT, and a Clear or the
  non-motion purge can hand that id to a different frame. Resolving on the pair (the entries
  490/504 rule) makes a reused id read as gone instead of showing someone else's frame in the
  very log meant to show whether a resident was hit.
- **The WAVs ship as 16-bit PCM under `edge/`** (new). `alarm1.wav` is 32-bit float, which a
  HAT's hardware device can reject outright. `alarm2.wav` is 24-bit. Re-encoding once to 16-bit
  44.1 kHz stereo means any device ALSA can open will play them. alarm1 peaks at 0.95, so
  nothing clips. The untracked originals in the repo root are left for the user to delete.

## Goals

- A foreign cat at the door hears the alarm while it is still there, until it leaves.
- The alarm never continues or restarts because of one of our cats, and never starts on a
  box the feed would dismiss as noise.
- Every alarm is auditable afterwards: when, how long, why, and on which frame.
- The sound can be tested in one click, from the admin page (the whole compute → Pi path)
  and from the Pi's own config page (the Pi alone).
- The speaker can be silenced at the Pi, whatever the compute is doing.

## Non-goals

- **Door lock, light, and notifications.** `DENY_ENTRY`/`NOTIFY` stay unbuilt; only `DETER`
  → `sound` is wired.
- **The user app.** Alarms are not shown there yet. The admin card is the only view.
- **Volume control.** The ALSA mixer stays whatever it is configured to on the Pi.
- **Cutting a clip short when the cat leaves.** The clip that is playing finishes. The
  compute has no stop call; only disabling sound on the Pi cuts a clip off.
- **Multi-cat policy.** If a stranger and a resident are both at the door, the recent-frame
  vote decides the start, and the stranger's frames keep the alarm going. That is deferred
  with the rest of the access policy.

## Design

### Pi: the sound player

A new `edge/actuators/sound.py` sits behind an injectable runner, the way `GpioOutputs`
hides `gpiozero`. Tests use a fake runner, and the dev Mac (no `aplay`) reports
`available: false` rather than pretending to play.

- **Sounds** are the `*.wav` files in `edge/actuators/sounds/`, named by their stem
  (`alarm1`, `alarm2`), and listed once at startup. A request names a sound. It never names
  a path, so nothing from the request reaches the filesystem.
- **Playing** runs `aplay -q [-D $CAT_EDGE_ALSA_DEVICE] <file>` as a non-blocking
  subprocess. With the env var unset it uses ALSA's default device, which is the HAT the user
  already configured.
- **`POST /api/sound/play {sound}`** returns:
  - `200 {sound, duration_s}` once `aplay` has survived a short settle window (~0.3 s).
  - `423` while sound is disabled on the Pi. This is a distinct code, so the compute records
    "disabled on the Pi" rather than mistaking it for a busy speaker.
  - `404` for an unknown name.
  - `409` while the previous play's process is still alive. Plays never overlap.
  - `503` when `aplay` is missing.
  - `502` with the tail of `aplay`'s stderr when it exits non-zero inside the settle window.
    That catches a device that is busy or rejects the format, so the compute never records a
    play that didn't happen.
- **`GET /api/sound`** returns `{available, enabled, sounds: [{name, duration_s}], playing,
  last_error}`.
- **`POST /api/sound {enabled}`** sets the switch, persisted with the edge's other settings
  so it survives a restart. Disabling while a clip plays stops that clip, because a mute that
  lets 6.5 s of siren finish is not what the switch promises.
- **Config UI**: a *Sound* section beside the GPIO switches, with:
  - the enable switch;
  - one *Play* button per sound, disabled while sound is off, with the reason shown;
  - the `playing` state and `last_error`.

  *Play* calls the Pi's own `POST /api/sound/play`, so it works with the compute down. A play
  from here writes no `actions` row: the Pi has no store, and the row is the compute's record
  of what it decided.

The edge service runs as `ingvar` under systemd, so that user needs ALSA access (the `audio`
group, which is the Pi OS default). This is verified on the device, not assumed.

### Compute: the alarm step

**Which frames get processed.** A new `Store.open_visit(since_id, now_ms)` returns
`(start_id, latest_id)` for the newest motion cluster after `since_id` that is **not** yet
closed, or `None`. It uses the same `_gap_split` clustering as `closed_visits`, so it is
exactly the cluster that `closed_visits` leaves out, and `latest_id` is the store's newest
frame. `since_id` is the worker's own watermark, which bounds the scan: an unbounded read of
every motion frame once a second on the shared connection is the entries 102–105 starvation
class.

The step keeps a high-water mark (HWM): the highest frame id it has processed. Each pass it
processes the frames above the HWM, oldest first, at most `_DETER_MAX_FRAMES` (30) per pass:

- **With no alarm running**, only the open visit's frames. If the HWM is below the visit's
  `start_id`, it jumps to `start_id − 1`, since the frames between visits are empty scene.
  With no open visit, it processes nothing.
- **While an alarm runs**, every stored frame above the HWM, in a visit or not. In Tuning,
  where non-motion frames are stored, a stranger sitting still keeps being seen.
- The HWM is clamped to `latest_id` and re-seeded on the worker's `reset_watermark` epoch.
  Frame ids can regress after a Clear or a purge (the closed pass clamps for the same reason),
  and an HWM left above every frame would leave the step silently processing nothing.

A pass that leaves frames above the HWM unprocessed has *not caught up*. Processing is
`_detect` then `_identify`: the same calls, with the same idempotency, that the closed pass
makes. The closed pass later skips everything done here, and the detection worker writing the
same frame's verdict is already harmless.

**Each tick, in this order, before any early return:**

1. **Episode bookkeeping** (only while an alarm runs). End the episode, letting the playing
   clip finish, when the first of these applies:
   - `switched_off`: `deterrent_mode` is Off;
   - `uncalibrated`: there is no active model, or its threshold is NULL;
   - `error`: the previous pass raised. An alarm must not keep sounding on a step that can't
     see.
   - `gone`: the previous pass caught up and the newest foreign frame's `recv_ts` is ≥ 3 s
     old. The check is skipped after a pass that did not catch up, or frames that simply haven't
     been identified yet would read as an absent cat.

   If none applies, play the next clip (`alarm2`, then `alarm1`, alternating) once the
   previous one is due to end, timed from the `200` response's `duration_s` plus a small
   margin (~0.3 s). The Pi's other answers:
   - A `409` is "not yet", retried next pass and not counted as a failure. It is routine,
     since `aplay` outlives `duration_s`, and a test play also causes it.
   - A `423` is recorded once as the episode's state ("disabled on the Pi") and keeps being
     checked, so clips resume if sound is re-enabled.
   - Anything else (unreachable, `5xx`) is counted, keeps the last error, and is retried next
     pass.

   Every sound call uses a short timeout (~1 s connect, ~2 s read). `EdgeClient`'s 5 s + 5 s
   defaults would stall the tick, and the Start page's poll behind it.
2. **Process frames** (switch On, calibrated gallery), as above. This ignores `is_busy`.
3. **Start** (no alarm running). Look at the counted frames with `recv_ts` in the last 5 s
   *and* after the previous alarm's end time, so a just-ended alarm's frames never count
   twice. If there are at least 10 and `_aggregate_identity` at the active threshold calls
   them foreign, start an episode: write the `actions` row and play `alarm1`.
4. **Closed visits**, as today (yielding to `is_busy`), but only when no visit is open while
   the switch is On with a calibrated gallery. Re-check `open_visit` between spans.

There is no time cap, by decision: the alarm runs for as long as the foreign cat is there.
The closed-visit pass names the visit as today. Its final name can differ from the verdict
that started the alarm, and the `actions` row keeps what the decision actually saw.

**Latency, measured rather than assumed.** The Pi delivers ~10 fps (entry 275 measured
7.7–9.4 stored, not the configured 15), and only frames with a cat box ≥ 0.3 count. So 10
counted frames is ~1–1.5 s of visible cat if most frames detect it, and the first sound lands
~1.5–2.5 s after the cat comes into view. That rests on two things this dev box cannot measure:
- the per-frame cost of detect + identify on the compute PC (yolo11x at 1280, beside the
  detection worker's own bursts);
- the share of a top-down cat's frames YOLO boxes at ≥ 0.3.

If a pass cannot keep up with ~10 fps, the step never catches up, so the gone check never
runs while motion lasts. Both figures are measured on the compute PC as part of verification.
If the step can't keep up, that is a spec problem to report, not something to tune silently.

**Known limits.**
- In *Collecting* and *Running* only motion frames are stored. A foreign cat that sits
  perfectly still produces no frames and reads as gone after 3 s. MOG2's slow background
  usually keeps a still cat in motion for a while, but not forever. Tuning is immune, because
  a running alarm processes the stored non-motion frames too.
- With no time cap, a box ≥ 0.3 stuck on something static that keeps reading as *unknown*
  sounds until either switch (the admin card's or the Pi's) is turned off. In Collecting or
  Running it also stops when the motion stops. The admin card shows when an alarm is sounding
  and for how long, which is how such a case gets noticed.

### The record

`actions` holds one row per alarm episode or test:
- start and end time, the mode (`on` / `test`), and the clips played and clips failed (a
  `409` is not a failure);
- the verdict (`neighbour` + `cat_id`, `unknown`, or `test`) and its evidence (counted frames
  in the window, the winner's votes, the distance);
- the model version and threshold used;
- the deciding frame, as the (`frame_id`, `recv_ts`) pair;
- `visit_start_ts`, the open visit's first frame time when the alarm started, which is what
  start latency is read against;
- how it ended: `gone`, `switched_off`, `uncalibrated`, `error`, or `interrupted`, plus
  "disabled on the Pi" and the last error, if either applied.

At startup, any row left open by a restart is completed as `interrupted`. Rows survive
`clear()` and eviction. A thumbnail resolves only where both halves of the pair still match a
frame, and otherwise reads gone.

### Admin: the Start page card

A new **Foreign-cat alarm** card, beside *Always on*:

- **Mode**: Off / On, in the stage-picker style, saved via `POST /api/deterrent {mode}`.
- **Readiness**, in words:
  - whether the Pi's sound endpoint answers, whether sound is **enabled on the Pi**, and
    which sounds it has (from `GET /api/sound`, through `GET /api/deterrent`, with the same
    short timeout). The card must never read On over a Pi that will refuse every play;
  - whether a **calibrated** active gallery exists. Without one, nothing can start, and the
    card says so rather than showing *On* over a dead path;
  - whether an alarm is sounding right now, and for how long.
- **Test**: *Play alarm1* / *Play alarm2* (`POST /api/deterrent/test {sound}`). These play
  in either mode, because a test is deliberate. Each still honours the Pi's overlap refusal
  and writes a `test` row, so the log proves the path end to end.
- **Recent**: the newest 20 rows (`GET /api/deterrent/log`). Each row shows start time,
  "started N s after the visit's first frame", duration, clips, verdict (cat name or
  *unknown cat*), evidence, how it ended, and a
  thumbnail of the frame that decided it, opening the full frame. This is where you check
  whether it ever sounded at a resident.

### Docs

ARCHITECTURE has to change to match:
- the Control API section gains the three sound endpoints and the Pi's enable switch;
- the Data model's *Action* entity becomes real;
- "In the current prototype there is no actuation at all" and "actuators fire only in
  running" are amended for this sound path and its switch;
- the actuator mapping's "Speaker: `DETER` → `sound` (rate-limited…)" and *Escalation*'s
  "DETER only when a foreign cat lingers, rate-limited" become "loops while it is present".

CONCEPT has to change too:
- *Status & phasing*: the door is still untouched, and sound is the first, opt-in actuator;
- *Deterrence (sound)* ("especially one that lingers", "not firing endlessly at the same
  stubborn intruder") and the "A stranger won't leave… after it lingers" scenario: the alarm
  loops while the cat is present.

`compute/CLAUDE.md`'s "there is no actuation yet" changes the same way.

### Consequence worth knowing

Including *unknown* ties the alarm to the gallery threshold. Today's active model declines
almost nothing (entry 425), so *unknown* is rare. If the threshold is tightened, more
strangers *and* more residents seen at a bad angle read as unknown, and the alarm follows.
Run 11 named a resident as a foreign cat on 1 of 515 visits. The `actions` log is how to see
whether the live rate is anything like that.

## Alternatives considered

- **Alarm after the visit closes** (hook the existing closed-visit naming). A much smaller
  change, but it plays 5–15 s after the cat has left: a "you were seen" noise, not a
  deterrent. Rejected in favour of deciding during the visit.
- **Start on a vote over the whole in-progress visit.** It matched the feed's span exactly,
  but it re-started the alarm at a resident following a stranger. Rejected for the
  recent-frames window.
- **Yield the alarm step to manual jobs**, and say so on the card. Honest, but the door is
  unguarded for the hours a motion-tuning re-run takes. Rejected.
- **A separate deterrent worker.** Cleaner to read, but more models resident on the GPU and
  duplicate detection.
- **The Pi loops the playlist itself.** Gapless, but it needs a lease so that a crashed
  compute cannot leave it sounding. Clip-by-clip from the compute gets that safety for free.
- **A rate-limited ladder** (gap, escalation, cap per 10 min), and a *Log only* dry-run mode.
  Both were in the first draft and dropped by the user, in favour of looping while the cat is
  present and a plain Off / On.
- **A safety cap** (stop after 2 min, then 10 min quiet) against a stuck detection. Rejected
  by the user: the alarm should run for as long as the cat is there.
- **15 identified frames** before starting. At the Pi's real ~10 fps that lands at ~2–3 s, too
  close to when a cat goes in, so it was lowered to 10.
- **Only in the `running` stage**, as ARCHITECTURE says. Rejected: the switch, off by
  default, is an explicit opt-in.
- **Neighbours only**, leaving *unknown* silent. Safer for residents, but a never-enrolled
  stranger would never be deterred. The user chose to include unknown.

## Implementation strategy

*Not part of the design — a starting point for whoever builds this.*

- **Single agent, Opus 5.5.** The edge player and the compute side are separable in
  principle, but the edge half is small, the HTTP contract between them has to match
  exactly, and both append to `docs/CHANGELOG.md`'s shared numbering. The compute half is
  one tightly coupled thread through `live_identify.py`, `store.py`, `app.py` and the admin
  page, and it is where the `(diverges)` / `(new)` decisions have to be interpreted.

## Amendments

- **2026-11-01.** The start window is floored at the previous alarm's end. Read literally,
  "the last 5 s" still held the departed cat's frames at `gone` and restarted the alarm in
  the same tick (one 6 s visit wrote three rows). The implementer built the floor and a test
  that fails under the literal rule.
- **2026-11-01.** `actions` gains `visit_start_ts`, and *Recent* shows "started N s after
  the visit's first frame". The Outcome's latency check could not be read off the record as
  first specified.
