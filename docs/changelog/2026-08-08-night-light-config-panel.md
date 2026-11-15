# Config UI gains a night-light panel

<!-- entry 119 -->

Config UI gained the Night light panel promised by entry 118: enable checkbox, channel dropdown
(sourced from `/api/gpio`'s output names), on/off minute offsets, latitude/longitude, and a Save
button that round-trips through `GET`/`POST /api/night-light`. Status line (on/off, next change,
sunrise/sunset) renders server UTC instants in the browser's local time. Disables + notes when
`available:false` (no GPIO backend), mirroring the existing GPIO-switches note.
