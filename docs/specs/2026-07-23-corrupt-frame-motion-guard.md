# Corrupt-frame motion guard

The Pi's CSI camera (IMX708 / Module 3) intermittently emits corrupt frames that
falsely trip the MOG2 motion gate: **thin coloured horizontal lines** (1–few
rows, full- or partial-width, any hue incl. dark navy, solid or dashed) and a
**full-frame magenta cast** (whole image magenta, scene still visible, green
channel collapsed to ~0). Both are genuine pixel changes vs. the learned
background, so MOG2 reads them as motion. This spec adds a cheap **corrupt-frame
guard** at the top of `shared/motion.py::MotionGate.process()`, before
`mog2.apply()`: a per-frame chroma test that, on detection, skips the frame
entirely — no background update, no debounce advance, motion reported False — and
surfaces a `corrupt` flag the edge logs to journald. Placing it in the shared
core means the edge live gate and the compute offline re-run (`MogAnalyzer`) get
the identical behaviour by construction.

## Key decisions

- **Guard lives inside `MotionGate.process()`, pre-`mog2.apply()`** (extends).
  One chokepoint both tiers already call, so the edge grabber *and* the offline
  `MogAnalyzer.analyze()`/`_warm_start()` skip corrupt frames identically — no
  second detector to drift (upholds the "one shared motion core" rule, changelog
  22). Warm-start gets it free: a corrupt frame no longer primes the background.
- **Detection signal is absolute BGR chroma, not HSV saturation** (new).
  `chroma = max(B,G,R) − min(B,G,R)` per pixel. Validated on real samples: HSV-S
  inflates on dark pixels (the neutral tile scene reads as high-S — a trap),
  while chroma stays ~0 for dark greys *and* for the bright neutral door frame,
  spiking only on genuinely coloured pixels.
- **One per-row mean-chroma profile drives both checks** (new). A **thin band**
  spiking above a *local median baseline* = line corruption (the wide baseline
  absorbs the bright sky diagonal and the bottom door frame, and rises inside a
  large real coloured object like a ginger cat, so neither false-positives). A
  raised **global** baseline + a collapsed colour channel = cast corruption.
- **Skip semantics: transparent to the gate's state** (extends). A corrupt frame
  returns before `_ensure_mog2`/`apply` and before touching `_streak`: the
  background model is not updated (no poisoning at `learning_rate`) and the
  debounce streak is neither advanced nor reset (a single glitch frame mid-
  crossing doesn't cost a real crossing its accumulated streak). The corrupt
  frame itself reports `motion=False` — we never assert motion on garbage.
- **Fixed module-level constants, not `MotionParams`** (diverges). Thresholds are
  hardware-glitch constants, not scene tuning, so they live as `_CORRUPT_*`
  constants in `shared/motion.py` and stay **out** of the persisted `MotionParams`
  NamedTuple — the edge `settings.json` wire contract and the tuning UI are
  untouched. (`MotionParams` is threaded through config, the tuning panel, and
  scorecards; widening it would ripple across both tiers for no tuning benefit.)
- **`process()` returns a `MotionResult` NamedTuple, not a bare tuple** (breaking,
  in-process only). Adds a `corrupt` field alongside `motion`/`bbox`/`area`. Both
  call sites (`edge/server/grabber.py`, `compute/analysis/mog2.py`) and
  `shared/tests/test_motion.py` update to attribute access in the same change (per
  `shared/`'s "update both sides together" rule). Chosen as a named, extensible
  result *because* the user's near-term goal is richer per-frame classification
  (see Goals) — a future field costs one line, not another contract change.
- **Suppression is observable via journald, wire untouched** (extends). When the
  edge grabber sees `corrupt`, it emits a throttled log line (reusing the
  `_FAILURE_LOG_INTERVAL_S` throttle pattern from changelog 95) — no new
  `/status` field or stream header. This is the "Option B" call: it keeps a
  breadcrumb that survives a future no-motion-frame drop, so a degrading cable
  can't become silently invisible.
- **Frame delivery unchanged** (reuses). `/stream` and `/frame` keep serving every
  frame (the continuous-delivery contract); only the motion *decision* is
  suppressed. Dropping corrupt frames from the stream/store is a separate future
  feature.
- **Ships on by default with conservative constants** (new). The guard is active
  from the start, but thresholds are biased so a real cat is never suppressed
  (fail-safe for residents, per CONCEPT) — a faint line slipping through until the
  constants are tightened on real frames is the accepted trade. No env flag: an
  off-by-default guard that no one remembers to enable protects nothing.

## Goals

- Stop both observed corruption types from triggering false MOG2 motion, at a
  single point shared by the live edge gate and the offline tuning re-run.
- Never poison the learned background with a corrupt frame.
- Keep the fix from ever suppressing a real cat — bias every threshold to
  fail-safe (miss faint corruption before risking a resident), per CONCEPT's
  resident-first principle.
- Make suppression observable (journald), laying groundwork for a future where
  the compute opts out of non-interesting frames to save LAN/GPU — corrupt frames
  become a first-class, countable category (a hardware-health signal), not
  silently folded into "no motion."

## Non-goals

- The future compute-side opt-out / motion-gated delivery itself, and any wire
  change (`/status` corrupt count, `X-Corrupt` header) it will need. This spec
  only establishes the internal signal.
- Dropping corrupt frames from `/stream`, `/frame`, or the compute frame store.
- Detecting corruption on mono/IR (single-channel) sources — chroma needs 3
  channels; such frames bypass the guard (the Module 3 delivers BGR, so it's
  covered). Brightness-only line glitches on a future mono camera are out of scope.
- Fixing the hardware. The full-width colour tearing points at a marginal/long
  CSI ribbon cable or EMI; reseating/shortening it is the real cure. The guard is
  the software mitigation and is worth having regardless.

## Design

### Detection (`shared/motion.py`)

A new helper, called at the very top of `process()` on the untransformed-but-
already-rotate+cropped `roi_bgr` (full row resolution — *before* the existing
downscale, which would blur thin lines):

1. If `roi_bgr` is not 3-channel → not corrupt (mono bypass), fall through to MOG2.
2. `chroma = max − min` across BGR. `row_chroma = chroma.mean(axis=1)` (one value
   per row).
3. **Cast check.** `median(row_chroma) ≥ _CORRUPT_CAST_CHROMA` **and** a collapsed
   channel: `min(mB,mG,mR) ≤ _CORRUPT_CHANNEL_RATIO · max(mB,mG,mR)`. Both required
   — a strong global colour *and* a near-dead channel — so a merely vivid (but
   healthy) scene isn't suppressed. The magenta sample: median chroma 136, green
   1.8 vs 155 → both trivially true.
4. **Line check.** Local baseline `base[r] = median(row_chroma[r−W : r+W])`
   (`W = _CORRUPT_BASELINE_HALFWIN`); a row is flagged when
   `row_chroma[r] − base[r] ≥ _CORRUPT_LINE_EXCESS`. Group contiguous flagged rows
   into bands; corruption = any band **≤ `_CORRUPT_LINE_MAX_ROWS` tall** (thin). A
   tall flagged band means the local baseline failed to absorb a large coloured
   region → treat as real, not corruption.
5. Either check true → corrupt.

The check runs on the **full ROI** with no resize — `chroma` and `row_chroma` are
plain numpy reductions, and at 5 fps over the clipped door ROI the cost is
expected to be a few ms; if a future large/high-res ROI makes it bite, a
width-reduction (all rows kept) is the obvious lever, but it's not worth the code
until measured to matter.

Constants (illustrative, **calibrated on downscaled chat previews** — the worst
case for line dilution; finalised on real frames, see Open questions). Chosen
conservative (see the rollout decision): bias to never suppress a real cat, so a
faint line may slip until the thresholds are tightened on real data.

```
_CORRUPT_CAST_CHROMA     = 60     # global median row-chroma (scene ~17–19)
_CORRUPT_CHANNEL_RATIO   = 0.30   # min-channel ≤ 0.30 · max-channel => collapsed
_CORRUPT_LINE_EXCESS     = 22     # per-row chroma over local baseline
_CORRUPT_BASELINE_HALFWIN= 15     # rows each side for the baseline median
_CORRUPT_LINE_MAX_ROWS   = 20     # a thin band; taller => real object
```

### Return shape

```python
class MotionResult(NamedTuple):
    motion: bool
    bbox: "tuple | None"
    area: float
    corrupt: bool
```

`process()` returns `MotionResult(False, None, 0.0, True)` on a corrupt frame
(area 0.0 — no blob was measured), else `MotionResult(motion, bbox, area, False)`.

### Callers

- **`edge/server/grabber.py`** — `_compute_motion` returns the `MotionResult`;
  `_grab_once_internal` reads `.motion/.bbox/.area` into the slot as today
  (`corrupt` is *not* stored in the slot — no wire change). When `.corrupt`, emit a
  throttled `_log.warning("motion gate suppressed corrupt frame (%d consecutive)")`
  using the existing failure-throttle fields.
- **`compute/analysis/mog2.py`** — `analyze()` reads the `MotionResult`; verdict
  stays `.motion`, score `.area`. `_warm_start` still discards the result. `corrupt`
  is echoed into the `detail` blob (additive JSON field) so an offline sweep can
  count/inspect corruption without re-running detection.

### Consequence: offline fidelity divergence

`MogAnalyzer` re-running *baseline* params over frames captured **before** this
guard shipped will now skip corrupt frames the old live edge may have stored with
`frames.motion=1`, so the fidelity check (changelog 24) will show disagreements on
exactly those frames. This is the intended improvement, not a regression — noted
in the changelog so a future reader doesn't chase it as a bug.

## Open questions

- **Q: Final constant values?** **Default:** calibrate on the compute PC via a
  `MogAnalyzer` re-run over a stored bucket containing both corrupt frames and cat
  visits; confirm every cat visit stays caught and corrupt frames flip to
  no-motion before trusting the guard in production.

## Alternatives considered

- **HSV saturation as the signal.** Rejected — dark pixels inflate S, so the
  neutral scene reads high-saturation; chroma separates cleanly (measured).
- **Post-filter (let MOG2 run, then veto the decision).** Rejected — a corrupt
  frame fed to `mog2.apply()` still poisons the background at `learning_rate`.
  Pre-filter skips it entirely.
- **A separate `shared` detector the callers gate on, leaving `process()`
  unchanged.** Cleaner separation and free state-freeze, but two call sites must
  each remember to check (drift risk), and warm-start would need its own guard. The
  in-gate chokepoint is safer.
- **Silent suppression (unchanged 3-tuple, Option A).** Rejected in favour of the
  observable `corrupt` flag: in a future that drops non-motion frames, silent
  corruption leaves no trace at all, hiding a degrading camera.
