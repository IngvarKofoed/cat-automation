# Enrol table kind column floor and renderCats mount race fixed

<!-- entry 368 -->

Three smaller ones. The 5→8 column change left the flexible kind column 50px at the
shared 720px table floor and clipped "neighbour"; it has its OWN 780px floor rather
than a raised shared one, since the other `wtable` users have different geometry.
Driving `renderCats` from `loadRuns` let it win the mount race and assert "No active
cats" — a claim, not a blank — until the roster landed; it now waits.
