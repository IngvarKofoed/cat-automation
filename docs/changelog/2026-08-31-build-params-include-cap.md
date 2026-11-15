# Gallery build dedup key includes the cap

<!-- entry 234 -->

Gallery-build's job `params` is now the `(qualities, max_per_cat)` PAIR, so the cap is in
the dedup key: changing only the cap and pressing Build again is genuinely different
work, and with the cap outside the key the double-click guard silently dropped it.
The cap also lands in the artifact dir name and on the version row's metrics — with
`n_vectors` alone, a gallery capped at 40 is indistinguishable from one whose cats only
ever had 40 crops.
