# Run comparability normalises grades and exclusions on both sides

<!-- entry 363 -->

Comparability normalises both sides, because neither is stored in the form the compare
implies. `all` is NOT equal to `gallery+ok+poor` — an explicit grade filter excludes
NULL-quality crops, so they score different sets — while an absent, null, and empty
exclusion list all mean "excluded nobody" (`_run_metrics` omits the key when empty,
`getExcluded()` returns null), which unnormalised would dim permanently in the
commonest configuration.
