# Feasibility headline kNN accuracy is inflated by same-visit neighbours

<!-- entry 314 -->

Feasibility run 6's headline "100% kNN" is INFLATED and must not be read as recognition
accuracy: the leave-one-out masks only the diagonal, so a crop's nearest neighbour is
usually the adjacent frame of its OWN visit — a near-duplicate. The separation AUC (0.878)
and the gallery's stamped `threshold_balanced_acc` (0.804) are the numbers that survive.
A visit-held-out probe is the fix; until it exists, no report scores the real task.
