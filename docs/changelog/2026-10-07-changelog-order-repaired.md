# Changelog ordering repaired to strictly monotonic

<!-- entry 421 -->

Changelog ordering repaired: entries 409-411 had been inserted ABOVE 407/408, stranding
two entries after 417 so the file no longer ended at its newest. Now strictly monotonic
222→421. The convention exists because this file is read top-down at session start.
