- [ ] All dected YOLO frames should be colors/marked, maybe with darker greens
- [ ] Drop bird for YOLO

- [x] Running mog2:candidate — 22981/154235 · 0.4 fps · ~101h 10m left · Day07


For the new admin
1) Actiity page: YOLO dection box in admin-new is flickering. It seems to work better in the user page
2) Actiity page: Remove the "det 100%" pill and use the same traffic light circle next to the label, also please Caps the labels
3) Frame review page: Move the "Cat x%" pill to the lower right.
4) Frame review page: What is Unswept? 
5) Frame review page: Please Caps the labelss
6) Frame review page: Could we in the overview box frames with motion and frames with corruption, corroptions takes priority
7) Motion tuning page: Change "YOLO coverage" to "YOLO"
8) Motion tuning page: Change "Day (last 4 weeks · click a day · Y/B/C = % swept)" to "Last 4 weeks"
9) Motion tuning page: In "Scorecards (visit recall)", is it possible to, for each "Live gate", "Baseline" and "Candidate", below The stats, to show the MOG2 parameters, starting with the one that makes sense to change in the top (var_threshold, min_area, ...) you had suggestions on which made sense to change. And then highlight those that have been change compared to the "Live gate".
10) Motion tuning page: Please start Caps the stats for "Live gate", "Baseline" and "Candidate"
11) Motion tuning page: Please rename "Scorecards (visit recall)" to "Scorecards"
12) Motion tuning page: Please remove: Candidate → edge: var_threshold=10 learning_rate=0.001 min_area=0.005 max_area_fraction=0.6 persistence=2 motion_downscale=320
13) Motion tuning page: At the bottom, list each of the MOG2 parameters in most important order and have a little description of each of them.  
14) Motion tuning page: Could we remove the _ from the MOG2 parameter names and start Caps them?

1) These "Recall
78/90 visits
Missed
1,110
False
553
Day
94%
Night
22%" should not be all caps, just start with cap



