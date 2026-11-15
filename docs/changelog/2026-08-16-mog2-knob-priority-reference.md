# MOG2 params shown in one priority order with a reference card

<!-- entry 162 -->

admin-next Motion tuning: MOG2 params are now displayed as "Var threshold", not `var_threshold`, in
ONE priority order everywhere — fields, per-scorecard readouts, and a new bottom reference card that
ranks each knob with which way to turn it and what it does (ported from `/admin`'s hints). Keys on the
wire stay snake_case; only the display changed.
Each scorecard now ends with the params that produced it, with every value differing from the live
gate marked amber — so "what did I change" is answerable without diffing two columns by eye. The
copy-to-edge param line is gone (redundant), stat labels are small-caps, and the headings are
"Last 4 weeks" / "YOLO" / "Scorecards"; calendar cells spell out YOLO/BASE/CAND now that the
header carries no legend.
