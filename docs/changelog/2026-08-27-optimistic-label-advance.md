# Labelling advances immediately and POST settles behind

<!-- entry 216 -->

Annotation labelling no longer waits on its own write: a keypress advances to the next visit
at once and the POST settles behind it. A label crops one JPEG per visit frame — tens of
frames, seconds under store-lock contention — and awaiting that read as a dropped key, so
it got pressed twice. Writes stay STRICTLY SERIAL on one chain: a fast labeller must not
pile parallel crop-writing threads onto the shared lock, and undo must follow its label.
