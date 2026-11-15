# Days are the compute PC's local calendar days, DST-aware

<!-- entry 562 -->

Days are the compute PC's OWN local calendar days (naive `datetime.timestamp()`, which works on
Windows without `tzdata`), not a browser offset: the window spans DST switches, and one fixed
offset would shift every earlier day by an hour. The DST day is 23 or 25 hours long.
