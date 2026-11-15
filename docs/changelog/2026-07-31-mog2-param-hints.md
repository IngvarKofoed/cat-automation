# Tuning fields carry per-knob descriptions

<!-- entry 79 -->

#tuning MOG2 fields now carry a per-knob description (`.param-hint`): what each param does and
which way to turn it to detect more (↓ var_threshold/learning_rate/min_area/persistence,
↑ max_area_fraction/motion_downscale). persistence notes it's frames-not-seconds, so a higher
capture fps shortens the same value's time window. Presentation only.
