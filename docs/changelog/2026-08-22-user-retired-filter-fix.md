# User dashboard retired-cat filter now works

<!-- entry 190 -->

The user dashboard's retired-cat filter never worked: it tested `c.active !== 0` against
a JSON **boolean**, and `false !== 0` is true. Latent until now only because nothing
could set `active` false. Fixed to a truthiness test.
