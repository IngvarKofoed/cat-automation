# Rail marks labelled visits with a muted check

<!-- entry 511 -->

The rail marks a labelled visit with a muted ✓ beside the ⚑, drawn at build time so it
shows for earlier sessions' labels too. Both marks are `aria-hidden`, so the sentence
goes in `visitAriaExtras`, shared with the dialog's own label (entry 255).
`renderActivityRowFlag` became `renderActivityRowMarks` and owns both: a confirm can
add the ✓ and clear an overlapping ⚑, which is one repaint of one row.
