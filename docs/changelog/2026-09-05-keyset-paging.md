# Paging is keyset, not offset

<!-- entry 260 -->

Paging is KEYSET, not offset: a page asks for `until_id` = (oldest loaded start_id − 1),
so page N costs what page 1 costs (measured equal at 1.5M frames) and pages can neither
overlap nor skip a visit. `truncated` is the "another page exists" test. The user feed
auto-loads on scroll, admin has a "Show older" button; both report the loaded count and
an end-stop, because a list that simply stops looks like one showing everything.
