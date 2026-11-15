# hidden now wins over display classes across admin

<!-- entry 204 -->

`[hidden] { display: none !important }` now backs the whole admin-next shell. The UA's
`[hidden]` rule is plain `display:none` at losing specificity, so any class setting display
— `.row{display:flex}` — silently defeated `el.hidden = true`; the Jobs card's Clear-queue
row stayed visible with an empty queue. Every `hidden` toggle in the file means "gone".
