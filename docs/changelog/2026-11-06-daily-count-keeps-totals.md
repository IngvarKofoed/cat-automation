# 90-day count also keeps per-day identity totals

<!-- entry 572 -->

The 90-day count now also keeps, per day, `totals {resident, neighbour, unknown_cat}` (the
identity half of `_classify_stats_event`) and the day's event count, so one walk and one
10-minute cache serve every cat AND every line. Cat visits is summed CLIENT-side, never sent:
the card's `cat_visits` also counts unidentified visits, which need the subject (~2x the walk),
and a field of that name meaning something else would break entry 348's rule.
