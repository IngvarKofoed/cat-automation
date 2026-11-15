# Right rail held by grow on the adjacent status note

<!-- entry 214 -->

That right rail is held by `grow` on the ADJACENT STATUS NOTE, never a bare spacer: flex
line-breaking uses each item's BASE size, so a full-width note bumps the button onto a second
line where it left-aligns, while a `grow` note has base size 0 and shrinks instead.
Consequence for future edits: such a note's tone class must be toggled via classList —
`className = 'note ok'` drops the `grow` and moves the button (hit and fixed in addCat).
Location instead uses margin-left:auto, so its bottom-aligned note+button group (entry 134)
stays right-aligned on the narrow widths where it wraps to its own line.
