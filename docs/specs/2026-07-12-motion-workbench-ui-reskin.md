# Motion-detection workbench UI reskin

A presentation-only reskin of `compute/api/web/index.html` (the motion-detection
workbench: Start / Buckets / Motion views). It introduces a two-tier CSS design-token
layer, replaces the 34 ad-hoc hex colors and 24 inline styles with a small semantic
palette and spacing scale, stabilizes the three sources of layout jump, and rationalizes
the topbar and pill usage. The default look is a dark "review console"; a light theme
ships as a one-attribute swap. **No behavior changes** — every id and JS-toggled class is
preserved; the design thesis is *color = verdict*: the canvas stays neutral and saturation
is reserved for the four verdict meanings plus one cool interactive accent.

## Key decisions

- **Two-tier design tokens** (new). Primitive palette vars (`--gray-*`, `--blue-*`, verdict
  hues) resolve into semantic tokens (`--color-bg`, `--color-surface`, `--color-primary`,
  `--color-status-missed`, `--space-*`, `--radius-*`, `--shadow-*`, type scale). Every
  component rule references *semantic* tokens only, so a theme is a ~20-variable remap that
  touches no component CSS. This is the load-bearing move that makes the file re-themeable.
- **Dark default, light alternate** (new). Dark "review console" values live under `:root`;
  light "field lab" values under `:root[data-theme="light"]`. Chosen because the content is
  dark night-camera frames reviewed in long triage sessions — a neutral dark canvas keeps
  frames calm and verdict colors legible.
- **Color = verdict** (new). The canvas (bg, surfaces, borders, text) is grayscale-neutral.
  The *only* saturated colors on the page are the four verdict meanings — missed (red),
  false (amber), caught (green), rep/selection (violet) — plus one cool accent for
  interactive affordance. This single rule is what actually resolves "inconsistent colors."
- **Shape encodes role** (diverges). Today the pill shape is used for nav links, every
  `.badge`, and tabs alike. Reserve the pill for *state chips* only (e.g. `Collecting: on`,
  verdict counts). Nav becomes an underlined/segmented control; static readouts
  (`Scope: …`, `Window: …`, `Params: …`, page labels) become quiet labels, not pills. The
  amber `warn` state is a **semantic modifier, not a shape** — it must keep rendering on
  whichever elements the JS toggles it on, chip or quiet-label alike (see below).
- **Full-bleed app bar** (extends `#nav`). The bar spans the viewport (`100vw` via a
  centered-column bleed) with an inner content column matching the body width, replacing the
  `margin: 0 -20px` inside a `max-width:1100px` body that makes it float on wide screens.
- **Anti-jump rules** (new). Reserved slots for transient banners, a fixed-height media
  stage for the visit frame, `tabular-nums` + `min-width` on live-updating badges, and
  `min-height` on async-populated regions — targeting the three concrete reflow sources
  below.
- **Behavior invariant** (reuses). Every `id` and every JS-toggled class name is preserved.
  The only JS edit is at one site (`renderTimeline`, ~line 2059), swapping its three runtime
  `rgba(...)` color literals for the token form (below); all logic, routing, endpoints, and
  the DOM contract are untouched.

## Goals

- One coherent, small semantic palette — retire the 34 near-duplicate hexes.
- Stable layout: no reflow when banners show/hide, when flipping visits, or when a badge's
  number updates on poll.
- A topbar that reads as intentional: full width, pills only where they mean something.
- Cleanly re-themeable: a theme is a token swap; dark and light both present from day one.
- Zero functional change.

## Non-goals

- No change to routing, endpoints, the DOM/id contract, or any JS logic.
- **No visible theme-toggle control** — that is new function. Default is dark, hardcoded
  under `:root`; light is reachable only via the `data-theme="light"` attribute. The tool
  deliberately does *not* auto-follow the OS via `prefers-color-scheme` — a night-camera
  triage tool flipping to light because the laptop is in light mode is usually wrong, and
  predictable beats clever.
- No redesign of the edge config UI (`edge/server/ui/`, a separate file).
- No dataviz/interaction overhaul of the density timeline or visit inbox beyond folding
  their colors into the shared system.
- No web fonts / CDN assets — the tool runs offline on the LAN, so system + monospace
  stacks only.

## Design

### Token layer

A single `:root` block at the top of the `<style>` defines primitives → semantics. Sketch
(dark default):

```css
:root {
  /* primitives */
  --gray-950:#0f1216; --gray-900:#14181c; --gray-850:#1b2127; --gray-700:#2b333c;
  --gray-500:#8b98a6; --gray-300:#c7d0d9; --gray-100:#e6edf3;
  --accent:#4ab3ff;
  --v-missed:#e5484d; --v-false:#f5a524; --v-caught:#3fb950; --v-rep:#a371f7;
  /* RGB channels for alpha-blended fills (timeline cells) */
  --v-missed-rgb:229 72 77; --v-false-rgb:245 165 36; --v-neutral-rgb:139 152 166;
  /* semantics */
  --color-bg:var(--gray-900); --color-surface:var(--gray-850);
  --color-surface-2:var(--gray-950); --color-border:var(--gray-700);
  --color-text:var(--gray-100); --color-text-muted:var(--gray-500);
  --color-primary:var(--accent);
  --color-status-missed:var(--v-missed); --color-status-false:var(--v-false);
  --color-status-caught:var(--v-caught); --color-status-rep:var(--v-rep);
  /* scales */
  --space-1:4px; --space-2:8px; --space-3:12px; --space-4:16px; --space-5:20px;
  --radius-sm:4px; --radius-md:8px; --radius-chip:999px;
  --shadow-1:0 1px 3px rgba(0,0,0,.35);
  --font-ui:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  --font-data:ui-monospace,SFMono-Regular,Menlo,monospace;
}
:root[data-theme="light"] {
  --color-bg:#f2efe9; --color-surface:#ffffff; --color-surface-2:#faf8f3;
  --color-border:#e2ddd3; --color-text:#1e2227; --color-text-muted:#6b7280;
  --color-primary:#1f6feb;
  --v-missed:#d1242f; --v-false:#bf8700; --v-caught:#1a7f37; --v-rep:#8250df;
  /* the alpha-blend channels must also be redefined, else the timeline fills keep the
     dark triples while the solid borders use the light hexes */
  --v-missed-rgb:209 36 47; --v-false-rgb:191 135 0; --v-neutral-rgb:107 114 128;
}
```

Verdict values are tuned *per theme* for adequate contrast on that canvas (dark values sit
brighter/less saturated than today's white-bg `#c62828`/`#f9a825`/`#2e7d32`/`#6a1b9a`).

### Typography

Two roles, both offline: `--font-ui` (system sans) for prose, labels, buttons; `--font-data`
(monospace) for ids, params, timestamps, counts, and all numerals — telemetry feel plus
clean tabular alignment. Live/number badges also carry `font-variant-numeric: tabular-nums`.

### Topbar and pills

- **App-shell restructure for the full-bleed bar.** Rather than a fragile
  `calc(50% - 50vw)` bleed (which resolves against the padded body box, so it under-reaches
  by the 20px body padding, *and* counts the scrollbar in `vw`, overflowing on the target
  Windows PC's space-taking scrollbar), move the layout constraint off `<body>`: `body` drops
  its `max-width`/horizontal padding, an inner `.app-main` wrapper (holding `#error` + the
  three `.view` sections) carries the `max-width:1100px` + horizontal padding, and `#nav`
  becomes a `width:100%` bar with an inner `.bar-inner` re-inset to that same column. Add
  `scrollbar-gutter: stable` on the scroll root. Wrapping the existing content in `.app-main`
  is nesting-agnostic — the router selects `.view`/`#view-*` by id/class, so it's a
  presentation-only structural edit.
- `#nav`: nav links lose their `border-radius:16px` pill; the active route is an
  underline/segment, not a filled pill.
- `.badge`: keep the pill radius **only** for genuine state chips (`statCount/Size/Motion/Span`,
  `collectorBadge`, verdict counts); give them one tonal surface (`--color-surface-2` +
  border) rather than each a different background.
- Static readouts currently rendered as `.badge` (`motionScopeBadge`, `tuningScopeBadge`,
  `tuningParamSource`, window/page labels) get a quieter label treatment (muted text, subtle
  divider) — same elements/ids, restyled class.
- **Preserve `.warn` on every element the JS toggles it on**, whether chip or quiet label:
  `statSize` (near cap), `collectorBadge` (collector off), `motionScopeBadge`/`tuningScopeBadge`
  (a scope filter is active), `tuningParamSource` (params are stale fallback, not from the
  edge). The new quiet-label class therefore carries a `.warn` variant too; `warn` maps to the
  amber warn token regardless of base class. Likewise the page-label class keeps the
  `min-width` + centering that `.pager .badge` gives `viewerPageLabel` today.

### Selection affordance (bucket picker)

The start/end frame picker currently borrows verdict colors — `.viewer-tile.pick-start`
green `#2e7d32`, `.viewer-tile.pick-end` red `#c62828`, `.in-range` blue `#e3f2fd`
(`index.html:230-240`) — which collides with green=caught / red=missed. Selection is an
affordance, not a verdict, so it uses the **cool accent** for both endpoints, told apart
non-chromatically: a small `S`/`E` corner tag plus position, not hue. `.in-range` becomes a
faint accent wash. This keeps the page's saturated colors to exactly {four verdicts + one
accent}. Class names (`pick-start`, `pick-end`, `in-range`) are unchanged — only their
styling.

### Anti-jump (the three concrete reflow sources)

1. **Show/hide banners** (`.hidden` = `display:none !important`, 31 toggles). Group the
   transient banners/notes into reserved slots so appearance doesn't reflow siblings. This
   must include `#error` (toggled `display:none`↔`.show`) — it sits directly below `#nav`
   above every view, so it is the single largest reflow, pushing the whole page down when a
   request fails. Also `#motionOnlyBanner`, `#enqueueWarn`, `#viewerReadonlyNote`,
   `#inboxMissesNote`, and the tuning notes. Truly conditional *panels* (`#resumePanel`,
   `#tuningCopyRow`) may still add/remove flow — they're deliberate, not churn.
2. **Visit frame resize** (`#inboxRep img`, `max-height:420px`, otherwise free). Wrap in a
   fixed-height review stage (e.g. `height` fixed, image `object-fit:contain` centered) so
   flipping visits never reflows the meta + filmstrip below.
3. **Badge width churn** (`Frames: —` → `Frames: 864,120`). `tabular-nums` + `min-width` on
   the stat/scope/progress badges so the row width holds steady across polls.

Also add `min-height` to async-populated regions (`#viewerGrid`, `#timelineStrip`,
`#inboxFilmstrip`) so the transient `innerHTML=''` → repopulate cycle doesn't collapse then
expand.

### The one JS touch

`renderTimeline` (defined at line 2034) builds three color literals — `rgba(198,40,40,α)`
(missed, 2051), `rgba(249,168,37,α)` (false, 2053), `rgba(120,144,156,α)` (neutral, 2055) —
and assigns the chosen one at the sole `.style.` write in the file, `cell.style.background`
(2059). Replace the three literals with the token channel form,
`rgb(var(--v-missed-rgb) / <alpha>)` (and `--v-false-rgb`, `--v-neutral-rgb`), so the density
strip's alpha-blended fills are theme-controlled too. Logic (which bin gets which color, the
alpha math) is unchanged.

### Class-name contract (must be preserved)

The reskin restyles but never renames these JS-touched hooks: `hidden`, `active`, `show`
(on `#error`), `warn`, `motion` (tile/film), `delta-good|bad|neutral`,
`state-done|failed|canceled`, `film-context|motion|miss|rep`, `pick-start|pick-end`,
`in-range`, `has-motion-only`. Inline `style=` attributes are lifted into utility/structural
classes driven by the spacing scale (e.g. the 8× `margin-left:auto`, one-off margins, and the
three inline `background:#…` legend swatches — missed, false, and the neutral "Frame density"
swatch → token-driven classes, the neutral one from `--v-neutral-rgb`).

### Quality floor

Visible keyboard focus ring (`--color-primary` outline) on all interactive elements;
verdict + text colors meet WCAG AA on their theme's surfaces; `prefers-reduced-motion`
respected for the one `button` transition.

## Alternatives considered

- **Terminal-green-on-black HUD.** Rejected: it's a templated AI-default look, and green is
  already spoken for as the "caught" verdict — a green canvas would collapse the color=verdict
  signal.
- **Light/white default.** Offered and not chosen; ships as the `data-theme="light"` alternate
  so the decision is reversible by attribute, not by a rewrite.
- **Single-tier tokens (semantic only, no primitives).** Simpler, but a palette-wide shift
  (e.g. warming every gray) would then mean editing many semantic vars; the primitive tier
  keeps the raw ramp swappable in one place.
