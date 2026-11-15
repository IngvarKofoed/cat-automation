# visit_start_ts and trigger column name left as is

<!-- entry 593 -->

Two review findings left UNFIXED by decision. `visit_start_ts` stays the FEED visit's start,
which is what the admin log labels it as; a presence spanning several feed visits therefore
reads a short latency. And the column stays named `trigger`, as the spec names it: SQLite
takes the keyword as an identifier, and Postgres does not reserve it.
