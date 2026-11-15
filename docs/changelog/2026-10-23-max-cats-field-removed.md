# max_cats_in_frame and its detail read are gone

<!-- entry 501 -->

`max_cats_in_frame` and its span-scoped `analysis.detail` read are gone with it, so
the probe is one read lighter than it was two commits ago. Nothing else consumed
either — the field existed for one afternoon.
