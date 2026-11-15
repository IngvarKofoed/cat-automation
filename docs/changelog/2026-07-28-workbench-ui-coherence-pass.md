# Buttons and lists share one geometry and hierarchy

<!-- entry 65 -->

Motion-workbench UI coherence pass (compute/api/web, presentation only).
Buttons now share one geometry with a calm hierarchy: neutral surface default, saturated accent
fill reserved for a single `.btn-primary` per group; the green `.btn-preset` is retired and danger
is a red *tint* — so no button competes with the red "missed" verdict (upholds #43's "color = verdict").
Badges are squared, not pills; checkboxes and radios are custom-styled (native controls never took the
dark theme). Job/queue lists are now real record rows — uppercase headings, mono right-aligned meta, a
live green-dot status strip, dot-led terminal-state log; Annotate's Queue/Labelled toggle is a segmented control.
