# Weakest-cat tile and chart share one tie-break

<!-- entry 377 -->

Review repairs. The TL;DR's "weakest cat" tile and the chart beneath it picked the
weakest with DIFFERENT tie-breaks — a plain `min` (first row, i.e. lowest cat_id) vs
the chart's (recall, -scored) — so on a tie one block named two different cats
weakest. Recall is a ratio of small integers, so ties are routine: 1/2, 2/4 and 3/6
all land on 0.5. One shared `_WEAKEST_KEY` now orders both.
