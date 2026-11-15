# Motion workbench reskinned on a design-token layer

<!-- entry 43 -->

Reskinned the compute motion-workbench UI (compute/api/web/index.html); presentation only, behavior unchanged.
Full design record in docs/specs/2026-07-12-motion-workbench-ui-reskin.md.
CSS now flows from a two-tier design-token layer, so a theme is a ~20-var swap: dark "review console" is the
default (:root), light lives under [data-theme="light"] — no visible toggle, deliberately not OS-following.
Organizing rule "color = verdict": neutral canvas, saturation only for the 4 verdict colors + one cool accent;
the bucket-picker start/end selection now uses the accent with S/E corner tags (was green/red, which clashed
with caught/missed).
Topbar is now a true full-bleed app bar (a .app-main column + scrollbar-gutter); it previously floated inside
the centered 1100px column. Pills reserved for state chips, nav is an underline, static readouts are quiet
labels, and .warn still renders amber on any element the JS toggles it on.
Layout-jump fixed at its frequent sources: a fixed-height visit stage (collapses when empty), tabular-nums +
min-width on live badges, and a 1px-transparent button border so disabling never resizes the box. Rare
state-driven banners (#error, warn notes) just collapse when hidden — reserving permanent slots for them
only left empty gaps.
Sole JS change: renderTimeline's three rgba() literals → rgb(var(--v-*-rgb)/α) token form; every id and
JS-toggled class preserved. Text on saturated fills uses --color-on-accent (dark on the light accent) so
buttons meet WCAG contrast — white on the accent was ~2.3:1.
