# Cat exclusion is part of the artifact identity and dedup key

<!-- entry 333 -->

The exclusion is part of the artifact's identity — dedup key (ids SORTED, so tick order
is not identity), a `-ex<n>` dir-slug fragment after `-max<cap>`, and the ids on the
version's / run's `metrics`. Without it a gallery built without Store Kali is
indistinguishable from one with it, and a second Build with a different selection is
silently dropped. Both lists NAME the cats (a count cannot be compared between builds).
