# POST crop-geometry echoes the validated value, not a re-read

<!-- entry 475 -->

`POST /api/crop-geometry` ECHOES the value it validated rather than re-reading the
store for the margin, matching `/api/lighting` and `/api/location`. `set_setting` and
`get_setting` take the store lock separately and the route runs in Starlette's
threadpool, so a second client's write landing between them returned this request's
geometry paired with that one's margin.
