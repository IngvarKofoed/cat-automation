# Skip checkbox disables itself and explains when labelled key absent

<!-- entry 512 -->

The checkbox DISABLES itself, dims, and says why VISIBLY (the footer's `.msg`, not a
`title` — a phone has no hover) when the payload carried no `labelled` key. Detection is
key PRESENCE, latched per page-1 load: `labelled: false` on every event is the ordinary
pre-labelling state, so a falsy test would banner a healthy system as an older build —
and the confirm path's own mirror would otherwise CREATE the key and re-enable a skip
that could only see this session's writes.
