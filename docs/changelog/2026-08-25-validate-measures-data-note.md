# Validate page states what it actually measures

<!-- entry 207 -->

Recorded what "Validate" actually measures, on the page itself: the feasibility probe
scores the LABELLED CROPS at the chosen grades with a pretrained DINOv2 + kNN — it
never reads a built gallery. So a run belongs to no model version (`feasibility_runs`
has no model id, by design), there is nothing to "re-validate", and a run is
identified by what it measured: grades + crop/cat counts. The UI said only "feasibility
probe, before promote", which read as "validates the last build".
