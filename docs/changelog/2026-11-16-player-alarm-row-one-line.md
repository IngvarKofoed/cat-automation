# Player Alarm row is always one line; the account is behind Read more

The user player's Alarm row is now always present and one line tall: blank with no alarm,
otherwise "state · verdict" ("Sounded · Store Jihn") and a Read more button. The full
account opens in a popup inside the player. So the row no longer changes the panel's height,
and the centred dialog stops moving the stage on every hop. Spec: docs/specs/2026-11-16-player-alarm-row-read-more.md.

## Detail
- Reverses the "absent, not none" rule of `2026-11-02-alarm-mark-on-activity-card` (spec
  user-visit-alarm-mark): the row is BLANK rather than absent, which keeps that rule's reason
  — "none" would be false for an alarm switched Off, and "—" means not measured here. Keeps
  `2026-11-14-alarm-row-first-stats-line`'s placement: the row is still the first line.
- The full text in the popup is today's `alarmText` per episode, word for word (one paragraph
  each, start order), then the row's former `title` explanation. The short verdict says
  "mixed verdicts" when episodes differ by verdict or cat, never a count of episodes.
- The row spans both grid columns (~209 px of status at 390 px, ~179 px at 360 px, measured),
  cut with "…"; Read more never shrinks. It has a fixed `height: 1.5em`, so blank and alarm
  rows are identical. Read more's tap target is widened by an `::after`, not padding.
- Measured at 390×844 (Chromium): before, a no-alarm visit put the stage top at 150.6 px and an
  explained alarm at 124.9 px; after, 137.98 px on no-alarm, explained, mixed and short alarms.
- Every dismissal while the popup is open closes the popup only (Esc, scrim, player backdrop).
  Opening it pauses playback and drops a hop waiting on a page fetch (verified: the page lands,
  only the nav updates). `closePlayer`/`openEvent` force it shut.
- Tab and Shift+Tab are HELD on the popup's Close in the key guard (`preventDefault`), beyond
  the spec's "every other key returns without preventDefault". `inert` covers only the rest of
  the dialog; the page behind the player is neither inert nor trapped, so Tab walked out to
  the feed's links (measured). Space and the arrows still go unprevented.
- Rejected (spec): Read more expanding in place (brings the jump back); reserving 2–3 lines
  (wastes height, still overflows); anchoring the dialog to the top (the footer's visit arrows
  would move by the full difference); native `<dialog>` / Popover API (a second modal mechanism,
  same Esc guard needed).
- Known limits, by spec: the head chip (absent when `subjectChip` is null), a time wrapping
  beside a wide chip, and the footer's messages still change the dialog's height. Against a
  backend with no `detection` field the panel holds the Alarm row alone. The popup's text is
  fixed when it opens.
- Deferred follow-up, found in review: Space on ANY focused player button toggles playback
  and is prevented, so it never presses the button; only Enter does. Pre-existing, but focus
  now returns to Read more after the popup closes, which makes it easy to hit. Fixing it
  changes Space for every player control, so it was left out. Also in docs/CLAUDE_TODO.md.
- Not exercised in a browser: a stats re-render while the popup is open (the Analyse job's
  path), which needs a write to the compute PC; focus then falls back to the dialog by code.
  The "unknown cat" and over-long-name cases were checked on a rewritten feed — the live store
  holds no unknown-verdict alarm. WebKit/iOS was not run.
