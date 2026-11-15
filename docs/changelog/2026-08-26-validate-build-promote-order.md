# Model building ordered Validate, Build, Promote

<!-- entry 208 -->

Model building is ordered Validate → Build → Promote, not Build → Validate → Promote.
Validate reads no gallery, so it is a gate on the DATA — "is this separable enough to
build from" — answered before a build is spent, not a check on one already made. The old
order was what made it read as "validates the last build". Validate is labelled optional:
a build recomputes the same threshold over the crops it actually enrols, so skipping it
costs nothing but the forecast.
