# Window bound can cut a bridged visit, left for decision

<!-- entry 605 -->

Known limit, found in review and left for a decision: a window bound (`since_id`) can cut a
BRIDGED visit. A cat sitting still across a Visits-page window start (or the daily walk's
floor) shows in the window as its later part only, named from those frames — so the Visits
page and the feed can disagree about that visit, against entry 341. A fix reads back past
`since_id` by up to the bridge cap, transitively, which changes what every window bound means.
