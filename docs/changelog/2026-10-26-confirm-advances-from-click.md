# Confirm advances with fromClick across page boundaries

<!-- entry 517 -->

Review repair: a confirm advances with `fromClick`, so a confirm sitting on a page
boundary completes its hop once the page lands, as an `Older` press does. Otherwise a
walk stalls at every hundredth visit — the one place it is least willing to be
interrupted, since the walk is what the feature is for.
