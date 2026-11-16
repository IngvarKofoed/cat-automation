# Night lighting priority spec parked behind an uncapped rebuild

A spec that gives double-LED night crops first claim on capped gallery places is drafted
and parked (docs/specs/2026-11-15-night-lighting-priority.md). It acts only under a per-cat
cap, and no build has ever been capped. The step first is an uncapped rebuild with the new
night labels, checked on night visits after it, since the alarm visits were labelled into it.

## Detail
- The 2nd IR LED went in on Sun 8 Nov 2026 at about 21:00. Night brightness was ~31–38 on
  every single-lamp night from 30 Jul, and has been ~60–65 since 9 Nov (mean Rec.601 luma of
  the clipped JPEG, 00:30–03:00). Model v6, built 13 Aug, holds only single-lamp night crops.
- Analysed 2026-11-15: 17 alarms since the 2 s hold went live (10 Nov). Four sounded at a
  resident the visit card names, each verified as one cat in the frames: Saffi 11/11 06:17,
  Sultan 11/11 19:02, Jhinie 14/11 15:09, Sultan 14/11 17:28. The household then labelled all
  17: the other 13 are nine Sultan and four Store Sultan. So 13 of 17 alarms sounded at a resident,
  and 11 of Sultan's 33 labelled visits since 10 Nov set it off, all at dusk or night. 16 of the
  17 had named Store Jihn, which no label since 10 Nov names.
- Causes, beside IR identification:
  - a still cat's near-identical frames vote as many (all four);
  - the 120 s vote floor saw only the tail of a 253 s bridged sit (15:09);
  - the takeover net fired on the same sitting cat (06:17);
  - the frontier gap below (17:28).
  At 06:17 the row reads `presence`, because `actions.trigger` records the vote at that tick,
  not the path that fired. The 2026-11-05 alarm spec's Outcome line "says which path fired it" is
  therefore not true when the net starts an alarm before the hold.
- `2026-11-12-alarm-hold-two-seconds` rejected a 50–60% vote share because it dropped neighbour
  card alarms "whose truth we don't know". The labels show nine of those were Sultan, so that
  reason is weaker. Rule changes are deferred until the rebuild is measured.
- The rebuild (model 7, 15 Nov 12:07) enrolled the 17 alarm visits' own crops, so replaying them
  matches each frame to itself (card distances 0.000–0.001) and proves nothing, on the PC's
  `alarm_replay` too. Measure on visits from 10:00 on 15 Nov, the first ones nobody labelled.
- Rejected in the spec:
  - a hard cut-off that never enrols old-lamp night (a thin night side reads unknown, which alarms);
  - a fixed night share (one more number, and no value fits every cat);
  - a date setting (midnight 9 Nov drops the evening of 8 Nov).
- Deferred follow-up: the alarm frontier still claims a closed visit's unprocessed tail as
  checked, a gap left by `2026-11-12-alarm-frontier-tick-start`. With no open visit,
  `_alarm_process` returns an idle caught-up pass (compute/learning/live_identify.py:599-601),
  and the frontier becomes the tick's start (:557-558). At 14/11 17:28 that turned a 0.8 s
  Store Jihn lead into a met 2 s hold. Also in docs/CLAUDE_TODO.md.
