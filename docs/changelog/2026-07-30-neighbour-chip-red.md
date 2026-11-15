# Non-resident named cats render red in Activity

<!-- entry 77 -->

Activity feed distinguishes resident from foreign matches: the event identity now carries
`is_resident`, and a named NON-resident (neighbour) cat renders RED, not green — a green chip
always means one of our cats. Resident = green, non-resident = red, unknown cat = amber (unchanged).
Chose red for a known stranger over reusing amber so a confident foreign match reads as an alert,
not a "second look".
