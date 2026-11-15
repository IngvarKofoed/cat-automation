# Unscoped scorecard cards are deliberately not pinned

<!-- entry 394 -->

UNSCOPED cards are deliberately NOT pinned: absent a range there is nothing to seek,
so pinning buys nothing (live 518M -> 471M steps) and costs a little on a slot column
(263M -> 276M). The unscoped SQL is byte-for-byte what it was, so that path carries
none of this change's risk.
