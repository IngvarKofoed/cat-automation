# Retire now removes cats from pickers and builds

<!-- entry 189 -->

"Retire" now means something — it had no working consumer. Retired cats leave the
annotation picker, the user dashboard's Cats view, and (via `labeled_crops(active_only=)`)
gallery build + the feasibility probe. Labels and crops are never touched, so it is
reversible; identification only changes on the next build+promote, never from the
checkbox itself. Consequence: retiring shifts the 1-9 digit bindings after that cat.
