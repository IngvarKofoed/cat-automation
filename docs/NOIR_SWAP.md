# NoIR camera swap — do these before re-tuning the motion gate

**Status:** open. Written 2026-07-27, swap expected ~28–29 July.
**Why this exists:** the NoIR module resets the motion-gate tuning baseline, and two of
the four items below have to be done *before* tuning or the tuning is wasted work.

Background on the camera choice itself is in `docs/CONCEPT.md` (day/night regimes) and
`docs/ARCHITECTURE.md` (*Camera source → Day/night visual regime*).

---

## 1. Lock AWB — and load the NoIR tuning file — BEFORE tuning

**Do this first.** Everything else is calibrated against whatever this produces.

Auto White Balance re-estimates the scene illuminant *every frame* and applies per-channel
R/B gains. That is a continuously moving transform sitting between the sensor and every
parameter you are about to tune.

**It moves the motion gate, not just colour.** MOG2 runs on grayscale, and grey is a
weighted channel sum (`0.299R + 0.587G + 0.114B`), so an AWB drift changes the grey value
of a *completely static* scene. MOG2 then sees a global change on a frame where nothing
moved: slow drift gets absorbed by the background model (it churns), a fast correction
throws a whole-ROI blob (which `max_area_fraction` exists to reject). Either way
`var_threshold` and `min_area` end up calibrated against a sliding baseline.

**NoIR is the worst case for this.** With no IR-cut filter, daylight carries a large NIR
component the sensor renders as a red cast — and how much varies with sun angle, cloud and
time of day, so AWB fights it differently every hour. At night the scene is essentially
monochromatic NIR with no real colour information to estimate from, and the moment the
night-light scheduler switches the IR lamp on, AWB gets a total illuminant change to hunt
through. A module *with* an IR-cut filter is far more stable here; NoIR is precisely where
the loop wants opening.

It also protects the identification cue — a drifting pink cast smears the day colour that
separates the ginger from the grey cat.

### What to build

Currently **AWB is never touched**: `edge/capture/picamera_source.py` sets only
`AfMode`/`LensPosition`. No `AwbEnable`, no `ColourGains`, no tuning file.

Locking is two controls:

```python
cam.set_controls({"AwbEnable": False, "ColourGains": (r_gain, b_gain)})
```

Get the numbers by letting AWB settle on a representative scene and reading them back from
`cam.capture_metadata()["ColourGains"]`, then persist them.

**This repo already solved the identical problem once — copy the `focus` design (changelog
14):** `null` = continuous auto, a value = locked there, applied after `cam.start()`,
capability-gated, best-effort (never gate capture), plus `POST /api/focus/autofocus` which
runs one auto cycle and stores the result *as* the lock. AWB wants exactly that shape: an
`awb_gains` setting (`null` = auto, `[r, b]` = locked) and an "AWB once" endpoint. Same
rationale too — a fixed door scene is better served by a stable transform than a hunting one.

Separate but complementary: `Picamera2(self._index)` loads the default tuning. A NoIR sensor
wants `tuning=Picamera2.load_tuning_file("imx708_noir.json")`, which carries colour matrices
and AWB priors calibrated for a filterless sensor.

---

## 2. There is only ONE MOG2 param set — no day/night switching

`edge/config/settings.py` holds a single flat set of six values (`var_threshold`,
`learning_rate`, `min_area`, `max_area_fraction`, `persistence`, `motion_downscale`).

The **scorecard** splits day/night (changelog 123, and the compute-side location is already
set), so you will be able to *see* the two regimes diverge — but you cannot currently give
them different parameters.

Know this before spending a day tuning and discovering you need a compromise. If the split
shows the two regimes genuinely want different values, that is a real feature to add
(per-regime params on the edge, selected by the same sun-times logic the night-light
scheduler already uses), not something to work around by picking a mediocre middle.

---

## 3. Expect night to come out CLEANER than day

Counter-intuitive, but physical: the IR LEDs sit right next to the lens, so shadows fall
largely *behind* the subject and are hidden from view.

If shadow-classification is what is currently eating the blobs (`detectShadows=True` marks
shadows grey 127 and the `threshold(mask, 254, …)` in `shared/motion.py` discards them —
see changelog 172), that is a **daylight** problem: side-lit sun casting a visible shadow.

So do not assume night is the hard regime and tune for it defensively. Score them
separately and let the numbers say. Expect day and night to want genuinely different
`var_threshold` / `min_area` — which loops straight back into item 2.

---

## 4. The `motion_downscale` trade flips at night

`motion_downscale` is not simply "higher = better, costs CPU". It is a signal-to-noise knob
with two mechanisms:

- `cv2.resize(..., INTER_AREA)` **averages** pixels — a low-pass filter that kills sensor
  noise before MOG2 ever sees it. Raise it and MOG2 gets grainier input.
- The `MORPH_OPEN` kernel is a fixed 3×3 and therefore **scale-relative**: at 320 it deletes
  speckle that is large relative to the frame; at 640 the same kernel removes proportionally
  *less*. You preserve small cat blobs — and equally preserve small noise blobs.

IR plus high sensor gain is grainy, so the averaging earns its keep more in the dark.
**Raising it may help day blobs and hurt night false-triggers at the same time** — another
reason the two regimes may not share one value.

Also note `area` is a **fraction of the ROI**, not a pixel count, so raising downscale does
*not* mechanically raise the number in the scorecard. It only helps when the mask is
*fragmented* (`MORPH_OPEN` removes pieces thinner than 3px; at a coarser ROI more of the
cat's fragments fall under that floor). On a solid blob, opening is nearly shape-preserving.

---

## Operational note: do not A/B across the swap

Stored oracle verdicts, the missed-visit records, and any candidate params from before the
swap come from a **different sensor**. Scope tuning to post-swap days (the motion-tuning
calendar day-picker makes this easy) rather than comparing across the boundary.

---

## Context worth having on hand

The last pre-swap measurement, and the question it left open (full detail in the
`mog2-blobs-far-smaller-than-assumed` memory):

- MOG2's largest blob **maxed at 0.8% of the ROI and typically sat at 0.4%**, against a
  candidate `min_area` of 0.0075 (0.75%) — the gate's bar was ~2× the typical frame, and
  `persistence=2` then killed the rare frame that cleared it (the streak resets on any
  sub-bar frame).
- This is the **opposite** of what the code assumes: the `above_max` hint calls a cat
  filling the ROI "the common top-down case". Real data never got near it.
- **Unresolved:** is 0.4% a *solid but genuinely small* blob (→ `min_area` is just mis-set)
  or a *big but shredded* mask (→ true silhouette 3–5%, only fragments survive `MORPH_OPEN`
  because shadow-classified body pixels are discarded)? The two call for opposite fixes.
  Settle it by comparing MOG2's `area` against YOLO's box area on the same missed frames.

Two diagnostics discussed but **not built**, either of which would help here:

- Attach YOLO's box area to the missed-visit records, so "MOG2 saw 0.4%, YOLO's box was 6%"
  is visible side by side instead of inferred.
- Expose `detect_shadows` as a `MotionParams` knob — it is hardcoded `True` in
  `shared/motion.py`, so the shadow hypothesis cannot currently be A/B'd. This is a
  `shared/` contract change, so both tiers in one go.

Also unhinted today: `learning_rate` and `motion_downscale` have **no** miss-reason `fix`
in the missed-visit attribution (`_MISS_FIX`, `compute/collection/store.py`), even though
both cause the misses being seen. The two causes are separable from data already collected —
a cat absorbed into the background shows area *decaying across the visit*, while a
shadow-eaten or over-eroded one is small from the first frame.
