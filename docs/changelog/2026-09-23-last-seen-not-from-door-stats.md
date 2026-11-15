# last_seen stays on cats_overview, not door_stats

<!-- entry 348 -->

`last_seen` deliberately does NOT come from `door_stats`. The page already fetches
`/api/cats/overview` for avatars, and that field spans the whole retained feed while
anything computed here would span 24 h — two fields of one name meaning different
things on one page.
