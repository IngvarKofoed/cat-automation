# YOLO oracle drops the bird class

<!-- entry 238 -->

COCO `bird` (14) is DROPPED from `yolo-serial`'s class set, reverting half of entry 89:
from the top-down door view a bird box was almost never a bird, and its own subject chip
dressed that noise up as a named subject. `person` (0) stays; verdict/score were always
cat-only, so no scorecard moves. Such motion now files as `unrecognized`/`motion_only`.
