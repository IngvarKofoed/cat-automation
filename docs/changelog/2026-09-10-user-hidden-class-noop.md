# User dashboard has no bare .hidden rule

<!-- entry 287 -->

The user dashboard has NO bare `.hidden` rule — only per-element qualified ones
(`.note.hidden`, `.modal.hidden`, …) — so `classList.toggle('hidden')` on an element
without its own rule is a silent NO-OP. The analyse button therefore showed on every
visit, and clicking it on an already-analysed one enqueued a pointless GPU job.
Verify visibility by COMPUTED STYLE, never by `classList.contains` — the class was
present the whole time, which is what made the browser check read as passing.
